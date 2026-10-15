# SPDX-License-Identifier: GPL-2.0-only
"""Generated steps of the user installation guide.

Every URL, fingerprint, and Pacman line comes from the Arch repository policy,
so the guide cannot drift from the repository it describes.
"""

from __future__ import annotations

from ..arch_repository import (
    ArchRepositoryPolicy,
    repository_configuration,
    repository_setup_script,
)


def _grouped(fingerprint: str) -> str:
    return " ".join(
        fingerprint[index : index + 4] for index in range(0, len(fingerprint), 4)
    )


def repository_setup(policy: ArchRepositoryPolicy) -> str:
    """The one-time repository setup: the checked script and a manual route."""
    key_url = f"{policy.server_url}/{policy.public_key_asset}"
    script = repository_setup_script(policy).rstrip()
    configuration = repository_configuration(policy).rstrip()
    return f"""This step tells Pacman where to find HyperFlux and which key signs its packages.
The command below downloads the HyperFlux key, checks its fingerprint, and then
adds the key and the repository to Pacman. If the fingerprint does not match, or
Pacman already has a HyperFlux entry, it stops without changing anything.

The key's fingerprint is `{_grouped(policy.primary_fingerprint)}`.

<details>
<summary>Show the setup command</summary>

Copy the whole block into a terminal. It asks for your password because it uses
`sudo`.

```bash
{script}
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
   curl --proto '=https' --tlsv1.2 --fail --location --remote-name -- \\
     {key_url}
   ```

2. Show its fingerprints:

   ```bash
   gpg --show-keys --with-subkey-fingerprints {policy.public_key_asset}
   ```

   You should see exactly these two fingerprints. If either one is different,
   stop here and do not continue.

   - Main key: `{policy.primary_fingerprint}`
   - Signing subkey: `{policy.signing_subkey_fingerprint}`

3. Add the key to Pacman and trust it:

   ```bash
   sudo pacman-key --add {policy.public_key_asset}
   sudo pacman-key --lsign-key {policy.primary_fingerprint}
   ```

4. Open `/etc/pacman.conf` as root, for example with
   `sudoedit /etc/pacman.conf`, and add these lines at the end:

   ```ini
{_indent(configuration, "   ")}
   ```

</details>"""


def package_fallback(policy: ArchRepositoryPolicy) -> str:
    """Install the release's package files directly, without the repository."""
    release_tag = policy.product_release_tag
    download = policy.product_release_url.replace(
        f"/releases/tag/{release_tag}", f"/releases/download/{release_tag}"
    )
    by_name = {package.name: package for package in policy.packages}
    core = by_name["hyperflux-next-linux"].filename
    full = by_name["hyperflux-next-full"].filename
    return f"""This downloads the Core and Full package files from the release page,
[{release_tag}]({policy.product_release_url}),
checks them against the release's checksum list, and installs both in one step.
Full needs Core, and without the repository Pacman cannot fetch Core by itself.

```bash
curl --proto '=https' --tlsv1.2 --fail --location --remote-name-all -- \\
  {download}/{core} \\
  {download}/{full} \\
  {download}/SHA256SUMS
awk '$2 == "{core}" || \\
     $2 == "{full}"' \\
  SHA256SUMS > SHA256SUMS-packages
test "$(wc -l < SHA256SUMS-packages)" -eq 2
sha256sum -c SHA256SUMS-packages
sudo pacman -U -- \\
  ./{core} \\
  ./{full}
```

`sha256sum` should print `OK` for both files. If it prints `FAILED`, delete the
files and download them again."""


def _indent(text: str, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" if line else line for line in text.splitlines())
