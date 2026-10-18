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
   published-release pin in `tests/test_assurance.py`. In the same change, move the outgoing
   `packages` entries into `assurance/arch-repository-retention.json`, which keeps exactly
   one earlier release downloadable for downgrades.
2. Run `./hfx generate`.
3. Build into a fresh output path, using a private GnuPG home that holds the reviewed key,
   and verify the result. Pass `--package` once for every package of the release.

```sh
./hfx arch-repository build \
  --package /path/to/hyperflux-next-dkms.pkg.tar.zst \
  --package /path/to/hyperflux-next-linux.pkg.tar.zst \
  --package /path/to/hyperflux-next-openrazer-driver-dkms.pkg.tar.zst \
  --package /path/to/hyperflux-next-full.pkg.tar.zst \
  --gnupg-home "$PRIVATE_GNUPG_HOME" \
  --key-fingerprint 8BF8A266E756BD1C24C930918B3F3850007159E8 \
  --output /path/to/fresh-repository-assets
./hfx arch-repository verify \
  --repository /path/to/fresh-repository-assets
```

   Releases from 2.1.0-1 on have these four packages. A release before 2.1.0-1 has two,
   `hyperflux-next-linux` and `hyperflux-next-full`.

4. Publish the complete output only on the fixed `arch-repository` machine channel, marked
   as a prerelease so it does not replace the latest human-facing product release.
5. Delete every earlier package and signature that the retention file does not list, then
   run `./hfx arch-repository verify-public --output /path/to/receipt.json`. It accepts the
   current repository plus exactly the retained packages and their signatures, checks
   their digests and signatures, and rejects anything else on the channel. The repository
   database lists only the current release.

What the builder checks and produces:

- It rejects any drift in package bytes or metadata, and any package set other than the
  one `packaging/arch-split.json` defines for the release version.
- It requires the exact signing subkey and a private GnuPG home readable only by its owner.
- It creates stable database and files aliases and their canonical archives, signs every
  package and metadata file, embeds the package signatures in the database, and seals a
  manifest bound to their checksums.
- It builds and verifies in a sibling temporary directory, then exposes the requested
  output in one atomic step.

Keep the private key and its verified backup outside the repository; publish only the
tracked public key and its full fingerprint. The Arch guide's setup command checks the key
fingerprint and then writes the three-line Pacman block itself, rather than downloading a
configuration file; a manual route is offered beside it. The apt and rpm guides check the
same fingerprint, then download a small repository file that points at the signed site.

## Publish The Package Repository Site

One GitHub Pages site carries the package repositories for every distribution:
`/arch/` (a Pacman database), `/ubuntu/` and `/debian/` (apt `dists` and `pool`, suites
`stonking` and `forky`), `/fedora/` and `/opensuse/` (rpm-md, each with a `.repo` file that
sets `gpgcheck=1` and `repo_gpgcheck=1`), the public key
`hyperflux-next-repository-key.asc`, and a plain front page. Every repository is signed by
the same Arch repository key. `packaging/repository-site.json` names the site, and its
`state` is `published`. That lets the Ubuntu, Debian, Fedora, and openSUSE targets in
`packaging/distributions.json` carry `maturity: "published"`; the catalog refuses that
maturity while the site state is `unpublished`. With the site published, the user guides
give the Pages addresses, say the packages are not yet confirmed on real hardware, and the
Arch guide points new installs at `/arch/` on the site.

The work is split so the private key never reaches CI.

1. The `distribution-packages` workflow builds every target's packages, then runs
   `./hfx repositories assemble` and uploads the unsigned site as the
   `hyperflux-package-repositories-unsigned` artifact. Assembly writes the apt and rpm-md
   metadata with tools from the pinned distribution images, so nothing on the runner
   decides the layout.
2. On the signing machine, download that artifact into a fresh directory and sign it. The
   Arch part is copied from a verified `./hfx arch-repository build` output, so the Pages
   copy and the `arch-repository` release hold the same bytes.

```sh
gh run download RUN_ID --repo offalexjackson777-stack/hyperflux-next \
  --name hyperflux-package-repositories-unsigned --dir /path/to/fresh-unsigned-site
HFX_OCI_ENGINE=podman ./hfx repositories sign /path/to/fresh-unsigned-site \
  --arch-repository /path/to/fresh-repository-assets \
  --gnupg-home "$PRIVATE_GNUPG_HOME" \
  --key-fingerprint 8BF8A266E756BD1C24C930918B3F3850007159E8 \
  --output /path/to/fresh-signed-site
./hfx repositories verify /path/to/fresh-signed-site
```

3. Read the plan, then publish. Without `--execute` the command only prints what it would
   do.

```sh
./hfx repositories publish /path/to/fresh-signed-site
./hfx repositories publish /path/to/fresh-signed-site --execute
```

What the signer checks and produces:

- Before signing, it checks the unsigned site file by file against its manifest, so a
  changed or extra file in the download stops the run.
- It signs each apt `Release` file twice, as `InRelease` and `Release.gpg`, signs every rpm
  with `rpmsign`, rebuilds the rpm-md metadata so it lists the signed packages, and signs
  `repomd.xml` as `repomd.xml.asc`. It needs `gpg` and `rpmsign` on the signing machine
  and a container engine for the metadata tools.
- It accepts only the subkey of the tracked key. It then verifies the finished site in a
  throwaway keyring that holds just the tracked public key: each signature must be exactly
  one good signature by that subkey, and every package must match its recorded hash.

What publishing does:

- It commits the signed site as the only commit of `gh-pages` and force-pushes that
  branch, so the branch never keeps old packages.
- That commit is authored by `publication_author` in `packaging/repository-site.json`
  (the owner's GitHub noreply address) and is never signed, so publishing does not
  depend on the machine's git identity or `commit.gpgsign` setting.
- It uploads the Arch database, files, and their signatures to the `arch-repository`
  release with `--clobber`, and adds package files the release does not have yet. It
  refuses to replace a package file whose bytes differ from the one already there. The
  release stays a mirror for systems set up before the site existed.

`./hfx repositories rehearse` runs the whole chain in containers. It generates a throwaway
key inside the signing container, on a memory-only mount, and never writes the secret key
out. It signs a site with it, installs `hyperflux-next-linux` with apt on Ubuntu and Debian,
dnf on Fedora, zypper on openSUSE, and pacman on Arch, and checks that each client refuses
the same site under a different key.

`governance/github.json` wants Pages served from the `gh-pages` branch, path `/`, legacy
build type, HTTPS enforced. After the first `./hfx repositories publish --execute` has
created `gh-pages`, the maintainer runs `./hfx github apply --component pages` once to point
Pages at it. `./hfx github verify` reports any later drift.
