// SPDX-License-Identifier: GPL-2.0-only

use crate::battery::ChildChargeReading;
use hfx_bridge::{LifecycleObservation, LifecycleObservationKind};
use hfx_core::{
    ChildIdentity, EndpointIdentity, EndpointReturnLifecycle, EndpointReturnPolicy, LifecycleError,
    ObservationStamp, ReturnEvidence, ReturnTransition,
};
use hfx_domain::{
    ActivityState, BatteryPercent, ConnectionMode, ContactState, DeviceKind, DomainValueError,
    EndpointId, EvidenceClaimId, EvidenceConfidence, FreshnessState, GenerationId, LogicalDeviceId,
    MonotonicMs, PairingState, PowerState, ProductId, ReceiverId, ReceiverLifecycleState,
    RouteKind, RouteState, SequenceNumber, SleepState,
};
use hfx_kernel_transport::{
    ChildBatteryLane, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
    HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT, HFX_UAPI_OBSERVATION_CONFIDENCE_OBSERVED,
    HFX_UAPI_OBSERVATION_CONFIDENCE_RAW, HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ACTIVITY,
    HFX_UAPI_OBSERVATION_KIND_ENDPOINT_BATTERY_RAW, HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CHARGE_RAW,
    HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CONTACT_RAW, HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID,
    HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ROUTE_RAW, HFX_UAPI_OBSERVATION_KIND_IDENTITY_CONFLICT,
    HFX_UAPI_OBSERVATION_KIND_RECEIVER_AVAILABLE, HFX_UAPI_OBSERVATION_KIND_RECEIVER_SUSPENDED,
    HFX_UAPI_OBSERVATION_SOURCE_HID_FEATURE, HFX_UAPI_OBSERVATION_SOURCE_HID_INPUT,
    HFX_UAPI_OBSERVATION_SOURCE_HID_PASSIVE, HFX_UAPI_OBSERVATION_SOURCE_POWER_MANAGEMENT,
    RawKernelObservation,
};
use hfx_profiles::{
    LifecyclePolicyRecord, PassiveBatteryEncoding, PassiveEndpointLane, PassiveTelemetryRecord,
    ReturnEvidenceSource, RuntimeProfileCatalog, return_policy_by_id,
};
use std::collections::BTreeMap;
use std::fmt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PassiveDisposition {
    Applied,
    IgnoredIndeterminateSemantic,
    IgnoredUnknownLane,
    IgnoredUnqualifiedSemantic,
    ReceiverUnavailable,
    IdentityConflict,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeviceRestoreReadiness {
    pub device_id: LogicalDeviceId,
    pub epoch_sequence: SequenceNumber,
    pub opened_at_ms: MonotonicMs,
    pub evidence_at_ms: MonotonicMs,
    pub retry_offsets_ms: Vec<u64>,
    pub phase: DeviceRestoreReadinessPhase,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeviceRestoreReadinessPhase {
    EpochOpened,
    EpochAccelerated,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PassiveTranslation {
    pub disposition: PassiveDisposition,
    pub observations: Vec<LifecycleObservation>,
    /// One device whose first generation-bound input, or first input after a
    /// bounded quiet interval, proves that its child route is ready for a
    /// restoration attempt. This is not a claim that the device was asleep.
    pub restore_ready: Option<DeviceRestoreReadiness>,
    /// One charge state decoded by the child profile's qualified charge or
    /// route charge decoder. It is daemon-local battery annotation, not a
    /// lifecycle fact.
    pub charge: Option<(ChildBatteryLane, ChildChargeReading)>,
}

impl PassiveTranslation {
    fn applied(observations: Vec<LifecycleObservation>) -> Self {
        Self {
            disposition: PassiveDisposition::Applied,
            observations,
            restore_ready: None,
            charge: None,
        }
    }

    fn restore_ready(
        observations: Vec<LifecycleObservation>,
        device_id: LogicalDeviceId,
        epoch_sequence: SequenceNumber,
        opened_at_ms: MonotonicMs,
        evidence_at_ms: MonotonicMs,
        retry_offsets_ms: Vec<u64>,
        phase: DeviceRestoreReadinessPhase,
    ) -> Self {
        Self {
            disposition: PassiveDisposition::Applied,
            observations,
            restore_ready: Some(DeviceRestoreReadiness {
                device_id,
                epoch_sequence,
                opened_at_ms,
                evidence_at_ms,
                retry_offsets_ms,
                phase,
            }),
            charge: None,
        }
    }

    fn disposition(disposition: PassiveDisposition) -> Self {
        Self {
            disposition,
            observations: Vec::new(),
            restore_ready: None,
            charge: None,
        }
    }

    fn charge(lane: ChildBatteryLane, reading: ChildChargeReading) -> Self {
        Self {
            disposition: PassiveDisposition::Applied,
            observations: Vec::new(),
            restore_ready: None,
            charge: Some((lane, reading)),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PassiveTranslationError {
    UnsupportedSource(u32),
    UnsupportedConfidence(u32),
    UnsupportedEndpointSlot(u32),
    InvalidObservationValue,
    ProfileLaneMismatch(ProductId),
    MissingReturnPolicy(ProductId),
    Domain(DomainValueError),
    Lifecycle(LifecycleError),
}

impl fmt::Display for PassiveTranslationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::UnsupportedSource(_) => "passive observation source is unsupported",
            Self::UnsupportedConfidence(_) => "passive observation confidence is unsupported",
            Self::UnsupportedEndpointSlot(_) => "passive observation endpoint lane is unsupported",
            Self::InvalidObservationValue => "passive observation value is invalid",
            Self::ProfileLaneMismatch(_) => {
                "passive observation contradicts the child profile lane"
            }
            Self::MissingReturnPolicy(_) => {
                "qualified child profile references an unavailable return policy"
            }
            Self::Domain(_) => "passive observation contains an invalid typed value",
            Self::Lifecycle(_) => "passive observation contains an invalid lifecycle identity",
        })
    }
}

impl std::error::Error for PassiveTranslationError {}

impl From<DomainValueError> for PassiveTranslationError {
    fn from(error: DomainValueError) -> Self {
        Self::Domain(error)
    }
}

impl From<LifecycleError> for PassiveTranslationError {
    fn from(error: LifecycleError) -> Self {
        Self::Lifecycle(error)
    }
}

#[derive(Clone, Debug)]
struct LaneBinding {
    product_id: ProductId,
    device_id: LogicalDeviceId,
    endpoint_id: EndpointId,
    passive: Option<PassiveTelemetryRecord>,
    return_lifecycle: Option<EndpointReturnLifecycle>,
    last_route_state: Option<RouteState>,
    /// Byte 3 of the latest report, decoded through the contact decoder and
    /// kept only so the charge field that follows it in the same report can
    /// be read in context.
    last_contact: Option<(SequenceNumber, ContactState)>,
    /// Sequence of the latest decoded battery level, kept only so a route
    /// word that follows it in the same keyboard status report can be read
    /// as that report's charge state.
    last_battery: Option<SequenceNumber>,
}

pub struct PassiveObservationTranslator {
    receiver_id: ReceiverId,
    generation_id: GenerationId,
    catalog: RuntimeProfileCatalog,
    lanes: BTreeMap<u32, LaneBinding>,
    receiver_was_suspended: bool,
}

impl PassiveObservationTranslator {
    #[must_use]
    pub fn new(
        receiver_id: ReceiverId,
        generation_id: GenerationId,
        catalog: RuntimeProfileCatalog,
    ) -> Self {
        Self {
            receiver_id,
            generation_id,
            catalog,
            lanes: BTreeMap::new(),
            receiver_was_suspended: false,
        }
    }

    /// Converts one raw kernel fact into zero or more typed lifecycle facts.
    /// Unknown children keep identity and activity visibility but inherit no
    /// model decoder.
    ///
    /// # Errors
    ///
    /// Returns a typed contradiction or malformed-value failure without
    /// changing the lane registry.
    pub fn translate(
        &mut self,
        raw: RawKernelObservation,
    ) -> Result<PassiveTranslation, PassiveTranslationError> {
        let stamp = self.stamp(&raw)?;
        match raw.kind {
            HFX_UAPI_OBSERVATION_KIND_RECEIVER_AVAILABLE => {
                self.receiver_available(raw.value, stamp)
            }
            HFX_UAPI_OBSERVATION_KIND_RECEIVER_SUSPENDED => {
                self.receiver_suspended(raw.value, stamp)
            }
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID => {
                self.product_identity(raw.endpoint_slot, raw.value, stamp)
            }
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ACTIVITY => {
                self.activity(raw.endpoint_slot, raw.value, stamp)
            }
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_BATTERY_RAW => {
                self.battery(raw.endpoint_slot, raw.value, stamp)
            }
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CONTACT_RAW => {
                Ok(self.contact(raw.endpoint_slot, raw.value, stamp))
            }
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ROUTE_RAW => {
                Ok(self.route(raw.endpoint_slot, raw.value, stamp))
            }
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CHARGE_RAW => {
                Ok(self.charge(raw.endpoint_slot, raw.value, &stamp))
            }
            HFX_UAPI_OBSERVATION_KIND_IDENTITY_CONFLICT => Ok(PassiveTranslation::disposition(
                PassiveDisposition::IdentityConflict,
            )),
            _ => Ok(PassiveTranslation::disposition(
                PassiveDisposition::IgnoredUnqualifiedSemantic,
            )),
        }
    }

    /// Returns the child lane and catalog model name bound to one logical
    /// device, for forwarding its battery to the kernel.
    #[must_use]
    pub fn battery_lane(
        &self,
        device_id: &LogicalDeviceId,
    ) -> Option<(ChildBatteryLane, &'static str)> {
        let (slot, binding) = self
            .lanes
            .iter()
            .find(|(_, binding)| binding.device_id == *device_id)?;
        let lane = ChildBatteryLane::from_endpoint_slot(*slot)?;
        let model_name = self
            .catalog
            .child(binding.product_id)
            .map_or("", |profile| profile.model_name);
        Some((lane, model_name))
    }

    fn stamp(
        &self,
        raw: &RawKernelObservation,
    ) -> Result<ObservationStamp, PassiveTranslationError> {
        let confidence = match raw.confidence {
            HFX_UAPI_OBSERVATION_CONFIDENCE_RAW => EvidenceConfidence::Derived,
            HFX_UAPI_OBSERVATION_CONFIDENCE_OBSERVED | HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT => {
                EvidenceConfidence::Observed
            }
            value => return Err(PassiveTranslationError::UnsupportedConfidence(value)),
        };
        let claim = match raw.source {
            HFX_UAPI_OBSERVATION_SOURCE_HID_PASSIVE => "kernel-hid-passive-v1",
            HFX_UAPI_OBSERVATION_SOURCE_HID_INPUT => "kernel-hid-input-v1",
            HFX_UAPI_OBSERVATION_SOURCE_POWER_MANAGEMENT => "kernel-power-management-v1",
            HFX_UAPI_OBSERVATION_SOURCE_HID_FEATURE => "kernel-hid-feature-v1",
            value => return Err(PassiveTranslationError::UnsupportedSource(value)),
        };
        ObservationStamp::new(
            self.generation_id,
            SequenceNumber::try_from(raw.sequence)?,
            MonotonicMs::try_from(raw.observed_boottime_ns / 1_000_000)?,
            confidence,
            EvidenceClaimId::try_from(claim)?,
        )
        .map_err(PassiveTranslationError::Lifecycle)
    }

    fn receiver_available(
        &mut self,
        value: u32,
        stamp: ObservationStamp,
    ) -> Result<PassiveTranslation, PassiveTranslationError> {
        match value {
            1 => Ok(self.receiver_state(ReceiverLifecycleState::Active, stamp)),
            0 => Ok(PassiveTranslation::disposition(
                PassiveDisposition::ReceiverUnavailable,
            )),
            _ => Err(PassiveTranslationError::InvalidObservationValue),
        }
    }

    fn receiver_suspended(
        &mut self,
        value: u32,
        stamp: ObservationStamp,
    ) -> Result<PassiveTranslation, PassiveTranslationError> {
        let state = match value {
            0 => ReceiverLifecycleState::Active,
            1 => ReceiverLifecycleState::Suspended,
            _ => return Err(PassiveTranslationError::InvalidObservationValue),
        };
        Ok(self.receiver_state(state, stamp))
    }

    fn receiver_state(
        &mut self,
        state: ReceiverLifecycleState,
        stamp: ObservationStamp,
    ) -> PassiveTranslation {
        if state == ReceiverLifecycleState::Suspended {
            if !self.receiver_was_suspended {
                for binding in self.lanes.values_mut() {
                    if let Some(lifecycle) = binding.return_lifecycle.as_mut() {
                        lifecycle.reset_observation_anchor();
                    }
                    binding.last_route_state = None;
                }
            }
            self.receiver_was_suspended = true;
        } else if state == ReceiverLifecycleState::Active {
            self.receiver_was_suspended = false;
        }
        PassiveTranslation::applied(vec![
            self.observation(stamp, LifecycleObservationKind::ReceiverState(state)),
        ])
    }

    fn product_identity(
        &mut self,
        slot: u32,
        value: u32,
        stamp: ObservationStamp,
    ) -> Result<PassiveTranslation, PassiveTranslationError> {
        let lane = lane(slot)?;
        let product_raw = u16::try_from(value)
            .ok()
            .filter(|value| *value != 0 && *value != u16::MAX)
            .ok_or(PassiveTranslationError::InvalidObservationValue)?;
        let product_id = ProductId::try_from(product_raw)?;
        let profile = self.catalog.child(product_id);
        if let Some(passive) = profile.and_then(|profile| profile.passive)
            && passive.endpoint_lane != lane
        {
            return Err(PassiveTranslationError::ProfileLaneMismatch(product_id));
        }
        if let Some(current) = self.lanes.get(&slot)
            && current.product_id != product_id
        {
            return Ok(PassiveTranslation::disposition(
                PassiveDisposition::IdentityConflict,
            ));
        }
        let device_kind = profile.map_or(DeviceKind::Unknown, |profile| profile.device_kind);
        let device_id = LogicalDeviceId::try_from(format!(
            "paired-{}-{product_raw:04x}",
            device_kind.as_str()
        ))?;
        let endpoint_id = EndpointId::try_from(format!("hyperflux-{}", lane_name(lane)))?;
        let lifecycle = profile.and_then(|profile| profile.lifecycle);
        let prior = self.lanes.get(&slot);
        let return_lifecycle = if let Some(prior) = prior {
            prior.return_lifecycle.clone()
        } else {
            lifecycle
                .map(|lifecycle| return_lifecycle(product_id, lifecycle))
                .transpose()?
        };
        let last_route_state = prior.and_then(|binding| binding.last_route_state);
        let binding = LaneBinding {
            product_id,
            device_id: device_id.clone(),
            endpoint_id: endpoint_id.clone(),
            passive: profile.and_then(|profile| profile.passive),
            return_lifecycle,
            last_route_state,
            last_contact: None,
            last_battery: None,
        };
        let child = ChildIdentity::new(device_id.clone(), device_kind, product_id)?;
        let endpoint = EndpointIdentity::new(
            endpoint_id,
            RouteKind::HyperfluxWireless,
            ConnectionMode::Hyperflux24ghz,
        )?;
        let observations = vec![
            self.observation(
                stamp.clone(),
                LifecycleObservationKind::RegisterDevice(child),
            ),
            self.observation(
                stamp.clone(),
                LifecycleObservationKind::RegisterEndpoint {
                    device_id: device_id.clone(),
                    identity: endpoint,
                },
            ),
            self.observation(
                stamp,
                LifecycleObservationKind::Pairing {
                    device_id,
                    value: PairingState::Paired,
                },
            ),
        ];
        self.lanes.insert(slot, binding);
        Ok(PassiveTranslation::applied(observations))
    }

    fn activity(
        &mut self,
        slot: u32,
        value: u32,
        stamp: ObservationStamp,
    ) -> Result<PassiveTranslation, PassiveTranslationError> {
        if value != 1 {
            return Err(PassiveTranslationError::InvalidObservationValue);
        }
        let Some(binding) = self.lanes.get_mut(&slot) else {
            return Ok(PassiveTranslation::disposition(
                PassiveDisposition::IgnoredUnknownLane,
            ));
        };
        let observed_at_ms = stamp.observed_at_ms();
        let evidence = route_evidence(binding, RouteState::Available)
            .unwrap_or(ReturnEvidence::EndpointActivity);
        let return_transition = binding
            .return_lifecycle
            .as_mut()
            .map_or(ReturnTransition::None, |lifecycle| {
                lifecycle.observe(evidence, stamp.sequence(), observed_at_ms)
            });
        let readiness = restore_readiness(binding, return_transition, observed_at_ms);
        let binding = binding.clone();
        let observations = vec![
            self.endpoint_observation(
                &binding,
                stamp.clone(),
                EndpointFact::Route(RouteState::Available),
            ),
            self.endpoint_observation(&binding, stamp.clone(), EndpointFact::Power(PowerState::On)),
            self.endpoint_observation(
                &binding,
                stamp.clone(),
                EndpointFact::Sleep(SleepState::Awake),
            ),
            self.endpoint_observation(
                &binding,
                stamp.clone(),
                EndpointFact::Activity(ActivityState::Active),
            ),
            self.endpoint_observation(
                &binding,
                stamp,
                EndpointFact::Freshness(FreshnessState::Fresh),
            ),
        ];
        Ok(if let Some(readiness) = readiness {
            PassiveTranslation::restore_ready(
                observations,
                readiness.device_id,
                readiness.epoch_sequence,
                readiness.opened_at_ms,
                readiness.evidence_at_ms,
                readiness.retry_offsets_ms,
                readiness.phase,
            )
        } else {
            PassiveTranslation::applied(observations)
        })
    }

    fn battery(
        &mut self,
        slot: u32,
        value: u32,
        stamp: ObservationStamp,
    ) -> Result<PassiveTranslation, PassiveTranslationError> {
        let Some(binding) = self.lanes.get_mut(&slot) else {
            return Ok(PassiveTranslation::disposition(
                PassiveDisposition::IgnoredUnknownLane,
            ));
        };
        let Some(passive) = binding.passive else {
            return Ok(PassiveTranslation::disposition(
                PassiveDisposition::IgnoredUnqualifiedSemantic,
            ));
        };
        let raw =
            u8::try_from(value).map_err(|_| PassiveTranslationError::InvalidObservationValue)?;
        let percent = match passive.battery_encoding {
            PassiveBatteryEncoding::Linear255 => {
                u8::try_from((u32::from(raw) * 100 + 127) / 255)
                    .map_err(|_| PassiveTranslationError::InvalidObservationValue)?
            }
        };
        binding.last_battery = Some(stamp.sequence());
        let observed_at_ms = stamp.observed_at_ms();
        let evidence = if passive.report_implies_route_available {
            route_evidence(binding, RouteState::Available).unwrap_or(ReturnEvidence::BatteryReport)
        } else {
            ReturnEvidence::BatteryReport
        };
        let return_transition = binding
            .return_lifecycle
            .as_mut()
            .map_or(ReturnTransition::None, |lifecycle| {
                lifecycle.observe(evidence, stamp.sequence(), observed_at_ms)
            });
        let readiness = restore_readiness(binding, return_transition, observed_at_ms);
        let binding = binding.clone();
        let mut observations = vec![self.observation(
            stamp.clone(),
            LifecycleObservationKind::BatteryReported {
                device_id: binding.device_id.clone(),
                percentage: BatteryPercent::try_from(percent)?,
            },
        )];
        if passive.report_implies_route_available {
            observations.extend(self.route_observations(&binding, stamp, RouteState::Available));
        }
        Ok(if let Some(readiness) = readiness {
            PassiveTranslation::restore_ready(
                observations,
                readiness.device_id,
                readiness.epoch_sequence,
                readiness.opened_at_ms,
                readiness.evidence_at_ms,
                readiness.retry_offsets_ms,
                readiness.phase,
            )
        } else {
            PassiveTranslation::applied(observations)
        })
    }

    fn contact(&mut self, slot: u32, value: u32, stamp: ObservationStamp) -> PassiveTranslation {
        let Some(binding) = self.lanes.get_mut(&slot) else {
            return PassiveTranslation::disposition(PassiveDisposition::IgnoredUnknownLane);
        };
        let Some(passive) = binding.passive else {
            return PassiveTranslation::disposition(PassiveDisposition::IgnoredUnqualifiedSemantic);
        };
        if passive.contact_off_mat.is_empty() && passive.contact_on_mat.is_empty() {
            return PassiveTranslation::disposition(PassiveDisposition::IgnoredUnqualifiedSemantic);
        }
        let state = if passive.contact_off_mat.contains(&value) {
            ContactState::OffMat
        } else if passive.contact_on_mat.contains(&value) {
            ContactState::OnMat
        } else {
            ContactState::Unknown
        };
        binding.last_contact = Some((stamp.sequence(), state));
        let observed_at_ms = stamp.observed_at_ms();
        let evidence = if passive.report_implies_route_available {
            route_evidence(binding, RouteState::Available).unwrap_or(ReturnEvidence::ContactReport)
        } else {
            ReturnEvidence::ContactReport
        };
        let return_transition = if state == ContactState::OnMat {
            binding
                .return_lifecycle
                .as_mut()
                .map_or(ReturnTransition::None, |lifecycle| {
                    lifecycle.observe(evidence, stamp.sequence(), observed_at_ms)
                })
        } else {
            ReturnTransition::None
        };
        let readiness = restore_readiness(binding, return_transition, observed_at_ms);
        let binding = binding.clone();
        let mut observations =
            vec![self.endpoint_observation(&binding, stamp.clone(), EndpointFact::Contact(state))];
        if passive.report_implies_route_available {
            observations.extend(self.route_observations(&binding, stamp, RouteState::Available));
        }
        match readiness {
            Some(readiness) => PassiveTranslation::restore_ready(
                observations,
                readiness.device_id,
                readiness.epoch_sequence,
                readiness.opened_at_ms,
                readiness.evidence_at_ms,
                readiness.retry_offsets_ms,
                readiness.phase,
            ),
            None => PassiveTranslation::applied(observations),
        }
    }

    /// Decodes one charge field through the child profile's qualified charge
    /// decoder. A profile without that decoder keeps its charge unknown.
    ///
    /// A charging value means charging whatever byte 3 of the same report
    /// reads. A not-charging value means discharging only when byte 3, which
    /// the kernel emits as the contact field just before the charge field,
    /// reads its proven off-the-mat value. Byte 3 is not only a mat contact:
    /// it read 06 on a USB cable, so every other byte 3 value, and a missing
    /// or older one, leaves the state unknown.
    fn charge(&mut self, slot: u32, value: u32, stamp: &ObservationStamp) -> PassiveTranslation {
        let (Some(lane), Some(binding)) = (
            ChildBatteryLane::from_endpoint_slot(slot),
            self.lanes.get_mut(&slot),
        ) else {
            return PassiveTranslation::disposition(PassiveDisposition::IgnoredUnknownLane);
        };
        let Some(passive) = binding.passive else {
            return PassiveTranslation::disposition(PassiveDisposition::IgnoredUnqualifiedSemantic);
        };
        if passive.charge_charging.is_empty() && passive.charge_not_charging.is_empty() {
            return PassiveTranslation::disposition(PassiveDisposition::IgnoredUnqualifiedSemantic);
        }
        let same_report_contact = binding.last_contact.take().and_then(|(sequence, state)| {
            (sequence.get().checked_add(1) == Some(stamp.sequence().get())).then_some(state)
        });
        let reading = if passive.charge_charging.contains(&value) {
            ChildChargeReading::Charging
        } else if passive.charge_not_charging.contains(&value)
            && same_report_contact == Some(ContactState::OffMat)
        {
            ChildChargeReading::Discharging
        } else {
            ChildChargeReading::Unknown
        };
        PassiveTranslation::charge(lane, reading)
    }

    /// Decodes one route value. The keyboard status report also carries its
    /// charge state in that value, so the route word is read a second time
    /// through the route charge decoder without changing the route decoding.
    fn route(&mut self, slot: u32, value: u32, stamp: ObservationStamp) -> PassiveTranslation {
        let charge = self.route_charge(slot, value, &stamp);
        let mut translation = self.route_state(slot, value, stamp);
        translation.charge = charge;
        translation
    }

    /// Reads the charge state from a keyboard status-report route word.
    ///
    /// The kernel emits the battery level of that report and then its route
    /// word, power source in the high byte and charge in the low byte, at the
    /// next sequence number. The identity report also emits a route value,
    /// but never right after a battery level, so only a route word at the
    /// sequence after this lane's battery level counts. The decoder lists
    /// whole route words; every other word reads as unknown.
    fn route_charge(
        &mut self,
        slot: u32,
        value: u32,
        stamp: &ObservationStamp,
    ) -> Option<(ChildBatteryLane, ChildChargeReading)> {
        let lane = ChildBatteryLane::from_endpoint_slot(slot)?;
        let binding = self.lanes.get_mut(&slot)?;
        let same_report = binding
            .last_battery
            .take()
            .is_some_and(|sequence| sequence.get().checked_add(1) == Some(stamp.sequence().get()));
        let passive = binding.passive?;
        if !same_report
            || (passive.route_charge_charging.is_empty()
                && passive.route_charge_discharging.is_empty())
        {
            return None;
        }
        let reading = if passive.route_charge_charging.contains(&value) {
            ChildChargeReading::Charging
        } else if passive.route_charge_discharging.contains(&value) {
            ChildChargeReading::Discharging
        } else {
            ChildChargeReading::Unknown
        };
        Some((lane, reading))
    }

    fn route_state(
        &mut self,
        slot: u32,
        value: u32,
        stamp: ObservationStamp,
    ) -> PassiveTranslation {
        let Some(binding) = self.lanes.get_mut(&slot) else {
            return PassiveTranslation::disposition(PassiveDisposition::IgnoredUnknownLane);
        };
        let Some(passive) = binding.passive else {
            return PassiveTranslation::disposition(PassiveDisposition::IgnoredUnqualifiedSemantic);
        };
        let state = if passive.route_available.contains(&value) {
            RouteState::Available
        } else if passive.route_unavailable.contains(&value) {
            RouteState::Unavailable
        } else if passive.route_indeterminate.contains(&value) {
            return PassiveTranslation::disposition(
                PassiveDisposition::IgnoredIndeterminateSemantic,
            );
        } else {
            return PassiveTranslation::disposition(PassiveDisposition::IgnoredUnqualifiedSemantic);
        };
        let observed_at_ms = stamp.observed_at_ms();
        let route_return = route_evidence(binding, state);
        let return_transition = route_return.map_or(ReturnTransition::None, |evidence| {
            binding
                .return_lifecycle
                .as_mut()
                .map_or(ReturnTransition::None, |lifecycle| {
                    lifecycle.observe(evidence, stamp.sequence(), observed_at_ms)
                })
        });
        let readiness = restore_readiness(binding, return_transition, observed_at_ms);
        let binding = binding.clone();
        let observations = self.route_observations(&binding, stamp, state);
        match readiness {
            Some(readiness) => PassiveTranslation::restore_ready(
                observations,
                readiness.device_id,
                readiness.epoch_sequence,
                readiness.opened_at_ms,
                readiness.evidence_at_ms,
                readiness.retry_offsets_ms,
                readiness.phase,
            ),
            None => PassiveTranslation::applied(observations),
        }
    }

    fn route_observations(
        &self,
        binding: &LaneBinding,
        stamp: ObservationStamp,
        state: RouteState,
    ) -> Vec<LifecycleObservation> {
        let mut observations =
            vec![self.endpoint_observation(binding, stamp.clone(), EndpointFact::Route(state))];
        if matches!(state, RouteState::Available | RouteState::Unavailable) {
            observations.push(self.endpoint_observation(
                binding,
                stamp,
                EndpointFact::Freshness(FreshnessState::Fresh),
            ));
        }
        observations
    }

    fn endpoint_observation(
        &self,
        binding: &LaneBinding,
        stamp: ObservationStamp,
        fact: EndpointFact,
    ) -> LifecycleObservation {
        let kind = match fact {
            EndpointFact::Route(value) => LifecycleObservationKind::Route {
                device_id: binding.device_id.clone(),
                endpoint_id: binding.endpoint_id.clone(),
                value,
            },
            EndpointFact::Power(value) => LifecycleObservationKind::Power {
                device_id: binding.device_id.clone(),
                endpoint_id: binding.endpoint_id.clone(),
                value,
            },
            EndpointFact::Sleep(value) => LifecycleObservationKind::Sleep {
                device_id: binding.device_id.clone(),
                endpoint_id: binding.endpoint_id.clone(),
                value,
            },
            EndpointFact::Activity(value) => LifecycleObservationKind::Activity {
                device_id: binding.device_id.clone(),
                endpoint_id: binding.endpoint_id.clone(),
                value,
            },
            EndpointFact::Contact(value) => LifecycleObservationKind::Contact {
                device_id: binding.device_id.clone(),
                endpoint_id: binding.endpoint_id.clone(),
                value,
            },
            EndpointFact::Freshness(value) => LifecycleObservationKind::Freshness {
                device_id: binding.device_id.clone(),
                endpoint_id: binding.endpoint_id.clone(),
                value,
            },
        };
        self.observation(stamp, kind)
    }

    fn observation(
        &self,
        stamp: ObservationStamp,
        kind: LifecycleObservationKind,
    ) -> LifecycleObservation {
        LifecycleObservation {
            receiver_id: self.receiver_id.clone(),
            stamp,
            kind,
        }
    }
}

#[derive(Clone, Copy)]
enum EndpointFact {
    Route(RouteState),
    Power(PowerState),
    Sleep(SleepState),
    Activity(ActivityState),
    Contact(ContactState),
    Freshness(FreshnessState),
}

fn lane(slot: u32) -> Result<PassiveEndpointLane, PassiveTranslationError> {
    match slot {
        HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE => Ok(PassiveEndpointLane::Pointer),
        HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE => Ok(PassiveEndpointLane::Keyboard),
        _ => Err(PassiveTranslationError::UnsupportedEndpointSlot(slot)),
    }
}

fn return_lifecycle(
    product_id: ProductId,
    lifecycle: LifecyclePolicyRecord,
) -> Result<EndpointReturnLifecycle, PassiveTranslationError> {
    let policy = return_policy_by_id(lifecycle.return_policy_id)
        .ok_or(PassiveTranslationError::MissingReturnPolicy(product_id))?;
    if !lifecycle
        .open_evidence
        .iter()
        .all(|evidence| policy.allowed_open_evidence.contains(evidence))
        || !lifecycle
            .accelerator_evidence
            .iter()
            .all(|evidence| policy.allowed_accelerator_evidence.contains(evidence))
    {
        return Err(PassiveTranslationError::Lifecycle(
            LifecycleError::InvalidReturnPolicy,
        ));
    }
    let open_evidence = lifecycle
        .open_evidence
        .iter()
        .copied()
        .map(ReturnEvidenceSource::domain)
        .collect::<Vec<_>>();
    let accelerator_evidence = lifecycle
        .accelerator_evidence
        .iter()
        .copied()
        .map(ReturnEvidenceSource::domain)
        .collect::<Vec<_>>();
    let policy = EndpointReturnPolicy::new(
        policy.quiescence_candidate_ms,
        policy.observation_jitter_ms,
        policy.retry_offsets_ms,
        &open_evidence,
        &accelerator_evidence,
    )?;
    Ok(EndpointReturnLifecycle::new(policy))
}

fn route_evidence(binding: &mut LaneBinding, state: RouteState) -> Option<ReturnEvidence> {
    let departed = binding.last_route_state != Some(RouteState::Unavailable)
        && state == RouteState::Unavailable;
    let returned = binding.last_route_state == Some(RouteState::Unavailable)
        && state == RouteState::Available
        && binding.return_lifecycle.as_ref().is_some_and(|lifecycle| {
            lifecycle
                .policy()
                .accelerator_evidence()
                .contains(&ReturnEvidence::RouteReturn)
        });
    binding.last_route_state = Some(state);
    if departed {
        Some(ReturnEvidence::RouteDeparture)
    } else {
        returned.then_some(ReturnEvidence::RouteReturn)
    }
}

fn restore_readiness(
    binding: &LaneBinding,
    transition: ReturnTransition,
    evidence_at_ms: MonotonicMs,
) -> Option<DeviceRestoreReadiness> {
    let (epoch, phase) = match transition {
        ReturnTransition::EpochOpened(epoch) => (epoch, DeviceRestoreReadinessPhase::EpochOpened),
        ReturnTransition::EpochAccelerated(epoch) => {
            (epoch, DeviceRestoreReadinessPhase::EpochAccelerated)
        }
        ReturnTransition::None
        | ReturnTransition::QuiescenceCandidate
        | ReturnTransition::BecameUnavailable
        | ReturnTransition::Synchronized(_)
        | ReturnTransition::WindowCompleted(_) => return None,
    };
    let retry_offsets_ms = binding
        .return_lifecycle
        .as_ref()?
        .policy()
        .retry_offsets_ms()
        .to_vec();
    Some(DeviceRestoreReadiness {
        device_id: binding.device_id.clone(),
        epoch_sequence: epoch.sequence(),
        opened_at_ms: epoch.opened_at_ms(),
        evidence_at_ms,
        retry_offsets_ms,
        phase,
    })
}

const fn lane_name(lane: PassiveEndpointLane) -> &'static str {
    match lane {
        PassiveEndpointLane::Pointer => "pointer",
        PassiveEndpointLane::Keyboard => "keyboard",
    }
}

#[cfg(test)]
mod tests;
