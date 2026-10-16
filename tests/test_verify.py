# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.hfxdev.model import ModelError, load_json, repository_files
from tools.hfxdev.verify import (
    _check_build_cache_clock,
    _kernel_build_directories,
    _openrazer_native_payload_errors_propagate,
    _openrazer_payload_retry_is_safe,
    _resolve_schema_reference,
    _run_openrgb_adapter_contracts,
    _run_openrgb_native_route_contracts,
    _run_openrgb_thread_sanitizer,
)
from tools.hfxdev.verify_repository import (
    SCHEMA_CLOSURE_EXEMPTIONS,
    SCHEMA_DECLARATION_EXEMPTIONS,
    _check_schema_closure,
    _check_schema_declarations,
    _check_schema_references,
)

ROOT = Path(__file__).resolve().parents[1]


class SchemaDeclarationTests(unittest.TestCase):
    def test_document_with_schema_needs_no_exemption(self) -> None:
        _check_schema_declarations({"a.json": {"$schema": "x.schema.json"}}, {})

    def test_document_without_schema_must_be_exempted(self) -> None:
        documents = {"a.json": {"$schema": "x.schema.json"}, "new.json": {"value": 1}}
        with self.assertRaisesRegex(ModelError, "new.json"):
            _check_schema_declarations(documents, {})
        _check_schema_declarations(documents, {"new.json": "byte-frozen: golden"})

    def test_exemption_needs_a_reason(self) -> None:
        with self.assertRaisesRegex(ModelError, "needs a reason"):
            _check_schema_declarations({"a.json": {}}, {"a.json": " "})

    def test_stale_exemptions_are_refused(self) -> None:
        with self.assertRaisesRegex(ModelError, "names no tracked JSON file"):
            _check_schema_declarations({}, {"gone.json": "byte-frozen: golden"})
        with self.assertRaisesRegex(ModelError, "remove its schema declaration exemption"):
            _check_schema_declarations(
                {"a.json": {"$schema": "x.schema.json"}},
                {"a.json": "byte-frozen: golden"},
            )

    def test_exempted_repository_files_exist_and_carry_reasons(self) -> None:
        for relative, reason in SCHEMA_DECLARATION_EXEMPTIONS.items():
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file())
                self.assertTrue(reason.strip())


def _closed(**properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "properties": properties}


class SchemaClosureTests(unittest.TestCase):
    def test_closed_objects_pass(self) -> None:
        schema = {
            **_closed(name={"type": "string"}, inner={"$ref": "#/$defs/inner"}),
            "$defs": {
                "inner": {"type": "object", "unevaluatedProperties": False, "properties": {"a": {}}},
            },
        }
        _check_schema_closure({"s.schema.json": schema}, {})

    def test_open_objects_anywhere_are_refused(self) -> None:
        open_object = {"type": "object", "properties": {"a": {"type": "string"}}}
        cases = {
            "/": open_object,
            "/properties/inner": _closed(inner=open_object),
            "/$defs/thing": {**_closed(), "$defs": {"thing": open_object}},
            "/properties/list/items": _closed(list={"type": "array", "items": open_object}),
            "/properties/tuple/prefixItems/0": _closed(tuple={"prefixItems": [open_object]}),
            "/properties/map/additionalProperties": _closed(
                map={"type": "object", "additionalProperties": open_object}
            ),
            "/properties/explicit": _closed(explicit={**open_object, "additionalProperties": True}),
            "/properties/typed": _closed(typed={**open_object, "additionalProperties": {"type": "string"}}),
            "/properties/x/then/properties/y": _closed(
                x={**_closed(y={}), "then": {"properties": {"y": open_object}}}
            ),
        }
        for pointer, schema in cases.items():
            with self.subTest(pointer=pointer):
                with self.assertRaisesRegex(ModelError, re.escape(f"s.schema.json{pointer}: object schema")):
                    _check_schema_closure({"s.schema.json": schema}, {})
                _check_schema_closure(
                    {"s.schema.json": schema}, {("s.schema.json", "" if pointer == "/" else pointer): "reason"}
                )

    def test_branches_of_a_closed_object_must_name_their_keys_on_it(self) -> None:
        good = {
            **_closed(state={"enum": ["a", "b"]}, value={}),
            "oneOf": [
                {"properties": {"state": {"const": "a"}, "value": {"type": "null"}}},
                {"properties": {"state": {"const": "b"}}},
            ],
            "allOf": [{"if": {"properties": {"state": {"const": "a"}}}, "then": {"properties": {"value": {}}}}],
        }
        _check_schema_closure({"s.schema.json": good}, {})
        # additionalProperties only sees keys named beside it, so keys named
        # only in a branch are always rejected: the source-validated bug.
        bad = {"type": "object", "additionalProperties": False, "required": ["state"],
               "oneOf": [{"properties": {"state": {"const": "a"}}}]}
        with self.assertRaisesRegex(ModelError, "oneOf/0: an in-place branch names state"):
            _check_schema_closure({"s.schema.json": bad}, {})
        then_bad = {**_closed(state={}), "allOf": [{"if": {"properties": {"state": {}}}, "then": {"properties": {"extra": {}}}}]}
        with self.assertRaisesRegex(ModelError, "allOf/0/then: an in-place branch names extra"):
            _check_schema_closure({"s.schema.json": then_bad}, {})
        # unevaluatedProperties sees through branches, so they may add keys.
        unevaluated = {"type": "object", "unevaluatedProperties": False,
                       "allOf": [{"properties": {"a": {}}}, {"properties": {"b": {}}}]}
        _check_schema_closure({"s.schema.json": unevaluated}, {})

    def test_ref_refinements_follow_their_closed_target(self) -> None:
        target = {"oneOf": [_closed(name={}, state={"const": "x"}), _closed(name={}, state={"const": "y"})]}
        refine = {"$ref": "#/$defs/target", "properties": {"name": {"const": "n"}}}
        schema = {**_closed(item=refine), "$defs": {"target": target}}
        _check_schema_closure({"s.schema.json": schema}, {})
        refine_bad = {"$ref": "#/$defs/target", "properties": {"other": {}}}
        schema_bad = {**_closed(item=refine_bad), "$defs": {"target": target}}
        with self.assertRaisesRegex(ModelError, "a \\$ref refinement names other"):
            _check_schema_closure({"s.schema.json": schema_bad}, {})
        open_target = {**_closed(item=refine), "$defs": {"target": {"properties": {"name": {}}}}}
        with self.assertRaisesRegex(ModelError, "properties/item: object schema"):
            _check_schema_closure({"s.schema.json": open_target}, {("s.schema.json", "/$defs/target"): "r"})

    def test_conditions_are_not_checked(self) -> None:
        schema = {
            **_closed(a={}),
            "allOf": [{"if": {"properties": {"a": {"properties": {"deep": {}}}}}, "then": {"required": ["a"]}}],
            "not": {"properties": {"a": {"const": 1}}},
        }
        _check_schema_closure({"s.schema.json": schema}, {})

    def test_exemptions_need_reasons_and_must_still_be_open(self) -> None:
        open_schema = {"type": "object", "properties": {"a": {}}}
        with self.assertRaisesRegex(ModelError, "needs a reason"):
            _check_schema_closure({"s.schema.json": open_schema}, {("s.schema.json", ""): " "})
        with self.assertRaisesRegex(ModelError, "remove its schema closure exemption"):
            _check_schema_closure({"s.schema.json": _closed(a={})}, {("s.schema.json", ""): "reason"})
        with self.assertRaisesRegex(ModelError, "remove its schema closure exemption"):
            _check_schema_closure({}, {("gone.schema.json", ""): "reason"})

    def test_repository_exemptions_name_real_schemas_with_reasons(self) -> None:
        for (relative, pointer), reason in SCHEMA_CLOSURE_EXEMPTIONS.items():
            with self.subTest(relative=relative, pointer=pointer):
                self.assertTrue((ROOT / relative).is_file())
                self.assertTrue(reason.strip())


BASE = "https://hyperflux.dev/schemas/"


def _policy_schema() -> dict:
    return {
        "$id": f"{BASE}example-policy-v1.json",
        "$defs": {"matcher": {"type": "string"}, "a/b~c": {"type": "integer"}},
        "type": "object",
    }


def _catalog_schema(reference: str) -> dict:
    return {
        "$id": f"{BASE}example-catalog-v1.json",
        "$defs": {"name": {"type": "string"}},
        "type": "object",
        "properties": {
            "name": {"$ref": "#/$defs/name"},
            "matcher": {"$ref": reference},
            "default": {"const": {"$ref": "not a reference"}},
        },
    }


class SchemaReferenceTests(unittest.TestCase):
    def check(self, reference: str) -> None:
        _check_schema_references(
            {
                "schemas/example-policy.schema.json": _policy_schema(),
                "schemas/example-catalog.schema.json": _catalog_schema(reference),
            }
        )

    def test_references_to_registered_ids_and_local_pointers_resolve(self) -> None:
        self.check(f"{BASE}example-policy-v1.json#/$defs/matcher")
        self.check("example-policy-v1.json#/$defs/matcher")
        self.check(f"{BASE}example-policy-v1.json#/$defs/a~1b~0c")
        self.check(f"{BASE}example-policy-v1.json")
        self.check("#/$defs/name")

    def test_reference_to_an_unregistered_id_is_refused(self) -> None:
        # The relative file name resolves against $id to a URL nobody registers.
        with self.assertRaisesRegex(ModelError, "names no registered schema \\$id"):
            self.check("example-policy.schema.json#/$defs/matcher")

    def test_reference_to_a_missing_pointer_is_refused(self) -> None:
        with self.assertRaisesRegex(ModelError, "#/properties/matcher: .*points at nothing"):
            self.check(f"{BASE}example-policy-v1.json#/$defs/missing")
        with self.assertRaisesRegex(ModelError, "points at nothing"):
            self.check("#/$defs/missing")
        with self.assertRaisesRegex(ModelError, "points at nothing"):
            self.check("#anchor")

    def test_nested_ids_and_duplicate_ids_are_refused(self) -> None:
        nested = _catalog_schema("#/$defs/name")
        nested["$defs"]["name"]["$id"] = f"{BASE}nested.json"
        with self.assertRaisesRegex(ModelError, "nested \\$id"):
            _check_schema_references({"schemas/a.schema.json": nested})
        with self.assertRaisesRegex(ModelError, "duplicate JSON Schema id"):
            _check_schema_references(
                {"schemas/a.schema.json": _policy_schema(), "schemas/b.schema.json": _policy_schema()}
            )

    def test_repository_schemas_resolve_every_reference(self) -> None:
        schemas = {
            path.relative_to(ROOT).as_posix(): load_json(path)
            for path in repository_files(ROOT)
            if path.name.endswith(".schema.json")
        }
        self.assertIn("schemas/openrazer-native-command-catalog.schema.json", schemas)
        _check_schema_references(schemas)


class VerificationGuardTests(unittest.TestCase):
    def test_openrazer_single_attempt_payload_needs_no_retry_guard(self) -> None:
        source = """
static int razer_send_payload(struct device *device)
{
    int err = razer_get_report(device);
    return err;
}
"""
        self.assertTrue(_openrazer_payload_retry_is_safe(source))

    def test_openrazer_retry_requires_an_uncertain_outcome_guard(self) -> None:
        unsafe = """
static int razer_send_payload(struct device *device)
{
    int err;
retry:
    err = razer_get_report(device);
    if (err)
        goto retry;
    return err;
}
"""
        guarded = unsafe.replace(
            "    if (err)\n",
            "    if (razer_hyperflux_error_is_uncertain(err))\n"
            "        return err;\n"
            "    if (err)\n",
        )
        self.assertFalse(_openrazer_payload_retry_is_safe(unsafe))
        self.assertTrue(_openrazer_payload_retry_is_safe(guarded))

    def test_openrazer_native_errors_return_before_response_parsing(self) -> None:
        source = """
static int razer_send_payload_transport(void) { return 0; }
#define razer_send_payload(device, request, response)                         \\
({                                                                            \\
    struct razer_mouse_device *__hyperflux_device = (device);                 \\
    int __hyperflux_error = razer_send_payload_transport(                     \\
        __hyperflux_device, (request), (response));                           \\
    if (__hyperflux_error &&                                                  \\
        razer_is_hyperflux_native(__hyperflux_device->hdev))                  \\
        return __hyperflux_error;                                             \\
    __hyperflux_error;                                                        \\
})
"""
        self.assertTrue(
            _openrazer_native_payload_errors_propagate(
                source, "razer_mouse_device"
            )
        )
        self.assertFalse(
            _openrazer_native_payload_errors_propagate(
                source.replace(
                    "razer_is_hyperflux_native(__hyperflux_device->hdev)",
                    "false",
                ),
                "razer_mouse_device",
            )
        )

    def test_openrazer_direct_payload_definition_cannot_bypass_native_wrapper(self) -> None:
        source = """
static int razer_send_payload_transport(void) { return 0; }
static int razer_send_payload(void) { return 0; }
#define razer_send_payload(device, request, response)                         \\
({                                                                            \\
    struct razer_mouse_device *__hyperflux_device = (device);                 \\
    int __hyperflux_error = razer_send_payload_transport(                     \\
        __hyperflux_device, (request), (response));                           \\
    if (__hyperflux_error &&                                                  \\
        razer_is_hyperflux_native(__hyperflux_device->hdev))                  \\
        return __hyperflux_error;                                             \\
    __hyperflux_error;                                                        \\
})
"""
        self.assertFalse(
            _openrazer_native_payload_errors_propagate(
                source, "razer_mouse_device"
            )
        )

    def test_kernel_build_directories_deduplicate_resolved_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = root / "kernel-headers"
            canonical.mkdir()
            (canonical / "Makefile").write_text("# fixture\n", encoding="utf-8")
            alias = root / "kernel-headers-alias"
            alias.symlink_to(canonical, target_is_directory=True)

            with patch.dict(
                os.environ,
                {"HFX_KERNEL_BUILD_DIRS": os.pathsep.join((str(canonical), str(alias)))},
            ):
                self.assertEqual(_kernel_build_directories(), [canonical])

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

    def test_schema_reference_resolution_accepts_only_local_or_canonical_install_paths(
        self,
    ) -> None:
        root = Path("/checkout")
        document = root / "packaging" / "generated" / "bridge.json"
        self.assertEqual(
            _resolve_schema_reference(
                root,
                document,
                "/usr/share/hyperflux-next/schemas/bridge-config.schema.json",
            ),
            root / "schemas" / "bridge-config.schema.json",
        )
        self.assertEqual(
            _resolve_schema_reference(root, document, "../../schemas/bridge-config.schema.json"),
            root / "schemas" / "bridge-config.schema.json",
        )
        self.assertIsNone(
            _resolve_schema_reference(
                root,
                document,
                "/tmp/bridge-config.schema.json",
            )
        )

    def test_openrgb_release_and_sanitizer_lanes_share_complete_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "openrgb"
            source.mkdir()
            (source / "OpenRGBPluginInterface.h").write_text("// fixture\n", encoding="utf-8")
            native_source = root / "prepared-openrgb"
            native_source.mkdir()
            native_result = native_source / "hyperflux-native-transform-result.json"
            native_result.write_text(
                '{"schema":"hyperflux-openrgb-native-transform-result-v1",'
                '"network_access_executed":false,"hardware_queried":false,'
                '"hardware_write_executed":false}\n',
                encoding="utf-8",
            )
            native_result.chmod(0o600)
            prepared = SimpleNamespace(root=native_source, result=native_result)
            node = SimpleNamespace(timeout_seconds=300)
            with (
                patch("tools.hfxdev.verify._openrgb_source", return_value=source),
                patch(
                    "tools.hfxdev.verify.prepare_openrgb_native_source",
                    return_value=prepared,
                ) as prepare,
            ):
                with patch("tools.hfxdev.verify._run_command") as command:
                    _run_openrgb_adapter_contracts(root, node)
                    self.assertEqual(command.call_count, 3)
                    release_configure = command.call_args_list[0].args[1]
                    self.assertIn("-DCMAKE_BUILD_TYPE=Release", release_configure)
                    self.assertNotIn("-DHFX_OPENRGB_THREAD_SANITIZER=ON", release_configure)
                    self.assertEqual(command.call_args_list[2].args[1][0], "ctest")
                    self.assertIsNone(command.call_args_list[2].kwargs["environment"])

                with patch("tools.hfxdev.verify._run_command") as command:
                    _run_openrgb_native_route_contracts(root, node)
                    self.assertEqual(command.call_count, 3)
                    native_configure = command.call_args_list[0].args[1]
                    self.assertIn("-DCMAKE_BUILD_TYPE=Release", native_configure)
                    self.assertIn(
                        f"-DHFX_OPENRGB_NATIVE_SOURCE_DIR={native_source}",
                        native_configure,
                    )

                with patch("tools.hfxdev.verify._run_command") as command:
                    _run_openrgb_thread_sanitizer(root, node)
                    self.assertEqual(command.call_count, 3)
                    sanitizer_configure = command.call_args_list[0].args[1]
                    self.assertIn("-DCMAKE_BUILD_TYPE=RelWithDebInfo", sanitizer_configure)
                    self.assertIn("-DHFX_OPENRGB_THREAD_SANITIZER=ON", sanitizer_configure)
                    self.assertEqual(command.call_args_list[2].args[1][0], "ctest")
                    self.assertIn(
                        "halt_on_error=1",
                        command.call_args_list[2].kwargs["environment"]["TSAN_OPTIONS"],
                    )
                self.assertEqual(prepare.call_count, 2)


if __name__ == "__main__":
    unittest.main()
