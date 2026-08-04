# HyperFlux Next

HyperFlux Next 2.0 makes supported Razer devices paired through HyperFlux V2
available to ordinary Linux applications without replacing those applications.

[![Verification](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/verification.yml/badge.svg)](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/verification.yml)
[![CodeQL](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/codeql.yml/badge.svg)](https://github.com/offalexjackson777-stack/hyperflux-next/actions/workflows/codeql.yml)
[![License: GPL-2.0-only](docs/assets/badge-license.svg)](LICENSE)

<!-- HFX-GENERATED-BEGIN: release-status -->
> [!IMPORTANT]
> **Arch 2.0.0 release.** Arch Linux x86_64 Core and Full 2.0.0-1 packages are published in the human-facing GitHub Release v2.0.0. The signed HyperFlux Next third-party Pacman repository is served separately through the fixed `arch-repository` machine channel.
<!-- HFX-GENERATED-END: release-status -->

## What It Does

- **Discovers paired children naturally.** Capability-based routing needs no
  serial, USB-port, or machine-specific binding.
- **Preserves stock applications.** OpenRazer, Polychromatic, and OpenRGB keep
  their public APIs, controls, and user experience.
- **Fails closed.** Stale, unknown, or unavailable routes stay non-writable and
  `hyperfluxctl doctor` reports the first missing boundary.

## Packages

<!-- HFX-GENERATED-BEGIN: recommended-package -->
**Core (`hyperflux-next-linux`):** HyperFlux receiver driver, bridge, SDK, CLI, and base services.

**Full (`hyperflux-next-full`):** Core plus the OpenRazer compatibility transport and OpenRGB integration for optional stock applications.

The Arch Linux x86_64 packages are published in
[GitHub Release v2.0.0](https://github.com/offalexjackson777-stack/hyperflux-next/releases/tag/v2.0.0).
The signed project-operated Pacman repository is a separate machine-managed
channel, not an official Arch Linux repository.

### 1. Add the HyperFlux repository - one time only

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

### 2. Install Full - recommended

```bash
sudo pacman -Syu hyperflux-next-full
```

Full resolves its exact Core dependency automatically. Core alone is an
advanced alternative:

```bash
sudo pacman -Syu hyperflux-next-linux
```

### Security details

The setup block pins the complete primary and signing-subkey fingerprints,
locally writes `Required DatabaseRequired TrustedOnly`, and adds one idempotent
Pacman include. It never downloads a configuration or executes a remote script.
Users never manually select machine-repository assets. See
[installation](docs/user/installation.md) for details and the local-file fallback.
<!-- HFX-GENERATED-END: recommended-package -->

## Hardware And Applications

<!-- HFX-GENERATED-BEGIN: compatibility-summary -->
- Stock OpenRazer `3.12.4` userspace,
  stock Polychromatic, and stock OpenRGB remain application owners.
- 2 devices are physically proven,
  8 are source-qualified operational
  candidates, and 2 are blocked or
  research-only. The physical fixtures are not an allowlist.
- Restoration ships disabled.
<!-- HFX-GENERATED-END: compatibility-summary -->

See the [hardware tiers](docs/user/hardware.md) and
[application guide](docs/user/applications.md) for the exact support boundary.

## Start Here

Use the [documentation hub](docs/README.md), [installation guide](docs/user/installation.md),
[first-run guide](docs/user/first-run.md), and [troubleshooting guide](docs/user/troubleshooting.md).
Project links: [Support](SUPPORT.md), [Contributing](CONTRIBUTING.md),
[Journey](docs/project/journey.md), [Security](SECURITY.md), and [License](LICENSE).
