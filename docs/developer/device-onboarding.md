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

## Owner Reports

A device listed as expected to work has had its support checked in the upstream source
code, but nobody on the project has tried it. Its owners can close part of that gap. An
owner runs `hyperfluxctl qualification --json`, pastes the output into the device support
form, and ticks what they saw: that the device worked normally, and in which lighting app
it responded. The [user hardware page](../user/hardware.md#confirm-that-your-device-works)
gives them those steps.

To turn such a request into a record:

1. Save the issue body, as written, to a file such as `issue.md`.
2. Run `./hfx qualification ingest issue.md --issue N`, where `N` is the issue number.
   Add `--check` first to see the record without writing it. If the report holds more
   than one device that could be confirmed, name it with `--device device-2`.
3. Run `./hfx generate`. The device moves into the "Confirmed by owners" list in the
   [supported-hardware reference](../generated/supported-hardware.md), linked to the issue.
4. Review and commit the new file under `evidence/owner-reports/` with the regenerated
   pages.

If the command refuses the report, reply on the issue with its message. Every message says
what to change, and none of them repeats what the report contained.

### What the command checks

The rules live in `assurance/owner-reports.json` and the record format in
`schemas/owner-report.schema.json`. The command reads the pasted report and checks it
against the report format in `apps/device-qualification/contracts`. The report has to come
from `hyperfluxctl qualification` itself, talking to the real local service, and say that
it is ready, that it is not the simulator, and that nothing was written to hardware. Its
HyperFlux version must be at least the first release that has the command, and none of its
problem codes may be an error or critical.

The receiver must be the HyperFlux receiver, with the exact receiver profile this
repository ships today. A report from an older build fails here, and the owner is asked to
update and run it again: an older profile may not behave the same way, so its result says
little about the current code. The device must match one route that is expected to work.
Devices that are already tested, or not supported yet, are refused, as is any device that
HyperFlux reports with a profile, because a profiled device is not what an owner report is
for.

The owner must tick that the device worked, and at least one lighting app. Each ticked app
has to be one the upstream source code supports for that device; ticking OpenRGB for a
device only OpenRazer knows is refused, since the claim cannot be true.

### How privacy is kept

The report format already leaves out serial numbers, paths and names, but a report pasted
into an issue can be edited, so the command does not trust that. It scans every string in
the report for text that looks private: home and system paths, user or host names, email
addresses, network and hardware addresses, UUIDs, environment variables, and long mixed
letter-and-digit tokens that could be serial numbers. If it finds any, it refuses the whole
report and names only the place in the report, never the text. The record it writes is
built from a fixed list of fields; nothing free-form is copied from the report or the issue,
and the record is scanned again before it is written.

### What a record proves, and what it does not

A record shows that one owner, with a current build, saw the device recognised through the
receiver and ticked that it worked. One record is enough to list a device as confirmed by
owners. The ticks are the owner's own word; the report proves the device was present, but
it never sends a command, so it cannot show that the lighting changed. The tier is
documentation only: HyperFlux treats the device exactly as before, and it is not the same as
the project testing the device on real hardware, which still needs the steps above.
