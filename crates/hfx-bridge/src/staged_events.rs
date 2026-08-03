// SPDX-License-Identifier: GPL-2.0-only

use hfx_core::{BoundedEventLog, EventDelivery, EventDraft, EventLogError, EventSink};
use hfx_domain::{EventKind, GenerationId, LeaseId, LogicalDeviceId, ReceiverId};
use hfx_protocol::BridgeEvent;

/// In-memory event sink used while a multi-owner bridge mutation is staged.
///
/// Canonical events enter the bounded log before state is committed. External
/// delivery remains best effort and happens only after the state commit.
#[derive(Clone, Debug, Default)]
pub(crate) struct StagedEvents {
    events: Vec<BridgeEvent>,
}

impl StagedEvents {
    pub(crate) fn append_lifecycle(
        &mut self,
        events: &mut BoundedEventLog,
        kind: EventKind,
        receiver_id: ReceiverId,
        generation_id: GenerationId,
        device_id: Option<LogicalDeviceId>,
    ) -> Result<(), EventLogError> {
        self.append(
            events,
            EventDraft {
                kind,
                receiver_id: Some(receiver_id),
                generation_id: Some(generation_id),
                device_id,
                lease_id: None,
                transaction_id: None,
                finding_id: None,
            },
        )
    }

    pub(crate) fn append_ownership(
        &mut self,
        events: &mut BoundedEventLog,
        lease_id: LeaseId,
    ) -> Result<(), EventLogError> {
        self.append(
            events,
            EventDraft {
                kind: EventKind::OwnershipChanged,
                receiver_id: None,
                generation_id: None,
                device_id: None,
                lease_id: Some(lease_id),
                transaction_id: None,
                finding_id: None,
            },
        )
    }

    pub(crate) fn publish<S: EventSink>(self, sink: &mut S) {
        for event in self.events {
            let _ = sink.try_emit(&event);
        }
    }

    fn append(
        &mut self,
        events: &mut BoundedEventLog,
        draft: EventDraft,
    ) -> Result<(), EventLogError> {
        let event = events.append(draft)?;
        let _ = self.try_emit(&event);
        Ok(())
    }
}

impl EventSink for StagedEvents {
    fn try_emit(&mut self, event: &BridgeEvent) -> EventDelivery {
        self.events.push(event.clone());
        EventDelivery::Accepted
    }
}
