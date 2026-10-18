// SPDX-License-Identifier: GPL-2.0-only

//! An in-process bridge on a temporary socket that keeps one lease ledger for
//! every connection: one owner per resource, renewal and release by that owner
//! only, and native exchanges refused as not sent unless the request carries
//! the lease that currently owns the resource. Tests use it to watch what a
//! relay endpoint asks of the bridge. A refusing backend only negotiates and
//! fails every lease request, for tests that need a connectable bridge and
//! nothing more.

use hfx_bridge::{
    AuthorizedSession, BackendRequestContext, BridgeRpcBackend, BridgeSessionConfig, RpcFailure,
    SessionIdentityError, SessionIdentitySource, SessionRegistry,
};
use hfx_domain::{
    ClientId, ComponentVersion, DeviceKind, EndpointId, FindingId, GenerationId, LeaseId,
    LeaseState, LogicalDeviceId, MonotonicMs, NativeProtocolFamily, NativeReportByteCount,
    NativeRouteAccess, NativeRouteState, NativeRouteToken, ProductId, ProtocolErrorKind,
    QueueCapacity, ReceiverId, ResourceKind, ServerInstanceId, VendorId,
};
use hfx_protocol::{
    BridgeSnapshot, DiagnosticSnapshot, EventBatch, IntegrationView, LeaseConflict, LeaseGrant,
    LeaseRejection, LeaseRequest, LeaseResult, NativeExchangeMatchedResponse,
    NativeExchangeNotSent, NativeExchangeOutcome, NativeExchangeRequest, NativeExchangeResult,
    NativeRoute, ReleaseLeaseRequest, RenewLeaseRequest, ResourceKey, SubscriptionRequest,
    TransactionLookup, TransactionRequest, TransactionResult,
};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;

static NEXT_SOCKET: AtomicU64 = AtomicU64::new(0);

/// What the test bridge has granted and been asked.
#[derive(Default)]
pub(crate) struct LeaseLedger {
    pub(crate) owner: Option<(ClientId, LeaseId)>,
    pub(crate) acquires: usize,
    pub(crate) renewals: usize,
    pub(crate) releases: usize,
    pub(crate) transacts: usize,
    pub(crate) refuse_renewal: bool,
    next_lease: u32,
}

impl LeaseLedger {
    fn owns(&self, client_id: &ClientId, lease_id: &LeaseId) -> bool {
        self.owner
            .as_ref()
            .is_some_and(|(owner, lease)| owner == client_id && lease == lease_id)
    }

    pub(crate) fn held_by(&self, client_id: &str) -> bool {
        self.owner
            .as_ref()
            .is_some_and(|(owner, _)| owner.as_str() == client_id)
    }
}

pub(crate) struct LeasingBridge {
    socket: PathBuf,
    ledger: Arc<Mutex<LeaseLedger>>,
    stop: Arc<AtomicBool>,
    acceptor: Option<thread::JoinHandle<()>>,
}

impl LeasingBridge {
    pub(crate) fn start() -> Self {
        let unique = NEXT_SOCKET.fetch_add(1, Ordering::Relaxed);
        let socket = std::env::temp_dir().join(format!(
            "hfx-relay-lease-{}-{unique}.sock",
            std::process::id()
        ));
        let listener = UnixListener::bind(&socket).expect("test bridge socket binds");
        let ledger = Arc::new(Mutex::new(LeaseLedger::default()));
        let stop = Arc::new(AtomicBool::new(false));
        let acceptor = {
            let ledger = Arc::clone(&ledger);
            let stop = Arc::clone(&stop);
            thread::spawn(move || {
                for (number, stream) in listener.incoming().enumerate() {
                    if stop.load(Ordering::SeqCst) {
                        break;
                    }
                    let Ok(mut stream) = stream else {
                        break;
                    };
                    let mut backend = LeasingBackend {
                        ledger: Arc::clone(&ledger),
                        refusing: false,
                    };
                    thread::spawn(move || serve(&mut stream, number, &mut backend));
                }
            })
        };
        Self {
            socket,
            ledger,
            stop,
            acceptor: Some(acceptor),
        }
    }

    pub(crate) fn socket(&self) -> &Path {
        &self.socket
    }

    pub(crate) fn ledger(&self) -> MutexGuard<'_, LeaseLedger> {
        self.ledger.lock().expect("ledger lock is not poisoned")
    }
}

impl Drop for LeasingBridge {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        let _ = UnixStream::connect(&self.socket);
        if let Some(acceptor) = self.acceptor.take() {
            let _ = acceptor.join();
        }
        let _ = std::fs::remove_file(&self.socket);
    }
}

/// Serves one connection until it closes. `number` keeps the protocol session
/// identities of different connections apart.
pub(crate) fn serve(stream: &mut UnixStream, number: usize, backend: &mut LeasingBackend) {
    let seed = u8::try_from(number % 16).unwrap_or(0).wrapping_mul(16);
    let _ = hfx_bridge::serve_connection(
        stream,
        session_config(),
        &mut SeededIdentities(seed),
        &mut SessionRegistry::new(QueueCapacity::try_from(4).expect("capacity is valid")),
        backend,
    );
}

fn session_config() -> BridgeSessionConfig {
    BridgeSessionConfig {
        server_instance_id: ServerInstanceId::try_from("relay-lease-test".to_owned())
            .expect("server instance is valid"),
        bridge_version: ComponentVersion::try_from("0.0.0-test".to_owned())
            .expect("bridge version is valid"),
        event_buffer_capacity: QueueCapacity::try_from(8).expect("capacity is valid"),
    }
}

struct SeededIdentities(u8);

impl SessionIdentitySource for SeededIdentities {
    fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError> {
        for byte in destination {
            *byte = self.0;
            self.0 = self.0.wrapping_add(1);
        }
        Ok(())
    }
}

/// A qualified, available route whose resource is the one the ledger leases.
pub(crate) fn leased_route() -> NativeRoute {
    let resource = route_resource();
    NativeRoute {
        receiver_id: resource.receiver_id.clone(),
        generation_id: resource.generation_id,
        device_id: resource.device_id.clone(),
        endpoint_id: EndpointId::try_from("hyperflux-pointer".to_owned())
            .expect("endpoint is valid"),
        device_kind: DeviceKind::Mouse,
        vendor_id: VendorId::try_from(0x1532_u16).expect("vendor is valid"),
        product_id: ProductId::try_from(0x00cd_u16).expect("product is valid"),
        route_state: NativeRouteState::Available,
        access: NativeRouteAccess::Qualified,
        protocol_family: NativeProtocolFamily::try_from("razer-feature-report-v1".to_owned())
            .expect("protocol family is valid"),
        report_bytes: NativeReportByteCount::try_from(90_u16).expect("size is valid"),
        route_token: Some(NativeRouteToken::try_from("b".repeat(64)).expect("token is valid")),
        resource: Some(resource),
    }
}

pub(crate) fn route_resource() -> ResourceKey {
    ResourceKey {
        receiver_id: ReceiverId::try_from("receiver-1".to_owned()).expect("receiver is valid"),
        generation_id: GenerationId::try_from(1_u64).expect("generation is valid"),
        device_id: LogicalDeviceId::try_from("paired-mouse-00cd".to_owned())
            .expect("device is valid"),
        kind: ResourceKind::NativeControl,
    }
}

fn finding() -> FindingId {
    FindingId::try_from("HFX-INTERNAL-001".to_owned()).expect("finding is valid")
}

fn grant(client_id: ClientId, lease_id: LeaseId, state: LeaseState) -> LeaseResult {
    LeaseResult::Granted(LeaseGrant {
        lease_id,
        client_id,
        resources: vec![route_resource()],
        expires_at_ms: MonotonicMs::try_from(30_000_u64).expect("expiry is valid"),
        state,
    })
}

fn rejected() -> LeaseResult {
    LeaseResult::Rejected(LeaseRejection {
        error_kind: ProtocolErrorKind::OwnershipConflict,
        finding_id: finding(),
    })
}

pub(crate) struct LeasingBackend {
    ledger: Arc<Mutex<LeaseLedger>>,
    refusing: bool,
}

impl LeasingBackend {
    pub(crate) fn refusing() -> Self {
        Self {
            ledger: Arc::default(),
            refusing: true,
        }
    }

    fn ledger(&self) -> MutexGuard<'_, LeaseLedger> {
        self.ledger.lock().expect("ledger lock is not poisoned")
    }
}

impl BridgeRpcBackend for LeasingBackend {
    fn snapshot(
        &mut self,
        _context: BackendRequestContext<'_>,
    ) -> Result<BridgeSnapshot, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn integration_view(
        &mut self,
        _context: BackendRequestContext<'_>,
    ) -> Result<IntegrationView, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn acquire_lease(
        &mut self,
        _context: BackendRequestContext<'_>,
        request: LeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        if self.refusing {
            return Err(RpcFailure::internal());
        }
        let mut ledger = self.ledger();
        if let Some((owner, _)) = &ledger.owner {
            return Ok(LeaseResult::Conflict(LeaseConflict {
                conflicting_client: owner.clone(),
                conflicting_resource: route_resource(),
            }));
        }
        ledger.acquires += 1;
        ledger.next_lease += 1;
        let lease_id =
            LeaseId::try_from(format!("lease-{}", ledger.next_lease)).expect("lease id is valid");
        ledger.owner = Some((request.client_id.clone(), lease_id.clone()));
        Ok(grant(request.client_id, lease_id, LeaseState::Granted))
    }

    fn renew_lease(
        &mut self,
        _context: BackendRequestContext<'_>,
        request: RenewLeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        let mut ledger = self.ledger();
        if ledger.refuse_renewal || !ledger.owns(&request.client_id, &request.lease_id) {
            return Ok(rejected());
        }
        ledger.renewals += 1;
        Ok(grant(
            request.client_id,
            request.lease_id,
            LeaseState::Renewed,
        ))
    }

    fn release_lease(
        &mut self,
        _context: BackendRequestContext<'_>,
        request: ReleaseLeaseRequest,
    ) -> Result<LeaseResult, RpcFailure> {
        let mut ledger = self.ledger();
        if !ledger.owns(&request.client_id, &request.lease_id) {
            return Ok(rejected());
        }
        ledger.releases += 1;
        ledger.owner = None;
        Ok(grant(
            request.client_id,
            request.lease_id,
            LeaseState::Released,
        ))
    }

    fn submit_transaction(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: TransactionRequest,
    ) -> Result<TransactionResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn transaction_outcome(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: TransactionLookup,
    ) -> Result<TransactionResult, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn subscribe(
        &mut self,
        _context: BackendRequestContext<'_>,
        _request: SubscriptionRequest,
    ) -> Result<EventBatch, RpcFailure> {
        Err(RpcFailure::internal())
    }

    fn diagnostics(
        &mut self,
        _context: BackendRequestContext<'_>,
    ) -> Result<DiagnosticSnapshot, RpcFailure> {
        Err(RpcFailure::internal())
    }

    /// Answers with the request report as the matched response when the
    /// request's lease owns the route, and as not sent otherwise, like the
    /// real bridge's ownership check before any SET.
    fn native_transact(
        &mut self,
        _context: BackendRequestContext<'_>,
        request: NativeExchangeRequest,
    ) -> Result<NativeExchangeOutcome, RpcFailure> {
        let mut ledger = self.ledger();
        ledger.transacts += 1;
        let result = if ledger.owns(&request.client_id, &request.lease_id) {
            NativeExchangeResult::MatchedResponse(NativeExchangeMatchedResponse {
                response: request.report,
            })
        } else {
            NativeExchangeResult::NotSent(NativeExchangeNotSent {
                error_kind: ProtocolErrorKind::OwnershipConflict,
                finding_id: finding(),
            })
        };
        Ok(NativeExchangeOutcome {
            request_id: request.request_id,
            route_token: request.route_token,
            result,
        })
    }

    /// Leaves the ledger alone, so a test sees only the leases a client gave
    /// back itself.
    fn disconnect(&mut self, _session: &AuthorizedSession) -> Result<(), RpcFailure> {
        Ok(())
    }
}
