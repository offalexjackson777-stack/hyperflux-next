// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod backend;
mod clock;
mod framing;
mod persistence;
mod profile_authority;
mod rpc;
mod runtime_identity;
mod session;
mod session_registry;
mod snapshot;
mod subscriptions;

pub use backend::{CoreBridgeBackend, CoreBridgeBackendError, CoreBridgeConfig};
pub use clock::LinuxMonotonicClock;
pub use framing::{
    FRAME_LENGTH_BYTES, FrameError, FrameIoStage, read_rpc_request, write_rpc_response,
};

pub use persistence::{
    AtomicFileCommitter, BRIDGE_PERSISTENCE_SCHEMA, BridgePersistenceDocument,
    DEFAULT_MAX_PERSISTED_RECEIVERS, DEFAULT_MAX_PERSISTENCE_BYTES, FilePersistenceConfig,
    FilePersistenceError, FilePersistenceStore, PersistenceCommitter, PersistenceIoStage,
};
pub use profile_authority::{
    DEFAULT_MAX_PROFILE_BINDINGS, ProfileBindingOutcome, ReceiverProfileBinding,
    RuntimeProfileAuthority, RuntimeProfileAuthorityError, RuntimeProfileView,
};
pub use rpc::{BackendRequestContext, BridgeRpcBackend, ConnectionDispatcher, RpcFailure};
pub use runtime_identity::{RuntimeIdentityError, RuntimeIdentityIssuer};
pub use session::{
    AuthorizedSession, BridgeSession, BridgeSessionConfig, KernelSessionIdentitySource,
    SessionError, SessionIdentityError, SessionIdentitySource,
};
pub use session_registry::{SessionRegistry, SessionRegistryError};
pub use snapshot::{
    DisabledRestorationSource, ReceiverRestorationSnapshot, RestorationProjectionError,
    RestorationSnapshotSource, SnapshotProjectionError, SnapshotProjector,
};
pub use subscriptions::{
    ActiveSubscription, DEFAULT_MAX_SUBSCRIPTIONS, SubscriptionRegistry, SubscriptionRegistryError,
};
