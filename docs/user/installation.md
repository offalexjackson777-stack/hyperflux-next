# Installation

HyperFlux Next has two packages. Choose one.

- **Full (`hyperflux-next-full`):** Core plus the OpenRazer compatibility
  transport and OpenRGB integration for optional stock applications.
- **Core (`hyperflux-next-linux`):** HyperFlux receiver driver, bridge, SDK,
  CLI, and base services.

Full is the normal desktop choice and pulls in Core by itself. Choose Core alone
only when you need the receiver transport and SDK without application
integration. Stock OpenRazer, Polychromatic, and OpenRGB are separate packages
that you install yourself if you want them; HyperFlux does not install
or replace them.

## Availability

Arch Linux x86_64 packages are published in
[GitHub Release v2.0.1](https://github.com/offalexjackson777-stack/hyperflux-next/releases/tag/v2.0.1).
Pacman installs and updates them from the signed HyperFlux Next repository. It
is a signed project-operated Arch repository, not an official Arch Linux
repository. Pacman reads its database and signatures from a fixed
`arch-repository` release; you never download those files yourself.

## 1. Add the HyperFlux repository - one time only

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

## 2. Install Full - recommended

```bash
sudo pacman -Syu hyperflux-next-full
```

Full pulls in the matching Core package. For an advanced Core-only
installation, run this instead:

```bash
sudo pacman -Syu hyperflux-next-linux
```

After this, your normal `sudo pacman -Syu` also installs HyperFlux updates.

## Security details

The one-time block downloads only the public key. It requires exactly the full
primary fingerprint `8BF8 A266 E756 BD1C 24C9 3091 8B3F 3850 0071 59E8` and
signing subkey before changing trust or configuration. It writes the strict
`Required DatabaseRequired TrustedOnly` repository locally and adds one
idempotent include without replacing existing Pacman settings. It does not
execute a remote script or install a downloaded configuration file.


## 3. Enable the services and reboot

Keep the receiver and its keyboard and mouse connected. Installing the
packages does not start any service, so enable the bridge and your desktop
socket, then reboot normally:

```bash
sudo systemctl enable hyperflux-next-bridge.service \
  "hyperflux-next-client@$(id -u).socket"
sudo reboot
```

## 4. Check the result

After you log in, run:

```bash
hyperfluxctl status
hyperfluxctl doctor
```

Doctor prints `HyperFlux Next is ready.` when everything works. Otherwise it
names the first problem and one thing to do about it. Continue with the
[first-run guide](first-run.md) to open your applications.

> [!WARNING]
> Do not unplug the only keyboard or mouse to complete a normal installation.
> Disconnected activation is an advanced recovery path and requires a built-in
> keyboard and touchpad, a second input pair, or a working SSH session.

## Local package fallback

Use this only if you cannot add the repository. Without it, Pacman cannot
fetch Core for Full, so this fallback downloads both package files from the
v2.0.1 release and installs them in one transaction. Download the two packages
and the checksum receipt, keep exactly those two receipt entries, then verify
and install:

```bash
curl --proto '=https' --tlsv1.2 --fail --location --remote-name-all -- \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/v2.0.1/hyperflux-next-linux-2.0.1-1-x86_64.pkg.tar.zst \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/v2.0.1/hyperflux-next-full-2.0.1-1-x86_64.pkg.tar.zst \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/v2.0.1/SHA256SUMS
awk '$2 == "hyperflux-next-linux-2.0.1-1-x86_64.pkg.tar.zst" || \
     $2 == "hyperflux-next-full-2.0.1-1-x86_64.pkg.tar.zst"' \
  SHA256SUMS > SHA256SUMS-packages
test "$(wc -l < SHA256SUMS-packages)" -eq 2
sha256sum -c SHA256SUMS-packages
sudo pacman -U -- \
  ./hyperflux-next-linux-2.0.1-1-x86_64.pkg.tar.zst \
  ./hyperflux-next-full-2.0.1-1-x86_64.pkg.tar.zst
```

Packages installed this way do not update with `pacman -Syu`. Add the
repository later to receive updates. Then continue with step 3 above.

The release's `arch-repository` channel carries detached signatures for both
package archives and the repository database, plus the public key and its full
fingerprint receipt. The private signing key is never published.

## Package Details

The generated [distribution contract](../generated/distributions.md) and
[installation contract](../generated/installation.md) list exact package
contents and lifecycle behavior.
