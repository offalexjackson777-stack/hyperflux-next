// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod actor;
mod backend;
mod clock;
mod connection;
mod generation;
mod observation;
mod persistence;
mod profile_authority;
mod restoration_runtime;
mod rpc;
mod runtime_identity;
mod session;
mod session_registry;
mod snapshot;
mod staged_events;
mod subscriptions;

pub use actor::{
    ActorConnectionServeError, BridgeActor, BridgeActorError, BridgeActorExit, BridgeActorHandle,
    BridgeActorLimits, BridgeActorStartError, BridgeActorTickFailure, BridgeConnectionId,
    serve_actor_connection,
};
pub use backend::{
    BridgeDispatchResult, CoreBridgeBackend, CoreBridgeBackendError, CoreBridgeConfig,
    StableCaptureStatus,
};
pub use clock::{LinuxMonotonicClock, LinuxWallClock};
pub use connection::{ConnectionServeError, ConnectionServeReport, serve_connection};
pub use generation::{
    GenerationActivation, GenerationActivationOutcome, GenerationOrchestrationError,
    GenerationOrchestrator, GenerationQualification, ReceiverDisconnectBegan,
    ReceiverDisconnectCompleted, ReceiverDisconnectCompletionOutcome,
    ReceiverDisconnectObservation, ReceiverDisconnectOutcome, ReceiverGenerationObservation,
};
pub use hfx_protocol::{
    FRAME_LENGTH_BYTES, FrameError, FrameIoStage, read_rpc_request, read_rpc_request_for_version,
    read_rpc_response, read_rpc_response_for_version, write_rpc_request,
    write_rpc_request_for_version, write_rpc_response, write_rpc_response_for_version,
};
pub use observation::{
    AppliedLifecycleObservation, LifecycleObservation, LifecycleObservationError,
    LifecycleObservationKind, LifecycleObservationOrchestrator, LifecycleObservationOutcome,
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
pub use restoration_runtime::{DurableRestorationRuntime, RestorationRuntime};
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
