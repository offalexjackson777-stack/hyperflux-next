#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import subprocess
import time
from typing import Any


SCHEMA = "hyperflux-arch-split-transaction-qualification-v1"
IMAGE = "localhost/hyperflux-next-dev:ci"
IMAGE_ID = "231e9d52818507fd55e8e4fb7ffb14c7c50df8b2562cc5b06a6375ab7a5cfd8f"
IMAGE_DIGEST = "sha256:dda426bb92dbfd50e9b207ea3e9e07c4e4ab45646d826bed75323350cee52728"
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_PROCESS_OUTPUT = 2 * 1024 * 1024
MAX_EVIDENCE_FILE_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 8 * 1024 * 1024
TRANSACTION_TIMEOUT = 1800.0
EXPECTED_INNER_OUTPUTS = {
    "config-before-upgrade.sha256",
    "dkms-after-rollback.txt",
    "dkms-after-upgrade.txt",
    "dkms-before-upgrade.txt",
    "dkms-build-hid-after-rollback.log",
    "dkms-build-hid-after-upgrade.log",
    "dkms-build-hid-before-upgrade.log",
    "dkms-kernel-fixture.txt",
    "dkms-build-openrazer-after-rollback.log",
    "dkms-build-openrazer-after-upgrade.log",
    "dkms-build-openrazer-before-upgrade.log",
    "dkms-module-hashes-after-rollback.txt",
    "dkms-module-hashes-before-upgrade.txt",
    "dkms-sources-after-upgrade.txt",
    "inner-result.json",
    "kernel-makefile-after-rollback.sha256",
    "kernel-makefile-after-upgrade.sha256",
    "kernel-makefile-before-upgrade.sha256",
    "matrix-registration-trace.txt",
    "matrix-result.json",
    "matrix-transactions.log",
    "packages-after-upgrade.txt",
    "packages-before-upgrade.txt",
    "registration-trace.txt",
    "registration-trace.unique.txt",
    "rollback.stderr",
    "rollback.stdout",
    "seed-package-db.json",
    "stock-package-versions.txt",
    "upgrade.stderr",
    "upgrade.stdout",
}


class QualificationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_file(value: Path, label: str) -> Path:
    if value.is_symlink():
        raise QualificationError(f"{label} may not be a symbolic link")
    path = value.resolve(strict=True)
    stat = path.stat()
    if not path.is_file() or stat.st_size <= 0 or stat.st_size > MAX_PACKAGE_BYTES:
        raise QualificationError(f"{label} is not a bounded regular file")
    return path


def bounded_text(command: list[str], label: str, timeout: float = 20.0) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QualificationError(f"cannot inspect {label}: {error}") from error
    if (
        completed.returncode != 0
        or len(completed.stdout) + len(completed.stderr) > 65_536
    ):
        raise QualificationError(f"cannot inspect {label}")
    try:
        return completed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise QualificationError(f"{label} output is not UTF-8") from error


def inspect_image() -> tuple[str, str]:
    text = bounded_text(
        ["podman", "image", "inspect", "--format", "{{.Id}}\n{{.Digest}}", IMAGE],
        "pinned transaction image",
    )
    lines = text.splitlines()
    if lines != [IMAGE_ID, IMAGE_DIGEST]:
        raise QualificationError("transaction image identity differs from the pin")
    return lines[0], lines[1]


def stop_container(name: str) -> None:
    try:
        subprocess.run(
            ["podman", "rm", "--force", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def bounded_container(command: list[str], name: str) -> tuple[int, bytes, bytes]:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise QualificationError(f"cannot start transaction container: {error}") from error
    if process.stdout is None or process.stderr is None:
        stop_container(name)
        raise QualificationError("transaction container output pipes are unavailable")

    selector = selectors.DefaultSelector()
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    buffers = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    streams = (process.stdout, process.stderr)
    total = 0
    deadline = time.monotonic() + TRANSACTION_TIMEOUT
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QualificationError("transaction container timed out")
            events = selector.select(remaining)
            if not events:
                raise QualificationError("transaction container timed out")
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), min(8192, MAX_PROCESS_OUTPUT + 1 - total))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[key.fileobj.fileno()].extend(chunk)
                total += len(chunk)
                if total > MAX_PROCESS_OUTPUT:
                    raise QualificationError("transaction container output exceeded its bound")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QualificationError("transaction container timed out")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise QualificationError("transaction container timed out") from error
    except QualificationError:
        stop_container(name)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)
        raise
    finally:
        selector.close()
        for stream in streams:
            stream.close()

    return returncode, bytes(buffers[stdout_fd]), bytes(buffers[stderr_fd])


def write_bytes(path: Path, value: bytes) -> None:
    with path.open("xb") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())
    path.chmod(0o600)


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def validate_inner_outputs(
    output: Path,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    names = {entry.name for entry in output.iterdir()}
    if names != EXPECTED_INNER_OUTPUTS:
        missing = sorted(EXPECTED_INNER_OUTPUTS - names)
        extra = sorted(names - EXPECTED_INNER_OUTPUTS)
        raise QualificationError(f"transaction evidence inventory drifted: {missing=} {extra=}")
    records: list[dict[str, Any]] = []
    total = 0
    for name in sorted(names):
        path = output / name
        if path.is_symlink() or not path.is_file():
            raise QualificationError(f"transaction evidence is not a regular file: {name}")
        stat = path.stat()
        if stat.st_mode & 0o777 != 0o600 or stat.st_size > MAX_EVIDENCE_FILE_BYTES:
            raise QualificationError(f"transaction evidence mode or size is invalid: {name}")
        total += stat.st_size
        if total > MAX_EVIDENCE_TOTAL_BYTES:
            raise QualificationError("transaction evidence exceeds its aggregate bound")
        records.append({"file": name, "sha256": sha256_file(path), "size": stat.st_size})
    try:
        inner = json.loads((output / "inner-result.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise QualificationError("transaction result is not valid JSON") from error
    if (
        inner.get("schema") != "hyperflux-arch-split-transaction-v1"
        or inner.get("verdict") != "passed"
    ):
        raise QualificationError("transaction result did not pass")
    try:
        matrix = json.loads((output / "matrix-result.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise QualificationError("transaction matrix result is not valid JSON") from error
    cases = matrix.get("cases")
    expected_cases = {f"case-{index:02d}" for index in range(1, 21)}
    if (
        matrix.get("schema") != "hyperflux-arch-two-package-transaction-matrix-v1"
        or matrix.get("verdict") != "passed"
        or not isinstance(cases, list)
        or {case.get("id") for case in cases if isinstance(case, dict)}
        != expected_cases
        or any(
            not isinstance(case, dict) or case.get("verdict") != "passed"
            for case in cases
        )
    ):
        raise QualificationError("transaction matrix did not pass all twenty cases")
    return inner, matrix, records


def transaction_command(
    *,
    name: str,
    seed_database: Path,
    inner: Path,
    output: Path,
    packages: dict[str, Path],
    image_id: str,
) -> list[str]:
    command = [
        "podman",
        "run",
        "--pull=never",
        "--name",
        name,
        "--rm",
        "--network",
        "none",
        "--cpus",
        "4",
        "--memory",
        "8g",
        "--pids-limit",
        "2048",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "0:0",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=1g",
        "--tmpfs",
        "/run:rw,nosuid,nodev,size=64m",
        "--volume",
        f"{seed_database}:/seed-local:ro",
        "--volume",
        f"{inner}:/qualification/inner.sh:ro",
        "--volume",
        f"{output}:/outputs:rw",
    ]
    for destination, source in sorted(packages.items()):
        command.extend(("--volume", f"{source}:/inputs/{destination}:ro"))
    command.extend((image_id, "/bin/bash", "/qualification/inner.sh"))
    return command


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--linux", type=Path, required=True)
    result.add_argument("--full", type=Path, required=True)
    result.add_argument("--old-monolith", type=Path, required=True)
    result.add_argument("--old-linux", type=Path, required=True)
    result.add_argument("--old-openrgb-plugin", type=Path, required=True)
    result.add_argument("--old-full", type=Path, required=True)
    result.add_argument("--release8-linux", type=Path, required=True)
    result.add_argument("--release8-full", type=Path, required=True)
    result.add_argument("--polychromatic", type=Path, required=True)
    result.add_argument("--openrazer-driver", type=Path, required=True)
    result.add_argument("--openrazer-daemon", type=Path, required=True)
    result.add_argument("--python-openrazer", type=Path, required=True)
    result.add_argument("--stock-openrgb", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    return result


def main() -> int:
    arguments = parser().parse_args()
    packages = {
        "linux.pkg": checked_file(arguments.linux, "core package"),
        "full.pkg": checked_file(arguments.full, "full package"),
        "old.pkg": checked_file(arguments.old_monolith, "old monolith package"),
        "old-linux.pkg": checked_file(arguments.old_linux, "old core package"),
        "old-openrgb-plugin.pkg": checked_file(
            arguments.old_openrgb_plugin, "old connector package"
        ),
        "old-full.pkg": checked_file(arguments.old_full, "old full package"),
        "release8-linux.pkg": checked_file(arguments.release8_linux, "release 8 core package"),
        "release8-full.pkg": checked_file(arguments.release8_full, "release 8 full package"),
        "polychromatic.pkg": checked_file(arguments.polychromatic, "Polychromatic package"),
        "openrazer-driver.pkg": checked_file(
            arguments.openrazer_driver, "OpenRazer driver package"
        ),
        "openrazer-daemon.pkg": checked_file(arguments.openrazer_daemon, "OpenRazer daemon package"),
        "python-openrazer.pkg": checked_file(arguments.python_openrazer, "OpenRazer client package"),
        "openrgb.pkg": checked_file(arguments.stock_openrgb, "stock OpenRGB package"),
    }
    inner = checked_file(
        Path(__file__).with_name("arch_split_transaction_inner.sh"),
        "transaction helper",
    )
    matrix_inner = checked_file(
        Path(__file__).with_name("arch_two_package_matrix_inner.sh"),
        "transaction matrix helper",
    )
    seed_database = Path("/var/lib/pacman/local")
    if seed_database.is_symlink() or not seed_database.is_dir():
        raise QualificationError("read-only package database seed is unavailable")
    output = arguments.output
    if output.exists() or output.is_symlink():
        raise QualificationError("transaction output already exists")
    parent = output.parent.resolve(strict=True)
    if not parent.is_dir():
        raise QualificationError("transaction output parent is unavailable")
    output.mkdir(mode=0o700)
    output = output.resolve(strict=True)

    image_id, image_digest = inspect_image()
    name = f"hfx-arch-transaction-{os.getpid()}-{secrets.token_hex(4)}"
    command = transaction_command(
        name=name,
        seed_database=seed_database,
        inner=inner,
        output=output,
        packages=packages,
        image_id=image_id,
    )

    returncode, stdout, stderr = bounded_container(command, name)
    if returncode != 0:
        write_bytes(output / "migration-container.stdout", stdout)
        write_bytes(output / "migration-container.stderr", stderr)
        detail = stderr.decode("utf-8", errors="replace")[-4096:]
        raise QualificationError(f"transaction container failed with {returncode}: {detail}")
    matrix_name = f"hfx-arch-matrix-{os.getpid()}-{secrets.token_hex(4)}"
    matrix_command = transaction_command(
        name=matrix_name,
        seed_database=seed_database,
        inner=matrix_inner,
        output=output,
        packages=packages,
        image_id=image_id,
    )
    matrix_returncode, matrix_stdout, matrix_stderr = bounded_container(
        matrix_command, matrix_name
    )
    if matrix_returncode != 0:
        write_bytes(output / "migration-container.stdout", stdout)
        write_bytes(output / "migration-container.stderr", stderr)
        write_bytes(output / "matrix-container.stdout", matrix_stdout)
        write_bytes(output / "matrix-container.stderr", matrix_stderr)
        detail = matrix_stderr.decode("utf-8", errors="replace")[-4096:]
        raise QualificationError(
            f"transaction matrix container failed with {matrix_returncode}: {detail}"
        )
    container_outputs = {
        "migration-container.stdout",
        "migration-container.stderr",
        "matrix-container.stdout",
        "matrix-container.stderr",
    }
    inner_result, matrix_result, evidence = validate_inner_outputs(output)
    write_bytes(output / "migration-container.stdout", stdout)
    write_bytes(output / "migration-container.stderr", stderr)
    write_bytes(output / "matrix-container.stdout", matrix_stdout)
    write_bytes(output / "matrix-container.stderr", matrix_stderr)
    result = {
        "schema": SCHEMA,
        "isolation": {
            "engine": "podman",
            "image": IMAGE,
            "image_id": image_id,
            "image_digest": image_digest,
            "network": "none",
            "package_database": "host-read-only-copy",
        },
        "inputs": [
            {
                "file": destination,
                "sha256": sha256_file(source),
                "size": source.stat().st_size,
            }
            for destination, source in sorted(packages.items())
        ],
        "inner_result": inner_result,
        "matrix_result": matrix_result,
        "evidence": evidence,
        "container_output": {
            name: {
                "sha256": sha256_file(output / name),
                "size": (output / name).stat().st_size,
            }
            for name in sorted(container_outputs)
        },
        "verdict": "passed",
    }
    write_json(output / "qualification.json", result)
    print("arch-split-transaction-qualification=PASS")
    print(f"evidence={output / 'qualification.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except QualificationError as error:
        print(f"arch-split-transaction-qualification=FAIL: {error}", file=os.sys.stderr)
        raise SystemExit(1)
