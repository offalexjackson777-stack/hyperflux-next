// SPDX-License-Identifier: GPL-2.0-only

//! Canonical translation from bridge subsystems into public RPC failures.

use crate::{
    NativeExchangeJournalError, NativeRouteJournalError, NativeRouteProjectionError, RpcFailure,
    RuntimeIdentityError, SnapshotProjectionError, SubscriptionRegistryError,
};
use hfx_core::{
    DesiredEffectError, EventLogError, LeaseManagerError, OutcomeJournalError, RestorationError,
    TransactionCoordinatorError, TransactionQueueError,
};
use hfx_domain::{FindingId, ProtocolErrorKind};
use hfx_errors::ErrorCode;

pub(super) fn finding(code: ErrorCode) -> FindingId {
    FindingId::try_from(code.as_str())
        .expect("generated error codes satisfy the finding identity bound")
}

pub(super) fn desired_effect_failure(_error: DesiredEffectError) -> RpcFailure {
    RpcFailure::internal()
}

pub(super) fn native_projection_failure(_error: NativeRouteProjectionError) -> RpcFailure {
    RpcFailure::internal()
}

pub(super) fn native_route_failure(error: NativeRouteJournalError) -> RpcFailure {
    match error {
        NativeRouteJournalError::SubscriptionMismatch => request_failure(),
        NativeRouteJournalError::RouteCapacity => queue_failure(),
        NativeRouteJournalError::InvalidCapacity
        | NativeRouteJournalError::Identity(_)
        | NativeRouteJournalError::InvalidStreamIdentity
        | NativeRouteJournalError::SequenceExhausted
        | NativeRouteJournalError::DropCounterExhausted => RpcFailure::internal(),
    }
}

pub(super) fn native_exchange_failure(error: NativeExchangeJournalError) -> RpcFailure {
    match error {
        NativeExchangeJournalError::RequestIdentityReused => request_failure(),
        NativeExchangeJournalError::InvalidCapacity => RpcFailure::internal(),
    }
}

pub(super) const fn request_failure() -> RpcFailure {
    RpcFailure::new(ErrorCode::HfxRequest001, ProtocolErrorKind::InvalidRequest)
}

pub(super) const fn ownership_failure() -> RpcFailure {
    RpcFailure::new(
        ErrorCode::HfxOwnership002,
        ProtocolErrorKind::OwnershipConflict,
    )
}

pub(super) const fn generation_failure() -> RpcFailure {
    RpcFailure::new(
        ErrorCode::HfxGeneration001,
        ProtocolErrorKind::StaleGeneration,
    )
}

pub(super) const fn profile_failure() -> RpcFailure {
    RpcFailure::new(
        ErrorCode::HfxProfile001,
        ProtocolErrorKind::UnsupportedFeature,
    )
}

pub(super) const fn transport_unavailable_failure() -> RpcFailure {
    RpcFailure::new(
        ErrorCode::HfxTransport001,
        ProtocolErrorKind::TransportFailure,
    )
}

pub(super) const fn queue_failure() -> RpcFailure {
    RpcFailure::new(ErrorCode::HfxQueue001, ProtocolErrorKind::QueueFull)
}

pub(super) const fn deadline_failure() -> RpcFailure {
    RpcFailure::new(
        ErrorCode::HfxDeadline001,
        ProtocolErrorKind::DeadlineExceeded,
    )
}

pub(super) const fn outcome_unknown_failure() -> RpcFailure {
    RpcFailure::new(ErrorCode::HfxOutcome001, ProtocolErrorKind::OutcomeUnknown)
}

pub(super) const fn outcome_evicted_failure() -> RpcFailure {
    RpcFailure::new(ErrorCode::HfxOutcome002, ProtocolErrorKind::OutcomeEvicted)
}

pub(super) fn identity_failure(_error: RuntimeIdentityError) -> RpcFailure {
    RpcFailure::internal()
}

pub(super) fn snapshot_failure(error: &SnapshotProjectionError) -> RpcFailure {
    match error {
        SnapshotProjectionError::Restoration(_) => RpcFailure::new(
            ErrorCode::HfxPersistence001,
            ProtocolErrorKind::InternalFailure,
        ),
        SnapshotProjectionError::InvalidSnapshot(_) => RpcFailure::internal(),
    }
}

pub(super) fn persistence_write_failure(_error: RestorationError) -> RpcFailure {
    RpcFailure::new(
        ErrorCode::HfxPersistence002,
        ProtocolErrorKind::InternalFailure,
    )
}

pub(super) fn lease_failure(error: &LeaseManagerError) -> RpcFailure {
    match error {
        LeaseManagerError::InvalidRequest(_) | LeaseManagerError::RequestIdReused => {
            request_failure()
        }
        LeaseManagerError::LeaseCapacity | LeaseManagerError::HistoryCapacity => queue_failure(),
        LeaseManagerError::UnknownLease | LeaseManagerError::NotOwner => ownership_failure(),
        LeaseManagerError::InvalidCapacity | LeaseManagerError::ClockOverflow => {
            RpcFailure::internal()
        }
    }
}

pub(super) fn transaction_failure(error: TransactionCoordinatorError) -> RpcFailure {
    match error {
        TransactionCoordinatorError::Digest(_)
        | TransactionCoordinatorError::RequestIdReused
        | TransactionCoordinatorError::TransactionIdReused => request_failure(),
        TransactionCoordinatorError::OutcomeEvicted(_) => outcome_evicted_failure(),
        TransactionCoordinatorError::SessionRevoked
        | TransactionCoordinatorError::OwnershipDenied => ownership_failure(),
        TransactionCoordinatorError::StaleGeneration => generation_failure(),
        TransactionCoordinatorError::UnsupportedResource => profile_failure(),
        TransactionCoordinatorError::ProfileBindingChanged => {
            RpcFailure::new(ErrorCode::HfxProfile002, ProtocolErrorKind::StaleGeneration)
        }
        TransactionCoordinatorError::DeviceNotReady(_) => RpcFailure::new(
            ErrorCode::HfxTransport001,
            ProtocolErrorKind::TransportFailure,
        ),
        TransactionCoordinatorError::Queue(error) => match error {
            TransactionQueueError::InvalidRequest(_) => request_failure(),
            TransactionQueueError::DeadlineElapsed => deadline_failure(),
            TransactionQueueError::Full => queue_failure(),
            TransactionQueueError::InvalidCapacity => RpcFailure::internal(),
        },
        TransactionCoordinatorError::Outcome(error) => match error {
            OutcomeJournalError::CapacityExhausted => queue_failure(),
            OutcomeJournalError::RequestIdReused => request_failure(),
            OutcomeJournalError::InvalidCapacity
            | OutcomeJournalError::UnavailableOutcome
            | OutcomeJournalError::IdentityChanged
            | OutcomeJournalError::InvalidProgression
            | OutcomeJournalError::TerminalOutcomeChanged => RpcFailure::internal(),
        },
        TransactionCoordinatorError::TransactionNotQueued(_) => outcome_unknown_failure(),
        TransactionCoordinatorError::InvalidCapacity
        | TransactionCoordinatorError::Event(_)
        | TransactionCoordinatorError::Transition(_)
        | TransactionCoordinatorError::FrameCount => RpcFailure::internal(),
    }
}

pub(super) fn event_failure(error: &EventLogError) -> RpcFailure {
    match error {
        EventLogError::SubscriptionMismatch => request_failure(),
        EventLogError::InvalidCapacity
        | EventLogError::SequenceExhausted
        | EventLogError::DropCounterExhausted => RpcFailure::internal(),
    }
}

pub(super) fn subscription_failure(error: SubscriptionRegistryError) -> RpcFailure {
    match error {
        SubscriptionRegistryError::CapacityExhausted => queue_failure(),
        SubscriptionRegistryError::UnknownSubscription
        | SubscriptionRegistryError::SubscriptionMismatch => request_failure(),
        SubscriptionRegistryError::InvalidCapacity | SubscriptionRegistryError::Identity(_) => {
            RpcFailure::internal()
        }
    }
}
