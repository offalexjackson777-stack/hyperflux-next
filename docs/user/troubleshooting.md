# Troubleshooting

Start with these two checks. They only read state:

```sh
hyperfluxctl status
hyperfluxctl doctor
```

Doctor names the first problem it finds, one thing to do about it, and how to
check that it worked. It does not change device settings or restart services.

## A Device Is Missing From An Application

1. Wake the device: move the mouse or press a key. A sleeping device may not
   answer until it wakes.
2. Close any other lighting application, such as OpenRGB or Polychromatic.
   Only one application should control the device while you check.
3. Reopen the affected application, or refresh it once.
4. Run `hyperfluxctl doctor` and do what it says.
5. If the device is still missing, preview a support bundle with
   `hyperfluxctl support-bundle --preview` and include the Doctor output in a
   [support request](../../SUPPORT.md).

Do not bind the receiver to another driver by hand or add it as a custom device
in an application. Neither makes a device appear. Some behaviour is a known
limit rather than a fault; check [known limitations](limitations.md) first.

### After Switching A Wireless Keyboard Off And On

The keyboard can leave OpenRazer while it is off and come back after it is
switched on. Polychromatic does not notice this by itself. Wait a few seconds
after switching the keyboard on, then press F5 on Polychromatic's Devices tab.
Retry on the device page or View → Force Refresh also works. If the keyboard
is still missing, run `hyperfluxctl status` to check that its route is back.

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
