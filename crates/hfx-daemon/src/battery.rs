// SPDX-License-Identifier: GPL-2.0-only

//! Forwarding of bridge-decoded child battery state to the kernel writer.
//!
//! The bridge keeps reading battery exactly as before. Each applied lifecycle
//! fact that changes what a child lane should publish is recorded here, and
//! only lanes whose state differs from what the current writer file already
//! carries are forwarded. The kernel drops every published battery when the
//! writer file closes, so a released or newly installed writer starts from
//! nothing published.

use hfx_bridge::LifecycleObservationKind;
use hfx_domain::{LogicalDeviceId, PairingState, RouteState};
use hfx_kernel_transport::{ChildBatteryLane, ChildBatteryState, ChildBatteryStatus};

/// The published-state mirror for one receiver endpoint.
#[derive(Debug, Default)]
pub struct ChildBatteryForwarder {
    desired: [ChildBatteryState; 2],
    published: [ChildBatteryState; 2],
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
        self.desired[lane.index()] = match reading {
            Some(capacity) => {
                ChildBatteryState::present(capacity, ChildBatteryStatus::Unknown, model_name)
            }
            None => ChildBatteryState::Absent,
        };
    }

    /// A new writer file starts with no published battery.
    pub fn writer_installed(&mut self) {
        self.published = Default::default();
    }

    /// Closing the writer file unregisters every battery it published, and
    /// a released writer must not resend old values on its own.
    pub fn writer_released(&mut self) {
        self.published = Default::default();
        self.desired = Default::default();
    }

    /// Forwards every lane whose desired state differs from the published
    /// one. A rejected forward is not retried until the lane changes again,
    /// so one unbound interface cannot turn every tick into a kernel call.
    /// Returns the number of forwards that the kernel accepted.
    pub fn flush<E>(
        &mut self,
        mut publish: impl FnMut(ChildBatteryLane, &ChildBatteryState) -> Result<(), E>,
    ) -> usize {
        let mut accepted = 0;
        for lane in ChildBatteryLane::ALL {
            let index = lane.index();
            if self.desired[index] == self.published[index] {
                continue;
            }
            if publish(lane, &self.desired[index]).is_ok() {
                accepted += 1;
            }
            self.published[index] = self.desired[index].clone();
        }
        accepted
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

    #[test]
    fn a_rejected_forward_waits_for_the_next_change() {
        let mut forwarder = ChildBatteryForwarder::default();
        forwarder.record(&reported(20), lookup);
        assert_eq!(forwarder.flush(|_, _| Err(())), 0);
        assert!(sent(&mut forwarder).is_empty());
        forwarder.record(&reported(19), lookup);
        assert_eq!(sent(&mut forwarder).len(), 1);
    }
}
