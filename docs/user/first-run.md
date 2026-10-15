# First Run

This guide takes you from a fresh installation to working lighting in your
apps. Keep the receiver plugged in and use the keyboard and mouse normally the
whole time.

## Make Sure HyperFlux Is Turned On

If you followed every step of the [installation guide](installation.md), skip to
[the next section](#check-that-hyperflux-is-ready).

Otherwise, turn on the HyperFlux service and your desktop connection to it:

```sh
sudo systemctl enable hyperflux-next-bridge.service \
  "hyperflux-next-client@$(id -u).socket"
```

Then restart, so everything starts in the right order:

```sh
sudo reboot
```

With Full, the HyperFlux service also starts the piece that connects it to
OpenRazer. It does not replace or manage OpenRazer's own background service.

## Check That HyperFlux Is Ready

1. After you log in, run:

   ```sh
   hyperfluxctl status
   ```

   You should see the driver, the service, and your receiver, followed by a
   `Next:` line. When nothing needs doing, it says `Next: no action required`.

2. Then run:

   ```sh
   hyperfluxctl doctor
   ```

   You should see `HyperFlux Next is ready.` and `No action required.`

If Doctor prints `HyperFlux Next needs attention.`, it names the problem, why it
matters, one thing to do (`Do:`), and how to check again (`Verify:`). Do that,
then run Doctor again. For more detail, run `hyperfluxctl doctor --explain` or
see [troubleshooting](troubleshooting.md).

## Install Your Apps

The apps are separate projects. Install only the ones you want.

- **OpenRGB** is in the official Arch repositories:

  ```sh
  sudo pacman -S openrgb
  ```

- **OpenRazer** is the background service that Polychromatic needs. It is in
  the official Arch repositories:

  ```sh
  sudo pacman -S openrazer-daemon python-openrazer
  ```

- **Polychromatic** is not in the official Arch repositories. Install it from
  the AUR or your usual source.

## Try Each App, One At A Time

1. Open OpenRGB. Your keyboard and mouse should appear by name. Change a colour
   and check that the device changes. Close OpenRGB.
2. Open Polychromatic. Your keyboard and mouse should appear by name. Apply an
   effect and check that the device changes. Close Polychromatic.

Trying one app at a time shows you which one has a problem, if any. After that,
use them however you like. The [applications guide](applications.md) explains
what each one can do.

If a device is missing, see
[a device is missing from an app](troubleshooting.md#a-device-is-missing-from-an-application).

## Saved Lighting

Your apps keep their own lighting settings. HyperFlux also has its own lighting
restore, which is off by default. Leave it off. Turning it on does not make a
missing device appear.
