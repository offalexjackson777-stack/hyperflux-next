// SPDX-License-Identifier: GPL-2.0-only

use crate::endpoint::RAZER_STATUS_TIMEOUT;
use crate::status::{
    RelayPublicationState, RelayRouteStatus, RelayRouteStatusIdentity, RelayStatusWriter,
};
use crate::{
    BatteryExchangeDiagnostic, DirectRouteScanner, DirectRouteSnapshot, DirectRouteTransition,
    FeatureEndpoint, FeatureEndpointState, NativeBridge, NativeBridgeError, NativeCommandPolicy,
    NativeRouteCatalog, NativeRouteMonitor, NativeRouteRecord, PublicationFailureDisposition,
    PublicationIdentityFailure, RouteAction, RouteIdentity, RouteSupervisor, SerialFingerprint,
    UHID_EVENT_BYTES, UhidEvent, UhidReply, VirtualDeviceSpec,
};
use hfx_domain::EventBatchLimit;
use hfx_protocol::{EventCursor, NativeRoute};
use hfx_sdk::EventSubscription;
use rustix::fs::{OFlags, fcntl_getfl, fcntl_setfl};
use std::collections::{BTreeMap, VecDeque};
use std::fmt;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

const WATCH_INTERVAL: Duration = Duration::from_millis(100);
const STARTUP_BRIDGE_ATTEMPTS: usize = 25;
const STARTUP_BRIDGE_RETRY_INTERVAL: Duration = Duration::from_millis(100);
const DIRECT_SCAN_INTERVAL: Duration = Duration::from_millis(250);
const ROUTE_IDENTITY_STABILITY_INTERVAL: Duration = Duration::from_millis(250);
const ROUTE_IDENTITY_FAST_RETRY: Duration = Duration::from_millis(100);
const ROUTE_IDENTITY_FAST_ATTEMPTS: u32 = 5;
const MAX_ROUTE_IDENTITY_ATTEMPTS: u32 = u16::MAX as u32;
const UHID_START_TIMEOUT: Duration = Duration::from_secs(10);
const IDLE_INTERVAL: Duration = Duration::from_millis(10);
const MAX_PENDING_REPLIES: usize = 8;
const WATCH_BATCH_SIZE: u16 = 32;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RelayPaths {
    pub bridge_socket: PathBuf,
    pub uhid_device: PathBuf,
    pub hid_driver_root: PathBuf,
    pub status_file: PathBuf,
}

impl RelayPaths {
    #[must_use]
    pub fn linux() -> Self {
        Self {
            bridge_socket: PathBuf::from(hfx_runtime::BRIDGE_SOCKET_PATH),
            uhid_device: PathBuf::from("/dev/uhid"),
            hid_driver_root: PathBuf::from("/sys/bus/hid/drivers"),
            status_file: PathBuf::from("/run/hyperflux-next-openrazer-relay/status.json"),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RelayBridgeOperation {
    MonitorConnect,
    Enumerate,
    Watch,
    RouteConnect,
}

impl fmt::Display for RelayBridgeOperation {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::MonitorConnect => "monitor connection",
            Self::Enumerate => "route enumeration",
            Self::Watch => "route watch",
            Self::RouteConnect => "device-route connection",
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RelayRuntimeError {
    Catalog,
    Bridge {
        operation: RelayBridgeOperation,
        source: NativeBridgeError,
    },
    Supervisor,
    UhidOpen,
    UhidRead,
    UhidWrite,
    UhidCodec,
    Endpoint,
    ReplyQueue,
    InvalidBatchLimit,
    InstanceExhausted,
    Status,
}

impl fmt::Display for RelayRuntimeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Catalog => "OpenRazer native-route catalog is invalid",
            Self::Bridge { operation, source } => {
                return write!(
                    formatter,
                    "HyperFlux native-route {operation} failed: {source}"
                );
            }
            Self::Supervisor => "native-route supervisor rejected bridge state",
            Self::UhidOpen => "feature-only UHID endpoint could not be opened",
            Self::UhidRead => "feature-only UHID event could not be read",
            Self::UhidWrite => "feature-only UHID reply could not be written",
            Self::UhidCodec => "feature-only UHID event or reply is invalid",
            Self::Endpoint => "feature-only UHID request sequence is invalid",
            Self::ReplyQueue => "feature-only UHID reply queue exceeded its bound",
            Self::InvalidBatchLimit => "native-route watch batch limit is invalid",
            Self::InstanceExhausted => "native-route endpoint instance space is exhausted",
            Self::Status => "relay status projection could not be published",
        })
    }
}

impl std::error::Error for RelayRuntimeError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Bridge { source, .. } => Some(source),
            Self::Catalog
            | Self::Supervisor
            | Self::UhidOpen
            | Self::UhidRead
            | Self::UhidWrite
            | Self::UhidCodec
            | Self::Endpoint
            | Self::ReplyQueue
            | Self::InvalidBatchLimit
            | Self::InstanceExhausted
            | Self::Status => None,
        }
    }
}

fn bridge_error(operation: RelayBridgeOperation, source: NativeBridgeError) -> RelayRuntimeError {
    RelayRuntimeError::Bridge { operation, source }
}

struct PendingWrite {
    bytes: Vec<u8>,
    offset: usize,
}

pub(crate) struct RunningEndpoint<B: crate::BridgeExchange> {
    file: File,
    endpoint: Option<FeatureEndpoint<B>>,
    pending: VecDeque<PendingWrite>,
    last_battery_diagnostic: Option<BatteryExchangeDiagnostic>,
    destroyed: bool,
    created_at: Instant,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum RunningEndpointStep {
    Running,
    RetainedRouteDeparture,
}

impl<B: crate::BridgeExchange> RunningEndpoint<B> {
    pub(crate) fn create(
        paths: &RelayPaths,
        bridge: B,
        device: VirtualDeviceSpec,
        command_policy: NativeCommandPolicy,
    ) -> Result<Self, RelayRuntimeError> {
        let mut file = open_uhid(&paths.uhid_device)?;
        write_blocking(&mut file, &UhidReply::Create(device))?;
        let flags = fcntl_getfl(&file).map_err(|_| RelayRuntimeError::UhidOpen)?;
        fcntl_setfl(&file, flags | OFlags::NONBLOCK).map_err(|_| RelayRuntimeError::UhidOpen)?;
        Ok(Self {
            file,
            endpoint: Some(FeatureEndpoint::new(bridge, command_policy)),
            pending: VecDeque::new(),
            last_battery_diagnostic: None,
            destroyed: false,
            created_at: Instant::now(),
        })
    }

    pub(crate) fn step(&mut self) -> Result<RunningEndpointStep, RelayRuntimeError> {
        self.flush()?;
        if self.departure_reply_flushed() {
            return Ok(RunningEndpointStep::RetainedRouteDeparture);
        }
        let mut bytes = [0_u8; UHID_EVENT_BYTES];
        loop {
            match self.file.read(&mut bytes) {
                Ok(0) => return Err(RelayRuntimeError::UhidRead),
                Ok(count) => {
                    let event = UhidEvent::decode(&bytes[..count])
                        .map_err(|_| RelayRuntimeError::UhidCodec)?;
                    self.process_event(event)?;
                    if self.endpoint().retained_route_departure_ready() {
                        break;
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
                Err(_) => return Err(RelayRuntimeError::UhidRead),
            }
        }
        self.flush()?;
        Ok(if self.departure_reply_flushed() {
            RunningEndpointStep::RetainedRouteDeparture
        } else {
            RunningEndpointStep::Running
        })
    }

    fn process_event(&mut self, event: UhidEvent) -> Result<(), RelayRuntimeError> {
        if let Some(reply) = self
            .endpoint_mut()
            .handle(event)
            .map_err(|_| RelayRuntimeError::Endpoint)?
        {
            self.enqueue(&reply)?;
        }
        self.report_battery_diagnostic();
        Ok(())
    }

    fn report_battery_diagnostic(&mut self) {
        let Some(diagnostic) = self.endpoint_mut().take_battery_diagnostic() else {
            return;
        };
        if self.last_battery_diagnostic == Some(diagnostic) {
            return;
        }
        self.last_battery_diagnostic = Some(diagnostic);
        match diagnostic {
            BatteryExchangeDiagnostic::Matched {
                transaction_id,
                status,
                raw_value,
                attempts,
            } => eprintln!(
                "HFX-OPENRAZER-RELAY-BATTERY tx={transaction_id:02x} certainty=matched status={status:02x} raw={raw_value} attempts={attempts}"
            ),
            BatteryExchangeDiagnostic::NotSent {
                transaction_id,
                attempts,
            } => eprintln!(
                "HFX-OPENRAZER-RELAY-BATTERY tx={transaction_id:02x} certainty=not-sent status=none raw=none attempts={attempts}"
            ),
            BatteryExchangeDiagnostic::MayHaveBeenSent {
                transaction_id,
                attempts,
            } => eprintln!(
                "HFX-OPENRAZER-RELAY-BATTERY tx={transaction_id:02x} certainty=may-have-been-sent status=none raw=none attempts={attempts}"
            ),
        }
    }

    fn enqueue(&mut self, reply: &UhidReply) -> Result<(), RelayRuntimeError> {
        if self.pending.len() >= MAX_PENDING_REPLIES {
            return Err(RelayRuntimeError::ReplyQueue);
        }
        self.pending.push_back(PendingWrite {
            bytes: reply.encode().map_err(|_| RelayRuntimeError::UhidCodec)?,
            offset: 0,
        });
        Ok(())
    }

    fn flush(&mut self) -> Result<(), RelayRuntimeError> {
        while let Some(write) = self.pending.front_mut() {
            match self.file.write(&write.bytes[write.offset..]) {
                Ok(0) => return Err(RelayRuntimeError::UhidWrite),
                Ok(count) => {
                    write.offset += count;
                    if write.offset == write.bytes.len() {
                        self.pending.pop_front();
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
                Err(_) => return Err(RelayRuntimeError::UhidWrite),
            }
        }
        Ok(())
    }

    pub(crate) fn destroy(mut self) {
        self.destroy_inner();
    }

    pub(crate) fn destroy_into_bridge(mut self) -> B {
        self.destroy_inner();
        self.endpoint
            .take()
            .expect("running endpoint retains its bridge until withdrawal")
            .into_bridge()
    }

    pub(crate) fn bridge_mut(&mut self) -> &mut B {
        self.endpoint_mut().bridge_mut()
    }

    pub(crate) fn state(&self) -> FeatureEndpointState {
        self.endpoint().state()
    }

    fn start_timed_out(&self, now: Instant) -> bool {
        self.state() == FeatureEndpointState::Created
            && now.duration_since(self.created_at) >= UHID_START_TIMEOUT
    }

    fn destroy_inner(&mut self) {
        if self.destroyed {
            return;
        }
        self.destroyed = true;
        if let Some(endpoint) = self.endpoint.as_mut() {
            endpoint.prepare_withdrawal();
        }
        let _ = self.flush();
        if let Ok(flags) = fcntl_getfl(&self.file) {
            let _ = fcntl_setfl(&self.file, flags & !OFlags::NONBLOCK);
        }
        let _ = write_blocking(&mut self.file, &UhidReply::Destroy);
    }

    fn endpoint(&self) -> &FeatureEndpoint<B> {
        self.endpoint
            .as_ref()
            .expect("running endpoint retains its feature endpoint")
    }

    fn endpoint_mut(&mut self) -> &mut FeatureEndpoint<B> {
        self.endpoint
            .as_mut()
            .expect("running endpoint retains its feature endpoint")
    }

    fn departure_reply_flushed(&self) -> bool {
        self.pending.is_empty() && self.endpoint().retained_route_departure_ready()
    }
}

struct ManagedRoute {
    route: NativeRoute,
    record: NativeRouteRecord,
    device: VirtualDeviceSpec,
    serial_fingerprint: Option<SerialFingerprint>,
    publication: PendingPublication<NativeBridge>,
    endpoint: Option<RunningEndpoint<NativeBridge>>,
    last_diagnostic: Option<DiagnosticKey>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct DiagnosticKey {
    state: RelayPublicationState,
    failure: Option<PublicationIdentityFailure>,
    retry_tier: u8,
}

#[derive(Clone, Debug)]
/// Holds a qualified route privately until two source-derived serial reads,
/// separated by the stability interval, agree on one child identity.
struct RoutePublicationGate {
    pass_one: Option<SerialFingerprint>,
    attempts: u32,
    failure: Option<PublicationIdentityFailure>,
    blocked: bool,
    next_probe: Instant,
    next_delay: Duration,
}

struct PendingPublication<B: crate::BridgeExchange> {
    bridge: Option<B>,
    gate: RoutePublicationGate,
    ready_fingerprint: Option<SerialFingerprint>,
    direct_suppressed: bool,
    terminal_failure: Option<PublicationIdentityFailure>,
}

impl RoutePublicationGate {
    fn new(now: Instant) -> Self {
        Self {
            pass_one: None,
            attempts: 0,
            failure: None,
            blocked: false,
            next_probe: now,
            next_delay: Duration::ZERO,
        }
    }

    fn due(&self, now: Instant) -> bool {
        !self.blocked && now >= self.next_probe
    }

    fn observe(
        &mut self,
        result: Result<SerialFingerprint, PublicationIdentityFailure>,
        now: Instant,
    ) -> Option<SerialFingerprint> {
        match result {
            Ok(identity) => {
                self.failure = None;
                if let Some(pass_one) = self.pass_one.take() {
                    if pass_one == identity {
                        self.next_probe = now;
                        self.next_delay = Duration::ZERO;
                        return Some(identity);
                    }
                    self.fail(PublicationIdentityFailure::IdentityMismatch, now);
                } else {
                    self.pass_one = Some(identity);
                    self.next_probe = now + ROUTE_IDENTITY_STABILITY_INTERVAL;
                    self.next_delay = ROUTE_IDENTITY_STABILITY_INTERVAL;
                }
            }
            Err(failure) => self.fail(failure, now),
        }
        None
    }

    fn fail(&mut self, failure: PublicationIdentityFailure, now: Instant) {
        self.pass_one = None;
        self.attempts = self
            .attempts
            .saturating_add(1)
            .min(MAX_ROUTE_IDENTITY_ATTEMPTS);
        self.failure = Some(failure);
        self.blocked = failure.disposition() == PublicationFailureDisposition::Blocked;
        self.next_delay = retry_delay(self.attempts);
        self.next_probe = now + self.next_delay;
    }

    fn state(&self) -> RelayPublicationState {
        if self.blocked {
            RelayPublicationState::Blocked
        } else if self.failure.is_some() {
            RelayPublicationState::BackingOff
        } else {
            RelayPublicationState::Pending
        }
    }

    fn mark_completed(&mut self) {
        self.pass_one = None;
        self.attempts = 0;
        self.failure = None;
        self.blocked = false;
        self.next_delay = Duration::ZERO;
        self.next_probe = Instant::now();
    }

    fn next_retry_ms(&self) -> u32 {
        let milliseconds = self.next_delay.as_millis();
        u32::try_from(milliseconds.min(30_000)).unwrap_or(30_000)
    }
}

fn retry_delay(attempt: u32) -> Duration {
    match attempt {
        0..=ROUTE_IDENTITY_FAST_ATTEMPTS => ROUTE_IDENTITY_FAST_RETRY,
        6 => Duration::from_secs(1),
        7 => Duration::from_secs(2),
        8 => Duration::from_secs(4),
        9 => Duration::from_secs(8),
        10 => Duration::from_secs(16),
        _ => Duration::from_secs(30),
    }
}

impl<B: crate::BridgeExchange> PendingPublication<B> {
    fn new(bridge: B, now: Instant) -> Self {
        Self {
            bridge: Some(bridge),
            gate: RoutePublicationGate::new(now),
            ready_fingerprint: None,
            direct_suppressed: false,
            terminal_failure: None,
        }
    }

    fn retrying(bridge: B, failure: PublicationIdentityFailure, now: Instant) -> Self {
        let mut pending = Self::new(bridge, now);
        pending.gate.fail(failure, now);
        pending
    }

    fn probe(&mut self, record: &NativeRouteRecord) {
        let now = Instant::now();
        if self.bridge.is_none()
            || self.ready_fingerprint.is_some()
            || self.direct_suppressed
            || self.terminal_failure.is_some()
            || !self.gate.due(now)
        {
            return;
        }
        let bridge = self
            .bridge
            .as_mut()
            .expect("pending bridge was checked above");
        let result = record.publication_identity.query(bridge);
        let Some(fingerprint) = self.gate.observe(result, now) else {
            return;
        };
        self.ready_fingerprint = Some(fingerprint);
    }

    fn ready(&self) -> bool {
        self.ready_fingerprint.is_some()
            && self.bridge.is_some()
            && !self.direct_suppressed
            && self.terminal_failure.is_none()
    }

    fn complete(
        &mut self,
        paths: &RelayPaths,
        direct: &DirectRouteSnapshot,
        record: &NativeRouteRecord,
        device: &VirtualDeviceSpec,
        serial_fingerprint: &mut Option<SerialFingerprint>,
        endpoint: &mut Option<RunningEndpoint<B>>,
    ) {
        let Some(fingerprint) = self.ready_fingerprint.take() else {
            return;
        };
        *serial_fingerprint = Some(fingerprint.clone());
        let bridge = self
            .bridge
            .take()
            .expect("ready publication retains its bridge");
        if direct.suppresses(&record.profile_id, &fingerprint) {
            self.direct_suppressed = true;
            self.gate.mark_completed();
            return;
        }
        match RunningEndpoint::create(paths, bridge, device.clone(), record.command_policy.clone())
        {
            Ok(running) => {
                *endpoint = Some(running);
                self.gate.mark_completed();
            }
            Err(error) => self.terminal_failure = Some(endpoint_failure(&error)),
        }
    }

    fn direct_scan_failed(&mut self) {
        if self.ready_fingerprint.take().is_some() {
            self.gate.fail(
                PublicationIdentityFailure::DirectStateUnavailable,
                Instant::now(),
            );
        }
    }

    fn block(&mut self, failure: PublicationIdentityFailure) {
        self.bridge.take();
        self.ready_fingerprint = None;
        self.terminal_failure = Some(failure);
        self.direct_suppressed = false;
    }

    fn status(
        &self,
        endpoint: Option<&RunningEndpoint<NativeBridge>>,
    ) -> (
        RelayPublicationState,
        Option<PublicationIdentityFailure>,
        u32,
        u32,
    ) {
        if let Some(endpoint) = endpoint {
            return (
                if endpoint.state() == FeatureEndpointState::Started {
                    RelayPublicationState::KernelStarted
                } else {
                    RelayPublicationState::CreateSent
                },
                None,
                0,
                0,
            );
        }
        if self.direct_suppressed {
            return (RelayPublicationState::DirectSuppressed, None, 0, 0);
        }
        if let Some(failure) = self.terminal_failure {
            return (RelayPublicationState::Blocked, Some(failure), 1, 0);
        }
        (
            self.gate.state(),
            self.gate.failure,
            self.gate.attempts,
            self.gate.next_retry_ms(),
        )
    }
}

fn endpoint_failure(error: &RelayRuntimeError) -> PublicationIdentityFailure {
    match error {
        RelayRuntimeError::UhidOpen => PublicationIdentityFailure::UhidOpen,
        RelayRuntimeError::UhidRead => PublicationIdentityFailure::UhidRead,
        RelayRuntimeError::UhidWrite => PublicationIdentityFailure::UhidWrite,
        RelayRuntimeError::UhidCodec
        | RelayRuntimeError::Endpoint
        | RelayRuntimeError::ReplyQueue => PublicationIdentityFailure::UhidProtocol,
        _ => PublicationIdentityFailure::KernelBinding,
    }
}

impl ManagedRoute {
    fn start(
        paths: &RelayPaths,
        route: NativeRoute,
        record: NativeRouteRecord,
        device: VirtualDeviceSpec,
        instance: u32,
    ) -> Result<Self, RelayRuntimeError> {
        let bridge = NativeBridge::connect(&paths.bridge_socket, route.clone(), instance)
            .map_err(|source| bridge_error(RelayBridgeOperation::RouteConnect, source))?;
        Ok(Self {
            route,
            record,
            device,
            serial_fingerprint: None,
            publication: PendingPublication::new(bridge, Instant::now()),
            endpoint: None,
            last_diagnostic: None,
        })
    }

    fn step(&mut self) {
        let mut failure = None;
        let mut retained_route_departure = false;
        if let Some(endpoint) = &mut self.endpoint {
            match endpoint.step() {
                Ok(RunningEndpointStep::RetainedRouteDeparture) => {
                    retained_route_departure = true;
                }
                Ok(RunningEndpointStep::Running) => {
                    if endpoint.start_timed_out(Instant::now()) {
                        failure = Some(PublicationIdentityFailure::UhidStartTimeout);
                    } else if endpoint.state() == FeatureEndpointState::Stopped {
                        failure = Some(PublicationIdentityFailure::KernelBinding);
                    }
                }
                Err(error) => failure = Some(endpoint_failure(&error)),
            }
        }
        if retained_route_departure {
            recycle_retained_route(
                self.endpoint
                    .take()
                    .expect("departure retains the running endpoint"),
                &mut self.publication,
                &mut self.serial_fingerprint,
                Instant::now(),
            );
            return;
        }
        if let Some(failure) = failure {
            if let Some(endpoint) = self.endpoint.take() {
                endpoint.destroy();
            }
            self.publication.block(failure);
        }
    }

    fn probe_publication(&mut self) {
        if self.endpoint.is_none() {
            self.publication.probe(&self.record);
        }
    }

    fn publication_ready(&self) -> bool {
        self.endpoint.is_none() && self.publication.ready()
    }

    fn complete_publication(&mut self, paths: &RelayPaths, direct: &DirectRouteSnapshot) {
        self.publication.complete(
            paths,
            direct,
            &self.record,
            &self.device,
            &mut self.serial_fingerprint,
            &mut self.endpoint,
        );
    }

    fn direct_scan_failed(&mut self) {
        self.publication.direct_scan_failed();
    }

    fn refresh_direct(
        &mut self,
        paths: &RelayPaths,
        direct: &DirectRouteSnapshot,
        next_instance: &mut u32,
    ) -> Result<(), RelayRuntimeError> {
        match direct.transition(
            &self.record.profile_id,
            self.serial_fingerprint.as_ref(),
            self.endpoint.is_some(),
        ) {
            DirectRouteTransition::Suppress => {
                if let Some(endpoint) = self.endpoint.take() {
                    endpoint.destroy();
                    self.publication.direct_suppressed = true;
                }
            }
            DirectRouteTransition::Expose => {
                if self.publication.direct_suppressed {
                    let instance = take_instance(next_instance)?;
                    let bridge =
                        NativeBridge::connect(&paths.bridge_socket, self.route.clone(), instance)
                            .map_err(|source| {
                            bridge_error(RelayBridgeOperation::RouteConnect, source)
                        })?;
                    self.publication = PendingPublication::new(bridge, Instant::now());
                    self.serial_fingerprint = None;
                }
            }
            DirectRouteTransition::Keep => {}
        }
        Ok(())
    }

    fn status(&mut self) -> RelayRouteStatus {
        let (state, failure, attempt, next_retry_ms) =
            self.publication.status(self.endpoint.as_ref());
        let status = RelayRouteStatus::new(
            RelayRouteStatusIdentity {
                profile_id: self.record.profile_id.clone(),
                vendor_id: self.record.vendor_id.get(),
                product_id: self.record.wireless_product_id.get(),
                device_kind: match self.record.device_kind {
                    hfx_domain::DeviceKind::Keyboard => "keyboard",
                    hfx_domain::DeviceKind::Mouse => "mouse",
                    _ => "unknown",
                },
            },
            state,
            failure,
            attempt,
            next_retry_ms,
        );
        let retry_tier = match next_retry_ms {
            0 => 0,
            1..=100 => 1,
            101..=1_000 => 2,
            1_001..=2_000 => 3,
            2_001..=4_000 => 4,
            4_001..=8_000 => 5,
            8_001..=16_000 => 6,
            _ => 7,
        };
        let key = DiagnosticKey {
            state,
            failure,
            retry_tier,
        };
        if self.last_diagnostic != Some(key) {
            status.log_transition();
            self.last_diagnostic = Some(key);
        }
        status
    }

    fn destroy(mut self) {
        self.publication.bridge.take();
        if let Some(endpoint) = self.endpoint.take() {
            endpoint.destroy();
        }
    }
}

fn recycle_retained_route<B: crate::BridgeExchange>(
    endpoint: RunningEndpoint<B>,
    publication: &mut PendingPublication<B>,
    serial_fingerprint: &mut Option<SerialFingerprint>,
    now: Instant,
) {
    let bridge = endpoint.destroy_into_bridge();
    *serial_fingerprint = None;
    *publication = PendingPublication::retrying(
        bridge,
        PublicationIdentityFailure::Status(RAZER_STATUS_TIMEOUT),
        now,
    );
}

impl<B: crate::BridgeExchange> Drop for RunningEndpoint<B> {
    fn drop(&mut self) {
        self.destroy_inner();
    }
}

/// Runs one source-bound `OpenRazer` native-route relay session.
///
/// # Errors
///
/// Fails closed on bridge, catalog, supervisor, or UHID errors. Dropping the
/// session destroys every virtual endpoint. Route leases exist only around an
/// individual native Feature exchange.
pub fn run_relay(paths: &RelayPaths, termination: &AtomicBool) -> Result<(), RelayRuntimeError> {
    let catalog = NativeRouteCatalog::load().map_err(|_| RelayRuntimeError::Catalog)?;
    let mut direct_scanner = DirectRouteScanner::new(paths.hid_driver_root.clone());
    let Some(mut monitor) = connect_startup_monitor(&paths.bridge_socket, termination)
        .map_err(|source| bridge_error(RelayBridgeOperation::MonitorConnect, source))?
    else {
        return Ok(());
    };
    let inventory = monitor
        .enumerate()
        .map_err(|source| bridge_error(RelayBridgeOperation::Enumerate, source))?;
    let mut supervisor = RouteSupervisor::default();
    let actions = supervisor
        .reconcile(&inventory, &catalog)
        .map_err(|_| RelayRuntimeError::Supervisor)?;
    let mut routes = BTreeMap::new();
    let mut next_instance = 1_u32;
    apply_actions(paths, actions, &mut routes, &mut next_instance)?;
    let mut status_writer = RelayStatusWriter::new(paths.status_file.clone());
    publish_status(&mut status_writer, &mut routes)?;

    let batch_limit = EventBatchLimit::try_from(WATCH_BATCH_SIZE)
        .map_err(|_| RelayRuntimeError::InvalidBatchLimit)?;
    let mut subscription_id = None;
    let mut cursor = Some(inventory.cursor);
    let mut next_watch = Instant::now();
    let mut next_direct_scan = Instant::now() + DIRECT_SCAN_INTERVAL;

    while !termination.load(Ordering::Relaxed) {
        if Instant::now() >= next_watch {
            loop {
                let batch = monitor
                    .watch(EventSubscription {
                        subscription_id: subscription_id.clone(),
                        expected_cursor: cursor.clone(),
                        max_events: batch_limit,
                    })
                    .map_err(|source| bridge_error(RelayBridgeOperation::Watch, source))?;
                if batch.cursor_gap {
                    let inventory = monitor
                        .enumerate()
                        .map_err(|source| bridge_error(RelayBridgeOperation::Enumerate, source))?;
                    let actions = supervisor
                        .reconcile(&inventory, &catalog)
                        .map_err(|_| RelayRuntimeError::Supervisor)?;
                    apply_actions(paths, actions, &mut routes, &mut next_instance)?;
                    subscription_id = None;
                    cursor = Some(inventory.cursor);
                    break;
                }
                subscription_id = Some(batch.subscription_id);
                cursor = Some(batch.next_cursor);
                for event in &batch.events {
                    let actions = supervisor
                        .apply_event(event, &catalog)
                        .map_err(|_| RelayRuntimeError::Supervisor)?;
                    apply_actions(paths, actions, &mut routes, &mut next_instance)?;
                }
                if !batch.has_more {
                    break;
                }
            }
            next_watch = Instant::now() + WATCH_INTERVAL;
        }
        if Instant::now() >= next_direct_scan {
            if let Ok(snapshot) = direct_scanner.scan(&catalog) {
                refresh_direct_routes(paths, &mut routes, &snapshot, &mut next_instance)?;
            }
            next_direct_scan = Instant::now() + DIRECT_SCAN_INTERVAL;
        }
        for route in routes.values_mut() {
            route.step();
        }
        for route in routes.values_mut() {
            route.probe_publication();
        }
        if let Some(authoritative_cursor) = complete_ready_publications(
            paths,
            &mut monitor,
            &mut supervisor,
            &catalog,
            &mut direct_scanner,
            &mut routes,
            &mut next_instance,
        )? {
            subscription_id = None;
            cursor = Some(authoritative_cursor);
            next_watch = Instant::now() + WATCH_INTERVAL;
        }
        publish_status(&mut status_writer, &mut routes)?;
        thread::sleep(IDLE_INTERVAL);
    }
    destroy_all(&mut routes);
    Ok(())
}

fn complete_ready_publications(
    paths: &RelayPaths,
    monitor: &mut NativeRouteMonitor,
    supervisor: &mut RouteSupervisor,
    catalog: &NativeRouteCatalog,
    direct_scanner: &mut DirectRouteScanner,
    routes: &mut BTreeMap<RouteIdentity, ManagedRoute>,
    next_instance: &mut u32,
) -> Result<Option<EventCursor>, RelayRuntimeError> {
    if !routes.values().any(ManagedRoute::publication_ready) {
        return Ok(None);
    }
    // A second serial match is only preparation. Refresh every authoritative
    // route field before CREATE so queued lifecycle or epoch changes win.
    let inventory = monitor
        .enumerate()
        .map_err(|source| bridge_error(RelayBridgeOperation::Enumerate, source))?;
    let actions = supervisor
        .reconcile(&inventory, catalog)
        .map_err(|_| RelayRuntimeError::Supervisor)?;
    apply_actions(paths, actions, routes, next_instance)?;

    let ready = routes
        .iter()
        .filter(|(_, route)| route.publication_ready())
        .map(|(identity, route)| (identity.clone(), route.record.profile_id.clone()))
        .collect::<Vec<_>>();
    for (identity, profile_id) in ready {
        let direct = direct_scanner.scan_profile(catalog, &profile_id);
        let Some(route) = routes.get_mut(&identity) else {
            continue;
        };
        if !route.publication_ready() {
            continue;
        }
        match direct {
            Ok(snapshot) => route.complete_publication(paths, &snapshot),
            Err(_) => route.direct_scan_failed(),
        }
    }
    Ok(Some(inventory.cursor))
}

fn publish_status(
    writer: &mut RelayStatusWriter,
    routes: &mut BTreeMap<RouteIdentity, ManagedRoute>,
) -> Result<(), RelayRuntimeError> {
    writer
        .publish(routes.values_mut().map(ManagedRoute::status).collect())
        .map_err(|_| RelayRuntimeError::Status)
}

fn connect_startup_monitor(
    socket_path: &Path,
    termination: &AtomicBool,
) -> Result<Option<NativeRouteMonitor>, NativeBridgeError> {
    retry_startup_bridge(
        termination,
        STARTUP_BRIDGE_ATTEMPTS,
        || NativeRouteMonitor::connect(socket_path),
        || thread::sleep(STARTUP_BRIDGE_RETRY_INTERVAL),
    )
}

fn retry_startup_bridge<T, C, W>(
    termination: &AtomicBool,
    attempts: usize,
    mut connect: C,
    mut wait: W,
) -> Result<Option<T>, NativeBridgeError>
where
    C: FnMut() -> Result<T, NativeBridgeError>,
    W: FnMut(),
{
    debug_assert!(attempts > 0);
    for attempt in 0..attempts {
        if termination.load(Ordering::Relaxed) {
            return Ok(None);
        }
        match connect() {
            Ok(value) => return Ok(Some(value)),
            Err(NativeBridgeError::SocketUnavailable) => {}
            Err(error) => return Err(error),
        }
        if attempt + 1 < attempts {
            wait();
        }
    }
    Err(NativeBridgeError::SocketUnavailable)
}

fn apply_actions(
    paths: &RelayPaths,
    actions: Vec<RouteAction>,
    routes: &mut BTreeMap<RouteIdentity, ManagedRoute>,
    next_instance: &mut u32,
) -> Result<(), RelayRuntimeError> {
    for action in actions {
        match action {
            RouteAction::Stop { identity } => {
                if let Some(route) = routes.remove(&identity) {
                    eprintln!(
                        "HFX-OPENRAZER-RELAY-STATE profile={} vid={:04x} pid={:04x} kind={} state=withdrawn stage=none class=none status=none attempt=0 retry_ms=0",
                        route.record.profile_id,
                        route.record.vendor_id.get(),
                        route.record.wireless_product_id.get(),
                        match route.record.device_kind {
                            hfx_domain::DeviceKind::Keyboard => "keyboard",
                            hfx_domain::DeviceKind::Mouse => "mouse",
                            _ => "unknown",
                        },
                    );
                    route.destroy();
                }
            }
            RouteAction::Start {
                identity,
                route,
                record,
                device,
            } => {
                let instance = take_instance(next_instance)?;
                let managed = ManagedRoute::start(paths, *route, *record, *device, instance)?;
                if let Some(previous) = routes.insert(identity, managed) {
                    previous.destroy();
                }
            }
        }
    }
    Ok(())
}

fn refresh_direct_routes(
    paths: &RelayPaths,
    routes: &mut BTreeMap<RouteIdentity, ManagedRoute>,
    direct: &DirectRouteSnapshot,
    next_instance: &mut u32,
) -> Result<(), RelayRuntimeError> {
    for route in routes.values_mut() {
        route.refresh_direct(paths, direct, next_instance)?;
    }
    Ok(())
}

fn take_instance(next_instance: &mut u32) -> Result<u32, RelayRuntimeError> {
    let instance = *next_instance;
    *next_instance = next_instance
        .checked_add(1)
        .ok_or(RelayRuntimeError::InstanceExhausted)?;
    Ok(instance)
}

fn destroy_all(routes: &mut BTreeMap<RouteIdentity, ManagedRoute>) {
    while let Some((_, route)) = routes.pop_first() {
        route.destroy();
    }
}

fn open_uhid(path: &Path) -> Result<File, RelayRuntimeError> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .map_err(|_| RelayRuntimeError::UhidOpen)
}

fn write_blocking(file: &mut File, reply: &UhidReply) -> Result<(), RelayRuntimeError> {
    let bytes = reply.encode().map_err(|_| RelayRuntimeError::UhidCodec)?;
    file.write_all(&bytes)
        .map_err(|_| RelayRuntimeError::UhidWrite)
}

#[cfg(test)]
mod tests;
