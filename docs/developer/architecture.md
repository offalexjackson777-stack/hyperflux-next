# Architecture

HyperFlux Next 2.0 is a bridge and compatibility product. The kernel transport
preserves ordinary HID input, the bridge owns route and generation authority,
and thin connectors expose qualified exchanges to stock applications.

## Stable Boundaries

Protocols v1 through v7 and the kernel UAPI are generated from canonical
registries. Application adapters cannot submit arbitrary receiver frames or
invent device authority.

## Detailed References

Read the [design book](../architecture/design-book.md),
[bridge core](../architecture/bridge-core.md),
[kernel boundary](../architecture/kernel.md), and
[persistence contract](../architecture/persistence.md) for the detailed model.
