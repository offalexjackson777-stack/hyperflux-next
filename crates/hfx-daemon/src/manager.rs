// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    EndpointCandidate, EndpointDiscovery, EndpointDiscoveryError, PassiveDisposition,
    PassiveObservationTranslator, PassiveTranslationError, ProductionBackend,
    ReceiverIdentityAuthority, ReceiverIdentityError, RestorationScheduleError,
    RestorationScheduler, RestorationTickReport, WriterAuthorityError, derive_capability_digest,
};
use hfx_bridge::{
    GenerationOrchestrationError, LifecycleObservationError, LifecycleObservationOutcome,
    ReceiverDisconnectObservation, ReceiverGenerationObservation, SessionRegistry,
    StableCaptureStatus,
};
use hfx_core::{Clock, LifecycleError, RestorationError};
use hfx_domain::{
    DomainValueError, EvidenceClaimId, EvidenceConfidence, MonotonicMs, PresenceState, ReceiverId,
    ReceiverLifecycleState, SequenceNumber,
};
use hfx_kernel_transport::{
    HFX_UAPI_MAX_OBSERVATIONS, KernelEndpointInfo, KernelObservationReader,
    KernelReceiverTransport, KernelRouteError, KernelSessionMaterial, KernelTransportError,
    KernelTransportErrorKind, LinuxKernelIo,
};
use hfx_profiles::RuntimeProfileCatalog;
use hfx_runtime::{
    BridgeMode, DISPATCHES_PER_TICK, KERNEL_SESSION_DURATION_MS, OBSERVATION_BATCHES_PER_TICK,
};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::time::{Duration, Instant};

struct ActiveEndpoint {
    device_path: std::path::PathBuf,
    reader: KernelObservationReader<LinuxKernelIo>,
    receiver_id: ReceiverId,
    translator: PassiveObservationTranslator,
    cursor: u64,
    write_disqualified: bool,
    retirement_pending: bool,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct RuntimeTickReport {
    pub admitted: usize,
    pub retired: usize,
    pub observation_batches: usize,
    pub observations: usize,
    pub dispatched: usize,
    pub writer_downgrades: usize,
    pub restoration: RestorationTickReport,
}

#[derive(Debug)]
pub enum RuntimeTickError {
    Discovery(EndpointDiscoveryError),
    Identity(ReceiverIdentityError),
    WriterAuthority(WriterAuthorityError),
    Kernel(KernelTransportError),
    Route(KernelRouteError),
    Generation(GenerationOrchestrationError),
    Observation(LifecycleObservationError),
    Translation(PassiveTranslationError),
    Lifecycle(LifecycleError),
    Domain(DomainValueError),
    Restoration(RestorationScheduleError),
    Dispatch,
    StableCapture(RestorationError),
    DuplicateReceiverEndpoint,
    EndpointGenerationMismatch,
    SequenceExhausted,
}

impl fmt::Display for RuntimeTickError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Discovery(_) => "receiver endpoint discovery failed",
            Self::Identity(_) => "receiver identity derivation failed",
            Self::WriterAuthority(_) => "kernel writer authority derivation failed",
            Self::Kernel(_) => "kernel receiver observation failed",
            Self::Route(_) => "kernel receiver routing failed",
            Self::Generation(_) => "receiver generation orchestration failed",
            Self::Observation(_) => "receiver lifecycle observation failed",
            Self::Translation(_) => "passive receiver observation is contradictory",
            Self::Lifecycle(_) => "receiver lifecycle stamp is invalid",
            Self::Domain(_) => "receiver runtime identity value is invalid",
            Self::Restoration(_) => "durable restoration scheduling failed",
            Self::Dispatch => "queued transaction dispatch failed",
            Self::StableCapture(_) => {
                "hardware completed but durable stable-lighting capture failed"
            }
            Self::DuplicateReceiverEndpoint => "receiver has multiple current kernel endpoints",
            Self::EndpointGenerationMismatch => {
                "kernel endpoint name and generation identity disagree"
            }
            Self::SequenceExhausted => "receiver observation sequence is exhausted",
        })
    }
}

impl std::error::Error for RuntimeTickError {}

pub struct LinuxRuntimeManager {
    discovery: EndpointDiscovery,
    receiver_identities: ReceiverIdentityAuthority,
    catalog: RuntimeProfileCatalog,
    mode: BridgeMode,
    daemon_nonce: [u8; 32],
    endpoints: BTreeMap<String, ActiveEndpoint>,
    next_discovery: Option<Instant>,
    discovery_interval: Duration,
    restoration: RestorationScheduler,
    prefer_restoration: bool,
}

impl LinuxRuntimeManager {
    #[must_use]
    pub fn new(
        discovery: EndpointDiscovery,
        receiver_identities: ReceiverIdentityAuthority,
        catalog: RuntimeProfileCatalog,
        mode: BridgeMode,
        daemon_nonce: [u8; 32],
        discovery_interval: Duration,
        restoration: RestorationScheduler,
    ) -> Self {
        Self {
            discovery,
            receiver_identities,
            catalog,
            mode,
            daemon_nonce,
            endpoints: BTreeMap::new(),
            next_discovery: None,
            discovery_interval,
            restoration,
            prefer_restoration: true,
        }
    }

    /// Reconciles hotplug, passive lifecycle, writer admission, and bounded
    /// queued transport work on the actor thread.
    ///
    /// # Errors
    ///
    /// Returns one typed runtime failure after preserving fail-closed routing.
    pub fn tick(
        &mut self,
        backend: &mut ProductionBackend,
        sessions: &SessionRegistry,
    ) -> Result<RuntimeTickReport, RuntimeTickError> {
        let mut report = RuntimeTickReport::default();
        let now = Instant::now();
        if self.next_discovery.is_none_or(|deadline| now >= deadline) {
            let service_start = self.next_discovery.is_none();
            self.reconcile_discovery(backend, &mut report, service_start)?;
            self.next_discovery = Some(now + nonzero_interval(self.discovery_interval));
        }
        self.process_observations(backend, &mut report)?;
        let application_waiting = backend.queued_transactions() > 0;
        if !application_waiting || self.prefer_restoration {
            report.restoration = self
                .restoration
                .tick(backend, sessions)
                .map_err(RuntimeTickError::Restoration)?;
        }
        if report.restoration.claims_dispatched > 0 {
            self.prefer_restoration = false;
            return Ok(report);
        }
        for _ in 0..DISPATCHES_PER_TICK {
            if backend.queued_transactions() == 0 {
                break;
            }
            let dispatch = backend
                .dispatch_next(sessions)
                .map_err(|_| RuntimeTickError::Dispatch)?;
            report.dispatched += 1;
            if let StableCaptureStatus::Failed(error) = dispatch.stable_capture {
                return Err(RuntimeTickError::StableCapture(error));
            }
        }
        if report.dispatched > 0 {
            self.prefer_restoration = true;
        }
        Ok(report)
    }

    #[must_use]
    pub fn endpoint_count(&self) -> usize {
        self.endpoints.len()
    }

    fn reconcile_discovery(
        &mut self,
        backend: &mut ProductionBackend,
        report: &mut RuntimeTickReport,
        service_start: bool,
    ) -> Result<(), RuntimeTickError> {
        let candidates = self.discovery.scan().map_err(RuntimeTickError::Discovery)?;
        let present = candidates
            .iter()
            .map(|candidate| candidate.name.as_str().to_owned())
            .collect::<BTreeSet<_>>();
        let removed = self
            .endpoints
            .keys()
            .filter(|name| !present.contains(*name))
            .cloned()
            .collect::<Vec<_>>();
        for name in removed {
            self.retire_named(&name, backend)?;
            report.retired += 1;
        }
        for candidate in candidates {
            let name = candidate.name.as_str().to_owned();
            if self.endpoints.contains_key(&name) {
                continue;
            }
            if let Some((receiver_id, generation_id)) = self.admit(candidate, backend)? {
                let schedule = if service_start {
                    self.restoration
                        .schedule_service_start(receiver_id, generation_id)
                } else {
                    self.restoration
                        .schedule_receiver_generation(receiver_id, generation_id)
                };
                if let Err(error) = schedule {
                    self.retire_named(&name, backend)?;
                    return Err(RuntimeTickError::Restoration(error));
                }
                report.admitted += 1;
            }
        }
        Ok(())
    }

    fn admit(
        &mut self,
        candidate: EndpointCandidate,
        backend: &mut ProductionBackend,
    ) -> Result<Option<(ReceiverId, hfx_domain::GenerationId)>, RuntimeTickError> {
        let reader = match KernelObservationReader::open(&candidate.device_path) {
            Ok(reader) => reader,
            Err(error) if error.kind() == KernelTransportErrorKind::Io => return Ok(None),
            Err(error) => return Err(RuntimeTickError::Kernel(error)),
        };
        let info = reader.binding();
        if info.generation_id.get() != candidate.name.generation {
            return Err(RuntimeTickError::EndpointGenerationMismatch);
        }
        if info.flags.disconnecting() || info.bound_interfaces == 0 {
            return Ok(None);
        }
        let receiver_id = self
            .receiver_identities
            .derive(&candidate.topology_path, info.vendor_id, info.product_id)
            .map_err(RuntimeTickError::Identity)?;
        if self
            .endpoints
            .values()
            .any(|endpoint| endpoint.receiver_id == receiver_id)
        {
            return Ok(None);
        }
        backend
            .transport_mut()
            .observe(receiver_id.clone(), info.generation_id)
            .map_err(RuntimeTickError::Route)?;
        let stamp = kernel_stamp(
            info.generation_id,
            0,
            reader.boottime_ns().map_err(RuntimeTickError::Kernel)?,
            "kernel-endpoint-info-v1",
        )?;
        if let Err(error) = backend.activate_generation(ReceiverGenerationObservation {
            receiver_id: receiver_id.clone(),
            vendor_id: info.vendor_id,
            product_id: info.product_id,
            stamp,
        }) {
            backend
                .transport_mut()
                .remove(&receiver_id, info.generation_id);
            return Err(RuntimeTickError::Generation(error));
        }
        let name = candidate.name.as_str().to_owned();
        let mut endpoint = ActiveEndpoint {
            device_path: candidate.device_path,
            reader,
            receiver_id: receiver_id.clone(),
            translator: PassiveObservationTranslator::new(
                receiver_id.clone(),
                info.generation_id,
                self.catalog.clone(),
            ),
            cursor: 0,
            write_disqualified: false,
            retirement_pending: false,
        };
        self.try_admit_writer(&mut endpoint, info, backend)?;
        self.endpoints.insert(name, endpoint);
        Ok(Some((receiver_id, info.generation_id)))
    }

    fn process_observations(
        &mut self,
        backend: &mut ProductionBackend,
        report: &mut RuntimeTickReport,
    ) -> Result<(), RuntimeTickError> {
        let names = self.endpoints.keys().cloned().collect::<Vec<_>>();
        for name in names {
            let Some(mut endpoint) = self.endpoints.remove(&name) else {
                continue;
            };
            match self.process_endpoint(&mut endpoint, backend, report) {
                Ok(true) => {
                    self.endpoints.insert(name, endpoint);
                }
                Ok(false) => {
                    endpoint.retirement_pending = true;
                    match Self::downgrade(&mut endpoint, backend) {
                        Ok(true) => report.writer_downgrades += 1,
                        Ok(false) => {}
                        Err(error) => {
                            self.endpoints.insert(name, endpoint);
                            return Err(error);
                        }
                    }
                    if let Err(error) = Self::retire_endpoint(&endpoint, backend) {
                        self.endpoints.insert(name, endpoint);
                        return Err(error);
                    }
                    self.restoration.retire_generation(
                        &endpoint.receiver_id,
                        endpoint.reader.binding().generation_id,
                    );
                    report.retired += 1;
                }
                Err(error) => {
                    let downgrade = Self::downgrade(&mut endpoint, backend);
                    self.endpoints.insert(name, endpoint);
                    downgrade?;
                    return Err(error);
                }
            }
        }
        Ok(())
    }

    fn process_endpoint(
        &mut self,
        endpoint: &mut ActiveEndpoint,
        backend: &mut ProductionBackend,
        report: &mut RuntimeTickReport,
    ) -> Result<bool, RuntimeTickError> {
        if endpoint.retirement_pending {
            return Ok(false);
        }
        let info = match endpoint.reader.info() {
            Ok(info) => info,
            Err(error) if error.kind() == KernelTransportErrorKind::Io => return Ok(true),
            Err(error) => return Err(RuntimeTickError::Kernel(error)),
        };
        if info.flags.disconnecting() || info.bound_interfaces == 0 {
            return Ok(false);
        }
        self.try_admit_writer(endpoint, info, backend)?;
        for _ in 0..OBSERVATION_BATCHES_PER_TICK {
            let batch = match endpoint.reader.read_observations(endpoint.cursor) {
                Ok(batch) => batch,
                Err(error) if error.kind() == KernelTransportErrorKind::Io => return Ok(true),
                Err(error) => return Err(RuntimeTickError::Kernel(error)),
            };
            report.observation_batches += 1;
            if batch.cursor_gap && Self::downgrade(endpoint, backend)? {
                report.writer_downgrades += 1;
            }
            if batch.observations.is_empty() {
                break;
            }
            let count = batch.observations.len();
            for raw in batch.observations {
                let sequence = raw.sequence;
                match endpoint.translator.translate(raw) {
                    Ok(translation) => {
                        if translation.disposition == PassiveDisposition::IdentityConflict
                            && Self::downgrade(endpoint, backend)?
                        {
                            report.writer_downgrades += 1;
                        }
                        if translation.disposition == PassiveDisposition::ReceiverUnavailable {
                            endpoint.cursor = sequence;
                            return Ok(false);
                        }
                        for observation in translation.observations {
                            let sequence = observation.stamp.sequence();
                            let outcome = backend
                                .observe_lifecycle(observation)
                                .map_err(RuntimeTickError::Observation)?;
                            if let LifecycleObservationOutcome::Applied(applied) = outcome {
                                if matches!(
                                    applied.receiver_before,
                                    ReceiverLifecycleState::Suspended
                                        | ReceiverLifecycleState::PartiallySuspended
                                ) && applied.receiver_after == ReceiverLifecycleState::Active
                                {
                                    self.restoration
                                        .schedule_system_resume(
                                            applied.receiver_id.clone(),
                                            applied.generation_id,
                                            sequence,
                                        )
                                        .map_err(RuntimeTickError::Restoration)?;
                                }
                                if matches!(
                                    applied.device_presence_before,
                                    Some(PresenceState::Sleeping | PresenceState::Unavailable)
                                ) && applied.device_presence_after
                                    == Some(PresenceState::Available)
                                    && let Some(device_id) = applied.device_id
                                {
                                    self.restoration
                                        .schedule_device_return(
                                            applied.receiver_id,
                                            applied.generation_id,
                                            device_id,
                                            sequence,
                                        )
                                        .map_err(RuntimeTickError::Restoration)?;
                                }
                            }
                        }
                    }
                    Err(error) => {
                        if Self::downgrade(endpoint, backend)? {
                            report.writer_downgrades += 1;
                        }
                        if !matches!(
                            error,
                            PassiveTranslationError::ProfileLaneMismatch(_)
                                | PassiveTranslationError::InvalidObservationValue
                        ) {
                            return Err(RuntimeTickError::Translation(error));
                        }
                    }
                }
                endpoint.cursor = sequence;
                report.observations += 1;
            }
            if count < HFX_UAPI_MAX_OBSERVATIONS {
                break;
            }
        }
        Ok(true)
    }

    fn try_admit_writer(
        &self,
        endpoint: &mut ActiveEndpoint,
        info: KernelEndpointInfo,
        backend: &mut ProductionBackend,
    ) -> Result<(), RuntimeTickError> {
        if self.mode != BridgeMode::QualifiedLive
            || endpoint.write_disqualified
            || backend.transport().is_writable(&endpoint.receiver_id)
            || info.flags.suspended()
            || info.flags.disconnecting()
            || info.flags.writer_open()
        {
            return Ok(());
        }
        let Some(receiver) = self.catalog.receiver(info.vendor_id, info.product_id) else {
            endpoint.write_disqualified = true;
            return Ok(());
        };
        let material = KernelSessionMaterial {
            receiver_id: endpoint.receiver_id.clone(),
            generation_id: info.generation_id,
            receiver_profile_id: receiver.profile_id.clone(),
            receiver_profile_digest: receiver.runtime_digest.clone(),
            capability_digest: derive_capability_digest(&self.catalog, receiver)
                .map_err(RuntimeTickError::WriterAuthority)?,
            daemon_nonce: self.daemon_nonce,
            session_duration: Duration::from_millis(KERNEL_SESSION_DURATION_MS),
        };
        match KernelReceiverTransport::open(&endpoint.device_path, self.catalog.clone(), material) {
            Ok(writer) => {
                backend
                    .transport_mut()
                    .install_writable(endpoint.receiver_id.clone(), info.generation_id, writer)
                    .map_err(RuntimeTickError::Route)?;
            }
            Err(error) if writer_failure_is_transient(error.kind()) => {}
            Err(_) => endpoint.write_disqualified = true,
        }
        Ok(())
    }

    fn downgrade(
        endpoint: &mut ActiveEndpoint,
        backend: &mut ProductionBackend,
    ) -> Result<bool, RuntimeTickError> {
        if endpoint.write_disqualified {
            return Ok(false);
        }
        endpoint.write_disqualified = true;
        backend
            .transport_mut()
            .observe(
                endpoint.receiver_id.clone(),
                endpoint.reader.binding().generation_id,
            )
            .map_err(RuntimeTickError::Route)?;
        Ok(true)
    }

    fn retire_named(
        &mut self,
        name: &str,
        backend: &mut ProductionBackend,
    ) -> Result<(), RuntimeTickError> {
        let mut endpoint = self
            .endpoints
            .remove(name)
            .ok_or(RuntimeTickError::DuplicateReceiverEndpoint)?;
        endpoint.retirement_pending = true;
        if let Err(error) = Self::downgrade(&mut endpoint, backend) {
            self.endpoints.insert(name.to_owned(), endpoint);
            return Err(error);
        }
        if let Err(error) = Self::retire_endpoint(&endpoint, backend) {
            self.endpoints.insert(name.to_owned(), endpoint);
            return Err(error);
        }
        self.restoration.retire_generation(
            &endpoint.receiver_id,
            endpoint.reader.binding().generation_id,
        );
        Ok(())
    }

    fn retire_endpoint(
        endpoint: &ActiveEndpoint,
        backend: &mut ProductionBackend,
    ) -> Result<(), RuntimeTickError> {
        let generation_id = endpoint.reader.binding().generation_id;
        backend
            .transport_mut()
            .remove(&endpoint.receiver_id, generation_id);
        let sequence = endpoint
            .cursor
            .checked_add(1)
            .ok_or(RuntimeTickError::SequenceExhausted)?;
        let stamp = monotonic_stamp(generation_id, sequence, "kernel-endpoint-absence-v1")?;
        let observation = ReceiverDisconnectObservation {
            receiver_id: endpoint.receiver_id.clone(),
            stamp,
        };
        backend
            .begin_receiver_disconnect(observation.clone())
            .map_err(RuntimeTickError::Generation)?;
        backend
            .complete_receiver_disconnect(observation)
            .map_err(RuntimeTickError::Generation)?;
        Ok(())
    }
}

fn writer_failure_is_transient(kind: KernelTransportErrorKind) -> bool {
    matches!(
        kind,
        KernelTransportErrorKind::Io | KernelTransportErrorKind::SessionUnavailable
    )
}

fn kernel_stamp(
    generation_id: hfx_domain::GenerationId,
    sequence: u64,
    boottime_ns: u64,
    claim: &str,
) -> Result<hfx_core::ObservationStamp, RuntimeTickError> {
    let monotonic =
        MonotonicMs::try_from(boottime_ns / 1_000_000).map_err(RuntimeTickError::Domain)?;
    stamp(generation_id, sequence, monotonic, claim)
}

fn monotonic_stamp(
    generation_id: hfx_domain::GenerationId,
    sequence: u64,
    claim: &str,
) -> Result<hfx_core::ObservationStamp, RuntimeTickError> {
    stamp(
        generation_id,
        sequence,
        hfx_bridge::LinuxMonotonicClock.now(),
        claim,
    )
}

fn stamp(
    generation_id: hfx_domain::GenerationId,
    sequence: u64,
    monotonic: MonotonicMs,
    claim: &str,
) -> Result<hfx_core::ObservationStamp, RuntimeTickError> {
    hfx_core::ObservationStamp::new(
        generation_id,
        SequenceNumber::try_from(sequence).map_err(RuntimeTickError::Domain)?,
        monotonic,
        EvidenceConfidence::Observed,
        EvidenceClaimId::try_from(claim).map_err(RuntimeTickError::Domain)?,
    )
    .map_err(RuntimeTickError::Lifecycle)
}

fn nonzero_interval(interval: Duration) -> Duration {
    if interval.is_zero() {
        Duration::from_millis(1)
    } else {
        interval
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writer_failure_policy_retries_only_absence_or_contention() {
        assert!(writer_failure_is_transient(KernelTransportErrorKind::Io));
        assert!(writer_failure_is_transient(
            KernelTransportErrorKind::SessionUnavailable
        ));
        assert!(!writer_failure_is_transient(
            KernelTransportErrorKind::ProfileMismatch
        ));
        assert!(!writer_failure_is_transient(
            KernelTransportErrorKind::GenerationMismatch
        ));
    }

    #[test]
    fn synthetic_lifecycle_stamps_preserve_generation_and_sequence() {
        let generation = hfx_domain::GenerationId::try_from(9_u64).expect("generation is valid");
        let value = kernel_stamp(generation, 4, 5_000_000, "kernel-endpoint-info-v1")
            .expect("stamp is valid");
        assert_eq!(value.generation_id(), generation);
        assert_eq!(value.sequence().get(), 4);
        assert_eq!(value.observed_at_ms().get(), 5);
    }
}
