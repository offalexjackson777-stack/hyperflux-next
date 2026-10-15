# Device Onboarding

Adding a device is risky because a wrong guess sends lighting or settings commands to
hardware that may not understand them. So new device support starts from what the pinned
upstream projects already record about the device, not from a local serial number, a
receiver path, or a rule that covers a whole vendor. A new device stays non-writable until
it has been checked on real hardware and a maintainer has reviewed the change.

## Add A Device

1. Import the exact upstream identity and command records for the device, with
   `./hfx knowledge import` against the pinned OpenRazer and OpenRGB checkouts.
2. Sort its capabilities into operational ones and research-only ones.
3. Regenerate the route catalogs with `./hfx generate`.
4. Run the contracts, including the negative ones that prove a wrong or unknown device is
   refused, not only the positive ones.
5. Check the device on real hardware. This is a separate, final step.

The [native device onboarding guide](../architecture/native-device-onboarding.md) has the
commands for drafting a profile from a receiver observation and assessing whether it can be
promoted, and the checks a maintainer reviews before any profile data changes.

## Why It Works This Way

The [device knowledge model](../architecture/device-knowledge.md) explains how upstream
records become the device catalogs, and why HyperFlux does not guess a transport for a
device it has not seen recorded.
