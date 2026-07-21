// SPDX-License-Identifier: GPL-2.0-only

use crate::{ProductionRestoration, StructuredEventSink};
use hfx_bridge::{
    AuthorizedSession, BridgeActorLimits, BridgeSessionConfig, CoreBridgeBackend,
    CoreBridgeBackendError, CoreBridgeConfig, KernelSessionIdentitySource, LinuxMonotonicClock,
    LinuxWallClock, RuntimeProfileAuthority, RuntimeProfileAuthorityError, SessionRegistry,
    SessionRegistryError,
};
use hfx_core::{LifecycleLimits, ReceiverLifecycleRegistry, ReceiverRegistryError};
use hfx_domain::{
    AuthorizationEpoch, ClientId, ComponentVersion, ProjectionRevision, ProtocolVersion,
    QueueCapacity, ServerInstanceId, SessionId, StreamEpoch, StreamId,
};
use hfx_kernel_transport::{KernelReceiverTransport, KernelTransportRouter, LinuxKernelIo};
use hfx_profiles::RuntimeProfileCatalog;
use hfx_protocol::CURRENT_PROTOCOL_VERSION;
use hfx_runtime::{
    ACTOR_RESPONSE_TIMEOUT_MS, COMMAND_QUEUE_CAPACITY, DIAGNOSTIC_CAPACITY, EVENT_CAPACITY,
    LEASE_CAPACITY, LEASE_HISTORY_CAPACITY, MAX_CONNECTIONS, MAX_RECEIVER_GENERATIONS,
    PRODUCT_VERSION, SUBSCRIPTION_CAPACITY, TRANSACTION_CAPACITY,
};
use std::fmt;
use std::time::Duration;

pub type ProductionWriter = KernelReceiverTransport<LinuxKernelIo>;
pub type ProductionTransport = KernelTransportRouter<ProductionWriter>;
pub type ProductionBackend = CoreBridgeBackend<
    LinuxMonotonicClock,
    LinuxWallClock,
    ProductionTransport,
    ProductionRestoration,
    StructuredEventSink,
>;

pub struct ProductionComposition {
    pub backend: ProductionBackend,
    pub session_config: BridgeSessionConfig,
    pub session_registry: SessionRegistry,
    pub restoration_session: Option<AuthorizedSession>,
    pub actor_limits: BridgeActorLimits,
    pub session_identities: KernelSessionIdentitySource,
}

#[derive(Debug)]
pub enum ProductionBuildError {
    InvalidGeneratedRuntime,
    ReceiverRegistry(ReceiverRegistryError),
    Profile(RuntimeProfileAuthorityError),
    SessionRegistry(SessionRegistryError),
    Backend(CoreBridgeBackendError),
}

impl fmt::Display for ProductionBuildError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidGeneratedRuntime => "generated Linux runtime authority is invalid",
            Self::ReceiverRegistry(_) => "receiver registry initialization failed",
            Self::Profile(_) => "runtime profile authority initialization failed",
            Self::SessionRegistry(_) => "internal bridge session initialization failed",
            Self::Backend(_) => "bridge backend initialization failed",
        })
    }
}

impl std::error::Error for ProductionBuildError {}

/// Composes every bounded state owner required by the production actor.
///
/// # Errors
///
/// Fails before any socket is exposed or writer session is requested.
pub fn compose_production(
    catalog: RuntimeProfileCatalog,
    daemon_nonce: [u8; 32],
    event_sink: StructuredEventSink,
    restoration: ProductionRestoration,
) -> Result<ProductionComposition, ProductionBuildError> {
    let receiver_capacity = queue_capacity(
        u16::try_from(MAX_RECEIVER_GENERATIONS)
            .map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)?,
    )?;
    let command_capacity = queue_capacity(COMMAND_QUEUE_CAPACITY)?;
    let connection_capacity = queue_capacity(MAX_CONNECTIONS)?;
    let session_capacity = queue_capacity(
        MAX_CONNECTIONS
            .checked_add(u16::from(restoration.is_enabled()))
            .ok_or(ProductionBuildError::InvalidGeneratedRuntime)?,
    )?;
    let lease_capacity = queue_capacity(LEASE_CAPACITY)?;
    let lease_history_capacity = queue_capacity(LEASE_HISTORY_CAPACITY)?;
    let transaction_capacity = queue_capacity(TRANSACTION_CAPACITY)?;
    let event_capacity = queue_capacity(EVENT_CAPACITY)?;
    let diagnostic_capacity = queue_capacity(DIAGNOSTIC_CAPACITY)?;
    let subscription_capacity = queue_capacity(SUBSCRIPTION_CAPACITY)?;

    let server_instance_id = server_instance_id(&daemon_nonce)?;
    let stream_epoch = StreamEpoch::try_from(
        u64::from_be_bytes(
            daemon_nonce[..8]
                .try_into()
                .map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)?,
        )
        .max(1),
    )
    .map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)?;
    let session_config = BridgeSessionConfig {
        server_instance_id,
        bridge_version: ComponentVersion::try_from(PRODUCT_VERSION)
            .map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)?,
        event_buffer_capacity: event_capacity,
    };
    let mut sessions = SessionRegistry::new(session_capacity);
    let restoration_session = restoration
        .is_enabled()
        .then(|| internal_restoration_session(&daemon_nonce))
        .transpose()?;
    if let Some(session) = &restoration_session {
        sessions
            .register(session.clone())
            .map_err(ProductionBuildError::SessionRegistry)?;
    }
    let receivers = ReceiverLifecycleRegistry::new(usize::from(receiver_capacity.get()))
        .map_err(ProductionBuildError::ReceiverRegistry)?;
    let profiles = RuntimeProfileAuthority::new(catalog, usize::from(receiver_capacity.get()))
        .map_err(ProductionBuildError::Profile)?;
    let transport = KernelTransportRouter::new(receiver_capacity);
    let mut backend_identities = KernelSessionIdentitySource;
    let backend = CoreBridgeBackend::new(
        CoreBridgeConfig {
            lifecycle_limits: LifecycleLimits::default(),
            lease_capacity,
            lease_history_capacity,
            transaction_capacity,
            event_capacity,
            diagnostic_capacity,
            subscription_capacity,
            stream_id: StreamId::try_from("bridge-events")
                .map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)?,
            stream_epoch,
            projection_revision: ProjectionRevision::try_from(1_u32)
                .map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)?,
        },
        LinuxMonotonicClock,
        LinuxWallClock,
        transport,
        restoration,
        &mut backend_identities,
        receivers,
        profiles,
        event_sink,
    )
    .map_err(ProductionBuildError::Backend)?;
    Ok(ProductionComposition {
        backend,
        session_config,
        session_registry: sessions,
        restoration_session,
        actor_limits: BridgeActorLimits {
            command_capacity,
            connection_capacity,
            response_timeout: Duration::from_millis(ACTOR_RESPONSE_TIMEOUT_MS),
        },
        session_identities: KernelSessionIdentitySource,
    })
}

fn internal_restoration_session(
    daemon_nonce: &[u8; 32],
) -> Result<AuthorizedSession, ProductionBuildError> {
    let mut session_id = String::from("restore-session-");
    for byte in &daemon_nonce[..16] {
        use std::fmt::Write as _;
        write!(session_id, "{byte:02x}")
            .map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)?;
    }
    let epoch = u64::from_be_bytes(
        daemon_nonce[16..24]
            .try_into()
            .map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)?,
    )
    .max(1);
    Ok(AuthorizedSession {
        client_id: ClientId::try_from("bridge-restoration")
            .map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)?,
        selected_version: ProtocolVersion::try_from(CURRENT_PROTOCOL_VERSION)
            .map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)?,
        session_id: SessionId::try_from(session_id)
            .map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)?,
        authorization_epoch: AuthorizationEpoch::try_from(epoch)
            .map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)?,
    })
}

fn queue_capacity(value: u16) -> Result<QueueCapacity, ProductionBuildError> {
    QueueCapacity::try_from(value).map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)
}

fn server_instance_id(daemon_nonce: &[u8; 32]) -> Result<ServerInstanceId, ProductionBuildError> {
    let mut value = String::from("bridge-");
    for byte in &daemon_nonce[..12] {
        use std::fmt::Write as _;
        write!(value, "{byte:02x}").map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)?;
    }
    ServerInstanceId::try_from(value).map_err(|_| ProductionBuildError::InvalidGeneratedRuntime)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn production_composition_uses_only_generated_nonzero_bounds() {
        let (sink, logger) = StructuredEventSink::spawn_stderr(4).expect("logger starts");
        let composition = compose_production(
            RuntimeProfileCatalog::load().expect("catalog loads"),
            [0x55; 32],
            sink,
            ProductionRestoration::disabled(),
        )
        .expect("production composes");
        assert_eq!(
            composition.actor_limits.connection_capacity.get(),
            MAX_CONNECTIONS
        );
        assert_eq!(
            composition.session_config.event_buffer_capacity.get(),
            EVENT_CAPACITY
        );
        assert!(composition.restoration_session.is_none());
        drop(composition);
        assert!(logger.join().is_ok());
    }

    #[test]
    fn durable_runtime_reserves_one_non_client_session() {
        let root = std::env::temp_dir().join(format!(
            "hfx-production-restoration-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        std::fs::create_dir(&root).expect("state directory creates");
        std::fs::set_permissions(
            &root,
            <std::fs::Permissions as std::os::unix::fs::PermissionsExt>::from_mode(0o700),
        )
        .expect("state directory mode sets");
        let restoration = ProductionRestoration::durable(&root.join("state.json"), 4, 1024 * 1024)
            .expect("durable runtime opens");
        let (sink, logger) = StructuredEventSink::spawn_stderr(4).expect("logger starts");
        let composition = compose_production(
            RuntimeProfileCatalog::load().expect("catalog loads"),
            [0x66; 32],
            sink,
            restoration,
        )
        .expect("durable production composes");
        let session = composition
            .restoration_session
            .as_ref()
            .expect("restoration session is reserved");
        assert!(hfx_core::SessionAuthority::authorizes(
            &composition.session_registry,
            &session.session_id,
            session.authorization_epoch,
        ));
        drop(composition);
        assert!(logger.join().is_ok());
        std::fs::remove_dir_all(root).expect("state directory removes");
    }
}
