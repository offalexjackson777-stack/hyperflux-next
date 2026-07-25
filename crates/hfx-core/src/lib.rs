// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod coordinator;
mod desired_effects;
mod diagnostics;
mod events;
mod leases;
mod lifecycle;
mod ports;
mod receiver_registry;
mod restoration;
mod transactions;

pub use coordinator::{
    CompletedTransaction, DispatchResult, SubmissionResult, TransactionCoordinator,
    TransactionCoordinatorError,
};
pub use desired_effects::{
    DesiredEffectError, EffectCapture, EffectFrameKey, EffectReplay, VolatileEffectRegistry,
};
pub use diagnostics::{BoundedDiagnosticSink, DiagnosticRegistry, DiagnosticRegistryError};
pub use events::{BoundedEventLog, EventDraft, EventLogError};
pub use hfx_domain::{RestoreDeliveryState, ReturnEvidence, ReturnPhase};
pub use leases::{LeaseDecision, LeaseManager, LeaseManagerError};
pub use lifecycle::{
    BatteryLifecycle, BatteryValue, ChildIdentity, DEFAULT_MAX_DEVICE_ENDPOINTS,
    DEFAULT_MAX_LIFECYCLE_DEVICES, DeviceLifecycle, EndpointIdentity, EndpointLifecycle,
    EndpointReturnLifecycle, EndpointReturnPolicy, LifecycleError, LifecycleFact, LifecycleLimits,
    MAX_RETURN_RETRY_ATTEMPTS, MAX_RETURN_RETRY_WINDOW_MS, ObservationStamp,
    ReceiverGenerationLifecycle, ReceiverLifecycleMachine, ReturnEpoch, ReturnTransition,
};
pub use ports::{
    Clock, DeviceStateAuthority, EventDelivery, EventSink, NATIVE_REPORT_BYTES,
    NATIVE_ROUTE_DIGEST_BYTES, NativeEndpointLane, NativeTransportDispatch, NativeTransportOutcome,
    NativeTransportResult, PersistedRestorePolicy, PersistedStableEntry, PersistedStableIntent,
    PersistenceCasOutcome, PersistenceStore, ProfileRegistry, QualifiedDeviceProfile,
    QualifiedReceiverProfile, ReceiverTransport, RestoreAttempt, RestoreCompletion,
    RestoreDeferred, RestoreInvalidation, RestoreRecord, RestoreRecordChange, RestoreRecordStatus,
    RestoreTrigger, SessionAuthority, StableIntentChange, StableIntentTombstone, StableLighting,
    SubmissionBinding, TransportDispatch, TransportFailure, TransportFailureFacts,
    TransportReceipt, TransportReconciliation, TransportTerminal, WallClock,
};
pub use receiver_registry::{
    DEFAULT_MAX_RECEIVERS, ReceiverLifecycleRegistry, ReceiverRegistryError,
};
pub use restoration::{
    CURRENT_PERSISTENCE_SCHEMA_VERSION, MAX_RESTORE_RECORDS_PER_RECEIVER,
    MAX_STABLE_ENTRIES_PER_RECEIVER, PersistenceOperation, RestorationAuthority,
    RestorationCoordinator, RestorationError, RestoreAdvanceResult, RestoreGenerationRetirement,
    RestorePlanResult, StableCommitOutcome, StableIntentCapture,
};
pub use transactions::{
    BoundedOutcomeJournal, BoundedTransactionQueue, DequeueDecision, OutcomeJournalError,
    OutcomeLookup, QueueDecision, QueuedTransaction, RequestDigestError, RequestReplay,
    TransactionMachine, TransactionQueueError, TransactionTransitionError,
    canonical_request_digest,
};
