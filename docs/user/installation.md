# Installation

HyperFlux Next has exactly two user-facing packages.

- **Core (`hyperflux-next-linux`):** HyperFlux receiver driver, bridge, SDK, CLI, and base services.
- **Full (`hyperflux-next-full`):** Core plus the OpenRazer compatibility transport and OpenRGB integration for optional stock applications.

Full is the ordinary desktop choice. Core is useful when only the receiver
transport and SDK are needed. Stock OpenRazer userspace, Polychromatic, and
OpenRGB remain separate, optional distribution packages.

## Availability

Arch Linux x86_64 Core and Full packages are published in
[GitHub Release v2.0.0](https://github.com/offalexjackson777-stack/hyperflux-next/releases/tag/v2.0.0).
The HyperFlux Next project operates a signed third-party Pacman repository. It
is not an official Arch Linux repository. Pacman's machine-consumed files live
in a separate fixed `arch-repository` channel so the v2.0.0 release remains a
clear, human-facing product page. Users do not manually download or choose the
repository databases and signatures.

## 1. Add the HyperFlux repository - one time only

```bash
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
```

## 2. Install Full - recommended

```bash
sudo pacman -Syu hyperflux-next-full
```

Full resolves the exact Core package automatically. For an advanced Core-only
installation:

```bash
sudo pacman -Syu hyperflux-next-linux
```

Thereafter, ordinary `sudo pacman -Syu` runs discover HyperFlux updates.

## Security details

The one-time block downloads only the public key. It requires exactly the full
primary fingerprint `8BF8 A266 E756 BD1C 24C9 3091 8B3F 3850 0071 59E8` and
signing subkey before changing trust or configuration. It writes the strict
`Required DatabaseRequired TrustedOnly` repository locally and adds one
idempotent include without replacing existing Pacman settings. It does not
execute a remote script or install a downloaded configuration file.

Pacman consumes the separate machine channel automatically. Users never
manually download its databases, signatures, manifest, or checksum receipt.

## Local package fallback

The human-facing v2.0.0 release retains a local-file fallback. Download its two
packages and checksum receipt, select exactly those two receipt entries, then
verify and install them together:

```bash
curl --proto '=https' --tlsv1.2 --fail --location --remote-name-all -- \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/v2.0.0/hyperflux-next-linux-2.0.0-1-x86_64.pkg.tar.zst \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/v2.0.0/hyperflux-next-full-2.0.0-1-x86_64.pkg.tar.zst \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/v2.0.0/SHA256SUMS
awk '$2 == "hyperflux-next-linux-2.0.0-1-x86_64.pkg.tar.zst" || \
     $2 == "hyperflux-next-full-2.0.0-1-x86_64.pkg.tar.zst"' \
  SHA256SUMS > SHA256SUMS-packages
test "$(wc -l < SHA256SUMS-packages)" -eq 2
sha256sum -c SHA256SUMS-packages
sudo pacman -U -- \
  ./hyperflux-next-linux-2.0.0-1-x86_64.pkg.tar.zst \
  ./hyperflux-next-full-2.0.0-1-x86_64.pkg.tar.zst
```

The machine channel carries detached signatures for both package archives and
the repository database/files metadata, plus the public key and full
fingerprint receipt. The private signing key is never published.

## Safe Desktop Lifecycle

The normal receiver-only desktop path is:

1. Keep the receiver and devices connected.
2. Install Core and Full together with the command above.
3. Enable the bridge and per-user socket services:
   `sudo systemctl enable hyperflux-next-bridge.service "hyperflux-next-client@$(id -u).socket"`.
4. Reboot normally.
5. Run `hyperfluxctl status` and `hyperfluxctl doctor` after login.

Package registration does not start services, change hardware bindings, or
authorize a device write. Restoration ships disabled and remains independent
of normal device discovery.

> [!WARNING]
> Do not unplug the only keyboard or mouse to complete a normal installation.
> Disconnected activation is an advanced recovery path and requires a built-in
> keyboard and touchpad, a second input pair, or a working SSH session.

The generated [distribution contract](../generated/distributions.md) and
[installation contract](../generated/installation.md) describe exact package
ownership and lifecycle behavior for maintainers.
