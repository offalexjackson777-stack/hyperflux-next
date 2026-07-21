# License Decision

HyperFlux Next project-owned source, tests, tooling, and documentation use the
SPDX expression `GPL-2.0-only` unless a file declares another compatible
license. The Linux userspace ABI will use
`GPL-2.0 WITH Linux-syscall-note` where appropriate.

Cross-application Python SDK files use `GPL-2.0-or-later`. A consumer may
therefore select GPLv2 when integrating with GPL-2.0-only applications or GPLv3
when integrating with GPL-3.0-only applications. Application-specific adapter
files may use the application's compatible license and remain isolated from the
kernel module and core service. Every such exception is declared per file and
included with its corresponding license text.

This choice preserves compatibility with the Linux kernel module and the
OpenRGB integration boundary, while the SDK exception permits a native
Polychromatic adapter without relicensing the kernel or bridge. It does not
automatically admit code or data from the source repositories. Every imported
component must record its origin, source revision, license, transformation, and
migration decision.
