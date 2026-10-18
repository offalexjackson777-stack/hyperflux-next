# Install HyperFlux Next on Debian

This guide installs HyperFlux Next on Debian, so your Razer keyboard and mouse
on a HyperFlux receiver work in OpenRGB and in OpenRazer apps such as
Polychromatic.
The guide uses the signed HyperFlux package repository, so later updates arrive
with your normal system updates.

## What You Need

- Debian 14 (forky) on an x86_64 computer. Debian 14 is still Debian's testing
  release, and Debian 13 is not supported; see the
  [known limitations](limitations.md#only-the-newest-ubuntu-and-debian).
- The HyperFlux receiver, plugged in, with your keyboard and mouse paired to it.
- A user account that can run `sudo`.

HyperFlux Next comes in two editions:

- **Full (`hyperflux-next-full`), recommended:** your keyboard and mouse work in
  OpenRGB and in OpenRazer apps such as Polychromatic. Full installs Core for
  you.
- **Core (`hyperflux-next-linux`):** only the receiver driver, the background
  service, the `hyperfluxctl` tool, and the SDK, without the app connections.

If you are not sure, choose Full. Full works with Debian's own `openrgb`
package and with the stock OpenRazer service, which Debian packages as
`openrazer-daemon`. The apps themselves are separate;
[Core and Full on
Debian](../generated/distributions.md#core-and-full-on-debian) shows where they
come from.

## 1. Add The Repository And Install

<!-- HFX-GENERATED-BEGIN: debian-repository-setup -->
> [!NOTE]
> The Debian 14 packages are built and tested automatically, but not yet
> confirmed on real hardware. Debian 14 is still Debian's testing release,
> so Debian's own packages can change before it is released.

You do steps 1 to 3 once. The key's fingerprint is
`8BF8 A266 E756 BD1C 24C9 3091 8B3F 3850 0071 59E8`.

1. **Download the HyperFlux key.** The key proves that the packages really
   come from HyperFlux. If `curl` is missing, install it first with
   `sudo apt install curl`.

   ```bash
   sudo install -d -m 0755 /etc/apt/keyrings
   sudo curl --proto '=https' --tlsv1.2 --fail --location \
     --output /etc/apt/keyrings/hyperflux-next.asc -- \
     https://offalexjackson777-stack.github.io/hyperflux-next/hyperflux-next-repository-key.asc
   ```

2. **Check the key.**

   ```bash
   gpg --show-keys --with-subkey-fingerprints /etc/apt/keyrings/hyperflux-next.asc
   ```

   You should see exactly these two fingerprints. If either one is different,
   stop here and delete the file with `sudo rm /etc/apt/keyrings/hyperflux-next.asc`.

   - Main key: `8BF8A266E756BD1C24C930918B3F3850007159E8`
   - Signing subkey: `4C64CE4C855BC6844B4C1670C67DD9C42253ED27`

3. **Add the repository.** This saves one small file that tells apt where
   HyperFlux is and that only this key may sign it.

   ```bash
   sudo curl --proto '=https' --tlsv1.2 --fail --location \
     --output /etc/apt/sources.list.d/hyperflux-next.sources -- \
     https://offalexjackson777-stack.github.io/hyperflux-next/debian/hyperflux-next.sources
   ```

   The file holds these lines:

   ```text
   Types: deb
   URIs: https://offalexjackson777-stack.github.io/hyperflux-next/debian
   Suites: forky
   Components: main
   Architectures: amd64
   Signed-By: /etc/apt/keyrings/hyperflux-next.asc
   ```

4. **Install HyperFlux Next.** This installs Full:

   ```bash
   sudo apt update
   sudo apt install hyperflux-next-full
   ```

   To install Core instead, run `sudo apt install hyperflux-next-linux`.

   apt lists what it is about to install and asks
   `Do you want to continue? [Y/n]`. Press Enter. If Debian's own
   `openrazer-driver-dkms` package is installed, apt removes it. Full brings
   its own copy of that driver, so OpenRazer keeps working.

This is a signed repository run by the HyperFlux project, not an official
Debian repository.
<!-- HFX-GENERATED-END: debian-repository-setup -->

## 2. Turn On HyperFlux And Restart

Installing the package does not start anything. Turn on the HyperFlux
service and your desktop connection to it, then restart.

```bash
sudo systemctl enable hyperflux-next-bridge.service \
  "hyperflux-next-client@$(id -u).socket"
sudo reboot
```

`systemctl` prints one `Created symlink` line for each item it turns on.

> [!WARNING]
> Keep the receiver, keyboard, and mouse connected while you install. Do not
> unplug your only keyboard or mouse to finish the installation.

If Secure Boot is on, the driver only loads after you trust the DKMS key once.
[Secure Boot on Debian](../generated/distributions.md#secure-boot-on-debian) shows how.

## 3. Check That It Works

After you log in again, run:

```bash
hyperfluxctl doctor
```

When everything works, it prints:

```text
HyperFlux Next is ready.
Driver active or idle | Bridge running | 1 receiver generation(s)
No action required.
```

The second line shows the receiver driver, the HyperFlux service (`Bridge`),
and how many times the service has found your receiver since it started. With
Core, a third line reads `Optional Full integration is not installed.` That is
expected: it only means the app connections are not installed.

If you see `HyperFlux Next needs attention.` instead, do what the `Do:` line
says, then run `hyperfluxctl doctor` again. The
[troubleshooting guide](troubleshooting.md) covers the common cases.

Next, the [first-run guide](first-run.md) explains what `hyperfluxctl status`
shows, and the [known limitations](limitations.md) list what does not work yet.

## Updates

HyperFlux updates arrive with your normal system update:

```bash
sudo apt update && sudo apt upgrade
```

To remove HyperFlux, turn its service off and remove its packages. apt
skips the Full packages if you installed Core:

```bash
sudo systemctl disable --now hyperflux-next-bridge.service \
  "hyperflux-next-client@$(id -u).socket"
sudo apt remove hyperflux-next-full hyperflux-next-openrazer-driver-dkms hyperflux-next-linux hyperflux-next-dkms
```
