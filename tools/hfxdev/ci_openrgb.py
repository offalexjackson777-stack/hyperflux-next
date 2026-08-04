# SPDX-License-Identifier: GPL-2.0-only

"""Run packaged OpenRGB qualification outside the unprivileged CI container."""

from __future__ import annotations

from pathlib import Path

from .ci import (
    ContainerInvocation,
    _bounded_container_command,
    _prepare_bound_invocation,
    _relative_output,
    _run_bound_container,
    container_invocation,
)
from .model import ModelError, sha256_file


OPENRGB_QUALIFICATION_TIMEOUT_SECONDS = 300
MAX_OPENRGB_QUALIFICATION_OUTPUT_BYTES = 65_536
OPENRGB_EXECUTABLE_SCRATCH = "/qualification/runtime"
OPENRGB_EXECUTABLE_SCRATCH_ENVIRONMENT = "HFX_OPENRGB_EXECUTABLE_SCRATCH"


def openrgb_qualification_container_command(
    *,
    root: Path,
    invocation: ContainerInvocation,
    harness: Path,
    plugin: Path,
    output: Path,
    openrgb_commit: str,
) -> tuple[str, ...]:
    script = root / "tests" / "openrgb_packaged_plugin_sdk_qualification.py"
    if script.is_symlink() or not script.is_file():
        raise ModelError("packaged OpenRGB qualifier source is unavailable")
    command = [
        invocation.engine,
        "run",
        "--pull=never",
        "--rm",
        "--network",
        "none",
        "--ipc",
        "none",
        "--read-only",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "SYS_ADMIN",
        "--cap-add",
        "NET_ADMIN",
        "--cap-add",
        "DAC_OVERRIDE",
        "--cap-add",
        "SETPCAP",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "512",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec",
        "--tmpfs",
        f"{OPENRGB_EXECUTABLE_SCRATCH}:rw,nosuid,nodev,exec,mode=0755",
        "--env",
        "HFX_OPENRGB_OUTER_ISOLATION=1",
        "--env",
        f"{OPENRGB_EXECUTABLE_SCRATCH_ENVIRONMENT}={OPENRGB_EXECUTABLE_SCRATCH}",
        "--volume",
        f"{script}:/qualification/qualifier.py:ro",
        "--volume",
        f"{harness}:/qualification/harness:ro",
        "--volume",
        f"{plugin}:/qualification/plugin.so:ro",
        "--volume",
        f"{output}:/qualification/output:rw",
        "--entrypoint",
        "/usr/bin/python",
    ]
    if Path(invocation.engine).name == "docker":
        command.extend(("--security-opt", "apparmor=unconfined"))
    else:
        command.extend(("--security-opt", "label=disable"))
    command.extend(
        (
            invocation.image,
            "/qualification/qualifier.py",
            "--inside",
            "--isolation-authority",
            "outer-oci-container",
            "--harness",
            "/qualification/harness",
            "--plugin",
            "/qualification/plugin.so",
            "--openrgb-commit",
            openrgb_commit,
            "--evidence",
            "/qualification/output/evidence.json",
            "--runtime",
            "system",
        )
    )
    return tuple(command)


def run_external_openrgb_qualification(
    root: Path,
    *,
    image: str,
    output: Path,
    engine: str | None = None,
    uid: int | None = None,
    gid: int | None = None,
) -> int:
    from .verify_packaging import (
        finalize_external_openrgb_qualification,
        load_external_openrgb_qualification_request,
    )

    root = root.resolve()
    relative_output = _relative_output(
        root, output, "external OpenRGB qualification output"
    )
    output_root = root / relative_output
    preparation = container_invocation(
        root,
        image=image,
        operation="openrgb-qualification-prepare",
        output=Path(relative_output),
        engine=engine,
        uid=uid,
        gid=gid,
    )
    bound = _prepare_bound_invocation(preparation)
    result = _run_bound_container(bound)
    if result != 0:
        return result

    request_path = output_root / "request.json"
    request_sha256 = sha256_file(request_path)
    request, harness, plugin = load_external_openrgb_qualification_request(
        root, request_path
    )
    inputs = request["inputs"]
    if not isinstance(inputs, dict) or not isinstance(
        inputs.get("openrgb_commit"), str
    ):
        raise ModelError("external OpenRGB qualification request lost its inputs")
    runtime_output = output_root / "runtime"
    if runtime_output.exists() or runtime_output.is_symlink():
        raise ModelError("external OpenRGB qualification runtime output already exists")
    runtime_output.mkdir(mode=0o755)
    command = openrgb_qualification_container_command(
        root=root,
        invocation=bound,
        harness=harness,
        plugin=plugin,
        output=runtime_output,
        openrgb_commit=inputs["openrgb_commit"],
    )
    completed = _bounded_container_command(
        list(command),
        label="external isolated packaged OpenRGB qualification",
        max_output_bytes=MAX_OPENRGB_QUALIFICATION_OUTPUT_BYTES,
        timeout_seconds=OPENRGB_QUALIFICATION_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout).strip()
        raise ModelError(
            "external isolated packaged OpenRGB qualification failed "
            f"with exit status {completed.returncode}: {diagnostic}"
        )
    if sha256_file(request_path) != request_sha256:
        raise ModelError("external OpenRGB qualification request changed during execution")
    evidence = runtime_output / "evidence.json"
    binding = output_root / "binding.json"
    finalize_external_openrgb_qualification(
        root,
        request_path=request_path,
        evidence_path=evidence,
        binding_path=binding,
    )
    return 0
