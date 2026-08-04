# Building

Builds use the pinned toolchain and reviewed upstream sources recorded under
`toolchains/` and `integrations/`.

## Contributor Workflow

From the repository root:

```sh
./hfx generate
./hfx verify --changed-from <commit>
```

The first command refreshes derived files. The second selects the checks affected
by the change and includes their dependency closure. Individual Rust, Python,
and C++ tests can run directly while iterating.

## Maintainer Builds

The complete software graph runs in the pinned, network-disabled environment.
Release packages use independent A/B assemblies and require byte-identical
results. See [verification](verification.md) and the generated [distribution
contract](../generated/distributions.md) for those procedures.

## Signed Arch Repository

The release-side repository procedure wraps exact frozen package bytes; it does
not rebuild or rewrite them. First update `assurance/arch-repository.json` with
the reviewed release tag, filenames, hashes, public key, and full fingerprints.
Then use a fresh output directory and a private GnuPG home containing that key:

```sh
./hfx arch-repository build \
  --core /path/to/hyperflux-next-linux.pkg.tar.zst \
  --full /path/to/hyperflux-next-full.pkg.tar.zst \
  --gnupg-home "$PRIVATE_GNUPG_HOME" \
  --key-fingerprint 8BF8A266E756BD1C24C930918B3F3850007159E8 \
  --output /path/to/fresh-repository-assets
./hfx arch-repository verify \
  --repository /path/to/fresh-repository-assets
```

The builder rejects package-byte or metadata drift, requires the canonical
signing subkey, creates stable database/files aliases and their canonical
archives, signs every package and metadata artifact, embeds package signatures
in the database, and seals a checksum-bound manifest. Publish every generated
asset together at the stable release path. Keep the private key and its verified
backup outside the repository; publish only the tracked public key and full
fingerprint.

## Generated Files

Change canonical authorities rather than generated output. Hybrid documents are
the exception only inside declared generated-block markers; reviewed prose
outside those markers is protected.
