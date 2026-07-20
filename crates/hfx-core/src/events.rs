// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{
    DroppedEventCount, EventKind, FindingId, GenerationId, LeaseId, LogicalDeviceId,
    ProjectionRevision, ReceiverId, SequenceNumber, StreamEpoch, StreamId, SubscriptionId,
    TransactionId,
};
use hfx_protocol::{BridgeEvent, EventBatch, EventCursor, SubscriptionRequest};
use std::collections::VecDeque;
use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EventDraft {
    pub kind: EventKind,
    pub receiver_id: Option<ReceiverId>,
    pub generation_id: Option<GenerationId>,
    pub device_id: Option<LogicalDeviceId>,
    pub lease_id: Option<LeaseId>,
    pub transaction_id: Option<TransactionId>,
    pub finding_id: Option<FindingId>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum EventLogError {
    InvalidCapacity,
    SubscriptionMismatch,
    SequenceExhausted,
    DropCounterExhausted,
}

impl fmt::Display for EventLogError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidCapacity => "event log capacity is invalid",
            Self::SubscriptionMismatch => "event subscription identity does not match",
            Self::SequenceExhausted => "event sequence is exhausted",
            Self::DropCounterExhausted => "event drop counter is exhausted",
        })
    }
}

impl std::error::Error for EventLogError {}

#[derive(Clone, Debug)]
pub struct BoundedEventLog {
    stream_id: StreamId,
    stream_epoch: StreamEpoch,
    projection_revision: ProjectionRevision,
    capacity: usize,
    events: VecDeque<BridgeEvent>,
    latest: SequenceNumber,
    dropped: DroppedEventCount,
}

impl BoundedEventLog {
    /// Creates a bounded stream journal.
    ///
    /// # Errors
    ///
    /// Returns an error when capacity is outside the protocol event bound.
    pub fn new(
        stream_id: StreamId,
        stream_epoch: StreamEpoch,
        projection_revision: ProjectionRevision,
        capacity: usize,
    ) -> Result<Self, EventLogError> {
        if !(1..=4096).contains(&capacity) {
            return Err(EventLogError::InvalidCapacity);
        }
        Ok(Self {
            stream_id,
            stream_epoch,
            projection_revision,
            capacity,
            events: VecDeque::with_capacity(capacity),
            latest: SequenceNumber::try_from(0_u64)
                .map_err(|_| EventLogError::SequenceExhausted)?,
            dropped: DroppedEventCount::try_from(0_u64)
                .map_err(|_| EventLogError::DropCounterExhausted)?,
        })
    }

    /// Appends one typed event, evicting the oldest history when full.
    ///
    /// # Errors
    ///
    /// Returns an error if a canonical sequence or drop counter is exhausted.
    pub fn append(&mut self, draft: EventDraft) -> Result<BridgeEvent, EventLogError> {
        let next = self
            .latest
            .get()
            .checked_add(1)
            .ok_or(EventLogError::SequenceExhausted)?;
        let sequence =
            SequenceNumber::try_from(next).map_err(|_| EventLogError::SequenceExhausted)?;
        let event = BridgeEvent {
            sequence,
            kind: draft.kind,
            receiver_id: draft.receiver_id,
            generation_id: draft.generation_id,
            device_id: draft.device_id,
            lease_id: draft.lease_id,
            transaction_id: draft.transaction_id,
            finding_id: draft.finding_id,
        };
        let next_dropped = if self.events.len() == self.capacity {
            let dropped = self
                .dropped
                .get()
                .checked_add(1)
                .ok_or(EventLogError::DropCounterExhausted)?;
            Some(
                DroppedEventCount::try_from(dropped)
                    .map_err(|_| EventLogError::DropCounterExhausted)?,
            )
        } else {
            None
        };
        if self.events.len() == self.capacity {
            self.events.pop_front();
            self.dropped = next_dropped.ok_or(EventLogError::DropCounterExhausted)?;
        }
        self.latest = sequence;
        self.events.push_back(event.clone());
        Ok(event)
    }

    /// Reads a bounded event batch after the supplied cursor.
    ///
    /// # Errors
    ///
    /// Returns an error when an existing subscription is resumed under a
    /// different subscription identity.
    pub fn read(
        &self,
        subscription_id: SubscriptionId,
        request: &SubscriptionRequest,
    ) -> Result<EventBatch, EventLogError> {
        if request
            .subscription_id
            .as_ref()
            .is_some_and(|expected| expected != &subscription_id)
        {
            return Err(EventLogError::SubscriptionMismatch);
        }
        let stream_mismatch = request.expected_cursor.as_ref().is_some_and(|cursor| {
            cursor.stream_id != self.stream_id
                || cursor.stream_epoch != self.stream_epoch
                || cursor.projection_revision != self.projection_revision
        });
        let oldest = self
            .events
            .front()
            .map_or(self.latest, |event| event.sequence);
        let lower_bound = oldest.get().saturating_sub(1);
        let requested_sequence = request
            .expected_cursor
            .as_ref()
            .map_or(0, |cursor| cursor.sequence.get());
        let cursor = requested_sequence;
        let cursor_gap = stream_mismatch || cursor < lower_bound || cursor > self.latest.get();
        let events = if cursor_gap {
            Vec::new()
        } else {
            self.events
                .iter()
                .filter(|event| event.sequence.get() > requested_sequence)
                .take(usize::from(request.max_events.get()))
                .cloned()
                .collect()
        };
        let next_sequence = events
            .last()
            .map_or(requested_sequence, |event| event.sequence.get());
        let next_sequence = SequenceNumber::try_from(next_sequence)
            .map_err(|_| EventLogError::SequenceExhausted)?;
        let has_more = !cursor_gap && next_sequence < self.latest;
        Ok(EventBatch {
            subscription_id,
            next_cursor: EventCursor {
                stream_id: self.stream_id.clone(),
                stream_epoch: self.stream_epoch,
                projection_revision: self.projection_revision,
                sequence: next_sequence,
            },
            events,
            oldest_available: oldest,
            latest_available: self.latest,
            dropped_events: self.dropped,
            cursor_gap,
            has_more,
        })
    }

    #[must_use]
    pub const fn latest_sequence(&self) -> SequenceNumber {
        self.latest
    }
}
