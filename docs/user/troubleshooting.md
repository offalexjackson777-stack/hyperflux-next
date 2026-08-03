# Troubleshooting

Start with the read-only health check:

```sh
hyperfluxctl doctor
```

Doctor reports what works, the primary issue, one safe action, and how to verify
the result. Use the longer inventory only when more context is useful:

```sh
hyperfluxctl status
```

If a problem remains, inspect the privacy declaration before writing a support
bundle:

```sh
hyperfluxctl support-bundle --preview
```

The preview performs no upload, active device query, or hardware write. Never
post raw HID captures, hardware serials, private filesystem paths, or an
unreviewed journal dump in a public issue.

## Update Activation

Package installation does not activate HyperFlux services or replace an in-use
kernel module. When Doctor reports that a newer installed core driver is not
active, reboot or disconnect every supported HyperFlux receiver and run:

```sh
sudo /usr/lib/hyperflux-next/hyperflux-next-activate activate-disconnected-driver
```

Reconnect only after the command reports matching installed and loaded module
identities. A service restart is not a substitute for kernel activation. An
older loaded OpenRazer route module still requires a reboot; the explicit core
activation command does not reload OpenRazer modules.

For OpenRazer, package release suffixes on the verified source line do not
matter. A newer upstream source line is deliberately blocked until HyperFlux's
transport patch is rebased and reverified. Doctor reports that boundary; do not
replace files under `/usr/src` or force the relay to start. Install a HyperFlux
package verified for the new source, or keep the matching verified OpenRazer
daemon and Python client.

Rollback by installing a complete prior source-bound HyperFlux package through
the package manager. Mixing an ordinary `openrazer-driver-dkms` package with the
HyperFlux-owned transformed driver is not a supported recovery path.

## Missing Application Devices

First confirm that Doctor reports the driver and bridge ready. Then restart or
rescan the application so its adapter requests a current SDK snapshot. Unknown,
ambiguous, sleeping, or unqualified routes remain visible only where the
application can describe them truthfully; they receive no inherited write
authority.
