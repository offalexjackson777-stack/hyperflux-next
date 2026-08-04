# Updates And Removal

HyperFlux updates distinguish package registration, kernel activation, service
activation, and physical qualification. Completing one layer does not imply the
next.

## Before An Update

Review the exact Core and Full package identities, preserve configuration and
rollback inputs, close device applications, and confirm restoration remains
disabled.

## After An Update

Use Doctor before application testing. Kernel or service activation is an
explicit operation and may require a normal reboot when the active module bytes
do not match the installed candidate.

The signed third-party repository participates in ordinary `pacman -Syu` runs.
Pacman verifies the repository database and package signatures against the
locally trusted HyperFlux Next key before selecting an update.

## Removal

Remove packages through the distribution package manager so ownership decides
which files leave the system. Never remove files by broad name matching. Stock
OpenRGB, OpenRazer userspace, and Polychromatic remain independently owned.
