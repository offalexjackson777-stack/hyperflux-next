# Retired DKMS Driver Takes The Receiver

After a kernel update, an old pre-release HyperFlux driver could come back and take the
receiver away from the released driver. Nothing worked, yet Doctor said the driver was
active. 2.0.1 detects this precisely and adds a previewable migration that removes the old
driver safely.

Issue: [#34](https://github.com/offalexjackson777-stack/hyperflux-next/issues/34).

## What People Saw

The bridge kept running but had no receiver, so no devices appeared. Doctor still reported
"driver active". This was seen on 2026-08-11 on a machine that had once run a pre-release
build.

## How It Was Tracked Down

A running bridge with no receiver and a driver reported as active cannot both be right, so
the first suspect was Doctor's own driver check. It treated a loaded `hid-hyperflux-next`
module as proof of a usable driver. A module can be loaded and still not own the device.

The driver that did own the receiver was `hid-razer-hyperflux-v2`, from pre-release builds
0.1.0, 0.1.1, and 0.1.2. Those builds had registered it with DKMS, so every kernel update
rebuilt it.

## The Cause

`hid-razer-hyperflux-v2` advertises the same receiver modalias as `hid-hyperflux-next`.
When both are installed, the kernel may bind the old driver first. DKMS kept the old driver
alive across kernel updates, and stale boot images could still carry it.

The diagnosis gap made it worse. Doctor checked that a module was loaded, not that it owned
the receiver, so it reported a healthy driver and pointed the user at the wrong layer.

## The Fix

[f41d9bb](https://github.com/offalexjackson777-stack/hyperflux-next/commit/f41d9bb4beb44821e9e1d8987f5191d99344d297)

The fix has a read-only detection half and an opt-in migration half.

**Detection.** `runtime/linux.json` now lists the retired drivers. The probe reads the
driver actually bound to each qualified receiver interface from sysfs, and looks for DKMS
registrations of the exact retired versions. Doctor, status, and the support bundle report:

- `HFX-KERNEL-003` when another driver, or no driver, owns a receiver interface;
- `HFX-KERNEL-004` when a retired driver is still registered with DKMS.

The status line says "Driver: conflict (receiver owned by ...)" instead of "active", and
bridge and route findings are not reported on top of the real cause.

**Migration.** `hyperflux-next-activate migrate-retired-dkms` previews by default and
changes nothing. With `--apply`, run as root, it:

1. refuses if any unknown module claims the receiver modalias, if an unknown driver is
   bound, or if the boot-image tooling is missing or ambiguous;
2. records what it needs for a rollback;
3. removes only the exact known DKMS registrations;
4. moves, never deletes, their `/usr/src` trees into the migration record;
5. rebuilds boot images with the distribution's own dracut hook or mkinitcpio;
6. lists every rebuilt image and stops if a listing is empty, fails, or still contains a
   retired module.

We chose a narrow, previewable migration over a broad cleanup. The cost is that it
refuses on anything it does not recognise and leaves that machine for a person to look
at. In exchange it never unloads a live driver, never adds a blacklist, never cleans
`/usr/src` broadly, and never runs from a package hook.

## How It Was Checked

- 17 new unit tests cover the probe, the Doctor findings, and the migration.
- On a real machine without the old driver, Doctor stays Ready with no false finding, and
  the migration preview reports nothing to do.
- `--apply` without root refuses.

## What Is Still Open

The issue is done when a supported kernel update after migration cannot bring the old
driver back. That physical check is part of the 2.0.1 release checklist,
[#36](https://github.com/offalexjackson777-stack/hyperflux-next/issues/36).
