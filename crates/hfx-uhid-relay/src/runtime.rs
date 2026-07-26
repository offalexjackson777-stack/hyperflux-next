// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    BridgeExchangeResult, DirectRouteScanner, DirectRouteSnapshot, DirectRouteTransition,
    FeatureEndpoint, NativeBridge, NativeRouteCatalog, NativeRouteMonitor, NativeRouteRecord,
    RouteAction, RouteIdentity, RouteSupervisor, SerialFingerprint, SerialQuerySpec,
    UHID_EVENT_BYTES, UhidEvent, UhidReply, VirtualDeviceSpec,
};
use hfx_domain::EventBatchLimit;
use hfx_protocol::NativeRoute;
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
const DIRECT_SCAN_INTERVAL: Duration = Duration::from_millis(250);
const SERIAL_QUERY_INTERVAL: Duration = Duration::from_secs(1);
const IDLE_INTERVAL: Duration = Duration::from_millis(10);
const MAX_PENDING_REPLIES: usize = 8;
const WATCH_BATCH_SIZE: u16 = 32;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RelayPaths {
    pub bridge_socket: PathBuf,
    pub uhid_device: PathBuf,
    pub hid_driver_root: PathBuf,
}

impl RelayPaths {
    #[must_use]
    pub fn linux() -> Self {
        Self {
            bridge_socket: PathBuf::from(hfx_runtime::BRIDGE_SOCKET_PATH),
            uhid_device: PathBuf::from("/dev/uhid"),
            hid_driver_root: PathBuf::from("/sys/bus/hid/drivers"),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RelayRuntimeError {
    Catalog,
    Bridge,
    Supervisor,
    UhidOpen,
    UhidRead,
    UhidWrite,
    UhidCodec,
    Endpoint,
    ReplyQueue,
    InvalidBatchLimit,
}

impl fmt::Display for RelayRuntimeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Catalog => "OpenRazer native-route catalog is invalid",
            Self::Bridge => "HyperFlux native-route bridge session failed",
            Self::Supervisor => "native-route supervisor rejected bridge state",
            Self::UhidOpen => "feature-only UHID endpoint could not be opened",
            Self::UhidRead => "feature-only UHID event could not be read",
            Self::UhidWrite => "feature-only UHID reply could not be written",
            Self::UhidCodec => "feature-only UHID event or reply is invalid",
            Self::Endpoint => "feature-only UHID request sequence is invalid",
            Self::ReplyQueue => "feature-only UHID reply queue exceeded its bound",
            Self::InvalidBatchLimit => "native-route watch batch limit is invalid",
        })
    }
}

impl std::error::Error for RelayRuntimeError {}

struct PendingWrite {
    bytes: Vec<u8>,
    offset: usize,
}

pub(crate) struct RunningEndpoint<B: crate::BridgeExchange> {
    file: File,
    endpoint: FeatureEndpoint<B>,
    pending: VecDeque<PendingWrite>,
    destroyed: bool,
}

impl<B: crate::BridgeExchange> RunningEndpoint<B> {
    pub(crate) fn create(
        paths: &RelayPaths,
        bridge: B,
        device: VirtualDeviceSpec,
    ) -> Result<Self, RelayRuntimeError> {
        let mut file = open_uhid(&paths.uhid_device)?;
        write_blocking(&mut file, &UhidReply::Create(device))?;
        let flags = fcntl_getfl(&file).map_err(|_| RelayRuntimeError::UhidOpen)?;
        fcntl_setfl(&file, flags | OFlags::NONBLOCK).map_err(|_| RelayRuntimeError::UhidOpen)?;
        Ok(Self {
            file,
            endpoint: FeatureEndpoint::new(bridge),
            pending: VecDeque::new(),
            destroyed: false,
        })
    }

    pub(crate) fn step(&mut self) -> Result<(), RelayRuntimeError> {
        self.flush()?;
        let mut bytes = [0_u8; UHID_EVENT_BYTES];
        loop {
            match self.file.read(&mut bytes) {
                Ok(0) => return Err(RelayRuntimeError::UhidRead),
                Ok(count) => {
                    let event = UhidEvent::decode(&bytes[..count])
                        .map_err(|_| RelayRuntimeError::UhidCodec)?;
                    if let Some(reply) = self
                        .endpoint
                        .handle(event)
                        .map_err(|_| RelayRuntimeError::Endpoint)?
                    {
                        self.enqueue(&reply)?;
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
                Err(_) => return Err(RelayRuntimeError::UhidRead),
            }
        }
        self.flush()
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

    pub(crate) fn bridge_mut(&mut self) -> &mut B {
        self.endpoint.bridge_mut()
    }

    fn destroy_inner(&mut self) {
        if self.destroyed {
            return;
        }
        self.destroyed = true;
        let _ = self.flush();
        if let Ok(flags) = fcntl_getfl(&self.file) {
            let _ = fcntl_setfl(&self.file, flags & !OFlags::NONBLOCK);
        }
        let _ = write_blocking(&mut self.file, &UhidReply::Destroy);
    }
}

impl RunningEndpoint<NativeBridge> {
    fn query_serial(&mut self, query: &SerialQuerySpec) -> Option<SerialFingerprint> {
        let request = query.request();
        let fingerprint = match self.endpoint.management_exchange(request)? {
            BridgeExchangeResult::MatchedResponse(response) => {
                query.fingerprint_response(&request, &response)
            }
            BridgeExchangeResult::NotSent | BridgeExchangeResult::MayHaveBeenSent => None,
        };
        self.endpoint
            .bridge_mut()
            .take_observed_serial()
            .or(fingerprint)
    }

    fn take_observed_serial(&mut self) -> Option<SerialFingerprint> {
        self.endpoint.bridge_mut().take_observed_serial()
    }
}

struct ManagedRoute {
    route: NativeRoute,
    record: NativeRouteRecord,
    device: VirtualDeviceSpec,
    serial_fingerprint: Option<SerialFingerprint>,
    endpoint: Option<RunningEndpoint<NativeBridge>>,
    next_serial_query: Instant,
}

impl ManagedRoute {
    fn start(
        paths: &RelayPaths,
        route: NativeRoute,
        record: NativeRouteRecord,
        device: VirtualDeviceSpec,
        direct: &DirectRouteSnapshot,
        instance: u32,
    ) -> Result<Self, RelayRuntimeError> {
        let mut bridge = NativeBridge::connect(
            &paths.bridge_socket,
            route.clone(),
            record.serial_query.clone(),
            instance,
        )
        .map_err(|_| RelayRuntimeError::Bridge)?;
        let queried_serial = direct
            .has_profile_candidate(&record.profile_id)
            .then(|| record.serial_query.query(&mut bridge))
            .flatten();
        let serial_fingerprint = bridge.take_observed_serial().or(queried_serial);
        let suppressed = serial_fingerprint
            .as_ref()
            .is_some_and(|fingerprint| direct.suppresses(&record.profile_id, fingerprint));
        let endpoint = if suppressed {
            None
        } else {
            Some(RunningEndpoint::create(paths, bridge, device.clone())?)
        };
        Ok(Self {
            route,
            record,
            device,
            serial_fingerprint,
            endpoint,
            next_serial_query: Instant::now() + SERIAL_QUERY_INTERVAL,
        })
    }

    fn step(&mut self) -> Result<(), RelayRuntimeError> {
        if let Some(endpoint) = &mut self.endpoint {
            endpoint.step()?;
            if self.serial_fingerprint.is_none() {
                self.serial_fingerprint = endpoint.take_observed_serial();
            }
        }
        Ok(())
    }

    fn refresh_direct(
        &mut self,
        paths: &RelayPaths,
        direct: &DirectRouteSnapshot,
        next_instance: &mut u32,
    ) -> Result<(), RelayRuntimeError> {
        if self.serial_fingerprint.is_none()
            && direct.has_profile_candidate(&self.record.profile_id)
            && Instant::now() >= self.next_serial_query
        {
            if let Some(endpoint) = &mut self.endpoint {
                self.serial_fingerprint = endpoint.query_serial(&self.record.serial_query);
            }
            self.next_serial_query = Instant::now() + SERIAL_QUERY_INTERVAL;
        }

        match direct.transition(
            &self.record.profile_id,
            self.serial_fingerprint.as_ref(),
            self.endpoint.is_some(),
        ) {
            DirectRouteTransition::Suppress => {
                if let Some(endpoint) = self.endpoint.take() {
                    endpoint.destroy();
                }
            }
            DirectRouteTransition::Expose => {
                let instance = take_instance(next_instance)?;
                let bridge = NativeBridge::connect(
                    &paths.bridge_socket,
                    self.route.clone(),
                    self.record.serial_query.clone(),
                    instance,
                )
                .map_err(|_| RelayRuntimeError::Bridge)?;
                self.endpoint = Some(RunningEndpoint::create(paths, bridge, self.device.clone())?);
            }
            DirectRouteTransition::Keep => {}
        }
        Ok(())
    }

    fn destroy(mut self) {
        if let Some(endpoint) = self.endpoint.take() {
            endpoint.destroy();
        }
    }
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
    let mut direct_snapshot = direct_scanner.scan(&catalog).unwrap_or_default();
    let mut monitor =
        NativeRouteMonitor::connect(&paths.bridge_socket).map_err(|_| RelayRuntimeError::Bridge)?;
    let inventory = monitor.enumerate().map_err(|_| RelayRuntimeError::Bridge)?;
    let mut supervisor = RouteSupervisor::default();
    let actions = supervisor
        .reconcile(&inventory, &catalog)
        .map_err(|_| RelayRuntimeError::Supervisor)?;
    let mut routes = BTreeMap::new();
    let mut next_instance = 1_u32;
    apply_actions(
        paths,
        actions,
        &mut routes,
        &direct_snapshot,
        &mut next_instance,
    )?;

    let batch_limit = EventBatchLimit::try_from(WATCH_BATCH_SIZE)
        .map_err(|_| RelayRuntimeError::InvalidBatchLimit)?;
    let mut subscription_id = None;
    let mut cursor = Some(inventory.cursor);
    let mut next_watch = Instant::now();
    let mut next_direct_scan = Instant::now() + DIRECT_SCAN_INTERVAL;

    while !termination.load(Ordering::Relaxed) {
        for route in routes.values_mut() {
            route.step()?;
        }
        if Instant::now() >= next_direct_scan {
            if let Ok(snapshot) = direct_scanner.scan(&catalog) {
                direct_snapshot = snapshot;
                refresh_direct_routes(paths, &mut routes, &direct_snapshot, &mut next_instance)?;
            }
            next_direct_scan = Instant::now() + DIRECT_SCAN_INTERVAL;
        }
        if Instant::now() >= next_watch {
            let batch = monitor
                .watch(EventSubscription {
                    subscription_id: subscription_id.clone(),
                    expected_cursor: cursor.clone(),
                    max_events: batch_limit,
                })
                .map_err(|_| RelayRuntimeError::Bridge)?;
            if batch.cursor_gap {
                let inventory = monitor.enumerate().map_err(|_| RelayRuntimeError::Bridge)?;
                let actions = supervisor
                    .reconcile(&inventory, &catalog)
                    .map_err(|_| RelayRuntimeError::Supervisor)?;
                apply_actions(
                    paths,
                    actions,
                    &mut routes,
                    &direct_snapshot,
                    &mut next_instance,
                )?;
                subscription_id = None;
                cursor = Some(inventory.cursor);
            } else {
                for event in &batch.events {
                    let actions = supervisor
                        .apply_event(event, &catalog)
                        .map_err(|_| RelayRuntimeError::Supervisor)?;
                    apply_actions(
                        paths,
                        actions,
                        &mut routes,
                        &direct_snapshot,
                        &mut next_instance,
                    )?;
                }
                subscription_id = Some(batch.subscription_id);
                cursor = Some(batch.next_cursor);
            }
            next_watch = if batch.has_more {
                Instant::now()
            } else {
                Instant::now() + WATCH_INTERVAL
            };
        }
        thread::sleep(IDLE_INTERVAL);
    }
    destroy_all(&mut routes);
    Ok(())
}

fn apply_actions(
    paths: &RelayPaths,
    actions: Vec<RouteAction>,
    routes: &mut BTreeMap<RouteIdentity, ManagedRoute>,
    direct: &DirectRouteSnapshot,
    next_instance: &mut u32,
) -> Result<(), RelayRuntimeError> {
    for action in actions {
        match action {
            RouteAction::Stop { identity } => {
                if let Some(route) = routes.remove(&identity) {
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
                let managed =
                    ManagedRoute::start(paths, *route, *record, *device, direct, instance)?;
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
        .ok_or(RelayRuntimeError::Bridge)?;
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
