// SPDX-License-Identifier: GPL-2.0-only

//! Bridge-decoded child battery state forwarded to the kernel for
//! `power_supply` publication.
//!
//! The bridge is the only reader of battery reports. The kernel publishes
//! exactly the state forwarded here and never queries the device for it.

use crate::{
    AlignedU64, HFX_UAPI_BATTERY_MAX_CAPACITY, HFX_UAPI_BATTERY_MODEL_NAME_BYTES,
    HFX_UAPI_BATTERY_PRESENCE_ABSENT, HFX_UAPI_BATTERY_PRESENCE_PRESENT,
    HFX_UAPI_BATTERY_STATUS_CHARGING, HFX_UAPI_BATTERY_STATUS_DISCHARGING,
    HFX_UAPI_BATTERY_STATUS_FULL, HFX_UAPI_BATTERY_STATUS_UNKNOWN,
    HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
    HFX_UAPI_POWER_SUPPLY_ABI_VERSION, HfxUapiChildBattery, KernelTransportError,
};
use hfx_domain::GenerationId;

/// The manufacturer is published separately, so the forwarded model name
/// omits it and keeps the most specific part inside the fixed field.
const MANUFACTURER_PREFIX: &str = "Razer ";
const MODEL_NAME_LIMIT: usize = HFX_UAPI_BATTERY_MODEL_NAME_BYTES - 1;

/// One child lane that can carry a published battery.
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum ChildBatteryLane {
    Pointer,
    Keyboard,
}

impl ChildBatteryLane {
    pub const ALL: [Self; 2] = [Self::Pointer, Self::Keyboard];

    #[must_use]
    pub const fn endpoint_slot(self) -> u32 {
        match self {
            Self::Pointer => HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            Self::Keyboard => HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
        }
    }

    #[must_use]
    pub const fn from_endpoint_slot(endpoint_slot: u32) -> Option<Self> {
        match endpoint_slot {
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE => Some(Self::Pointer),
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE => Some(Self::Keyboard),
            _ => None,
        }
    }

    #[must_use]
    pub const fn index(self) -> usize {
        match self {
            Self::Pointer => 0,
            Self::Keyboard => 1,
        }
    }
}

/// Charge status as the bridge knows it. Only a child profile with a
/// qualified charge decoder yields a known status; every other child, and
/// every value that decoder cannot prove, forwards `Unknown`.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum ChildBatteryStatus {
    #[default]
    Unknown,
    Discharging,
    Charging,
    Full,
}

impl ChildBatteryStatus {
    const fn uapi(self) -> u32 {
        match self {
            Self::Unknown => HFX_UAPI_BATTERY_STATUS_UNKNOWN,
            Self::Discharging => HFX_UAPI_BATTERY_STATUS_DISCHARGING,
            Self::Charging => HFX_UAPI_BATTERY_STATUS_CHARGING,
            Self::Full => HFX_UAPI_BATTERY_STATUS_FULL,
        }
    }
}

/// One lane's forwarded battery state.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub enum ChildBatteryState {
    /// No battery is published for the lane.
    #[default]
    Absent,
    /// A bridge-decoded reading. Build it with [`ChildBatteryState::present`]
    /// so the capacity and model name are already bounded.
    Present {
        capacity: u8,
        status: ChildBatteryStatus,
        model_name: String,
    },
}

impl ChildBatteryState {
    /// Builds a bounded reading: capacity is capped at 100 percent and the
    /// model name keeps printable ASCII only, without the manufacturer
    /// prefix, within the fixed UAPI field.
    #[must_use]
    pub fn present(capacity: u8, status: ChildBatteryStatus, model_name: &str) -> Self {
        Self::Present {
            capacity: capacity.min(max_capacity()),
            status,
            model_name: bounded_model_name(model_name),
        }
    }

    #[must_use]
    pub const fn is_present(&self) -> bool {
        matches!(self, Self::Present { .. })
    }

    /// Encodes this state for one exact receiver generation and lane.
    ///
    /// # Errors
    ///
    /// Returns an ABI error when the fixed record size cannot be represented.
    pub fn encode(
        &self,
        lane: ChildBatteryLane,
        generation_id: GenerationId,
    ) -> Result<HfxUapiChildBattery, KernelTransportError> {
        let size = u32::try_from(core::mem::size_of::<HfxUapiChildBattery>()).map_err(|_| {
            KernelTransportError::safe(crate::KernelTransportErrorKind::AbiMismatch)
        })?;
        let mut request = HfxUapiChildBattery {
            version: HFX_UAPI_POWER_SUPPLY_ABI_VERSION,
            size,
            receiver_generation: AlignedU64(generation_id.get()),
            endpoint_slot: lane.endpoint_slot(),
            presence: HFX_UAPI_BATTERY_PRESENCE_ABSENT,
            ..HfxUapiChildBattery::default()
        };
        if let Self::Present {
            capacity,
            status,
            model_name,
        } = self
        {
            request.presence = HFX_UAPI_BATTERY_PRESENCE_PRESENT;
            request.capacity = u32::from((*capacity).min(max_capacity()));
            request.status = status.uapi();
            let name = bounded_model_name(model_name);
            request.model_name[..name.len()].copy_from_slice(name.as_bytes());
        }
        Ok(request)
    }
}

/// A writer that can forward child battery state to the kernel.
pub trait ChildBatteryPublisher {
    /// Forwards one lane's state for the writer's bound receiver generation.
    ///
    /// # Errors
    ///
    /// Returns a typed error when the kernel rejects the state, the writer
    /// file, or the generation, or when the lane's interface is not bound.
    fn publish_child_battery(
        &self,
        lane: ChildBatteryLane,
        state: &ChildBatteryState,
    ) -> Result<(), KernelTransportError>;
}

fn max_capacity() -> u8 {
    u8::try_from(HFX_UAPI_BATTERY_MAX_CAPACITY).unwrap_or(u8::MAX)
}

fn bounded_model_name(model_name: &str) -> String {
    let model_name = model_name
        .strip_prefix(MANUFACTURER_PREFIX)
        .unwrap_or(model_name);
    let mut bounded = model_name
        .chars()
        .filter(|character| matches!(character, ' '..='~'))
        .take(MODEL_NAME_LIMIT)
        .collect::<String>();
    let trimmed = bounded.trim_end().len();
    bounded.truncate(trimmed);
    bounded
}

#[cfg(test)]
mod tests {
    use super::*;

    fn generation(value: u64) -> GenerationId {
        GenerationId::try_from(value).expect("test generation is valid")
    }

    #[test]
    fn present_state_is_bounded_and_encoded_for_one_lane() {
        let state = ChildBatteryState::present(
            62,
            ChildBatteryStatus::Unknown,
            "Razer DeathStalker V2 Pro Tenkeyless",
        );
        assert!(state.is_present());
        let request = state
            .encode(ChildBatteryLane::Keyboard, generation(9))
            .expect("record encodes");

        assert_eq!(request.version, HFX_UAPI_POWER_SUPPLY_ABI_VERSION);
        assert_eq!(request.size, 64);
        assert_eq!(request.receiver_generation.0, 9);
        assert_eq!(request.endpoint_slot, HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE);
        assert_eq!(request.presence, HFX_UAPI_BATTERY_PRESENCE_PRESENT);
        assert_eq!(request.capacity, 62);
        assert_eq!(request.status, HFX_UAPI_BATTERY_STATUS_UNKNOWN);
        let end = request
            .model_name
            .iter()
            .position(|byte| *byte == 0)
            .expect("name is terminated");
        assert_eq!(
            &request.model_name[..end],
            b"DeathStalker V2 Pro Tenkeyless"
        );
        assert!(request.model_name[end..].iter().all(|byte| *byte == 0));
    }

    #[test]
    fn model_name_keeps_printable_ascii_inside_the_field() {
        let state = ChildBatteryState::present(
            150,
            ChildBatteryStatus::Charging,
            "Razer Ultra\u{e9}\nLong Model Name Which Does Not Fit",
        );
        let ChildBatteryState::Present {
            capacity,
            model_name,
            ..
        } = &state
        else {
            panic!("reading is present");
        };
        assert_eq!(*capacity, 100);
        assert_eq!(model_name.len(), MODEL_NAME_LIMIT);
        assert!(model_name.bytes().all(|byte| (0x20..=0x7e).contains(&byte)));
        assert!(model_name.starts_with("UltraLong Model"));

        let request = state
            .encode(ChildBatteryLane::Pointer, generation(1))
            .expect("record encodes");
        assert_eq!(request.capacity, 100);
        assert_eq!(request.status, HFX_UAPI_BATTERY_STATUS_CHARGING);
        assert_eq!(request.model_name[MODEL_NAME_LIMIT], 0);
    }

    #[test]
    fn absent_state_carries_no_value() {
        assert!(!ChildBatteryState::Absent.is_present());
        let request = ChildBatteryState::Absent
            .encode(ChildBatteryLane::Pointer, generation(3))
            .expect("record encodes");
        assert_eq!(request.endpoint_slot, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE);
        assert_eq!(request.presence, HFX_UAPI_BATTERY_PRESENCE_ABSENT);
        assert_eq!(request.capacity, 0);
        assert_eq!(request.status, HFX_UAPI_BATTERY_STATUS_UNKNOWN);
        assert!(request.model_name.iter().all(|byte| *byte == 0));
    }

    #[test]
    fn lanes_map_only_child_endpoint_slots() {
        for lane in ChildBatteryLane::ALL {
            assert_eq!(
                ChildBatteryLane::from_endpoint_slot(lane.endpoint_slot()),
                Some(lane)
            );
        }
        assert_eq!(
            ChildBatteryLane::from_endpoint_slot(crate::HFX_UAPI_ENDPOINT_SLOT_RECEIVER),
            None
        );
    }
}
