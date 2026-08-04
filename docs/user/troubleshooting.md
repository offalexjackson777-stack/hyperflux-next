# Troubleshooting

Start with the read-only checks:

```sh
hyperfluxctl status
hyperfluxctl doctor
```

Doctor reports the first missing boundary, one safe action, and how to verify
the result. It does not write hardware or restart services.

## A Device Is Missing From An Application

1. Confirm Doctor reports the driver, bridge, and expected route healthy.
2. Close the other device-control application so it releases its lease.
3. Refresh or reopen the affected stock application once.
4. If the route is unknown, sleeping, stale, or unqualified, leave it
   non-writable and include the Doctor summary in a support request.

Do not create a manual USB binding or register the receiver as a fake device.

## Installed Driver Does Not Match The Running Driver

The normal recovery is a reboot with the receiver connected. This preserves
input on a receiver-only desktop.

### Advanced Disconnected Activation

> [!WARNING]
> Stop here unless a built-in keyboard and touchpad, a second keyboard and
> mouse, or a verified SSH session can recover the machine. Disconnecting the
> only affected receiver can remove all desktop input.

With alternate control already verified:

1. Close OpenRGB and Polychromatic.
2. Disconnect only the affected HyperFlux receiver.
3. Run:

   ```sh
   sudo /usr/lib/hyperflux-next/hyperflux-next-activate activate-disconnected-driver
   ```

4. Reconnect only after the command reports matching installed and loaded
   identities.
5. If activation does not complete, leave the receiver connected and reboot
   normally. Do not force module removal or replace files under `/usr/src`.

An older loaded OpenRazer route module still requires a normal reboot; the Core
activation command does not reload OpenRazer modules.

## Support Bundle Privacy

Preview a support bundle before writing it:

```sh
hyperfluxctl support-bundle --preview
```

The preview performs no upload, active device query, or hardware write. Never
post raw HID captures, hardware serials, private filesystem paths, or an
unreviewed journal dump in a public issue.

## Rollback

Install a complete prior HyperFlux Core and Full package pair through the native
package manager, then reboot with the receiver connected. Mixing an ordinary
`openrazer-driver-dkms` package with the HyperFlux-transformed driver is not a
supported rollback.
