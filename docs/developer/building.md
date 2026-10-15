# Building

This page covers the build tasks in the order you are likely to need them: checking an
ordinary change, building the complete software graph, and publishing the signed Arch
repository. Every build uses the pinned toolchain and the reviewed upstream sources recorded
under `toolchains/` and `integrations/`, so two builds of the same commit see the same
inputs.

## Check An Ordinary Change

From the repository root:

```sh
./hfx generate
./hfx verify --fast --changed-from <commit>
```

`./hfx generate` refreshes derived files. `./hfx verify --fast --changed-from` runs the fast
lane for the checks affected by your change and everything they depend on. While you
iterate, you can run individual Rust, Python, and C++ tests directly.

## Change A Generated File

Do not edit generated output. Change the canonical source it is built from and run
`./hfx generate`. Hybrid documents are the one exception: inside their declared
generated-block markers the text is generated, and the reviewed prose outside those markers
is protected.

## Build The Complete Software Graph

Maintainers run the complete software graph, `./hfx verify --full`, in the pinned
environment with networking disabled. Release packages are assembled twice, independently,
as an A and a B build, and the two results must be byte-identical, which shows the build is
reproducible. See [Verification](verification.md) and the generated [distribution
contract](../generated/distributions.md) for those procedures.

## Publish The Signed Arch Repository

The signed Arch repository wraps package bytes that are already frozen; it does not rebuild
or rewrite them.

1. Update the release record. `assurance/arch-repository.json` is the single release
   record: the loader derives the product release tag, release URL, and package filenames
   from the package versions and rejects any disagreement. A release update changes the
   tag, URL, package versions and hashes, and signature timestamp together, along with the
   published-release pin in `tests/test_assurance.py`.
2. Run `./hfx generate`.
3. Build into a fresh output path, using a private GnuPG home that holds the reviewed key,
   and verify the result:

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

4. Publish the complete output only on the fixed `arch-repository` machine channel, marked
   as a prerelease so it does not replace the latest human-facing product release.

What the builder checks and produces:

- It rejects any drift in package bytes or metadata.
- It requires the exact signing subkey and a private GnuPG home readable only by its owner.
- It creates stable database and files aliases and their canonical archives, signs every
  package and metadata file, embeds the package signatures in the database, and seals a
  manifest bound to their checksums.
- It builds and verifies in a sibling temporary directory, then exposes the requested
  output in one atomic step.

Keep the private key and its verified backup outside the repository; publish only the
tracked public key and its full fingerprint. The user guide has users write the three-line
Pacman block themselves rather than trust a downloaded configuration file.
