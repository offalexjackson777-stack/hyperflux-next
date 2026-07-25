// SPDX-License-Identifier: GPL-2.0-only

use crate::{RuntimeIdentityError, RuntimeIdentityIssuer};
use hfx_domain::{
    DroppedEventCount, NativeRouteAccess, NativeRouteEventKind, NativeRouteState,
    ProjectionRevision, ReceiverId, ResourceKind, SequenceNumber, StreamEpoch, StreamId,
    SubscriptionId,
};
use hfx_protocol::{
    EventCursor, NativeRoute, NativeRouteEvent, NativeRouteEventBatch, NativeRouteInventory,
    ResourceKey, SubscriptionRequest,
};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, VecDeque};
use std::fmt;

const MAX_NATIVE_ROUTES: usize = 32;

#[allow(clippy::struct_field_names)]
#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct NativeRouteKey {
    receiver_id: ReceiverId,
    device_id: hfx_domain::LogicalDeviceId,
    endpoint_id: hfx_domain::EndpointId,
}

impl From<&NativeRoute> for NativeRouteKey {
    fn from(route: &NativeRoute) -> Self {
        Self {
            receiver_id: route.receiver_id.clone(),
            device_id: route.device_id.clone(),
            endpoint_id: route.endpoint_id.clone(),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeRouteJournalError {
    InvalidCapacity,
    RouteCapacity,
    Identity(RuntimeIdentityError),
    InvalidStreamIdentity,
    SequenceExhausted,
    DropCounterExhausted,
    SubscriptionMismatch,
}

impl fmt::Display for NativeRouteJournalError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidCapacity => "native route event capacity is invalid",
            Self::RouteCapacity => "native route inventory exceeds its public bound",
            Self::Identity(_) => "native route token generation failed",
            Self::InvalidStreamIdentity => "native route stream identity is invalid",
            Self::SequenceExhausted => "native route event sequence is exhausted",
            Self::DropCounterExhausted => "native route event drop counter is exhausted",
            Self::SubscriptionMismatch => "native route subscription cursor does not match",
        })
    }
}

impl std::error::Error for NativeRouteJournalError {}

/// Bounded current-route registry and transition journal for protocol v7.
///
/// Tokens are issued only for qualified, currently available routes. A new
/// generation receives a new token, while exact refreshes preserve authority.
#[derive(Clone, Debug)]
pub struct NativeRouteJournal {
    stream_id: StreamId,
    stream_epoch: StreamEpoch,
    projection_revision: ProjectionRevision,
    capacity: usize,
    current: BTreeMap<NativeRouteKey, NativeRoute>,
    events: VecDeque<NativeRouteEvent>,
    latest: SequenceNumber,
    dropped: DroppedEventCount,
}

impl NativeRouteJournal {
    /// Creates a route stream with identity derived from the ordinary event stream.
    ///
    /// # Errors
    ///
    /// Returns a typed capacity or generated stream-identity failure.
    pub fn new(
        base_stream_id: &StreamId,
        stream_epoch: StreamEpoch,
        projection_revision: ProjectionRevision,
        capacity: usize,
    ) -> Result<Self, NativeRouteJournalError> {
        if !(1..=4096).contains(&capacity) {
            return Err(NativeRouteJournalError::InvalidCapacity);
        }
        let mut hasher = Sha256::new();
        hasher.update(b"hyperflux-native-route-stream-v1\0");
        hasher.update(base_stream_id.as_str().as_bytes());
        let stream_id = StreamId::try_from(format!(
            "native-routes-{}",
            lowercase_hex(hasher.finalize().as_slice())
        ))
        .map_err(|_| NativeRouteJournalError::InvalidStreamIdentity)?;
        Ok(Self {
            stream_id,
            stream_epoch,
            projection_revision,
            capacity,
            current: BTreeMap::new(),
            events: VecDeque::with_capacity(capacity),
            latest: SequenceNumber::try_from(0_u64)
                .map_err(|_| NativeRouteJournalError::SequenceExhausted)?,
            dropped: DroppedEventCount::try_from(0_u64)
                .map_err(|_| NativeRouteJournalError::DropCounterExhausted)?,
        })
    }

    /// Atomically replaces the projected route set and records its transitions.
    ///
    /// # Errors
    ///
    /// Returns a typed route bound, identity, sequence, or drop-counter
    /// failure without partially changing the journal or identity issuer.
    pub fn synchronize(
        &mut self,
        routes: Vec<NativeRoute>,
        identities: &mut RuntimeIdentityIssuer,
    ) -> Result<(), NativeRouteJournalError> {
        if routes.len() > MAX_NATIVE_ROUTES {
            return Err(NativeRouteJournalError::RouteCapacity);
        }
        let mut next = self.clone();
        let mut next_identities = identities.clone();
        next.synchronize_inner(routes, &mut next_identities)?;
        *self = next;
        *identities = next_identities;
        Ok(())
    }

    fn synchronize_inner(
        &mut self,
        routes: Vec<NativeRoute>,
        identities: &mut RuntimeIdentityIssuer,
    ) -> Result<(), NativeRouteJournalError> {
        let mut incoming = BTreeMap::new();
        for mut route in routes {
            let key = NativeRouteKey::from(&route);
            if incoming.contains_key(&key) {
                return Err(NativeRouteJournalError::RouteCapacity);
            }
            self.bind_route_authority(&key, &mut route, identities)?;
            incoming.insert(key, route);
        }

        let previous = self.current.clone();
        for (key, route) in &incoming {
            match previous.get(key) {
                None => self.append(route_event_kind(route), route.clone())?,
                Some(old) if old.generation_id != route.generation_id => {
                    self.append(NativeRouteEventKind::GenerationReplaced, route.clone())?;
                }
                Some(old) if old != route => self.append(route_event_kind(route), route.clone())?,
                Some(_) => {}
            }
        }
        for (key, old) in previous {
            if !incoming.contains_key(&key) {
                let mut withdrawn = old;
                withdrawn.route_state = NativeRouteState::Withdrawn;
                withdrawn.route_token = None;
                withdrawn.resource = None;
                self.append(NativeRouteEventKind::Withdrawn, withdrawn)?;
            }
        }
        self.current = incoming;
        Ok(())
    }

    fn bind_route_authority(
        &self,
        key: &NativeRouteKey,
        route: &mut NativeRoute,
        identities: &mut RuntimeIdentityIssuer,
    ) -> Result<(), NativeRouteJournalError> {
        let writable = route.access == NativeRouteAccess::Qualified
            && route.route_state == NativeRouteState::Available;
        if !writable {
            route.route_token = None;
            route.resource = None;
            return Ok(());
        }
        if let Some(previous) = self.current.get(key)
            && previous.generation_id == route.generation_id
            && previous.access == NativeRouteAccess::Qualified
            && previous.route_state == NativeRouteState::Available
            && let Some(token) = &previous.route_token
        {
            route.route_token = Some(token.clone());
        } else {
            route.route_token = Some(
                identities
                    .native_route_token()
                    .map_err(NativeRouteJournalError::Identity)?,
            );
        }
        route.resource = Some(ResourceKey {
            receiver_id: route.receiver_id.clone(),
            generation_id: route.generation_id,
            device_id: route.device_id.clone(),
            kind: ResourceKind::NativeControl,
        });
        Ok(())
    }

    fn append(
        &mut self,
        kind: NativeRouteEventKind,
        route: NativeRoute,
    ) -> Result<(), NativeRouteJournalError> {
        let next = self
            .latest
            .get()
            .checked_add(1)
            .ok_or(NativeRouteJournalError::SequenceExhausted)?;
        let sequence = SequenceNumber::try_from(next)
            .map_err(|_| NativeRouteJournalError::SequenceExhausted)?;
        if self.events.len() == self.capacity {
            let dropped = self
                .dropped
                .get()
                .checked_add(1)
                .ok_or(NativeRouteJournalError::DropCounterExhausted)?;
            self.dropped = DroppedEventCount::try_from(dropped)
                .map_err(|_| NativeRouteJournalError::DropCounterExhausted)?;
            self.events.pop_front();
        }
        self.latest = sequence;
        self.events.push_back(NativeRouteEvent {
            sequence,
            kind,
            route,
        });
        Ok(())
    }

    #[must_use]
    pub fn inventory(&self) -> NativeRouteInventory {
        NativeRouteInventory {
            cursor: self.cursor(),
            routes: self.current.values().cloned().collect(),
        }
    }

    #[must_use]
    pub fn route_by_token(&self, token: &hfx_domain::NativeRouteToken) -> Option<&NativeRoute> {
        self.current
            .values()
            .find(|route| route.route_token.as_ref() == Some(token))
    }

    #[must_use]
    pub fn route_for_resource(&self, resource: &ResourceKey) -> Option<&NativeRoute> {
        self.current
            .values()
            .find(|route| route.resource.as_ref() == Some(resource))
    }

    /// Reads one bounded route-event batch for an exact subscription cursor.
    ///
    /// # Errors
    ///
    /// Returns a typed subscription, sequence, or cursor-accounting failure.
    pub fn read(
        &self,
        subscription_id: SubscriptionId,
        request: &SubscriptionRequest,
    ) -> Result<NativeRouteEventBatch, NativeRouteJournalError> {
        if request
            .subscription_id
            .as_ref()
            .is_some_and(|expected| expected != &subscription_id)
        {
            return Err(NativeRouteJournalError::SubscriptionMismatch);
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
        let requested = request
            .expected_cursor
            .as_ref()
            .map_or(0, |cursor| cursor.sequence.get());
        let cursor_gap =
            stream_mismatch || requested < lower_bound || requested > self.latest.get();
        let events = if cursor_gap {
            Vec::new()
        } else {
            self.events
                .iter()
                .filter(|event| event.sequence.get() > requested)
                .take(usize::from(request.max_events.get()))
                .cloned()
                .collect::<Vec<_>>()
        };
        let next_sequence = events
            .last()
            .map_or(requested, |event| event.sequence.get());
        let next_sequence = SequenceNumber::try_from(next_sequence)
            .map_err(|_| NativeRouteJournalError::SequenceExhausted)?;
        Ok(NativeRouteEventBatch {
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
            has_more: !cursor_gap && next_sequence < self.latest,
        })
    }

    #[must_use]
    pub fn cursor(&self) -> EventCursor {
        EventCursor {
            stream_id: self.stream_id.clone(),
            stream_epoch: self.stream_epoch,
            projection_revision: self.projection_revision,
            sequence: self.latest,
        }
    }
}

fn route_event_kind(route: &NativeRoute) -> NativeRouteEventKind {
    match route.route_state {
        NativeRouteState::Available => NativeRouteEventKind::Available,
        NativeRouteState::TemporarilyUnavailable => NativeRouteEventKind::TemporarilyUnavailable,
        NativeRouteState::Withdrawn => NativeRouteEventKind::Withdrawn,
    }
}

fn lowercase_hex(bytes: &[u8]) -> String {
    let mut value = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write as _;
        write!(value, "{byte:02x}").expect("writing to a String cannot fail");
    }
    value
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{SessionIdentityError, SessionIdentitySource};
    use hfx_domain::{
        DeviceKind, EventBatchLimit, GenerationId, NativeProtocolFamily, NativeReportByteCount,
        ProductId, VendorId,
    };

    #[derive(Debug)]
    struct DeterministicSource;

    impl SessionIdentitySource for DeterministicSource {
        fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError> {
            for (index, byte) in destination.iter_mut().enumerate() {
                *byte = u8::try_from(index).expect("test entropy index fits");
            }
            Ok(())
        }
    }

    fn text<T>(value: &str) -> T
    where
        T: TryFrom<String>,
        T::Error: fmt::Debug,
    {
        T::try_from(value.to_owned()).expect("test value is valid")
    }

    fn journal(capacity: usize) -> (NativeRouteJournal, RuntimeIdentityIssuer) {
        let stream_id = text::<StreamId>("events-1");
        let journal = NativeRouteJournal::new(
            &stream_id,
            StreamEpoch::try_from(1_u64).expect("epoch is valid"),
            ProjectionRevision::try_from(1_u32).expect("revision is valid"),
            capacity,
        )
        .expect("journal initializes");
        let mut source = DeterministicSource;
        let identities = RuntimeIdentityIssuer::new(&mut source).expect("identity issuer starts");
        (journal, identities)
    }

    fn route(generation: u64, access: NativeRouteAccess, state: NativeRouteState) -> NativeRoute {
        NativeRoute {
            receiver_id: text("receiver-1"),
            generation_id: GenerationId::try_from(generation).expect("generation is valid"),
            device_id: text("mouse-1"),
            endpoint_id: text("pointer"),
            device_kind: DeviceKind::Mouse,
            vendor_id: VendorId::try_from(0x1532_u16).expect("vendor is valid"),
            product_id: ProductId::try_from(0x00cd_u16).expect("product is valid"),
            route_state: state,
            access,
            protocol_family: text::<NativeProtocolFamily>("razer-feature-report-v1"),
            report_bytes: NativeReportByteCount::try_from(90_u16).expect("report size is valid"),
            route_token: None,
            resource: None,
        }
    }

    #[test]
    fn candidate_routes_never_receive_write_authority() {
        let (mut journal, mut identities) = journal(4);
        journal
            .synchronize(
                vec![route(
                    1,
                    NativeRouteAccess::Candidate,
                    NativeRouteState::Available,
                )],
                &mut identities,
            )
            .expect("candidate route synchronizes");

        let route = &journal.inventory().routes[0];
        assert!(route.route_token.is_none());
        assert!(route.resource.is_none());
        assert_eq!(journal.cursor().sequence.get(), 1);
    }

    #[test]
    fn exact_generation_reuses_token_and_replacement_rotates_it() {
        let (mut journal, mut identities) = journal(8);
        let qualified = route(1, NativeRouteAccess::Qualified, NativeRouteState::Available);
        journal
            .synchronize(vec![qualified.clone()], &mut identities)
            .expect("first route synchronizes");
        let first = journal.inventory().routes[0]
            .route_token
            .clone()
            .expect("qualified route has a token");

        journal
            .synchronize(vec![qualified], &mut identities)
            .expect("exact refresh is idempotent");
        assert_eq!(journal.cursor().sequence.get(), 1);
        assert_eq!(
            journal.inventory().routes[0].route_token.as_ref(),
            Some(&first)
        );

        journal
            .synchronize(
                vec![route(
                    2,
                    NativeRouteAccess::Qualified,
                    NativeRouteState::Available,
                )],
                &mut identities,
            )
            .expect("new generation synchronizes");
        let replacement = &journal.inventory().routes[0];
        assert_ne!(replacement.route_token.as_ref(), Some(&first));
        assert_eq!(journal.cursor().sequence.get(), 2);
        assert_eq!(
            journal.events.back().map(|event| event.kind),
            Some(NativeRouteEventKind::GenerationReplaced)
        );
    }

    #[test]
    fn removed_routes_are_reported_and_old_cursors_gap_after_eviction() {
        let (mut journal, mut identities) = journal(2);
        journal
            .synchronize(
                vec![route(
                    1,
                    NativeRouteAccess::Candidate,
                    NativeRouteState::Available,
                )],
                &mut identities,
            )
            .expect("first route synchronizes");
        let old_cursor = journal.cursor();
        journal
            .synchronize(
                vec![route(
                    1,
                    NativeRouteAccess::Candidate,
                    NativeRouteState::TemporarilyUnavailable,
                )],
                &mut identities,
            )
            .expect("stale route synchronizes");
        journal
            .synchronize(Vec::new(), &mut identities)
            .expect("removal synchronizes");

        let request = SubscriptionRequest {
            client_id: text("client-1"),
            subscription_id: Some(text("subscription-1")),
            expected_cursor: Some(old_cursor),
            max_events: EventBatchLimit::try_from(8_u16).expect("batch limit is valid"),
        };
        let batch = journal
            .read(text("subscription-1"), &request)
            .expect("bounded events are readable");
        assert!(!batch.cursor_gap);
        assert_eq!(batch.events.len(), 2);
        assert_eq!(
            batch.events[0].kind,
            NativeRouteEventKind::TemporarilyUnavailable
        );
        assert_eq!(batch.events[1].kind, NativeRouteEventKind::Withdrawn);
        assert!(journal.inventory().routes.is_empty());

        let beginning = EventCursor {
            sequence: SequenceNumber::try_from(0_u64).expect("zero sequence is valid"),
            ..journal.cursor()
        };
        let gap = journal
            .read(
                text("subscription-1"),
                &SubscriptionRequest {
                    expected_cursor: Some(beginning),
                    ..request
                },
            )
            .expect("gap is represented");
        assert!(gap.cursor_gap);
        assert!(gap.events.is_empty());
    }
}
