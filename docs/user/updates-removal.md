# Updates And Removal

## Update HyperFlux

HyperFlux updates arrive with your normal system update.

1. Close OpenRGB and Polychromatic.
2. Update your system:

   ```sh
   sudo pacman -Syu
   ```

   If a HyperFlux update is available, Pacman lists `hyperflux-next-linux` and,
   with Full, `hyperflux-next-full` among the packages to upgrade. Pacman checks
   their signatures against the HyperFlux key you added during installation.
   Your settings in `/etc/hyperflux-next/bridge.json` are kept.

3. Check the result:

   ```sh
   hyperfluxctl doctor
   ```

   You should see `HyperFlux Next is ready.` If Doctor asks you to reboot so
   the new driver becomes active, reboot normally with the receiver connected
   and run Doctor again.

If Doctor still needs attention, see [troubleshooting](troubleshooting.md).

## Go Back To An Earlier Version

Pacman keeps the packages you installed before in its cache.

1. See which versions you have:

   ```sh
   ls /var/cache/pacman/pkg/hyperflux-next-*
   ```

2. Install the earlier Core and Full files together, both from the same
   version, then reboot with the receiver connected:

   ```sh
   sudo pacman -U \
     /var/cache/pacman/pkg/hyperflux-next-linux-VERSION-x86_64.pkg.tar.zst \
     /var/cache/pacman/pkg/hyperflux-next-full-VERSION-x86_64.pkg.tar.zst
   sudo reboot
   ```

   Replace `VERSION` with the same earlier version in both lines. If you use
   Core alone, install only the Core file.

If the cache was cleaned, download both files from that version's GitHub
release. Do not install the stock `openrazer-driver-dkms` package while going
back.

## Remove Full And Keep Core

Full includes its own copy of the OpenRazer driver. If you use OpenRazer or
Polychromatic, swap that copy for the stock driver in one step:

```sh
sudo pacman -S openrazer-driver-dkms
```

Pacman says that `openrazer-driver-dkms` and `hyperflux-next-full` are in
conflict and asks whether to remove `hyperflux-next-full`. Type `y` and press
Enter.

If you don't use OpenRazer at all, remove Full directly:

```sh
sudo pacman -R hyperflux-next-full
```

Either way, Core and its services keep working, but your apps can no longer
reach devices paired to the receiver.

While the HyperFlux repository is still set up, a later `sudo pacman -Syu` may
ask:

```text
:: Replace openrazer-driver-dkms with hyperflux-next/hyperflux-next-full? [Y/n]
```

To keep the stock driver, type `n` and press Enter. Pressing Enter alone means
yes and installs Full again. Pacman may ask again on later updates; answer `n`
each time.

## Remove HyperFlux Next Completely

1. Turn off the HyperFlux services:

   ```sh
   sudo systemctl disable --now hyperflux-next-bridge.service \
     "hyperflux-next-client@$(id -u).socket"
   ```

2. Remove both packages:

   ```sh
   sudo pacman -R hyperflux-next-full hyperflux-next-linux
   ```

   If Pacman says that `openrazer-daemon` needs `openrazer-driver-dkms`, first
   swap in the stock driver as described in
   [Remove Full And Keep Core](#remove-full-and-keep-core), then remove
   `hyperflux-next-linux`.

   If you changed `/etc/hyperflux-next/bridge.json`, Pacman keeps your copy as
   `/etc/hyperflux-next/bridge.json.pacsave`. To remove that copy too, use
   `sudo pacman -Rn` instead of `sudo pacman -R`.

OpenRGB, OpenRazer, and Polychromatic are not removed. They belong to their own
packages.

## Remove The HyperFlux Repository

Do this only after you have removed the packages.

1. Open `/etc/pacman.conf` as root, for example with `sudoedit /etc/pacman.conf`.
   - If you used the setup command, delete the
     `Include = /etc/pacman.d/hyperflux-next.conf` line.
   - If you added the repository by hand, delete the `[hyperflux-next]` line and
     its `SigLevel` and `Server` lines.
2. Remove the repository file, if it exists, and the HyperFlux key:

   ```sh
   sudo rm -f /etc/pacman.d/hyperflux-next.conf
   sudo pacman-key --delete 8BF8A266E756BD1C24C930918B3F3850007159E8
   ```
