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

A compatible userspace update can resume the bridge automatically. Linux cannot
replace an in-use kernel module invisibly. When Doctor reports that a newer
installed driver is not active, reboot, or follow the receiver-disconnect and
module-reload action printed by Doctor. A service restart is not a substitute
for kernel activation.

## Missing Application Devices

First confirm that Doctor reports the driver and bridge ready. Then restart or
rescan the application so its adapter requests a current SDK snapshot. Unknown,
ambiguous, sleeping, or unqualified routes remain visible only where the
application can describe them truthfully; they receive no inherited write
authority.
