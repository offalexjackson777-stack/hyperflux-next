# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests import arch_split_transaction_qualification as transaction
from tests import package_ownership_qualification as ownership
from tests import stock_application_compatibility as compatibility


class BridgeOnlyReleaseQualifierTests(unittest.TestCase):
    def test_transaction_container_runs_as_root_in_its_rootless_namespace(self) -> None:
        command = transaction.transaction_command(
            name="qualification",
            seed_database=Path("/seed"),
            inner=Path("/inner.sh"),
            output=Path("/output"),
            packages={"linux.pkg": Path("/linux.pkg")},
            image_id="sha256:pinned",
        )
        user_index = command.index("--user")
        self.assertEqual(command[user_index + 1], "0:0")
        self.assertNotIn("--userns=keep-id", command)
        self.assertEqual(command[-3:], ["sha256:pinned", "/bin/bash", "/qualification/inner.sh"])

    def test_transaction_inner_uses_private_database_and_registration_only_hook(self) -> None:
        source = Path(transaction.__file__).with_name(
            "arch_split_transaction_inner.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('transaction-inner: rc=%d line=%d command=%q', source)
        self.assertIn('readonly DATABASE=/transaction-db', source)
        self.assertIn('readonly -a PACMAN=(pacman --dbpath "$DATABASE")', source)
        self.assertIn('cp -a /seed-local/. "$DATABASE/local/"', source)
        self.assertIn("stat.st_size > 8 * 1024 * 1024", source)
        self.assertIn("total_bytes > 128 * 1024 * 1024", source)
        self.assertIn('[[ ${#fixture_kernels[@]} -eq 1 ]]', source)
        self.assertIn('mkdir -m 0755 "$FIXTURE_KERNEL_ROOT/kernel"', source)
        self.assertIn('>"$OUTPUT/dkms-kernel-fixture.txt"', source)
        self.assertIn('openrazer-driver/3.12.4, $FIXTURE_KERNEL_VERSION, x86_64: installed', source)
        self.assertIn('hid-hyperflux-next/$hfx_version, $FIXTURE_KERNEL_VERSION, x86_64: installed', source)
        self.assertEqual(source.count('capture_dkms_log openrazer-driver 3.12.4'), 3)
        self.assertIn(
            'capture_dkms_log hid-hyperflux-next "$CANDIDATE_HFX_DKMS_VERSION"',
            source,
        )
        self.assertEqual(source.count('capture_dkms_log hid-hyperflux-next 0.0.0_dev.1'), 2)
        self.assertIn('capture_module_hashes "$OUTPUT/dkms-module-hashes-before-upgrade.txt"', source)
        self.assertIn('cmp "$OUTPUT/dkms-module-hashes-before-upgrade.txt"', source)
        self.assertIn('--hookdir "$ROLLBACK_HOOKS"', source)
        self.assertIn('70-dkms-upgrade.hook', source)
        self.assertIn('pretransaction_dkms_state_restored', source)
        self.assertIn('kernel_header_tree_preserved', source)
        self.assertIn("'removed_package_records'", source)
        self.assertIn("shutil.rmtree(candidate)", source)
        self.assertIn("polychromatic != ['0.9.7-1']", source)
        self.assertEqual(source.count('"${PACMAN[@]}" -Qq | grep -Fx polychromatic'), 2)
        self.assertNotIn('if "${PACMAN[@]}" -Q polychromatic', source)
        self.assertGreaterEqual(source.count('"${PACMAN[@]}" -Qoq '), 8)
        self.assertNotIn(' is owned by ', source)
        self.assertIn('"${PACMAN[@]}" -U --noconfirm', source)
        self.assertIn('-U --asdeps --noconfirm --noscriptlet', source)
        self.assertIn('"$INPUT/openrazer-daemon.pkg"', source)
        self.assertIn('"$INPUT/python-openrazer.pkg"', source)
        self.assertIn('"$INPUT/old.pkg" >/dev/null', source)
        self.assertIn('"${PACMAN[@]}" -D --asexplicit hyperflux-next-linux', source)
        self.assertIn("old_default_bytes = package_config('/inputs/old.pkg')", source)
        self.assertIn("candidate_default_bytes = package_config('/inputs/linux.pkg')", source)
        self.assertIn("if old_default_bytes != candidate_default_bytes:", source)
        self.assertIn("unchanged packaged default unexpectedly emitted as .pacnew", source)
        self.assertIn("'safe_disabled_default_preserved_in_package': True", source)
        self.assertIn(
            '-R --noconfirm hyperflux-next-full polychromatic', source
        )
        self.assertIn('openrazer-daemon python-openrazer', source)
        self.assertEqual(
            source.count('"${PACMAN[@]}" -D --asexplicit hyperflux-next-linux'),
            2,
        )
        self.assertIn('"${PACMAN[@]}" -Qd openrazer-daemon', source)
        self.assertIn('"${PACMAN[@]}" -Qd python-openrazer', source)
        self.assertIn("CANDIDATE_PACKAGE_VERSION=2.1.0-1", source)
        self.assertIn("hyperflux-next-linux $CANDIDATE_PACKAGE_VERSION", source)
        self.assertIn("hyperflux-next-full $CANDIDATE_PACKAGE_VERSION", source)
        self.assertNotIn('"$INPUT/openrgb-plugin.pkg"', source)
        self.assertNotIn("systemctl start", source)
        self.assertNotIn("systemctl stop", source)
        self.assertNotIn("udevadm trigger", source)

    def test_transaction_evidence_inventory_rejects_hidden_additions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name in transaction.EXPECTED_INNER_OUTPUTS:
                path = root / name
                if name == "inner-result.json":
                    path.write_text(
                        '{"schema":"hyperflux-arch-split-transaction-v1",'
                        '"verdict":"passed"}\n',
                        encoding="utf-8",
                    )
                else:
                    path.write_bytes(b"")
                path.chmod(0o600)
            matrix = root / "matrix-result.json"
            matrix.write_text(
                '{"schema":"hyperflux-arch-two-package-transaction-matrix-v1",'
                '"cases":['
                + ",".join(
                    f'{{"id":"case-{index:02d}","verdict":"passed"}}'
                    for index in range(1, 21)
                )
                + '],"verdict":"passed"}\n',
                encoding="utf-8",
            )
            matrix.chmod(0o600)
            result, matrix_result, evidence = transaction.validate_inner_outputs(root)
            self.assertEqual(result["verdict"], "passed")
            self.assertEqual(matrix_result["verdict"], "passed")
            self.assertEqual(len(evidence), len(transaction.EXPECTED_INNER_OUTPUTS))
            hidden = root / ".unexpected"
            hidden.write_bytes(b"x")
            hidden.chmod(0o600)
            with self.assertRaisesRegex(transaction.QualificationError, "inventory drifted"):
                transaction.validate_inner_outputs(root)

    def test_two_package_matrix_covers_exact_plan_without_unsafe_shortcuts(self) -> None:
        source = Path(transaction.__file__).with_name(
            "arch_two_package_matrix_inner.sh"
        ).read_text(encoding="utf-8")
        for index in range(1, 21):
            self.assertIn(f"record_case case-{index:02d}", source)
        self.assertIn("hyperflux-next-openrgb=0.0.0_dev.1-2", (
            Path(__file__).resolve().parents[1] / "packaging/distributions.json"
        ).read_text(encoding="utf-8"))
        self.assertIn("local-full-alone-atomic-failure", source)
        self.assertIn("unexpected-connector-fails-closed", source)
        self.assertIn("three-package-migration-and-rollback", source)
        self.assertIn("release8-and-retired-connector-migrate", source)
        self.assertIn("if not matches and marker == 'REASON':", source)
        self.assertIn("matches[0] not in (['0'], ['1'])", source)
        self.assertEqual(source.count("--ask=4"), 4)
        self.assertIn(
            'case-09a-full false -U --noconfirm --ask=4 "$INPUT/full.pkg"',
            source,
        )
        for case_id in ("case-11-migrate", "case-12b-migrate"):
            self.assertIn(
                f"{case_id} false -U --noconfirm --ask=4",
                source,
            )
        self.assertNotIn("case-11-remove-new-full", source)
        self.assertIn("case-11-restore-old false -U --noconfirm", source)
        self.assertIn("case-16-full false -U --noconfirm --ask=4", source)
        self.assertIn('"$INPUT/release8-linux.pkg" "$INPUT/release8-full.pkg"', source)
        self.assertIn('install_origin case-12b-openrgb "$INPUT/openrgb.pkg"', source)
        self.assertIn('install_origin case-15-openrgb "$INPUT/openrgb.pkg"', source)
        self.assertIn("case-19-preserve-remove false -R --noconfirm", source)
        self.assertIn("case-19-purge-remove false -Rn --noconfirm", source)
        self.assertIn(
            "removal-and-explicit-purge-configuration-semantics", source
        )
        self.assertIn("candidate.rename(destination)", source)
        self.assertIn('"${PACMAN[@]}" -Qq >"$installed"', source)
        self.assertIn('grep -Fxq -- "$1" "$installed"', source)
        self.assertIn(
            "^removing (openrgb|openrazer-daemon|python-openrazer|polychromatic)",
            source,
        )
        self.assertNotIn("(removing|remove)[^[:cntrl:]]*", source)
        self.assertNotIn("--overwrite", source)
        self.assertNotIn("--nodeps", source)
        self.assertNotIn("-Rdd", source)

    def test_stock_fixture_is_exactly_two_public_openrazer_devices(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            compatibility.create_fake_devices(root)
            directories = sorted(path.name for path in root.iterdir())
            self.assertEqual(
                directories,
                ["0003:1532:00CD.0001", "0003:1532:0296.0001"],
            )
            self.assertEqual(
                (root / directories[0] / "device_type").read_text(encoding="utf-8"),
                compatibility.MOUSE_NAME,
            )
            self.assertEqual(
                (root / directories[1] / "device_type").read_text(encoding="utf-8"),
                compatibility.KEYBOARD_NAME,
            )
            compatibility.remove_fake_devices(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_application_relation_parser_is_exact(self) -> None:
        self.assertEqual(ownership.relation_name("openrgb=1.0rc3"), "openrgb")
        self.assertEqual(
            ownership.relation_name("hyperflux-next-linux>=0.0.0_dev.1-2"),
            "hyperflux-next-linux",
        )


if __name__ == "__main__":
    unittest.main()
