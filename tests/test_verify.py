# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from tools.hfxdev.model import ModelError
from tools.hfxdev.verify import _check_build_cache_clock


class VerificationGuardTests(unittest.TestCase):
    def test_missing_and_current_build_caches_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _check_build_cache_clock(root, now=1_000.0)
            artifact = root / "target" / "debug" / "artifact"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"current")
            os.utime(artifact, (1_000.0, 1_000.0))
            _check_build_cache_clock(root, now=1_000.0)

    def test_future_dated_build_cache_is_rejected_with_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "target" / "debug" / "artifact"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"future")
            os.utime(artifact, (1_100.0, 1_100.0))
            with self.assertRaisesRegex(ModelError, "run cargo clean"):
                _check_build_cache_clock(root, now=1_000.0)


if __name__ == "__main__":
    unittest.main()
