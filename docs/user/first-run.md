# First Run

This guide takes you from installed packages to working lighting in your
applications. Keep the receiver plugged in and use the keyboard and mouse
normally the whole time.

## 1. Install

Install Full, or Core alone, as shown in the [installation guide](installation.md).
Installing the packages does not start any service.

## 2. Enable The Services

Enable the bridge and the socket for your desktop user:

```sh
sudo systemctl enable hyperflux-next-bridge.service \
  "hyperflux-next-client@$(id -u).socket"
```

With Full, the bridge also starts the OpenRazer relay for you. It does not
replace or manage the stock OpenRazer daemon.

## 3. Reboot

Reboot normally so the driver and services start in the right order:

```sh
sudo reboot
```

## 4. Check Status

After you log in, run:

```sh
hyperfluxctl status
```

Status shows the driver, the bridge, the receivers HyperFlux has found, and the
next thing to do, if anything.

## 5. Run Doctor

```sh
hyperfluxctl doctor
```

When everything works, Doctor prints `HyperFlux Next is ready.` That means the
driver is loaded, the bridge is running, and nothing needs fixing. With Full,
Doctor also checks that stock applications can reach HyperFlux.

If Doctor prints `HyperFlux Next needs attention.`, it names the first problem,
why it matters, one thing to do, and how to check again. Do that, then run
Doctor again. Run `hyperfluxctl doctor --explain` for more detail, or see
[troubleshooting](troubleshooting.md).

## 6. Open Your Applications One At A Time

Stock OpenRGB, OpenRazer, and Polychromatic are separate Arch packages. Install
the ones you want with Pacman. Then:

1. Open OpenRGB. Check that your keyboard and mouse appear and change a colour.
   Close OpenRGB.
2. Open Polychromatic. Check that your keyboard and mouse appear and apply an
   effect. Close Polychromatic.

Trying one application at a time makes it easy to see which one has a problem.
After that, use them however you like. See [applications](applications.md)
for what each one can do.

## Saved Lighting

HyperFlux's own lighting restore is off by default, and your applications keep
their own settings. Leave it off. Turning it on does not make a missing device
appear.
