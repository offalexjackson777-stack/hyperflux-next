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

Download the public key from the fixed machine channel and inspect it locally:

```bash
curl --proto '=https' --tlsv1.2 --fail --location --output \
  ./hyperflux-next-arch-repository-key.asc -- \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/arch-repository/hyperflux-next-arch-repository-key.asc
gpg --show-keys --with-fingerprint --with-subkey-fingerprint \
  ./hyperflux-next-arch-repository-key.asc
```

Before trusting the key, require the displayed primary fingerprint to match in
full:

```text
8BF8 A266 E756 BD1C 24C9 3091 8B3F 3850 0071 59E8
```

Import and locally trust only that verified key. Create the exact repository
block locally; do not install a downloaded configuration file:

```bash
sudo pacman-key --add ./hyperflux-next-arch-repository-key.asc
sudo pacman-key --lsign-key 8BF8A266E756BD1C24C930918B3F3850007159E8
sudo install -Dm0644 /dev/stdin /etc/pacman.d/hyperflux-next.conf <<'EOF'
[hyperflux-next]
SigLevel = Required DatabaseRequired TrustedOnly
Server = https://github.com/offalexjackson777-stack/hyperflux-next/releases/download/arch-repository
EOF
```

Add this line once to `/etc/pacman.conf`, outside every existing repository
section:

```ini
Include = /etc/pacman.d/hyperflux-next.conf
```

Choose one installation after the one-time setup. Full is recommended and
selects the exact Core dependency automatically:

```bash
# Recommended desktop installation: Full plus exact Core.
sudo pacman -Syu hyperflux-next-full

# Core only.
sudo pacman -Syu hyperflux-next-linux
```

Thereafter, ordinary `sudo pacman -Syu` runs discover HyperFlux updates.

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
