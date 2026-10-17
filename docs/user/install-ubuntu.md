# Install HyperFlux Next on Ubuntu

This guide installs HyperFlux Next on Ubuntu, so your Razer keyboard and mouse
on a HyperFlux receiver work in OpenRazer apps such as Polychromatic. OpenRGB
cannot reach them on Ubuntu yet, because Ubuntu does not package an OpenRGB
that can load the HyperFlux plugin; the [known
limitations](limitations.md#no-openrgb-plugin-outside-arch-linux) explain why.
The guide uses the signed HyperFlux package repository, so later updates arrive
with your normal system updates.

## What You Need

- Ubuntu 26.10 (stonking) on an x86_64 computer. Ubuntu 26.04 LTS is not
  supported, because its OpenRazer is too old for some keyboards; see the
  [known limitations](limitations.md#only-the-newest-ubuntu-and-debian).
- The HyperFlux receiver, plugged in, with your keyboard and mouse paired to it.
- A user account that can run `sudo`.

HyperFlux Next comes in two editions:

- **Full (`hyperflux-next-full`), recommended:** your keyboard and mouse work in
  OpenRazer apps such as Polychromatic. Full installs Core for you.
- **Core (`hyperflux-next-linux`):** only the receiver driver, the background
  service, the `hyperfluxctl` tool, and the SDK, without the app connections.

If you are not sure, choose Full. Full works with the stock OpenRazer service,
which Ubuntu packages as `openrazer-daemon`. The apps themselves are separate;
[Core and Full on
Ubuntu](../generated/distributions.md#core-and-full-on-ubuntu) shows where they
come from.

## 1. Add The Repository And Install

<!-- HFX-GENERATED-BEGIN: ubuntu-repository-setup -->
> [!NOTE]
> The Ubuntu 26.10 packages are not published yet. These steps show how
> installing will work once they are. Until then the repository address does
> not answer, and the key download in step 1 stops with an error.

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
     https://offalexjackson777-stack.github.io/hyperflux-next/ubuntu/hyperflux-next.sources
   ```

   The file holds these lines:

   ```text
   Types: deb
   URIs: https://offalexjackson777-stack.github.io/hyperflux-next/ubuntu
   Suites: stonking
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
   `Do you want to continue? [Y/n]`. Press Enter. If Ubuntu's own
   `openrazer-driver-dkms` package is installed, apt removes it. Full brings
   its own copy of that driver, so OpenRazer keeps working.

This is a signed repository run by the HyperFlux project, not an official
Ubuntu repository.
<!-- HFX-GENERATED-END: ubuntu-repository-setup -->

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
[Secure Boot on Ubuntu](../generated/distributions.md#secure-boot-on-ubuntu) shows how.

## 3. Check That It Works

After you log in again, run:

```bash
hyperfluxctl doctor
```

When everything works, it prints:

```text
HyperFlux Next is ready.
Driver active or idle | Bridge running | 1 receiver generation(s)
Full integration has no OpenRGB plugin here: this distribution does not ship an OpenRGB that can load it.
No action required.
```

The second line shows the receiver driver, the HyperFlux service (`Bridge`),
and how many times the service has found your receiver since it started. The
third line is expected on Ubuntu: OpenRazer apps work, only the OpenRGB plugin
is missing. With Core, that line reads
`Optional Full integration is not installed.` instead. That is expected too:
it only means the app connections are not installed.

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
