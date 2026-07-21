# Bridge Persistence

The bridge persists only semantic stable-lighting intent, restoration policy, and durable per-device restoration claims. It does not persist sessions, leases, route observations, battery values, raw reports, effect phase, application widgets, or private hardware identifiers.

Persistence is independent of RPC connection sessions. Protocol credentials, internal authorization epochs, leases, and queued requests become invalid when their owning connection ends and never enter the durable document.

## Trust Boundary

`FilePersistenceStore` opens one absolute path beneath a private directory owned by the service user. The directory, state file, and advisory lock must be ordinary filesystem objects with no group or other permission bits. Linux `O_NOFOLLOW` prevents a state or lock symlink from entering the boundary. The held exclusive advisory lock gives one bridge process write authority and is released automatically if that process exits.

The document is strict JSON with the identity `hyperflux-bridge-persistence-v1`. Unknown fields, unsupported nested persistence schema versions, duplicate identities, noncanonical ordering, oversized files, excess receivers, excess stable entries, and excess restoration records fail closed before the state becomes available to the bridge.

## Commit Protocol

Every compare-and-set mutation is prepared in memory and validated as a complete candidate before I/O. Generation retirement uses one batch compare-and-set for all sibling restoration claims; either every expected revision advances or none does.

The file commit then follows one protocol:

1. serialize the canonical bounded document;
2. create a unique same-directory file with mode `0600` and `O_EXCL | O_NOFOLLOW`;
3. write and sync the complete temporary file;
4. atomically replace the state path;
5. sync the parent directory;
6. publish the candidate as the process's current compare-and-set view.

Failure before replacement leaves both memory and the prior file unchanged. If replacement is visible but directory sync fails, the adapter advances its in-process view and returns an explicit durability error. That state may not survive power loss, but an immediate stale retry cannot overwrite it under an old expected revision. On restart, the bridge trusts only the document actually present after validation.

## Generation Retirement

The bridge stages receiver lifecycle, profile, ownership, transaction, and event changes in memory first. It then reconciles every durable restoration claim for the retiring generation as the last fallible step before publishing that staged state.

- planned or deferred claims are invalidated as stale;
- a revoked unsent transaction is invalidated from its exact retained terminal;
- a confirmed delivery remains succeeded even though its generation is retiring;
- retained failure facts remain failed with their exact side-effect certainty;
- evicted, unavailable, or conflicting transport history becomes a non-retryable possible-side-effect failure;
- only `NotObserved` proves an attempted claim can be safely invalidated.

All affected claim revisions and their canonical terminal events are prepared before the single durable batch commit. A compare-and-set conflict or file failure publishes no staged volatile generation transition. A process crash after atomic file replacement may lose only the old process's volatile event stream; the next process validates and projects the durable terminal records under a new stream epoch.

## Schema Evolution

The outer document identity and every nested persistence record carry explicit versions. This initial implementation accepts only version 1 and never guesses a migration. Future migrations must read the old document, produce and validate a complete new candidate, retain a rollback copy until service startup succeeds, and receive their own fault-injection tests before the accepted version set changes.
