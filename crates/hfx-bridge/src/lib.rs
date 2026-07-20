// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod framing;
mod persistence;
mod rpc;
mod session;
mod session_registry;

pub use framing::{
    FRAME_LENGTH_BYTES, FrameError, FrameIoStage, read_rpc_request, write_rpc_response,
};

pub use persistence::{
    AtomicFileCommitter, BRIDGE_PERSISTENCE_SCHEMA, BridgePersistenceDocument,
    DEFAULT_MAX_PERSISTED_RECEIVERS, DEFAULT_MAX_PERSISTENCE_BYTES, FilePersistenceConfig,
    FilePersistenceError, FilePersistenceStore, PersistenceCommitter, PersistenceIoStage,
};
pub use rpc::{BridgeRpcBackend, ConnectionDispatcher, RpcFailure};
pub use session::{
    AuthorizedSession, BridgeSession, BridgeSessionConfig, KernelSessionIdentitySource,
    SessionError, SessionIdentityError, SessionIdentitySource,
};
pub use session_registry::{SessionRegistry, SessionRegistryError};
