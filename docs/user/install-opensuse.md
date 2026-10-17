# Install HyperFlux Next on openSUSE

This guide installs HyperFlux Next on openSUSE, so your Razer keyboard and
mouse on a HyperFlux receiver work in OpenRazer apps such as Polychromatic.
OpenRGB cannot reach them on openSUSE yet, because openSUSE does not package an
OpenRGB that can load the HyperFlux plugin; the [known
limitations](limitations.md#no-openrgb-plugin-on-ubuntu-fedora-and-opensuse) explain why.
The guide uses the signed HyperFlux package repository, so later updates arrive
with your normal system updates.

## What You Need

- openSUSE Tumbleweed on an x86_64 computer.
- The HyperFlux receiver, plugged in, with your keyboard and mouse paired to it.
- A user account that can run `sudo`.

HyperFlux Next comes in two editions:

- **Full (`hyperflux-next-full`), recommended:** your keyboard and mouse work in
  OpenRazer apps such as Polychromatic. Full installs Core for you.
- **Core (`hyperflux-next-linux`):** only the receiver driver, the background
  service, the `hyperfluxctl` tool, and the SDK, without the app connections.

If you are not sure, choose Full. Full works with the stock OpenRazer service,
which OpenRazer's own repository on the openSUSE Build Service provides,
because openSUSE does not package OpenRazer. The apps themselves are separate;
[Core and Full on
openSUSE](../generated/distributions.md#core-and-full-on-opensuse) shows where
they come from.

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

4. **Install HyperFlux Next.** This installs Full:

   ```bash
   sudo zypper install hyperflux-next-full
   ```

   To install Core instead, run `sudo zypper install hyperflux-next-linux`.

   zypper lists what it is about to install and asks `Continue? [y/n/v/...? shows all options] (y)`.
   Press Enter. If OpenRazer's own `openrazer-kernel-modules-dkms` package is
   installed, zypper reports a conflict and offers solutions. Choose the one
   that deinstalls `openrazer-kernel-modules-dkms`. Full brings its own copy
   of that driver, so OpenRazer keeps working.

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
Full integration has no OpenRGB plugin here: this distribution does not ship an OpenRGB that can load it.
No action required.
```

The second line shows the receiver driver, the HyperFlux service (`Bridge`),
and how many times the service has found your receiver since it started. The
third line is expected on openSUSE: OpenRazer apps work, only the OpenRGB plugin
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
sudo zypper dup
```

To remove HyperFlux, turn its service off and remove its packages. With
Full:

```bash
sudo systemctl disable --now hyperflux-next-bridge.service \
  "hyperflux-next-client@$(id -u).socket"
sudo zypper remove hyperflux-next-full hyperflux-next-openrazer-driver-dkms hyperflux-next-linux hyperflux-next-dkms
```

With Core, the last command is
`sudo zypper remove hyperflux-next-linux hyperflux-next-dkms`.
