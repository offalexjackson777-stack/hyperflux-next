// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    BoundUnixListener, EndpointDiscovery, LinuxRuntimeManager, ProductionBuildError,
    ProductionConfigError, ProductionRestoration, ReceiverIdentityAuthority, ReceiverIdentityError,
    RestorationScheduleError, RestorationScheduler, RuntimeTickError, SocketBindError,
    StructuredEventLoggerError, compose_production, generate_daemon_nonce, load_production_config,
};
use hfx_bridge::{
    ActorConnectionServeError, BridgeActor, BridgeActorError, BridgeActorExit,
    BridgeActorStartError, FilePersistenceError, serve_actor_connection,
};
use hfx_profiles::{ProfileCatalogError, RuntimeProfileCatalog};
use hfx_runtime::{
    ACTOR_RESPONSE_TIMEOUT_MS, BRIDGE_CONFIGURATION_FILE_PATH, BRIDGE_IDENTITY_SECRET_FILE_PATH,
    BRIDGE_SOCKET_LOCK_PATH, BRIDGE_SOCKET_PATH, BRIDGE_STATE_FILE_PATH, CONFIGURATION_MAX_BYTES,
    DISCOVERY_INTERVAL_MS, EVENT_CAPACITY, MAX_CONNECTIONS, RESTORATION_MAX_PERSISTED_RECEIVERS,
    RESTORATION_MAX_PERSISTENCE_BYTES,
};
use std::collections::BTreeMap;
use std::fmt;
use std::io;
use std::net::Shutdown;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::thread::{self, JoinHandle};
use std::time::Duration;

#[derive(Clone, Debug)]
pub struct ProductionServicePaths {
    pub configuration: PathBuf,
    pub socket: PathBuf,
    pub socket_lock: PathBuf,
    pub identity_secret: PathBuf,
    pub state_file: PathBuf,
    pub discovery: EndpointDiscovery,
}

impl ProductionServicePaths {
    #[must_use]
    pub fn linux() -> Self {
        Self {
            configuration: PathBuf::from(BRIDGE_CONFIGURATION_FILE_PATH),
            socket: PathBuf::from(BRIDGE_SOCKET_PATH),
            socket_lock: PathBuf::from(BRIDGE_SOCKET_LOCK_PATH),
            identity_secret: PathBuf::from(BRIDGE_IDENTITY_SECRET_FILE_PATH),
            state_file: PathBuf::from(BRIDGE_STATE_FILE_PATH),
            discovery: EndpointDiscovery::linux(),
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct ProductionServiceExit {
    pub accepted_connections: usize,
    pub rejected_connections: usize,
    pub connection_failures: usize,
    pub connection_panics: usize,
    pub actor_exit: BridgeActorExit,
}

#[derive(Debug)]
pub enum ProductionServiceError {
    Configuration(ProductionConfigError),
    Persistence(FilePersistenceError),
    RestorationSchedule(RestorationScheduleError),
    Profile(ProfileCatalogError),
    Identity(ReceiverIdentityError),
    Entropy,
    Logging(StructuredEventLoggerError),
    Composition(ProductionBuildError),
    ActorStart(BridgeActorStartError),
    Socket(SocketBindError),
    Listener,
    WorkerSpawn,
    ActorSpawn,
    ActorStopped,
    ActorPanicked,
    ActorRuntime(RuntimeTickError),
    ActorShutdown(BridgeActorError),
}

impl fmt::Display for ProductionServiceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Configuration(_) => "production configuration failed validation",
            Self::Persistence(_) => "durable restoration state could not initialize",
            Self::RestorationSchedule(_) => "restoration scheduler could not initialize",
            Self::Profile(_) => "runtime profile catalog could not be loaded",
            Self::Identity(_) => "installation-local receiver identity could not initialize",
            Self::Entropy => "daemon runtime identity entropy is unavailable",
            Self::Logging(_) => "structured event logging could not initialize",
            Self::Composition(_) => "bounded bridge state owners could not initialize",
            Self::ActorStart(_) => "bridge actor configuration is invalid",
            Self::Socket(_) => "local SDK socket authority could not initialize",
            Self::Listener => "local SDK socket accept loop failed",
            Self::WorkerSpawn => "a bounded local SDK connection worker could not start",
            Self::ActorSpawn => "the bridge state-owner thread could not start",
            Self::ActorStopped => "the bridge state owner stopped unexpectedly",
            Self::ActorPanicked => "the bridge state owner terminated unexpectedly",
            Self::ActorRuntime(_) => "the Linux receiver runtime stopped fail-closed",
            Self::ActorShutdown(_) => "the bridge state owner did not shut down cleanly",
        })
    }
}

impl std::error::Error for ProductionServiceError {}

struct ConnectionWorker {
    control: UnixStream,
    worker: JoinHandle<()>,
}

#[derive(Clone, Copy)]
struct ConnectionWorkerResult {
    worker_id: u64,
    failed: bool,
}

type ActorRuntimeResult =
    Result<BridgeActorExit, hfx_bridge::BridgeActorTickFailure<RuntimeTickError>>;

struct RunningService {
    socket: BoundUnixListener,
    actor_handle: hfx_bridge::BridgeActorHandle,
    actor_thread: JoinHandle<ActorRuntimeResult>,
    event_logger: crate::StructuredEventLogger,
    finished_sender: SyncSender<ConnectionWorkerResult>,
    finished_receiver: Receiver<ConnectionWorkerResult>,
    workers: BTreeMap<u64, ConnectionWorker>,
    next_worker_id: u64,
    exit: ProductionServiceExit,
}

/// Runs the complete production bridge until the shared termination flag is
/// set or the state-owning actor stops.
///
/// # Errors
///
/// Returns one sanitized typed startup, runtime, or shutdown failure. The SDK
/// socket is removed only while this process still holds its exclusive lock.
pub fn run_production_service(
    paths: ProductionServicePaths,
    termination: &AtomicBool,
) -> Result<ProductionServiceExit, ProductionServiceError> {
    RunningService::start(paths)?.run(termination)
}

impl RunningService {
    fn start(paths: ProductionServicePaths) -> Result<Self, ProductionServiceError> {
        let config = load_production_config(&paths.configuration, CONFIGURATION_MAX_BYTES)
            .map_err(ProductionServiceError::Configuration)?;
        let catalog = RuntimeProfileCatalog::load().map_err(ProductionServiceError::Profile)?;
        let restoration = if config.restoration.enabled {
            ProductionRestoration::durable(
                &paths.state_file,
                RESTORATION_MAX_PERSISTED_RECEIVERS,
                RESTORATION_MAX_PERSISTENCE_BYTES,
            )
            .map_err(ProductionServiceError::Persistence)?
        } else {
            ProductionRestoration::disabled()
        };
        let receiver_identities = ReceiverIdentityAuthority::load_or_create(&paths.identity_secret)
            .map_err(ProductionServiceError::Identity)?;
        let daemon_nonce = generate_daemon_nonce().map_err(|_| ProductionServiceError::Entropy)?;
        let (event_sink, event_logger) =
            crate::StructuredEventSink::spawn_stderr(EVENT_CAPACITY.into())
                .map_err(ProductionServiceError::Logging)?;
        let composition =
            compose_production(catalog.clone(), daemon_nonce, event_sink, restoration)
                .map_err(ProductionServiceError::Composition)?;
        let restoration = RestorationScheduler::production(
            config.restoration.enabled,
            composition.restoration_session.clone(),
            daemon_nonce,
        )
        .map_err(ProductionServiceError::RestorationSchedule)?;
        let (actor, actor_handle) = BridgeActor::new(
            composition.session_config,
            composition.session_identities,
            composition.session_registry,
            composition.backend,
            composition.actor_limits,
        )
        .map_err(ProductionServiceError::ActorStart)?;
        let mut manager = LinuxRuntimeManager::new(
            paths.discovery,
            receiver_identities,
            catalog,
            config.mode,
            daemon_nonce,
            Duration::from_millis(DISCOVERY_INTERVAL_MS),
            restoration,
        );
        let socket = BoundUnixListener::bind(&paths.socket, &paths.socket_lock)
            .map_err(ProductionServiceError::Socket)?;
        socket
            .listener()
            .set_nonblocking(true)
            .map_err(|_| ProductionServiceError::Listener)?;

        let actor_thread = thread::Builder::new()
            .name("hfx-state-owner".to_owned())
            .spawn(move || {
                actor.run_with_runtime_tick(
                    Duration::from_millis(DISCOVERY_INTERVAL_MS),
                    |backend, sessions| manager.tick(backend, sessions).map(|_| true),
                )
            })
            .map_err(|_| ProductionServiceError::ActorSpawn)?;

        let (finished_sender, finished_receiver) =
            mpsc::sync_channel::<ConnectionWorkerResult>(usize::from(MAX_CONNECTIONS));
        Ok(Self {
            socket,
            actor_handle,
            actor_thread,
            event_logger,
            finished_sender,
            finished_receiver,
            workers: BTreeMap::new(),
            next_worker_id: 1,
            exit: ProductionServiceExit::default(),
        })
    }

    fn run(
        mut self,
        termination: &AtomicBool,
    ) -> Result<ProductionServiceExit, ProductionServiceError> {
        let mut loop_failure = None;
        while !termination.load(Ordering::Acquire) {
            self.reap_workers();
            if self.actor_thread.is_finished() {
                loop_failure = Some(ProductionServiceError::ActorStopped);
                break;
            }
            if let Err(error) = self.accept_once() {
                loop_failure = Some(error);
                break;
            }
        }
        self.shutdown(loop_failure)
    }

    fn accept_once(&mut self) -> Result<(), ProductionServiceError> {
        match self.socket.listener().accept() {
            Ok((stream, _)) if self.workers.len() >= usize::from(MAX_CONNECTIONS) => {
                self.exit.rejected_connections += 1;
                let _ = stream.shutdown(Shutdown::Both);
                Ok(())
            }
            Ok((stream, _)) => {
                let control = stream
                    .try_clone()
                    .map_err(|_| ProductionServiceError::Listener)?;
                let worker_id = self.next_worker_id;
                self.next_worker_id = self
                    .next_worker_id
                    .checked_add(1)
                    .ok_or(ProductionServiceError::WorkerSpawn)?;
                let worker = spawn_connection_worker(
                    worker_id,
                    stream,
                    self.actor_handle.clone(),
                    self.finished_sender.clone(),
                )?;
                self.workers
                    .insert(worker_id, ConnectionWorker { control, worker });
                self.exit.accepted_connections += 1;
                Ok(())
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(hfx_runtime::ACCEPT_POLL_INTERVAL_MS));
                Ok(())
            }
            Err(_) => Err(ProductionServiceError::Listener),
        }
    }

    fn reap_workers(&mut self) {
        while let Ok(result) = self.finished_receiver.try_recv() {
            self.exit.connection_failures += usize::from(result.failed);
            if let Some(worker) = self.workers.remove(&result.worker_id)
                && worker.worker.join().is_err()
            {
                self.exit.connection_panics += 1;
            }
        }
    }

    fn shutdown(
        mut self,
        loop_failure: Option<ProductionServiceError>,
    ) -> Result<ProductionServiceExit, ProductionServiceError> {
        for worker in self.workers.values() {
            let _ = worker.control.shutdown(Shutdown::Both);
        }
        for (_, worker) in std::mem::take(&mut self.workers) {
            if worker.worker.join().is_err() {
                self.exit.connection_panics += 1;
            }
        }
        self.reap_workers();

        let requested_exit = if self.actor_thread.is_finished() {
            None
        } else {
            Some(self.actor_handle.shutdown())
        };
        drop(self.actor_handle);
        let actor_result = self.actor_thread.join();
        drop(self.socket);
        let logger_result = self
            .event_logger
            .finish(Duration::from_millis(ACTOR_RESPONSE_TIMEOUT_MS));

        let actor_result = actor_result.map_err(|_| ProductionServiceError::ActorPanicked)?;
        self.exit.actor_exit =
            actor_result.map_err(|failure| ProductionServiceError::ActorRuntime(failure.error))?;
        if let Some(result) = requested_exit {
            let requested = result.map_err(ProductionServiceError::ActorShutdown)?;
            if requested != self.exit.actor_exit {
                return Err(ProductionServiceError::ActorShutdown(
                    BridgeActorError::Unavailable,
                ));
            }
        }
        logger_result.map_err(ProductionServiceError::Logging)?;
        if let Some(error) = loop_failure {
            return Err(error);
        }
        Ok(self.exit)
    }
}

fn spawn_connection_worker(
    worker_id: u64,
    mut stream: UnixStream,
    actor: hfx_bridge::BridgeActorHandle,
    finished: SyncSender<ConnectionWorkerResult>,
) -> Result<JoinHandle<()>, ProductionServiceError> {
    thread::Builder::new()
        .name(format!("hfx-sdk-{worker_id}"))
        .spawn(move || {
            let result: Result<_, ActorConnectionServeError> =
                serve_actor_connection(&mut stream, &actor);
            let _ = finished.send(ConnectionWorkerResult {
                worker_id,
                failed: result.is_err(),
            });
        })
        .map_err(|_| ProductionServiceError::WorkerSpawn)
}

#[cfg(test)]
mod tests {
    use super::*;
    use hfx_domain::{ClientId, ClientName, ProtocolFeatureId, ProtocolVersion};
    use hfx_protocol::CURRENT_PROTOCOL_VERSION;
    use hfx_runtime::BridgeMode;
    use hfx_sdk::{HyperFluxClient, KernelRequestIdentitySource, SdkClientConfig};
    use std::fs;
    use std::os::unix::fs::PermissionsExt as _;
    use std::sync::Arc;
    use std::time::{Instant, SystemTime, UNIX_EPOCH};

    const DEFAULT_CONFIG: &str = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../packaging/generated/bridge.json"
    ));

    fn temporary_paths() -> (PathBuf, ProductionServicePaths) {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "hfx-production-service-{}-{unique}",
            std::process::id()
        ));
        let runtime = root.join("run");
        let state = root.join("state");
        let devices = root.join("dev");
        let misc = root.join("misc");
        for directory in [&runtime, &state, &devices, &misc] {
            fs::create_dir_all(directory).expect("test directory creates");
        }
        fs::set_permissions(&runtime, fs::Permissions::from_mode(0o2750))
            .expect("runtime mode sets");
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700)).expect("state mode sets");
        let configuration = root.join("bridge.json");
        fs::write(&configuration, DEFAULT_CONFIG).expect("configuration writes");
        fs::set_permissions(&configuration, fs::Permissions::from_mode(0o600))
            .expect("configuration mode sets");
        let paths = ProductionServicePaths {
            configuration,
            socket: runtime.join("bridge.sock"),
            socket_lock: runtime.join("bridge.lock"),
            identity_secret: state.join("receiver-identity.key"),
            state_file: state.join("bridge-state.json"),
            discovery: EndpointDiscovery::new(devices, misc),
        };
        (root, paths)
    }

    fn sdk_config() -> SdkClientConfig {
        let version = ProtocolVersion::try_from(CURRENT_PROTOCOL_VERSION).expect("version");
        SdkClientConfig {
            client_id: ClientId::try_from("daemon-integration-test").expect("client id"),
            client_name: ClientName::try_from("Daemon Integration Test").expect("client name"),
            minimum_version: version,
            maximum_version: version,
            required_features: vec![
                ProtocolFeatureId::try_from("integration-view-projection").expect("feature"),
            ],
            optional_features: Vec::new(),
        }
    }

    #[test]
    fn complete_service_serves_snapshot_and_stops_without_a_stale_socket() {
        let (root, paths) = temporary_paths();
        let socket = paths.socket.clone();
        let termination = Arc::new(AtomicBool::new(false));
        let service_termination = Arc::clone(&termination);
        let service =
            thread::spawn(move || run_production_service(paths, service_termination.as_ref()));
        let deadline = Instant::now() + Duration::from_secs(3);
        while !socket.exists() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        assert!(socket.exists(), "service socket became ready");
        let stream = UnixStream::connect(&socket).expect("SDK connects");
        let mut client =
            HyperFluxClient::connect(stream, sdk_config(), KernelRequestIdentitySource)
                .expect("SDK negotiates");
        let snapshot = client.snapshot().expect("snapshot reads");
        assert!(snapshot.receivers.is_empty());
        drop(client);
        termination.store(true, Ordering::Release);
        let exit = service
            .join()
            .expect("service thread joins")
            .expect("service stops cleanly");
        assert_eq!(exit.accepted_connections, 1);
        assert_eq!(exit.actor_exit.cleanup_failures, 0);
        assert!(!socket.exists());
        fs::remove_dir_all(root).expect("test root removes");
    }

    #[test]
    fn read_only_restoration_fails_before_socket_or_hardware_authority() {
        let (root, paths) = temporary_paths();
        let enabled = DEFAULT_CONFIG.replace("\"enabled\": false", "\"enabled\": true");
        fs::write(&paths.configuration, enabled).expect("enabled config writes");
        assert!(matches!(
            run_production_service(paths.clone(), &AtomicBool::new(true)),
            Err(ProductionServiceError::Configuration(
                ProductionConfigError::Policy(
                    hfx_runtime::BridgeConfigError::RestorationRequiresQualifiedLive
                )
            ))
        ));
        assert!(!paths.socket.exists());
        assert!(!paths.identity_secret.exists());
        assert!(!paths.state_file.exists());
        fs::remove_dir_all(root).expect("test root removes");
    }

    #[test]
    fn qualified_live_restoration_composes_before_the_socket_is_served() {
        let (root, paths) = temporary_paths();
        let enabled = DEFAULT_CONFIG
            .replace("\"mode\": \"read-only\"", "\"mode\": \"qualified-live\"")
            .replace("\"enabled\": false", "\"enabled\": true");
        fs::write(&paths.configuration, enabled).expect("enabled config writes");
        let exit = run_production_service(paths.clone(), &AtomicBool::new(true))
            .expect("enabled service composes and stops");
        assert_eq!(exit.actor_exit.cleanup_failures, 0);
        assert!(!paths.socket.exists());
        assert!(paths.identity_secret.exists());
        fs::remove_dir_all(root).expect("test root removes");
    }

    #[test]
    fn qualified_live_is_the_only_mode_that_can_attempt_writer_admission() {
        assert_ne!(BridgeMode::ReadOnly, BridgeMode::QualifiedLive);
    }
}
