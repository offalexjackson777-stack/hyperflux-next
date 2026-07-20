# License Decision

HyperFlux Next project-owned source, tests, tooling, and documentation use the
SPDX expression `GPL-2.0-only` unless a file declares another compatible
license. The Linux userspace ABI will use
`GPL-2.0 WITH Linux-syscall-note` where appropriate.

This choice preserves compatibility with the Linux kernel module and the
OpenRGB integration boundary while giving users clear rights to inspect,
modify, and redistribute the complete system. It does not automatically admit
code or data from the source repositories. Every imported component must record
its origin, source revision, license, transformation, and migration decision.

