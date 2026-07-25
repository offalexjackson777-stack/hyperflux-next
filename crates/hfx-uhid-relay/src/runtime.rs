// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    FeatureEndpoint, NativeBridge, NativeRouteCatalog, NativeRouteMonitor, RouteAction,
    RouteIdentity, RouteSupervisor, UHID_EVENT_BYTES, UhidEvent, UhidReply,
};
use hfx_domain::EventBatchLimit;
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
const IDLE_INTERVAL: Duration = Duration::from_millis(10);
const MAX_PENDING_REPLIES: usize = 8;
const WATCH_BATCH_SIZE: u16 = 32;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RelayPaths {
    pub bridge_socket: PathBuf,
    pub uhid_device: PathBuf,
}

impl RelayPaths {
    #[must_use]
    pub fn linux() -> Self {
        Self {
            bridge_socket: PathBuf::from(hfx_runtime::BRIDGE_SOCKET_PATH),
            uhid_device: PathBuf::from("/dev/uhid"),
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

struct RunningEndpoint {
    file: File,
    endpoint: FeatureEndpoint<NativeBridge>,
    pending: VecDeque<PendingWrite>,
    destroyed: bool,
}

impl RunningEndpoint {
    fn create(
        paths: &RelayPaths,
        route: hfx_protocol::NativeRoute,
        device: crate::VirtualDeviceSpec,
        instance: u32,
    ) -> Result<Self, RelayRuntimeError> {
        let bridge = NativeBridge::connect(&paths.bridge_socket, route, instance)
            .map_err(|_| RelayRuntimeError::Bridge)?;
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

    fn step(&mut self) -> Result<(), RelayRuntimeError> {
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

    fn destroy(mut self) {
        self.destroy_inner();
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

impl Drop for RunningEndpoint {
    fn drop(&mut self) {
        self.destroy_inner();
    }
}

/// Runs one source-bound `OpenRazer` native-route relay session.
///
/// # Errors
///
/// Fails closed on bridge, catalog, supervisor, or UHID errors. Dropping the
/// session destroys every virtual endpoint and releases every route lease.
pub fn run_relay(paths: &RelayPaths, termination: &AtomicBool) -> Result<(), RelayRuntimeError> {
    let catalog = NativeRouteCatalog::load().map_err(|_| RelayRuntimeError::Catalog)?;
    let mut monitor =
        NativeRouteMonitor::connect(&paths.bridge_socket).map_err(|_| RelayRuntimeError::Bridge)?;
    let inventory = monitor.enumerate().map_err(|_| RelayRuntimeError::Bridge)?;
    let mut supervisor = RouteSupervisor::default();
    let actions = supervisor
        .reconcile(&inventory, &catalog)
        .map_err(|_| RelayRuntimeError::Supervisor)?;
    let mut endpoints = BTreeMap::new();
    let mut next_instance = 1_u32;
    apply_actions(paths, actions, &mut endpoints, &mut next_instance)?;

    let batch_limit = EventBatchLimit::try_from(WATCH_BATCH_SIZE)
        .map_err(|_| RelayRuntimeError::InvalidBatchLimit)?;
    let mut subscription_id = None;
    let mut cursor = Some(inventory.cursor);
    let mut next_watch = Instant::now();

    while !termination.load(Ordering::Relaxed) {
        for endpoint in endpoints.values_mut() {
            endpoint.step()?;
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
                apply_actions(paths, actions, &mut endpoints, &mut next_instance)?;
                subscription_id = None;
                cursor = Some(inventory.cursor);
            } else {
                for event in &batch.events {
                    let actions = supervisor
                        .apply_event(event, &catalog)
                        .map_err(|_| RelayRuntimeError::Supervisor)?;
                    apply_actions(paths, actions, &mut endpoints, &mut next_instance)?;
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
    destroy_all(&mut endpoints);
    Ok(())
}

fn apply_actions(
    paths: &RelayPaths,
    actions: Vec<RouteAction>,
    endpoints: &mut BTreeMap<RouteIdentity, RunningEndpoint>,
    next_instance: &mut u32,
) -> Result<(), RelayRuntimeError> {
    for action in actions {
        match action {
            RouteAction::Stop { identity } => {
                if let Some(endpoint) = endpoints.remove(&identity) {
                    endpoint.destroy();
                }
            }
            RouteAction::Start {
                identity,
                route,
                device,
                ..
            } => {
                let instance = *next_instance;
                *next_instance = next_instance
                    .checked_add(1)
                    .ok_or(RelayRuntimeError::Bridge)?;
                let endpoint = RunningEndpoint::create(paths, *route, *device, instance)?;
                if let Some(previous) = endpoints.insert(identity, endpoint) {
                    previous.destroy();
                }
            }
        }
    }
    Ok(())
}

fn destroy_all(endpoints: &mut BTreeMap<RouteIdentity, RunningEndpoint>) {
    while let Some((_, endpoint)) = endpoints.pop_first() {
        endpoint.destroy();
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
