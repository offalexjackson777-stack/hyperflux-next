# Troubleshooting

## Start Here

Run these two checks first. They only look; they change nothing.

```sh
hyperfluxctl status
hyperfluxctl doctor
```

If Doctor prints `HyperFlux Next is ready.`, HyperFlux itself is working. If it
prints `HyperFlux Next needs attention.`, it names the first problem, one thing
to do about it (`Do:`), and how to check that it worked (`Verify:`). Do that,
then run Doctor again. For more detail, run:

```sh
hyperfluxctl doctor --explain
```

Doctor does not change device settings or restart services.

Some behaviour is a known limit rather than a fault. Check
[known limitations](limitations.md) as well.

## A Device Is Missing From An Application

1. Wake the device: move the mouse or press a key. A sleeping device may not
   answer until it wakes.
2. Close any other lighting application, such as OpenRGB or Polychromatic.
   Only one application should control the device while you check.
3. Reopen the affected application, or refresh it once.
4. Run `hyperfluxctl doctor` and do what it says.
5. If the device is still missing, preview a support bundle with
   `hyperfluxctl support-bundle --preview` and [ask for help](#ask-for-help).

Do not bind the receiver to another driver by hand or add it as a custom device
in an application. Neither makes a device appear.

### After Switching A Wireless Keyboard Off And On

Polychromatic does not notice by itself when the keyboard comes back.

1. Wait a few seconds after switching the keyboard on.
2. Press F5 on Polychromatic's Devices tab. Retry on the device page, or
   View → Force Refresh, also works.
3. If the keyboard is still missing, run `hyperfluxctl status` and check that
   the receiver is listed and the `Next:` line asks for nothing.

## The Newly Installed Driver Is Not Active Yet

Doctor reports this as `HFX-KERNEL-001`, usually after an update. Reboot normally
with the receiver connected:

```sh
sudo reboot
```

Then run `hyperfluxctl doctor` again. This keeps your keyboard and mouse working
the whole time.

<details>
<summary>Advanced: activate the driver without rebooting</summary>

> [!WARNING]
> Stop here unless a built-in keyboard and touchpad, a second keyboard and
> mouse, or a working SSH session can control the computer. Unplugging the
> receiver removes the keyboard and mouse paired to it.

1. Close OpenRGB and Polychromatic.
2. Unplug only the HyperFlux receiver.
3. Run:

   ```sh
   sudo /usr/lib/hyperflux-next/hyperflux-next-activate activate-disconnected-driver
   ```

4. Plug the receiver back in only after the command reports that the installed
   and loaded driver match.
5. If it does not finish, plug the receiver back in and reboot normally. Do not
   force the driver out or replace files under `/usr/src`.

This does not reload OpenRazer's drivers. If those changed too, you still need
a normal reboot.

</details>

## Go Back To An Earlier Version

See [updates and removal](updates-removal.md#go-back-to-an-earlier-version).

## Ask For Help

1. Preview what a support bundle would contain:

   ```sh
   hyperfluxctl support-bundle --preview
   ```

   The preview uploads nothing, does not query your devices, and changes
   nothing.

2. Open a [support request](../../SUPPORT.md) and include the Doctor output and
   the preview.

Never post raw HID captures, hardware serial numbers, private file paths, or a
full system journal in a public issue. The [privacy guide](privacy.md) lists
what a bundle contains.
