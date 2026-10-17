// SPDX-License-Identifier: GPL-2.0-only

//! Forwarding of bridge-decoded child battery state to the kernel writer.
//!
//! The bridge keeps reading battery exactly as before. Each applied lifecycle
//! fact that changes what a child lane should publish is recorded here, and
//! only lanes whose state differs from what the current writer file already
//! carries are forwarded. The kernel drops every published battery when the
//! writer file closes, so a released or newly installed writer starts from
//! nothing published.
//!
//! The charge state rides on the battery reading of the same lane. It comes
//! only from a profile's qualified charge decoder, annotates only a lane that
//! currently has a reading, and is forgotten whenever that reading goes away
//! or the writer session ends, so an old charge value is never reused.

use hfx_bridge::LifecycleObservationKind;
use hfx_domain::{LogicalDeviceId, PairingState, RouteState};
use hfx_kernel_transport::{ChildBatteryLane, ChildBatteryState, ChildBatteryStatus};

/// One decoded charge state, as the child profile's qualified charge or
/// route charge decoder maps it in the context of its report. Anything the
/// decoder cannot prove is `Unknown`.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum ChildChargeReading {
    #[default]
    Unknown,
    Charging,
    /// Not charging while the same report proves the device is off its
    /// charging mat or cable.
    Discharging,
}

/// The published-state mirror for one receiver endpoint.
#[derive(Debug, Default)]
pub struct ChildBatteryForwarder {
    desired: [ChildBatteryState; 2],
    published: [ChildBatteryState; 2],
    charge: [ChildChargeReading; 2],
    /// The kernel rejected the last forward of this lane.
    rejected: [bool; 2],
    /// A new reading arrived since that rejection, so the next flush sends
    /// the lane again even when the reading itself did not change.
    resend: [bool; 2],
}

impl ChildBatteryForwarder {
    /// Records one applied lifecycle fact. `lookup` maps a logical device to
    /// its child lane and catalog model name; devices without a lane are
    /// ignored.
    pub fn record<'a>(
        &mut self,
        kind: &LifecycleObservationKind,
        lookup: impl FnOnce(&LogicalDeviceId) -> Option<(ChildBatteryLane, &'a str)>,
    ) {
        let Some((device_id, reading)) = battery_fact(kind) else {
            return;
        };
        let Some((lane, model_name)) = lookup(device_id) else {
            return;
        };
        let index = lane.index();
        self.resend[index] = self.rejected[index];
        if let Some(capacity) = reading {
            let status = charge_status(capacity, self.charge[index]);
            self.desired[index] = ChildBatteryState::present(capacity, status, model_name);
        } else {
            self.charge[index] = ChildChargeReading::Unknown;
            self.desired[index] = ChildBatteryState::Absent;
        }
    }

    /// Records one decoded charge field for a child lane. It only annotates
    /// a lane that already has a reading: the kernel reports the battery
    /// level before the charge field of the same report, and a lane without
    /// a reading must not keep a charge value for later.
    pub fn record_charge(&mut self, lane: ChildBatteryLane, reading: ChildChargeReading) {
        let index = lane.index();
        let ChildBatteryState::Present {
            capacity, status, ..
        } = &mut self.desired[index]
        else {
            return;
        };
        self.charge[index] = reading;
        *status = charge_status(*capacity, reading);
    }

    /// A new writer file starts with no published battery.
    pub fn writer_installed(&mut self) {
        self.published = Default::default();
        self.rejected = Default::default();
        self.resend = Default::default();
    }

    /// Closing the writer file unregisters every battery it published, and
    /// a released writer must not resend old values on its own.
    pub fn writer_released(&mut self) {
        self.published = Default::default();
        self.desired = Default::default();
        self.charge = Default::default();
        self.rejected = Default::default();
        self.resend = Default::default();
    }

    /// Forwards every lane whose desired state differs from the published
    /// one. A rejected forward is not retried on every tick, so one unbound
    /// interface cannot turn every tick into a kernel call. It is retried
    /// with the lane's next reading, even an unchanged one: a level rejected
    /// before the child's interface was bound must not stay unpublished
    /// until the level itself changes.
    /// Returns the number of forwards that the kernel accepted.
    pub fn flush<E>(
        &mut self,
        mut publish: impl FnMut(ChildBatteryLane, &ChildBatteryState) -> Result<(), E>,
    ) -> usize {
        let mut accepted = 0;
        for lane in ChildBatteryLane::ALL {
            let index = lane.index();
            if self.desired[index] == self.published[index] && !self.resend[index] {
                continue;
            }
            let ok = publish(lane, &self.desired[index]).is_ok();
            if ok {
                accepted += 1;
            }
            self.rejected[index] = !ok;
            self.resend[index] = false;
            self.published[index] = self.desired[index].clone();
        }
        accepted
    }
}

/// Maps a decoded charge field onto the published status. Full is inferred
/// from charging at a 100 percent level; the device sends no full value.
const fn charge_status(capacity: u8, reading: ChildChargeReading) -> ChildBatteryStatus {
    match reading {
        ChildChargeReading::Charging if capacity >= 100 => ChildBatteryStatus::Full,
        ChildChargeReading::Charging => ChildBatteryStatus::Charging,
        ChildChargeReading::Discharging => ChildBatteryStatus::Discharging,
        ChildChargeReading::Unknown => ChildBatteryStatus::Unknown,
    }
}

/// Returns the device and its new reading (`None` for no battery) when the
/// fact changes what the child lane should publish.
fn battery_fact(kind: &LifecycleObservationKind) -> Option<(&LogicalDeviceId, Option<u8>)> {
    match kind {
        LifecycleObservationKind::BatteryReported {
            device_id,
            percentage,
        } => Some((device_id, Some(percentage.get()))),
        LifecycleObservationKind::BatteryUnavailable { device_id }
        | LifecycleObservationKind::BatteryStale { device_id }
        | LifecycleObservationKind::Pairing {
            device_id,
            value: PairingState::Unpaired | PairingState::Unknown,
        }
        | LifecycleObservationKind::Route {
            device_id,
            value: RouteState::Unavailable,
            ..
        } => Some((device_id, None)),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use hfx_domain::{BatteryPercent, EndpointId};

    const MODEL: &str = "Razer Basilisk V3 Pro 35K";

    fn device(name: &str) -> LogicalDeviceId {
        LogicalDeviceId::try_from(name).expect("device id is valid")
    }

    fn mouse() -> LogicalDeviceId {
        device("paired-mouse-00ab")
    }

    fn lookup(device_id: &LogicalDeviceId) -> Option<(ChildBatteryLane, &'static str)> {
        (*device_id == mouse()).then_some((ChildBatteryLane::Pointer, MODEL))
    }

    fn reported(percent: u8) -> LifecycleObservationKind {
        LifecycleObservationKind::BatteryReported {
            device_id: mouse(),
            percentage: BatteryPercent::try_from(percent).expect("percent is valid"),
        }
    }

    fn sent(forwarder: &mut ChildBatteryForwarder) -> Vec<(ChildBatteryLane, ChildBatteryState)> {
        let mut sent = Vec::new();
        forwarder.flush(|lane, state| {
            sent.push((lane, state.clone()));
            Ok::<(), ()>(())
        });
        sent
    }

    fn present(capacity: u8) -> ChildBatteryState {
        ChildBatteryState::present(capacity, ChildBatteryStatus::Unknown, MODEL)
    }

    #[test]
    fn state_changes_are_forwarded_for_the_child_lane() {
        let mut forwarder = ChildBatteryForwarder::default();
        assert!(sent(&mut forwarder).is_empty());

        forwarder.record(&reported(80), lookup);
        assert_eq!(
            sent(&mut forwarder),
            [(ChildBatteryLane::Pointer, present(80))]
        );

        forwarder.record(&reported(79), lookup);
        let update = sent(&mut forwarder);
        assert_eq!(update, [(ChildBatteryLane::Pointer, present(79))]);
        let ChildBatteryState::Present { model_name, .. } = &update[0].1 else {
            panic!("reading is present");
        };
        assert_eq!(model_name, "Basilisk V3 Pro 35K");
    }

    #[test]
    fn identical_readings_are_forwarded_once() {
        let mut forwarder = ChildBatteryForwarder::default();
        forwarder.record(&reported(55), lookup);
        assert_eq!(sent(&mut forwarder).len(), 1);
        forwarder.record(&reported(55), lookup);
        forwarder.record(&reported(55), lookup);
        assert!(sent(&mut forwarder).is_empty());
    }

    #[test]
    fn stale_gone_or_unpaired_children_are_unpublished() {
        let endpoint_id = EndpointId::try_from("hyperflux-pointer").expect("endpoint id is valid");
        let gone = [
            LifecycleObservationKind::BatteryStale { device_id: mouse() },
            LifecycleObservationKind::BatteryUnavailable { device_id: mouse() },
            LifecycleObservationKind::Pairing {
                device_id: mouse(),
                value: PairingState::Unpaired,
            },
            LifecycleObservationKind::Route {
                device_id: mouse(),
                endpoint_id,
                value: RouteState::Unavailable,
            },
        ];
        for kind in gone {
            let mut forwarder = ChildBatteryForwarder::default();
            forwarder.record(&reported(40), lookup);
            assert_eq!(sent(&mut forwarder).len(), 1);
            forwarder.record(&kind, lookup);
            assert_eq!(
                sent(&mut forwarder),
                [(ChildBatteryLane::Pointer, ChildBatteryState::Absent)],
                "{kind:?} must unpublish the lane"
            );
        }
    }

    #[test]
    fn unrelated_facts_and_unknown_devices_change_nothing() {
        let mut forwarder = ChildBatteryForwarder::default();
        forwarder.record(
            &LifecycleObservationKind::Pairing {
                device_id: mouse(),
                value: PairingState::Paired,
            },
            lookup,
        );
        forwarder.record(
            &LifecycleObservationKind::BatteryReported {
                device_id: device("paired-keyboard-00cd"),
                percentage: BatteryPercent::try_from(10).expect("percent is valid"),
            },
            lookup,
        );
        assert!(sent(&mut forwarder).is_empty());
    }

    #[test]
    fn writer_session_end_clears_and_a_new_writer_republishes() {
        let mut forwarder = ChildBatteryForwarder::default();
        forwarder.record(&reported(66), lookup);
        assert_eq!(sent(&mut forwarder).len(), 1);

        // The kernel already dropped the batteries when the file closed, so
        // nothing is sent for the release itself.
        forwarder.writer_released();
        assert!(sent(&mut forwarder).is_empty());

        forwarder.record(&reported(65), lookup);
        forwarder.writer_installed();
        assert_eq!(
            sent(&mut forwarder),
            [(ChildBatteryLane::Pointer, present(65))]
        );
    }

    #[test]
    fn a_reinstalled_writer_receives_the_last_known_reading() {
        let mut forwarder = ChildBatteryForwarder::default();
        forwarder.record(&reported(30), lookup);
        assert_eq!(sent(&mut forwarder).len(), 1);
        forwarder.writer_installed();
        assert_eq!(
            sent(&mut forwarder),
            [(ChildBatteryLane::Pointer, present(30))]
        );
    }

    fn with_status(capacity: u8, status: ChildBatteryStatus) -> ChildBatteryState {
        ChildBatteryState::present(capacity, status, MODEL)
    }

    #[test]
    fn the_charge_field_sets_the_status_of_the_current_reading() {
        let mut forwarder = ChildBatteryForwarder::default();
        forwarder.record(&reported(80), lookup);
        let cases = [
            (ChildChargeReading::Charging, ChildBatteryStatus::Charging),
            (
                ChildChargeReading::Discharging,
                ChildBatteryStatus::Discharging,
            ),
            (ChildChargeReading::Unknown, ChildBatteryStatus::Unknown),
        ];
        for (reading, status) in cases {
            forwarder.record_charge(ChildBatteryLane::Pointer, reading);
            assert_eq!(
                sent(&mut forwarder),
                [(ChildBatteryLane::Pointer, with_status(80, status))],
                "{reading:?}"
            );
        }

        // The status is merged with the level before the next flush, and the
        // charge carries over to the next level of the same reading.
        forwarder.record_charge(ChildBatteryLane::Pointer, ChildChargeReading::Charging);
        forwarder.record(&reported(99), lookup);
        assert_eq!(
            sent(&mut forwarder),
            [(
                ChildBatteryLane::Pointer,
                with_status(99, ChildBatteryStatus::Charging)
            )]
        );
    }

    #[test]
    fn charging_at_a_full_level_is_published_as_full() {
        let mut forwarder = ChildBatteryForwarder::default();
        forwarder.record(&reported(100), lookup);
        forwarder.record_charge(ChildBatteryLane::Pointer, ChildChargeReading::Charging);
        assert_eq!(
            sent(&mut forwarder),
            [(
                ChildBatteryLane::Pointer,
                with_status(100, ChildBatteryStatus::Full)
            )]
        );
        forwarder.record(&reported(99), lookup);
        assert_eq!(
            sent(&mut forwarder),
            [(
                ChildBatteryLane::Pointer,
                with_status(99, ChildBatteryStatus::Charging)
            )]
        );
        forwarder.record(&reported(100), lookup);
        assert_eq!(
            sent(&mut forwarder),
            [(
                ChildBatteryLane::Pointer,
                with_status(100, ChildBatteryStatus::Full)
            )]
        );
    }

    #[test]
    fn the_keyboard_charge_sets_only_the_keyboard_lane() {
        const KEYBOARD_MODEL: &str = "Razer DeathStalker V2 Pro Tenkeyless";
        let keyboard = device("paired-keyboard-0296");
        let both = |device_id: &LogicalDeviceId| {
            if *device_id == keyboard {
                Some((ChildBatteryLane::Keyboard, KEYBOARD_MODEL))
            } else {
                lookup(device_id)
            }
        };
        let keyboard_level = |percent| LifecycleObservationKind::BatteryReported {
            device_id: keyboard.clone(),
            percentage: BatteryPercent::try_from(percent).expect("percent is valid"),
        };
        let mut forwarder = ChildBatteryForwarder::default();
        forwarder.record(&reported(80), both);
        forwarder.record(&keyboard_level(99), both);
        assert_eq!(sent(&mut forwarder).len(), 2);

        forwarder.record_charge(ChildBatteryLane::Keyboard, ChildChargeReading::Charging);
        let keyboard_charging =
            ChildBatteryState::present(99, ChildBatteryStatus::Charging, KEYBOARD_MODEL);
        assert_eq!(
            sent(&mut forwarder),
            [(ChildBatteryLane::Keyboard, keyboard_charging)]
        );

        // Charging at 100 percent is full; the pointer lane is untouched.
        forwarder.record(&keyboard_level(100), both);
        let keyboard_full =
            ChildBatteryState::present(100, ChildBatteryStatus::Full, KEYBOARD_MODEL);
        assert_eq!(
            sent(&mut forwarder),
            [(ChildBatteryLane::Keyboard, keyboard_full)]
        );
        forwarder.record(&reported(80), both);
        assert!(sent(&mut forwarder).is_empty());
    }

    #[test]
    fn a_lane_without_a_reading_keeps_no_charge_for_later() {
        let mut forwarder = ChildBatteryForwarder::default();
        forwarder.record_charge(ChildBatteryLane::Pointer, ChildChargeReading::Charging);
        assert!(sent(&mut forwarder).is_empty());
        forwarder.record(&reported(50), lookup);
        assert_eq!(
            sent(&mut forwarder),
            [(ChildBatteryLane::Pointer, present(50))]
        );
    }

    #[test]
    fn absence_and_writer_release_forget_the_charge() {
        let mut forwarder = ChildBatteryForwarder::default();
        forwarder.record(&reported(40), lookup);
        forwarder.record_charge(ChildBatteryLane::Pointer, ChildChargeReading::Charging);
        forwarder.record(
            &LifecycleObservationKind::BatteryStale { device_id: mouse() },
            lookup,
        );
        forwarder.record(&reported(41), lookup);
        assert_eq!(
            sent(&mut forwarder),
            [(ChildBatteryLane::Pointer, present(41))]
        );

        forwarder.record_charge(ChildBatteryLane::Pointer, ChildChargeReading::Discharging);
        forwarder.writer_released();
        forwarder.record(&reported(42), lookup);
        forwarder.writer_installed();
        assert_eq!(
            sent(&mut forwarder),
            [(ChildBatteryLane::Pointer, present(42))]
        );
    }

    #[test]
    fn a_reinstalled_writer_receives_the_last_known_charge() {
        let mut forwarder = ChildBatteryForwarder::default();
        forwarder.record(&reported(30), lookup);
        forwarder.record_charge(ChildBatteryLane::Pointer, ChildChargeReading::Discharging);
        assert_eq!(sent(&mut forwarder).len(), 1);
        forwarder.writer_installed();
        assert_eq!(
            sent(&mut forwarder),
            [(
                ChildBatteryLane::Pointer,
                with_status(30, ChildBatteryStatus::Discharging)
            )]
        );
    }

    #[test]
    fn a_rejected_forward_waits_for_the_next_change() {
        let mut forwarder = ChildBatteryForwarder::default();
        forwarder.record(&reported(20), lookup);
        assert_eq!(forwarder.flush(|_, _| Err(())), 0);
        assert!(sent(&mut forwarder).is_empty());
        forwarder.record(&reported(19), lookup);
        assert_eq!(sent(&mut forwarder).len(), 1);
    }

    #[test]
    fn a_rejected_forward_is_retried_with_the_next_unchanged_reading() {
        let mut forwarder = ChildBatteryForwarder::default();
        forwarder.record(&reported(20), lookup);
        assert_eq!(forwarder.flush(|_, _| Err(())), 0);
        assert!(sent(&mut forwarder).is_empty());

        // The same level read again is sent again, once.
        forwarder.record(&reported(20), lookup);
        assert_eq!(sent(&mut forwarder).len(), 1);
        forwarder.record(&reported(20), lookup);
        assert!(sent(&mut forwarder).is_empty());

        // An accepted forward ends the retry; a new writer starts clean.
        forwarder.record(&reported(20), lookup);
        assert_eq!(forwarder.flush(|_, _| Err(())), 0);
        forwarder.writer_installed();
        assert_eq!(sent(&mut forwarder).len(), 1);
        assert!(sent(&mut forwarder).is_empty());
    }
}
