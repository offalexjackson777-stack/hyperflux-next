# Updates And Removal

## Update

HyperFlux updates arrive with your normal system update. Close OpenRGB and
Polychromatic first, then run:

```sh
sudo pacman -Syu
```

Pacman checks the HyperFlux repository and package signatures against the
HyperFlux key you trusted during installation before it installs anything.
Your settings in `/etc/hyperflux-next/bridge.json` are kept.

Then check the result:

```sh
hyperfluxctl doctor
```

If Doctor asks you to reboot so the new driver becomes active, reboot normally
with the receiver connected and run Doctor again. See
[troubleshooting](troubleshooting.md) if it still needs attention.

## Go Back To An Earlier Version

Install an earlier Core and Full pair together, both from the same release,
then reboot with the receiver connected. Pacman keeps the packages you
installed before in its cache:

```sh
sudo pacman -U \
  /var/cache/pacman/pkg/hyperflux-next-linux-VERSION-x86_64.pkg.tar.zst \
  /var/cache/pacman/pkg/hyperflux-next-full-VERSION-x86_64.pkg.tar.zst
sudo reboot
```

Replace `VERSION` with the same earlier version for both files; run
`ls /var/cache/pacman/pkg/hyperflux-next-*` to see which versions you have. If
the cache was cleaned, download both files from that version's GitHub release.
If you use Core alone, install only the Core file. Do not mix in the stock
`openrazer-driver-dkms` package while going back.

## Remove Full And Keep Core

```sh
sudo pacman -R hyperflux-next-full
```

Core and its services keep working, but stock applications can no longer reach
devices paired to the receiver. To use stock OpenRazer with devices plugged in
by cable again, install its driver:

```sh
sudo pacman -S openrazer-driver-dkms
```

While the HyperFlux repository is still set up, a later `sudo pacman -Syu` may
offer to replace `openrazer-driver-dkms` with `hyperflux-next-full`. Answer no
to keep the stock driver.

## Remove HyperFlux Next Completely

Turn off the services first, then remove both packages:

```sh
sudo systemctl disable --now hyperflux-next-bridge.service \
  "hyperflux-next-client@$(id -u).socket"
sudo pacman -R hyperflux-next-full hyperflux-next-linux
```

If you changed `/etc/hyperflux-next/bridge.json`, Pacman keeps your copy as
`/etc/hyperflux-next/bridge.json.pacsave`. To remove it too, use
`sudo pacman -Rn` instead of `sudo pacman -R`.

Stock OpenRGB, OpenRazer, and Polychromatic are not removed. They belong to
their own packages.

## Remove The HyperFlux Repository

Do this only after you have removed the packages. Delete the
`Include = /etc/pacman.d/hyperflux-next.conf` line from `/etc/pacman.conf`,
then remove the repository file and the HyperFlux key:

```sh
sudo rm /etc/pacman.d/hyperflux-next.conf
sudo pacman-key --delete 8BF8A266E756BD1C24C930918B3F3850007159E8
```
