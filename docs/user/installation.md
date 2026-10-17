# Install HyperFlux Next On Arch Linux

This guide installs HyperFlux Next so your Razer keyboard and mouse on a
HyperFlux receiver work in OpenRazer, Polychromatic, and OpenRGB.

## What You Need

- Arch Linux on an x86_64 computer. For other distributions, use the
  [Ubuntu](install-ubuntu.md), [Debian](install-debian.md),
  [Fedora](install-fedora.md), or [openSUSE](install-opensuse.md) guide.
- The HyperFlux receiver, plugged in, with your keyboard and mouse paired to it.
- A user account that can run `sudo`.

## Choose A Package

HyperFlux Next comes in two editions. Choose one. Pacman then installs the
packages that edition needs.

- **Full (`hyperflux-next-full`), recommended:** your keyboard and mouse work in
  OpenRazer, Polychromatic, and OpenRGB. Full installs Core for you.
- **Core (`hyperflux-next-linux`):** only the receiver driver, the background
  service, the `hyperfluxctl` tool, and the SDK, without the app connections.

If you are not sure, choose Full. The apps themselves are separate. The
[first-run guide](first-run.md) shows how to install them.

## 1. Add The HyperFlux Repository (One Time Only)

<!-- HFX-GENERATED-BEGIN: repository-setup -->
This step tells Pacman where to find HyperFlux and which key signs its packages.
The command below downloads the HyperFlux key, checks its fingerprint, and then
adds the key and the repository to Pacman. If the fingerprint does not match, or
Pacman already has a HyperFlux entry, it stops without changing anything.

The key's fingerprint is `8BF8 A266 E756 BD1C 24C9 3091 8B3F 3850 0071 59E8`.

<details>
<summary>Show the setup command</summary>

Copy the whole block into a terminal. It asks for your password because it uses
`sudo`.

```bash
bash <<'HFX_REPOSITORY_SETUP'
set -euo pipefail

expected_primary='8BF8A266E756BD1C24C930918B3F3850007159E8'
expected_signing='4C64CE4C855BC6844B4C1670C67DD9C42253ED27'
key_url='https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/arch-repository/hyperflux-next-arch-repository-key.asc'
key_dir="$(mktemp -d)"
trap 'rm -rf "$key_dir"' EXIT
key_file="$key_dir/hyperflux-next-arch-repository-key.asc"

curl --disable --proto '=https' --tlsv1.2 --fail --location \
  --silent --show-error --output "$key_file" -- "$key_url"
mapfile -t fingerprints < <(
  gpg --batch --with-colons --show-keys "$key_file" | \
    awk -F: '$1 == "fpr" { print $10 }'
)
actual_primary="${fingerprints[0]-}"
actual_signing="${fingerprints[1]-}"
if [[ ${#fingerprints[@]} -ne 2 ||
      "$actual_primary" != "$expected_primary" ||
      "$actual_signing" != "$expected_signing" ]]; then
  printf 'HyperFlux repository key fingerprint mismatch; no trust or config changed.\n' >&2
  exit 1
fi

include_path='/etc/pacman.d/hyperflux-next.conf'
include_line='Include = /etc/pacman.d/hyperflux-next.conf'
include_count="$(sudo awk '
  /^[[:space:]]*Include[[:space:]]*=[[:space:]]*\/etc\/pacman\.d\/hyperflux-next\.conf[[:space:]]*$/ { count++ }
  END { print count + 0 }
' /etc/pacman.conf)"
section_count="$(sudo awk '
  /^[[:space:]]*\[hyperflux-next\][[:space:]]*$/ { count++ }
  END { print count + 0 }
' /etc/pacman.conf)"
if [[ "$include_count" -gt 1 || "$section_count" -gt 0 ]]; then
  printf 'Existing HyperFlux Pacman entries conflict; no trust or config changed.\n' >&2
  exit 1
fi

sudo pacman-key --add "$key_file"
sudo pacman-key --lsign-key "$expected_primary"
sudo install -d -m 0755 /etc/pacman.d
sudo install -m 0644 /dev/stdin "$include_path" <<'EOF'
[hyperflux-next]
SigLevel = Required DatabaseRequired TrustedOnly
Server = https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/arch-repository
EOF
if [[ "$include_count" -eq 0 ]]; then
  printf '\n%s\n' "$include_line" | sudo tee -a /etc/pacman.conf >/dev/null
fi
HFX_REPOSITORY_SETUP
```

Pacman prints a few lines while it adds and signs the key. The command also
writes `/etc/pacman.d/hyperflux-next.conf` and adds one `Include` line to the
end of `/etc/pacman.conf`. It downloads only the key and runs nothing else from
the internet.

</details>

<details>
<summary>Do the same steps by hand</summary>

1. Download the key:

   ```bash
   curl --proto '=https' --tlsv1.2 --fail --location --remote-name -- \
     https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/arch-repository/hyperflux-next-arch-repository-key.asc
   ```

2. Show its fingerprints:

   ```bash
   gpg --show-keys --with-subkey-fingerprints hyperflux-next-arch-repository-key.asc
   ```

   You should see exactly these two fingerprints. If either one is different,
   stop here and do not continue.

   - Main key: `8BF8A266E756BD1C24C930918B3F3850007159E8`
   - Signing subkey: `4C64CE4C855BC6844B4C1670C67DD9C42253ED27`

3. Add the key to Pacman and trust it:

   ```bash
   sudo pacman-key --add hyperflux-next-arch-repository-key.asc
   sudo pacman-key --lsign-key 8BF8A266E756BD1C24C930918B3F3850007159E8
   ```

4. Open `/etc/pacman.conf` as root, for example with
   `sudoedit /etc/pacman.conf`, and add these lines at the end:

   ```ini
   [hyperflux-next]
   SigLevel = Required DatabaseRequired TrustedOnly
   Server = https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/arch-repository
   ```

</details>
<!-- HFX-GENERATED-END: repository-setup -->

This is a signed project-operated Arch repository, not an official Arch Linux
repository. After this step, your normal `sudo pacman -Syu` also installs
HyperFlux updates.

## 2. Install The Package

Install Full:

```bash
sudo pacman -Syu hyperflux-next-full
```

To install Core instead, run `sudo pacman -Syu hyperflux-next-linux`.

Pacman lists the packages it is about to install and asks
`Proceed with installation? [Y/n]`. Press Enter to continue.

If Arch's own `openrazer-driver-dkms` package is already installed, Pacman says
it conflicts with `hyperflux-next-openrazer-driver-dkms` and asks whether to
remove it. Type `y`. Full brings its own copy of that driver, so OpenRazer
keeps working.

## 3. Turn On HyperFlux And Restart

Installing the package does not start anything. Turn on the HyperFlux service
and your desktop connection to it, then restart:

```bash
sudo systemctl enable hyperflux-next-bridge.service \
  "hyperflux-next-client@$(id -u).socket"
sudo reboot
```

`systemctl` prints one `Created symlink` line for each item it turns on.

> [!WARNING]
> Keep the receiver, keyboard, and mouse connected while you install. Do not
> unplug your only keyboard or mouse to finish the installation.

## 4. Check That It Works

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
Core, Doctor also prints `Optional Full integration is not installed.` before
the last line. That is expected: it only means the app connections are not
installed.

If you see `HyperFlux Next needs attention.` instead, do what the `Do:` line
says, then run `hyperfluxctl doctor` again. The
[troubleshooting guide](troubleshooting.md) covers the common cases.

Next, set up your apps with the [first-run guide](first-run.md).

## What Each Package Installs

Full installs four packages. Core installs the first two.

- `hyperflux-next-dkms`: the receiver driver.
- `hyperflux-next-linux` (Core): the HyperFlux service, the `hyperfluxctl`
  tool, and the SDK.
- `hyperflux-next-openrazer-driver-dkms`: a copy of OpenRazer's driver that can
  reach devices on the receiver.
- `hyperflux-next-full` (Full): the connections to OpenRazer and OpenRGB.

<details>
<summary>What Pacman shows for each package</summary>

- `hyperflux-next-dkms`: HyperFlux receiver kernel driver sources for DKMS.
- `hyperflux-next-linux`: HyperFlux receiver driver, bridge, SDK, CLI, and base
  services.
- `hyperflux-next-openrazer-driver-dkms`: OpenRazer kernel driver sources with
  the HyperFlux native transport, for DKMS.
- `hyperflux-next-full`: Core plus the OpenRazer compatibility transport and
  OpenRGB integration for optional stock applications.

</details>

The two `-dkms` packages hold kernel driver sources. DKMS builds them again for
each kernel you install, so a kernel update does not break the driver.
`hyperflux-next-openrazer-driver-dkms` takes the place of Arch's
`openrazer-driver-dkms`; if you have that package, Pacman asks to remove it.

HyperFlux does not install or replace OpenRazer's background service,
Polychromatic, or OpenRGB. The
[distribution contract](../generated/distributions.md) and
[installation contract](../generated/installation.md) list every file and
setting in detail.

## If You Cannot Add The Repository

Use this only if you do not want to add the HyperFlux repository and its key to
Pacman. Packages installed this way do not update with `sudo pacman -Syu`. Add
the repository later to get updates.

<!-- HFX-GENERATED-BEGIN: package-fallback -->
This downloads the four package files of Full from the release page,
[v2.1.1](https://github.com/offalexjackson777-stack/hyperflux-next/releases/tag/v2.1.1),
checks them against the release's checksum list, and installs them in one step.
Full needs the other packages, and without the repository Pacman cannot fetch
them by itself.

```bash
curl --proto '=https' --tlsv1.2 --fail --location --remote-name-all -- \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/v2.1.1/hyperflux-next-dkms-2.1.1-1-x86_64.pkg.tar.zst \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/v2.1.1/hyperflux-next-linux-2.1.1-1-x86_64.pkg.tar.zst \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/v2.1.1/hyperflux-next-openrazer-driver-dkms-2.1.1-1-x86_64.pkg.tar.zst \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/v2.1.1/hyperflux-next-full-2.1.1-1-x86_64.pkg.tar.zst \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/v2.1.1/SHA256SUMS
awk '$2 == "hyperflux-next-dkms-2.1.1-1-x86_64.pkg.tar.zst" || \
     $2 == "hyperflux-next-linux-2.1.1-1-x86_64.pkg.tar.zst" || \
     $2 == "hyperflux-next-openrazer-driver-dkms-2.1.1-1-x86_64.pkg.tar.zst" || \
     $2 == "hyperflux-next-full-2.1.1-1-x86_64.pkg.tar.zst"' \
  SHA256SUMS > SHA256SUMS-packages
test "$(wc -l < SHA256SUMS-packages)" -eq 4
sha256sum -c SHA256SUMS-packages
sudo pacman -U -- \
  ./hyperflux-next-dkms-2.1.1-1-x86_64.pkg.tar.zst \
  ./hyperflux-next-linux-2.1.1-1-x86_64.pkg.tar.zst \
  ./hyperflux-next-openrazer-driver-dkms-2.1.1-1-x86_64.pkg.tar.zst \
  ./hyperflux-next-full-2.1.1-1-x86_64.pkg.tar.zst
```

`sha256sum` should print `OK` for every file. If it prints `FAILED`, delete the
files and download them again.
<!-- HFX-GENERATED-END: package-fallback -->

Then continue with
[step 3](#3-turn-on-hyperflux-and-restart).
