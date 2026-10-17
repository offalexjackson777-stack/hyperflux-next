# Updates And Removal

These steps are for Arch Linux. On Ubuntu, Debian, Fedora, and openSUSE, the
update and removal steps are in your distribution's guide, linked from the
[install section of the README](../../README.md#install).

## Update HyperFlux

HyperFlux updates arrive with your normal system update.

1. Close OpenRGB and Polychromatic.
2. Update your system:

   ```sh
   sudo pacman -Syu
   ```

   If a HyperFlux update is available, Pacman lists the `hyperflux-next`
   packages among the packages to upgrade. Pacman checks their signatures
   against the HyperFlux key you added during installation.
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

Pacman keeps the packages you installed before in its cache. Every HyperFlux
package needs the other HyperFlux packages from exactly the same version, so
always install them together.

1. See which versions you have:

   ```sh
   ls /var/cache/pacman/pkg/hyperflux-next-*
   ```

2. Install all the earlier files from one version together, then reboot with
   the receiver connected. For a 2.1.0 or later version with Full, that is
   four files:

   ```sh
   sudo pacman -U \
     /var/cache/pacman/pkg/hyperflux-next-dkms-VERSION-x86_64.pkg.tar.zst \
     /var/cache/pacman/pkg/hyperflux-next-linux-VERSION-x86_64.pkg.tar.zst \
     /var/cache/pacman/pkg/hyperflux-next-openrazer-driver-dkms-VERSION-x86_64.pkg.tar.zst \
     /var/cache/pacman/pkg/hyperflux-next-full-VERSION-x86_64.pkg.tar.zst
   sudo reboot
   ```

   Replace `VERSION` with the same earlier version, for example `2.1.0-1`, in
   every line. If you use Core alone, install only the `hyperflux-next-dkms`
   and `hyperflux-next-linux` files.

   Going back to a 2.0.x version is different, because 2.0.x keeps its
   drivers inside its Core and Full packages. First remove the separate
   driver packages:

   ```sh
   sudo pacman -Rdd hyperflux-next-dkms hyperflux-next-openrazer-driver-dkms
   ```

   If you use Core alone, remove only `hyperflux-next-dkms`. Then straight
   away install the two 2.0.x files and reboot:

   ```sh
   sudo pacman -U \
     /var/cache/pacman/pkg/hyperflux-next-linux-VERSION-x86_64.pkg.tar.zst \
     /var/cache/pacman/pkg/hyperflux-next-full-VERSION-x86_64.pkg.tar.zst
   sudo reboot
   ```

   With Core alone, leave out the `hyperflux-next-full` line.

If the cache was cleaned, download all of that version's package files from
its GitHub release. Do not install Arch's own `openrazer-driver-dkms` package
while going back.

## Remove Full And Keep Core

1. Remove Full and the OpenRazer driver it brought in:

   ```sh
   sudo pacman -Rs hyperflux-next-full
   ```

   Core and its services keep working, but your apps can no longer reach
   devices paired to the receiver.

2. If you use OpenRazer or Polychromatic, put Arch's own OpenRazer driver back:

   ```sh
   sudo pacman -S openrazer-driver-dkms
   ```

   If Pacman says that it conflicts with `hyperflux-next-openrazer-driver-dkms`
   and asks whether to remove that package, type `y` and press Enter.

## Remove HyperFlux Next Completely

1. Turn off the HyperFlux services:

   ```sh
   sudo systemctl disable --now hyperflux-next-bridge.service \
     "hyperflux-next-client@$(id -u).socket"
   ```

2. Remove HyperFlux and the packages it brought in:

   ```sh
   sudo pacman -Rs hyperflux-next-full hyperflux-next-linux
   ```

   With Core alone, leave out `hyperflux-next-full`.

   If you changed `/etc/hyperflux-next/bridge.json`, Pacman keeps your copy as
   `/etc/hyperflux-next/bridge.json.pacsave`. To remove that copy too, use
   `sudo pacman -Rns` instead of `sudo pacman -Rs`.

3. If you use OpenRazer or Polychromatic, put Arch's own OpenRazer driver back as in
   step 2 of [Remove Full And Keep Core](#remove-full-and-keep-core).

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
