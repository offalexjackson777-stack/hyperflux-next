# Privacy And Support Evidence

HyperFlux diagnostics are structured before they become support material. The
default support preview names every included and excluded class so a user can
decide whether to create a bundle.

## Included

- package and kernel activation state;
- bounded service and receiver-generation state;
- profile qualification and capability state;
- bounded structured diagnostic events and transaction outcomes;
- an explicit declaration of queries, writes, and network activity.

## Excluded By Default

- hardware serial numbers and stable host identifiers;
- private filesystem paths and terminal history;
- raw HID or USB payloads;
- captures, memory dumps, and arbitrary journal text;
- active information-query responses.

Bundles are written with private permissions, are never uploaded automatically,
and do not authorize hardware access. See the repository [security policy](../../SECURITY.md)
for private vulnerability reporting and public-issue boundaries.
