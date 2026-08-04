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

## Generated Files

Change canonical authorities rather than generated output. Hybrid documents are
the exception only inside declared generated-block markers; reviewed prose
outside those markers is protected.
