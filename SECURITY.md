# Security Policy

If you think you found a security problem in HyperFlux, please report it
privately as described below. Do not post it in a public issue.

## Supported Versions

| Version | Platform | Supported |
| --- | --- | --- |
| Latest 2.0.x release (currently 2.0.2) | Arch Linux x86_64 | Yes |
| Earlier 2.0.x patch releases | Arch Linux x86_64 | No - update to the latest 2.0.x |
| Development branches and unpublished builds | Any | No |

Each 2.0.x patch release replaces the previous one. Install updates with an
ordinary `sudo pacman -Syu`. Other distributions are not supported yet; see the
[roadmap](docs/project/roadmap.md).

## How To Report

1. Open
   [GitHub private vulnerability reporting](https://github.com/offalexjackson777-stack/hyperflux-next/security/advisories/new).
   Only you and the maintainer can see the report.
2. Say what you did, what happened, and which HyperFlux version you use.
3. Include only what is needed to reproduce the problem.

Report privately if the problem could let something:

- send commands to a device without permission,
- break kernel memory safety,
- gain more privileges than it should,
- expose secrets or identify a person or machine,

or if it plausibly leads to any of these.

Please do not attach hardware serial numbers, machine or user names, private
file paths, raw HID or USB data, memory dumps, traffic captures, passwords, or
whole terminal or journal logs. After a first look, the maintainer may ask for
one limited file that you have checked for private data.

## What Happens Next

- The report stays private while the maintainer works out its impact.
- Receipt should be acknowledged within 7 days when the maintainer is available.
- When a fix ships and the problem is disclosed depends on how serious it is,
  whether it can be reproduced, and whether a published release is affected.
- A fix for a published release ships as a new 2.0.x package update.

## How HyperFlux Limits Damage

- Apps never talk to the receiver directly.
- Outside the kernel, only one background service writes to the hardware.
- Each write must belong to the receiver as it is currently connected, a tested
  path to the device, a known capability, a deadline, a transaction, and a
  short-lived claim on the device.
- When anything is unknown, out of date, malformed, partial, or untested,
  HyperFlux refuses the action.
- Every queue, message, log, set of frames, retry rule, and stored history has a
  fixed maximum size.
- Support bundles always leave out raw device traffic, private file paths,
  and anything that permanently identifies a device or machine.
- Research and hardware-test tools are never part of what the installed product
  depends on.

For ordinary help, see [SUPPORT.md](SUPPORT.md) instead of opening a security
report.
