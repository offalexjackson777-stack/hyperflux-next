# SPDX-License-Identifier: GPL-2.0-only

from pathlib import Path
import unittest

from hfxdev.model import ModelError
from hfxdev.openrgb_native_route import _safe_path, load_native_transform


ROOT = Path(__file__).resolve().parents[1]


class OpenRgbNativeRouteTests(unittest.TestCase):
    def test_worker_initialization_is_part_of_the_exact_source_transform(self) -> None:
        transform = load_native_transform(ROOT)
        self.assertEqual(
            [record["path"] for record in transform["files"]],
            [
                "Controllers/RazerController/RazerController/RazerController.cpp",
                "Controllers/RazerController/RazerController/RazerController.h",
                "RGBController/RGBController.cpp",
                "RGBController/RGBController.h",
            ],
        )

        patch = (
            ROOT
            / "integrations/openrgb/native-route/patches/0001-Add-HyperFlux-feature-transport-seam.patch"
        ).read_text(encoding="utf-8")
        constructor = patch.index("RGBController::RGBController()")
        leds = patch.index("CallFlag_UpdateLEDs = false", constructor)
        mode = patch.index("CallFlag_UpdateMode = false", constructor)
        thread = patch.index("DeviceCallThread = new std::thread", constructor)
        self.assertLess(leds, thread)
        self.assertLess(mode, thread)
        self.assertIn("HFX_TSAN_RELEASE(this)", patch)
        self.assertEqual(patch.count("HFX_TSAN_ACQUIRE(this)"), 2)
        worker = patch.index("void RGBController::DeviceCallThreadFunction()")
        self.assertIn("-    CallFlag_UpdateLEDs = false;", patch[worker:])
        self.assertIn("-    CallFlag_UpdateMode = false;", patch[worker:])
        destructor = patch.index("RGBController::~RGBController()")
        stop = patch.index("StopDeviceCallThread();", destructor)
        delete_thread = patch.index("delete DeviceCallThread;", destructor)
        self.assertLess(stop, delete_thread)
        self.assertIn("void RGBController::StopDeviceCallThread()", patch)

        upstream_registry = (
            ROOT / "integrations/openrgb/src/upstream_razer_registry.cpp"
        ).read_text(encoding="utf-8")
        self.assertIn("~HyperFluxRazerController() override", upstream_registry)
        self.assertIn("StopDeviceCallThread();", upstream_registry)

        native_controller = (
            ROOT / "integrations/openrgb/src/native_controller.cpp"
        ).read_text(encoding="utf-8")
        self.assertIn("NativeController::~NativeController()", native_controller)
        self.assertIn("HFX_OPENRGB_SAFE_DEVICE_THREAD_STOP", native_controller)

        cmake = (ROOT / "integrations/openrgb/CMakeLists.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '"${HFX_OPENRGB_CONTROLLER_SOURCE_DIR}/RGBController/RGBController.cpp"',
            cmake,
        )
        self.assertIn(
            'PROPERTIES COMPILE_OPTIONS "-fno-sanitize=thread"',
            cmake,
        )
        self.assertIn("PRIVATE HFX_OPENRGB_TSAN_ANNOTATIONS=1", cmake)
        self.assertIn("PRIVATE HFX_OPENRGB_SAFE_DEVICE_THREAD_STOP=1", cmake)

    def test_transform_does_not_expand_beyond_exact_base_and_razer_sources(self) -> None:
        self.assertEqual(
            _safe_path("RGBController/RGBController.h", "fixture").as_posix(),
            "RGBController/RGBController.h",
        )
        with self.assertRaisesRegex(ModelError, "leaves the pinned Razer controller"):
            _safe_path("RGBController/RGBControllerKeyNames.cpp", "fixture")


if __name__ == "__main__":
    unittest.main()
