# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
from typing import Any

from .model import ModelError


SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\.json\Z")


def write_new_private_json(directory: Path, filename: str, value: Any) -> Path:
    """Atomically create one non-overwriting, private JSON artifact."""

    if SAFE_FILENAME.fullmatch(filename) is None:
        raise ModelError("private artifact filename is invalid")
    requested = directory.expanduser()
    if requested.exists() and requested.is_symlink():
        raise ModelError("private artifact directory may not be a symbolic link")
    base = requested.resolve()
    base.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError as error:
        raise ModelError(f"cannot secure private artifact directory: {error}") from error
    destination = base / filename
    if destination.exists() or destination.is_symlink():
        raise ModelError("private artifact destination already exists")
    temporary = base / f".{filename}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    payload = (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode()
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError as error:
            raise ModelError("private artifact destination already exists") from error
        temporary.unlink()
        destination.chmod(0o600)
        directory_descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        raise ModelError(f"cannot write private artifact: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return destination
