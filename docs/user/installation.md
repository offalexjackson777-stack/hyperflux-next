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
is not an official Arch Linux repository.

Download the public key, its fingerprint receipt, and the repository block:

```bash
curl --proto '=https' --tlsv1.2 --fail --location --remote-name-all -- \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/latest/download/hyperflux-next-arch-repository-key.asc \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/latest/download/hyperflux-next-arch-repository-fingerprint.txt \
  https://github.com/offalexjackson777-stack/hyperflux-next/releases/latest/download/hyperflux-next.conf
gpg --show-keys --with-fingerprint --with-subkey-fingerprint \
  ./hyperflux-next-arch-repository-key.asc
cat ./hyperflux-next-arch-repository-fingerprint.txt
```

Before trusting the key, require the displayed primary fingerprint to match in
full:

```text
8BF8 A266 E756 BD1C 24C9 3091 8B3F 3850 0071 59E8
```

Import and locally trust only that verified key, then install the repository
block:

```bash
sudo pacman-key --add ./hyperflux-next-arch-repository-key.asc
sudo pacman-key --lsign-key 8BF8A266E756BD1C24C930918B3F3850007159E8
sudo install -Dm0644 ./hyperflux-next.conf \
  /etc/pacman.d/hyperflux-next.conf
```

Add this line once to `/etc/pacman.conf`, outside every existing repository
section:

```ini
Include = /etc/pacman.d/hyperflux-next.conf
```

Synchronize through a normal full system upgrade and install Full. Pacman
selects the exact Core dependency automatically:

```bash
sudo pacman -Syu hyperflux-next-full
```

After that first synchronized refresh, the ordinary package command is also
available and future `pacman -Syu` runs discover HyperFlux updates:

```bash
sudo pacman -S hyperflux-next-full
```

The release assets retain a local-file fallback. Download both packages and
`SHA256SUMS`, then verify and install them together:

```bash
sha256sum -c SHA256SUMS
sudo pacman -U -- \
  ./hyperflux-next-linux-2.0.0-1-x86_64.pkg.tar.zst \
  ./hyperflux-next-full-2.0.0-1-x86_64.pkg.tar.zst
```

Both package archives and the repository database/files metadata have detached
OpenPGP signatures. The private signing key is never published.

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
