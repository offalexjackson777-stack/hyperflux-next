# OpenRGB 1.0 Port

Arch moved OpenRGB from the 1.0 release candidate to OpenRGB 1.0, and the HyperFlux plugin
stopped loading. Porting it to the new plugin interface then exposed two lifetime bugs, one
of which crashed OpenRGB when it quit. All three were fixed before 2.0.1 was released.

Tracked in the 2.0.1 release checklist,
[#36](https://github.com/offalexjackson777-stack/hyperflux-next/issues/36), as the gate for
OpenRGB staging and API changes.

## What People Saw

On a current Arch system with OpenRGB 1.0, the HyperFlux plugin would not load, so the
receiver's devices were missing from OpenRGB. Testing the first port then showed that
OpenRGB could crash while quitting.

## How It Was Tracked Down

Arch `extra` now ships `openrgb 1.0-2`, built with plugin API 5 and Qt 6. The plugin had been
built against OpenRGB 1.0rc3, which used plugin API 4 and Qt 5, so it could not load. This
needed a port, not a bug fix.

The port brought two more problems to light:

- ThreadSanitizer reported lock-order inversions in the controller registry, the plugin
  coordinator, and the plugin application contracts. None were real deadlocks; they came
  from a lock that was destroyed while still held.
- The crash on quit came from the order in which OpenRGB tears itself down: the GUI stops
  following the device list before plugins are unloaded.

## The Cause

**API change.** Under plugin API 5, the host registers a plugin's controllers through the
plugin API's virtual controllers, and the plugin was built for the older interface.

**Lock destroyed while held.** OpenRGB 1.0's `RGBController::Shutdown()` takes
`AccessMutex` exclusively and returns without releasing it. `~NativeController` then
destroyed a locked `shared_mutex`, which is undefined behaviour, and ThreadSanitizer kept the
destroyed lock in the thread's held set, which produced the false inversions.

**Crash on quit.** On exit, `OpenRGBDialog::closeEvent` unregisters its resource manager
callback before `PluginManager::UnloadPlugins()`. Unloading deleted the
`RGBController_Virtual` objects the plugin had presented, but the GUI no longer followed the
device list, so its device pages survived. When the dialog was torn down later,
`~OpenRGBDevicePage` called `UnregisterUpdateCallback` on a freed controller.

## The Fix

**Port to OpenRGB 1.0 and plugin API 5.**

[d93bbfa](https://github.com/offalexjackson777-stack/hyperflux-next/commit/d93bbfad2714bcbcfe7e5a9eae124fbad47d1bd9)

- OpenRGB is pinned to the 1.0 tag commit `81bbe18a`, the transport-seam patch is rebased,
  and the plugin builds against Qt 6.
- `VirtualControllerMirror` builds the host's controller from the plugin's own one and
  forwards LED and mode updates from the host's update callback.
- `DeviceThreadBarrier` now requires `CONTROLLER_FLAG_RESET_BEFORE_UPDATE`; without it the
  device thread could drop a probe or a user update. `NativeController` sets the flag.
- The SDK test harness is ported to the 1.0 `ResourceManager` API and built with qmake6.
  OpenRGB 1.0 serves only hardware controllers to SDK clients by default, so the harness
  turns on "Serve All Controllers".

Follow-ups updated the pinned OpenRGB device catalog, which gained one Razer record
(Huntsman V3 HE Magnetic Mini, `1532:02E4`), and removed the API 4 detection-suspension
path that API 5 no longer uses.

**Release the lock after `Shutdown()`.** `~NativeController` now unlocks `AccessMutex`
after `Shutdown()`. By then the device thread is joined and the controller is unregistered,
so nothing else can be waiting on it. A contract test checks that `Shutdown()` still leaves
the lock held, so this is revisited if OpenRGB changes. The test's fake plugin host now
deletes virtual controllers the same way the real host does.

[e870c69](https://github.com/offalexjackson777-stack/hyperflux-next/commit/e870c69c460ad401fed00b48b515f68a84a82bc3)

**Keep presented controllers alive past an exit-time unload.** Unloading now tells the host
mirror to stop deleting controllers. From then on, each controller is unregistered, cut off
from plugin code, and parked in a process-wide store keyed by the plugin API object. A later
reload of the plugin frees the parked controllers once the live GUI has dropped their pages.
On exit they are never freed. Removing a device while the plugin is loaded still deletes its
controller at once.

[2f41a62](https://github.com/offalexjackson777-stack/hyperflux-next/commit/2f41a6233d5439a71db9e30ddab874686699e1a0)

We rejected keeping the plugin mapped with `-z nodelete`. The freed object belongs to the
host, not the plugin, and keeping the plugin mapped would keep its static state alive
across reloads.

## How It Was Checked

- C++ contracts passed: adapter 26 of 26 and native 34 of 34. The virtual controller mirror
  passed 600 of 600 stress runs.
- `openrgb-thread-sanitizer` passed 34 of 34 after the lock fix.
- The native plugin application contract now loads through plugin API 5 with OpenRGB's own
  `RGBController_Virtual`, attaches device pages, unloads, runs the page destructors, and
  reloads. It fails without the exit-time fix. It also runs under ThreadSanitizer.
- The packaged plugin passed SDK qualification in an isolated container.
- Two package builds from the same commit were byte-identical.
- An ABI check against the host's `/usr/bin/openrgb` found no unresolved symbols. This check
  is weak: the plugin compiles the OpenRGB classes it uses and imports no OpenRGB symbols,
  so it proves only that the Qt 6 and C++ runtime imports resolve.

## What Is Still Open

A host-load test still has to be run by hand, because starting OpenRGB touches real
hardware: the HyperFlux Next plugin tab and the receiver's devices should appear in OpenRGB.
It is listed in [#36](https://github.com/offalexjackson777-stack/hyperflux-next/issues/36).

SDK clients, such as scripts using the OpenRGB SDK or a second OpenRGB instance, see the
HyperFlux controllers only when **Settings → Server → Serve All Controllers** is on. Controls
inside OpenRGB itself are not affected.
