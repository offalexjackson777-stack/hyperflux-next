// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{LifecycleObservation, LifecycleObservationKind};
use hfx_core::{ChildIdentity, EndpointIdentity, LifecycleError, ObservationStamp};
use hfx_domain::{
    ActivityState, BatteryPercent, ConnectionMode, ContactState, DeviceKind, DomainValueError,
    EndpointId, EvidenceClaimId, EvidenceConfidence, FreshnessState, GenerationId, LogicalDeviceId,
    MonotonicMs, PairingState, PowerState, ProductId, ReceiverId, ReceiverLifecycleState,
    RouteKind, RouteState, SequenceNumber, SleepState,
};
use hfx_kernel_transport::{
    HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
    HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT, HFX_UAPI_OBSERVATION_CONFIDENCE_OBSERVED,
    HFX_UAPI_OBSERVATION_CONFIDENCE_RAW, HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ACTIVITY,
    HFX_UAPI_OBSERVATION_KIND_ENDPOINT_BATTERY_RAW, HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CONTACT_RAW,
    HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID, HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ROUTE_RAW,
    HFX_UAPI_OBSERVATION_KIND_IDENTITY_CONFLICT, HFX_UAPI_OBSERVATION_KIND_RECEIVER_AVAILABLE,
    HFX_UAPI_OBSERVATION_KIND_RECEIVER_SUSPENDED, HFX_UAPI_OBSERVATION_SOURCE_HID_FEATURE,
    HFX_UAPI_OBSERVATION_SOURCE_HID_INPUT, HFX_UAPI_OBSERVATION_SOURCE_HID_PASSIVE,
    HFX_UAPI_OBSERVATION_SOURCE_POWER_MANAGEMENT, RawKernelObservation,
};
use hfx_profiles::{
    PassiveBatteryEncoding, PassiveEndpointLane, PassiveTelemetryRecord, RuntimeProfileCatalog,
};
use hfx_runtime::ACTIVITY_RESUMPTION_QUIET_MS;
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
pub struct PassiveTranslation {
    pub disposition: PassiveDisposition,
    pub observations: Vec<LifecycleObservation>,
    /// One device whose first generation-bound input, or first input after a
    /// bounded quiet interval, proves that its child route is ready for a
    /// restoration attempt. This is not a claim that the device was asleep.
    pub restore_ready: Option<LogicalDeviceId>,
}

impl PassiveTranslation {
    fn applied(observations: Vec<LifecycleObservation>) -> Self {
        Self {
            disposition: PassiveDisposition::Applied,
            observations,
            restore_ready: None,
        }
    }

    fn restore_ready(observations: Vec<LifecycleObservation>, device_id: LogicalDeviceId) -> Self {
        Self {
            disposition: PassiveDisposition::Applied,
            observations,
            restore_ready: Some(device_id),
        }
    }

    fn disposition(disposition: PassiveDisposition) -> Self {
        Self {
            disposition,
            observations: Vec::new(),
            restore_ready: None,
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
    last_activity_ms: Option<MonotonicMs>,
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
            HFX_UAPI_OBSERVATION_KIND_IDENTITY_CONFLICT => Ok(PassiveTranslation::disposition(
                PassiveDisposition::IdentityConflict,
            )),
            _ => Ok(PassiveTranslation::disposition(
                PassiveDisposition::IgnoredUnqualifiedSemantic,
            )),
        }
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
                    binding.last_activity_ms = None;
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
        let last_activity_ms = self
            .lanes
            .get(&slot)
            .and_then(|binding| binding.last_activity_ms);
        let binding = LaneBinding {
            product_id,
            device_id: device_id.clone(),
            endpoint_id: endpoint_id.clone(),
            passive: profile.and_then(|profile| profile.passive),
            last_activity_ms,
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
        let restore_ready = binding.last_activity_ms.is_none_or(|prior| {
            observed_at_ms.get().saturating_sub(prior.get()) >= ACTIVITY_RESUMPTION_QUIET_MS
        });
        if binding
            .last_activity_ms
            .is_none_or(|prior| observed_at_ms > prior)
        {
            binding.last_activity_ms = Some(observed_at_ms);
        }
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
        Ok(if restore_ready {
            PassiveTranslation::restore_ready(observations, binding.device_id)
        } else {
            PassiveTranslation::applied(observations)
        })
    }

    fn battery(
        &self,
        slot: u32,
        value: u32,
        stamp: ObservationStamp,
    ) -> Result<PassiveTranslation, PassiveTranslationError> {
        let Some(binding) = self.lanes.get(&slot) else {
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
        let mut observations = vec![self.observation(
            stamp.clone(),
            LifecycleObservationKind::BatteryReported {
                device_id: binding.device_id.clone(),
                percentage: BatteryPercent::try_from(percent)?,
            },
        )];
        if passive.report_implies_route_available {
            observations.extend(self.route_observations(binding, stamp, RouteState::Available));
        }
        Ok(PassiveTranslation::applied(observations))
    }

    fn contact(&self, slot: u32, value: u32, stamp: ObservationStamp) -> PassiveTranslation {
        let Some(binding) = self.lanes.get(&slot) else {
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
        let mut observations =
            vec![self.endpoint_observation(binding, stamp.clone(), EndpointFact::Contact(state))];
        if passive.report_implies_route_available {
            observations.extend(self.route_observations(binding, stamp, RouteState::Available));
        }
        PassiveTranslation::applied(observations)
    }

    fn route(&self, slot: u32, value: u32, stamp: ObservationStamp) -> PassiveTranslation {
        let Some(binding) = self.lanes.get(&slot) else {
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
        PassiveTranslation::applied(self.route_observations(binding, stamp, state))
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

const fn lane_name(lane: PassiveEndpointLane) -> &'static str {
    match lane {
        PassiveEndpointLane::Pointer => "pointer",
        PassiveEndpointLane::Keyboard => "keyboard",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use hfx_kernel_transport::HFX_UAPI_ENDPOINT_SLOT_RECEIVER;

    fn translator() -> PassiveObservationTranslator {
        PassiveObservationTranslator::new(
            ReceiverId::try_from("receiver-test").expect("receiver id is valid"),
            GenerationId::try_from(7_u64).expect("generation is valid"),
            RuntimeProfileCatalog::load().expect("profile catalog is valid"),
        )
    }

    fn raw(sequence: u64, kind: u32, slot: u32, value: u32) -> RawKernelObservation {
        raw_at(sequence, sequence, kind, slot, value)
    }

    fn raw_at(
        sequence: u64,
        observed_at_ms: u64,
        kind: u32,
        slot: u32,
        value: u32,
    ) -> RawKernelObservation {
        RawKernelObservation {
            sequence,
            observed_boottime_ns: observed_at_ms * 1_000_000,
            kind,
            endpoint_slot: slot,
            source: HFX_UAPI_OBSERVATION_SOURCE_HID_PASSIVE,
            confidence: HFX_UAPI_OBSERVATION_CONFIDENCE_RAW,
            value,
            auxiliary: 0,
        }
    }

    fn activity(sequence: u64, observed_at_ms: u64, slot: u32) -> RawKernelObservation {
        let mut observation = raw_at(
            sequence,
            observed_at_ms,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ACTIVITY,
            slot,
            1,
        );
        observation.source = HFX_UAPI_OBSERVATION_SOURCE_HID_INPUT;
        observation.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_OBSERVED;
        observation
    }

    fn identify(
        translator: &mut PassiveObservationTranslator,
        sequence: u64,
        slot: u32,
        product_id: u32,
    ) -> PassiveTranslation {
        let mut observation = raw(
            sequence,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID,
            slot,
            product_id,
        );
        observation.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT;
        translator
            .translate(observation)
            .expect("identity observation translates")
    }

    #[test]
    fn bounded_feature_inventory_uses_the_same_generation_identity_gate() {
        let mut translator = translator();
        let mut observation = raw(
            1,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            0x00cd,
        );
        observation.source = HFX_UAPI_OBSERVATION_SOURCE_HID_FEATURE;
        observation.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT;

        let identity = translator
            .translate(observation)
            .expect("feature inventory identity translates");
        assert_eq!(identity.disposition, PassiveDisposition::Applied);
        assert!(matches!(
            &identity.observations[0].kind,
            LifecycleObservationKind::RegisterDevice(child)
                if child.device_kind() == DeviceKind::Mouse
                    && child.product_id().get() == 0x00cd
        ));
    }

    #[test]
    fn qualified_mouse_decoder_projects_identity_contact_route_and_battery() {
        let mut translator = translator();
        let identity = identify(
            &mut translator,
            1,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            0x00cd,
        );
        assert_eq!(identity.disposition, PassiveDisposition::Applied);
        assert_eq!(identity.observations.len(), 3);
        assert!(matches!(
            &identity.observations[0].kind,
            LifecycleObservationKind::RegisterDevice(child)
                if child.device_kind() == DeviceKind::Mouse
                    && child.product_id().get() == 0x00cd
        ));

        let contact = translator
            .translate(raw(
                2,
                HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CONTACT_RAW,
                HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
                1,
            ))
            .expect("contact translates");
        assert!(matches!(
            &contact.observations[0].kind,
            LifecycleObservationKind::Contact {
                value: ContactState::OnMat,
                ..
            }
        ));
        assert!(matches!(
            &contact.observations[1].kind,
            LifecycleObservationKind::Route {
                value: RouteState::Available,
                ..
            }
        ));
        assert!(matches!(
            &contact.observations[2].kind,
            LifecycleObservationKind::Freshness {
                value: FreshnessState::Fresh,
                ..
            }
        ));
        assert_eq!(contact.restore_ready, None);

        let battery = translator
            .translate(raw(
                3,
                HFX_UAPI_OBSERVATION_KIND_ENDPOINT_BATTERY_RAW,
                HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
                128,
            ))
            .expect("battery translates");
        assert!(matches!(
            &battery.observations[0].kind,
            LifecycleObservationKind::BatteryReported { percentage, .. }
                if percentage.get() == 50
        ));
        assert_eq!(battery.restore_ready, None);
    }

    #[test]
    fn qualified_keyboard_route_decoder_preserves_available_and_unavailable() {
        let mut translator = translator();
        identify(
            &mut translator,
            1,
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
            0x0296,
        );
        for (sequence, raw_value, expected) in [
            (2, 3, RouteState::Available),
            (3, 2, RouteState::Unavailable),
        ] {
            let translated = translator
                .translate(raw(
                    sequence,
                    HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ROUTE_RAW,
                    HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
                    raw_value,
                ))
                .expect("route translates");
            assert!(matches!(
                &translated.observations[0].kind,
                LifecycleObservationKind::Route { value, .. } if *value == expected
            ));
            assert!(matches!(
                &translated.observations[1].kind,
                LifecycleObservationKind::Freshness {
                    value: FreshnessState::Fresh,
                    ..
                }
            ));
        }
    }

    #[test]
    fn externally_powered_keyboard_route_cannot_override_observed_activity() {
        let mut translator = translator();
        identify(
            &mut translator,
            1,
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
            0x0296,
        );
        let active = translator
            .translate(activity(2, 1_000, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE))
            .expect("keyboard activity translates");
        assert!(matches!(
            &active.observations[0].kind,
            LifecycleObservationKind::Route {
                value: RouteState::Available,
                ..
            }
        ));

        let mut external_power = raw(
            3,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ROUTE_RAW,
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
            0x0101,
        );
        external_power.auxiliary = 2;
        let route = translator
            .translate(external_power)
            .expect("indeterminate external-power route is bounded");
        assert_eq!(
            route.disposition,
            PassiveDisposition::IgnoredIndeterminateSemantic
        );
        assert!(route.observations.is_empty());
    }

    #[test]
    fn unknown_child_keeps_identity_and_activity_but_inherits_no_decoder() {
        let mut translator = translator();
        let identity = identify(
            &mut translator,
            1,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            0x1234,
        );
        assert!(matches!(
            &identity.observations[0].kind,
            LifecycleObservationKind::RegisterDevice(child)
                if child.device_kind() == DeviceKind::Unknown
        ));

        let battery = translator
            .translate(raw(
                2,
                HFX_UAPI_OBSERVATION_KIND_ENDPOINT_BATTERY_RAW,
                HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
                200,
            ))
            .expect("unknown battery is safely ignored");
        assert_eq!(
            battery.disposition,
            PassiveDisposition::IgnoredUnqualifiedSemantic
        );

        let mut activity = raw(
            3,
            HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ACTIVITY,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            1,
        );
        activity.source = HFX_UAPI_OBSERVATION_SOURCE_HID_INPUT;
        activity.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_OBSERVED;
        let activity = translator
            .translate(activity)
            .expect("activity translates without a model decoder");
        assert_eq!(activity.observations.len(), 5);
    }

    #[test]
    fn first_generation_activity_and_first_activity_after_quiet_emit_restore_readiness() {
        let mut translator = translator();
        identify(
            &mut translator,
            1,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            0x00cd,
        );

        let first = translator
            .translate(activity(2, 1_000, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
            .expect("first activity translates");
        assert_eq!(
            first.restore_ready.as_ref().map(LogicalDeviceId::as_str),
            Some("paired-mouse-00cd")
        );

        let resumed = translator
            .translate(activity(
                3,
                1_000 + ACTIVITY_RESUMPTION_QUIET_MS,
                HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            ))
            .expect("post-quiet activity translates");
        assert_eq!(
            resumed.restore_ready.as_ref().map(LogicalDeviceId::as_str),
            Some("paired-mouse-00cd")
        );

        let continuous = translator
            .translate(activity(
                4,
                1_001 + ACTIVITY_RESUMPTION_QUIET_MS,
                HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            ))
            .expect("continuous activity translates");
        assert_eq!(continuous.restore_ready, None);
    }

    #[test]
    fn identity_replay_preserves_activity_continuity_and_sibling_isolation() {
        let mut translator = translator();
        identify(
            &mut translator,
            1,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            0x00cd,
        );
        identify(
            &mut translator,
            2,
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
            0x0296,
        );
        translator
            .translate(activity(3, 1_000, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
            .expect("mouse activity translates");
        translator
            .translate(activity(4, 1_000, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE))
            .expect("keyboard activity translates");

        identify(
            &mut translator,
            5,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            0x00cd,
        );
        let mouse = translator
            .translate(activity(
                6,
                1_000 + ACTIVITY_RESUMPTION_QUIET_MS,
                HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            ))
            .expect("mouse resumes after identity replay");
        assert_eq!(
            mouse.restore_ready.as_ref().map(LogicalDeviceId::as_str),
            Some("paired-mouse-00cd")
        );

        let keyboard = translator
            .translate(activity(7, 1_001, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE))
            .expect("keyboard remains continuous");
        assert_eq!(keyboard.restore_ready, None);
    }

    #[test]
    fn a_new_generation_first_activity_is_restore_ready() {
        let mut next_generation = PassiveObservationTranslator::new(
            ReceiverId::try_from("receiver-test").expect("receiver id is valid"),
            GenerationId::try_from(8_u64).expect("generation is valid"),
            RuntimeProfileCatalog::load().expect("profile catalog is valid"),
        );
        identify(
            &mut next_generation,
            1,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            0x00cd,
        );
        let first = next_generation
            .translate(activity(
                2,
                ACTIVITY_RESUMPTION_QUIET_MS * 10,
                HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            ))
            .expect("first generation-bound activity translates");
        assert_eq!(
            first.restore_ready.as_ref().map(LogicalDeviceId::as_str),
            Some("paired-mouse-00cd")
        );
    }

    #[test]
    fn lane_mismatch_and_identity_replacement_fail_closed() {
        let mut translator = translator();
        assert!(matches!(
            translator.translate(raw(
                1,
                HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID,
                HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
                0x00cd,
            )),
            Err(PassiveTranslationError::ProfileLaneMismatch(_))
        ));

        identify(
            &mut translator,
            2,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            0x00cd,
        );
        let conflict = identify(
            &mut translator,
            3,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            0x1234,
        );
        assert_eq!(conflict.disposition, PassiveDisposition::IdentityConflict);
        assert!(conflict.observations.is_empty());
    }

    #[test]
    fn suspend_and_resume_are_generation_preserving_receiver_facts() {
        let mut translator = translator();
        for (sequence, raw_value, expected) in [
            (1, 1, ReceiverLifecycleState::Suspended),
            (2, 0, ReceiverLifecycleState::Active),
        ] {
            let mut observation = raw(
                sequence,
                HFX_UAPI_OBSERVATION_KIND_RECEIVER_SUSPENDED,
                HFX_UAPI_ENDPOINT_SLOT_RECEIVER,
                raw_value,
            );
            observation.source = HFX_UAPI_OBSERVATION_SOURCE_POWER_MANAGEMENT;
            observation.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT;
            let translated = translator
                .translate(observation)
                .expect("power observation translates");
            assert!(matches!(
                translated.observations[0].kind,
                LifecycleObservationKind::ReceiverState(value) if value == expected
            ));
        }
    }

    #[test]
    fn short_same_generation_resume_requires_fresh_input_from_each_device() {
        let mut translator = translator();
        identify(
            &mut translator,
            1,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            0x00cd,
        );
        identify(
            &mut translator,
            2,
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
            0x0296,
        );
        translator
            .translate(activity(3, 1_000, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
            .expect("initial mouse activity translates");
        translator
            .translate(activity(4, 1_001, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE))
            .expect("initial keyboard activity translates");

        for (sequence, observed_at_ms, raw_value) in [(5, 1_002, 1), (6, 1_003, 0)] {
            let mut observation = raw_at(
                sequence,
                observed_at_ms,
                HFX_UAPI_OBSERVATION_KIND_RECEIVER_SUSPENDED,
                HFX_UAPI_ENDPOINT_SLOT_RECEIVER,
                raw_value,
            );
            observation.source = HFX_UAPI_OBSERVATION_SOURCE_POWER_MANAGEMENT;
            observation.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT;
            translator
                .translate(observation)
                .expect("power observation translates");
        }

        let mouse = translator
            .translate(activity(7, 1_004, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
            .expect("post-resume mouse activity translates");
        assert_eq!(
            mouse.restore_ready.as_ref().map(LogicalDeviceId::as_str),
            Some("paired-mouse-00cd")
        );
        let keyboard = translator
            .translate(activity(8, 1_005, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE))
            .expect("post-resume keyboard activity translates");
        assert_eq!(
            keyboard.restore_ready.as_ref().map(LogicalDeviceId::as_str),
            Some("paired-keyboard-0296")
        );
        let continuous = translator
            .translate(activity(9, 1_006, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
            .expect("continuous mouse activity translates");
        assert_eq!(continuous.restore_ready, None);
    }

    #[test]
    fn activity_between_suspend_and_active_remains_the_one_readiness_signal() {
        let mut translator = translator();
        identify(
            &mut translator,
            1,
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            0x00cd,
        );
        translator
            .translate(activity(2, 1_000, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
            .expect("initial activity translates");

        for (sequence, observed_at_ms, raw_value) in [(3, 1_001, 1), (5, 1_003, 0)] {
            let mut observation = raw_at(
                sequence,
                observed_at_ms,
                HFX_UAPI_OBSERVATION_KIND_RECEIVER_SUSPENDED,
                HFX_UAPI_ENDPOINT_SLOT_RECEIVER,
                raw_value,
            );
            observation.source = HFX_UAPI_OBSERVATION_SOURCE_POWER_MANAGEMENT;
            observation.confidence = HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT;
            translator
                .translate(observation)
                .expect("power observation translates");

            if raw_value == 1 {
                let early = translator
                    .translate(activity(4, 1_002, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
                    .expect("pre-active input translates");
                assert_eq!(
                    early.restore_ready.as_ref().map(LogicalDeviceId::as_str),
                    Some("paired-mouse-00cd")
                );
            }
        }

        let continuous = translator
            .translate(activity(6, 1_004, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE))
            .expect("post-active input remains continuous");
        assert_eq!(continuous.restore_ready, None);
    }
}
