# Install HyperFlux Next on openSUSE

This guide installs HyperFlux Next Core on openSUSE. Core adds the receiver
driver, the HyperFlux service, the `hyperfluxctl` tool, and the SDK. OpenRGB
and Polychromatic cannot change the lighting of devices on the receiver yet on
openSUSE; that needs Full, which is available on Arch Linux only. The guide uses
the signed HyperFlux package repository, so later updates arrive with your
normal system updates.

## What You Need

- openSUSE Tumbleweed on an x86_64 computer.
- The HyperFlux receiver, plugged in, with your keyboard and mouse paired to it.
- A user account that can run `sudo`.

On openSUSE there is one package, `hyperflux-next-linux`. It installs the
receiver driver, the background service, the `hyperfluxctl` tool, and the SDK.
It brings the driver sources, `hyperflux-next-dkms`, with it. The app
connections in the Arch Full package are not available on openSUSE yet.

## 1. Add The Repository And Install

<!-- HFX-GENERATED-BEGIN: opensuse-repository-setup -->
> [!NOTE]
> The openSUSE Tumbleweed packages are not published yet. These steps show how
> installing will work once they are. Until then the repository address does
> not answer, and the key download in step 1 stops with an error.

You do steps 1 to 3 once. The key's fingerprint is
`8BF8 A266 E756 BD1C 24C9 3091 8B3F 3850 0071 59E8`.

1. **Download the HyperFlux key.** The key proves that the packages really
   come from HyperFlux.

   ```bash
   curl --proto '=https' --tlsv1.2 --fail --location --remote-name -- \
     https://offalexjackson777-stack.github.io/hyperflux-next/hyperflux-next-repository-key.asc
   ```

2. **Check the key, then trust it.**

   ```bash
   gpg --show-keys --with-subkey-fingerprints hyperflux-next-repository-key.asc
   ```

   You should see exactly these two fingerprints. If either one is different,
   stop here and delete the file with `rm hyperflux-next-repository-key.asc`.

   - Main key: `8BF8A266E756BD1C24C930918B3F3850007159E8`
   - Signing subkey: `4C64CE4C855BC6844B4C1670C67DD9C42253ED27`

   If both match, give the key to the package manager:

   ```bash
   sudo rpm --import hyperflux-next-repository-key.asc
   ```

3. **Add the repository.** This tells zypper where HyperFlux is and to check
   the signature on every package and on the package list.

   ```bash
   sudo zypper addrepo https://offalexjackson777-stack.github.io/hyperflux-next/opensuse/hyperflux-next.repo
   sudo zypper refresh
   ```

   The repository file holds these lines:

   ```ini
   [hyperflux-next]
   name=HyperFlux Next for openSUSE
   baseurl=https://offalexjackson777-stack.github.io/hyperflux-next/opensuse/tumbleweed/$basearch/
   type=rpm-md
   enabled=1
   autorefresh=1
   gpgcheck=1
   repo_gpgcheck=1
   gpgkey=https://offalexjackson777-stack.github.io/hyperflux-next/hyperflux-next-repository-key.asc
   ```

4. **Install HyperFlux Next.**

   ```bash
   sudo zypper install hyperflux-next-linux
   ```

   zypper lists what it is about to install and asks `Continue? [y/n/v/...? shows all options] (y)`.
   Press Enter.

This is a signed repository run by the HyperFlux project, not an official
openSUSE repository.
<!-- HFX-GENERATED-END: opensuse-repository-setup -->

## 2. Turn On HyperFlux And Restart

Installing the package does not start anything. openSUSE also ships the DKMS
startup service turned off. Turn on DKMS, the HyperFlux service, and your
desktop connection to it, then restart:

```bash
sudo systemctl enable dkms.service hyperflux-next-bridge.service \
  "hyperflux-next-client@$(id -u).socket"
sudo reboot
```

DKMS builds the HyperFlux driver again after each kernel update, so it has to
run at startup.

`systemctl` prints one `Created symlink` line for each item it turns on.

> [!WARNING]
> Keep the receiver, keyboard, and mouse connected while you install. Do not
> unplug your only keyboard or mouse to finish the installation.

If Secure Boot is on, the driver only loads after you trust the DKMS key once.
[Secure Boot on openSUSE](../generated/distributions.md#secure-boot-on-opensuse) shows how.

## 3. Check That It Works

After you log in again, run:

```bash
hyperfluxctl doctor
```

When everything works, it prints:

```text
HyperFlux Next is ready.
Driver active or idle | Bridge running | 1 receiver generation(s)
Optional Full integration is not installed.
No action required.
```

The second line shows the receiver driver, the HyperFlux service (`Bridge`),
and how many times the service has found your receiver since it started. The
line about Full is expected on openSUSE: Full, the app connections, is
available on Arch Linux only.

If you see `HyperFlux Next needs attention.` instead, do what the `Do:` line
says, then run `hyperfluxctl doctor` again. The
[troubleshooting guide](troubleshooting.md) covers the common cases.

Next, the [first-run guide](first-run.md) explains what `hyperfluxctl status`
shows, and the [known limitations](limitations.md) list what does not work yet.

## Updates

HyperFlux updates arrive with your normal system update:

```bash
sudo zypper dup
```

To remove HyperFlux, turn its service off and remove both packages:

```bash
sudo systemctl disable --now hyperflux-next-bridge.service \
  "hyperflux-next-client@$(id -u).socket"
sudo zypper remove hyperflux-next-linux hyperflux-next-dkms
```
