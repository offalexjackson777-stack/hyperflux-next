// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod coordinator;
mod diagnostics;
mod events;
mod leases;
mod lifecycle;
mod ports;
mod receiver_registry;
mod restoration;
mod transactions;

pub use coordinator::{
    DispatchResult, SubmissionResult, TransactionCoordinator, TransactionCoordinatorError,
};
pub use diagnostics::{BoundedDiagnosticSink, DiagnosticRegistry, DiagnosticRegistryError};
pub use events::{BoundedEventLog, EventDraft, EventLogError};
pub use leases::{LeaseManager, LeaseManagerError};
pub use lifecycle::{
    BatteryLifecycle, BatteryValue, ChildIdentity, DEFAULT_MAX_DEVICE_ENDPOINTS,
    DEFAULT_MAX_LIFECYCLE_DEVICES, DeviceLifecycle, EndpointIdentity, EndpointLifecycle,
    LifecycleError, LifecycleFact, LifecycleLimits, ObservationStamp, ReceiverGenerationLifecycle,
    ReceiverLifecycleMachine,
};
pub use ports::{
    Clock, DeviceStateAuthority, EventDelivery, EventSink, PersistedRestorePolicy,
    PersistedStableEntry, PersistedStableIntent, PersistenceCasOutcome, PersistenceStore,
    ProfileRegistry, QualifiedDeviceProfile, QualifiedReceiverProfile, ReceiverTransport,
    RestoreAttempt, RestoreCompletion, RestoreDeferred, RestoreInvalidation, RestoreRecord,
    RestoreRecordStatus, RestoreTrigger, SessionAuthority, StableIntentChange,
    StableIntentTombstone, StableLighting, SubmissionBinding, TransportDispatch, TransportFailure,
    TransportFailureFacts, TransportReceipt, TransportReconciliation, TransportTerminal,
};
pub use receiver_registry::{
    DEFAULT_MAX_RECEIVERS, ReceiverLifecycleRegistry, ReceiverRegistryError,
};
pub use restoration::{
    CURRENT_PERSISTENCE_SCHEMA_VERSION, MAX_RESTORE_RECORDS_PER_RECEIVER,
    MAX_STABLE_ENTRIES_PER_RECEIVER, PersistenceOperation, RestorationAuthority,
    RestorationCoordinator, RestorationError, RestoreAdvanceResult, RestorePlanResult,
    StableIntentCapture,
};
pub use transactions::{
    BoundedOutcomeJournal, BoundedTransactionQueue, DequeueDecision, OutcomeJournalError,
    OutcomeLookup, QueueDecision, QueuedTransaction, RequestDigestError, RequestReplay,
    TransactionMachine, TransactionQueueError, TransactionTransitionError,
    canonical_request_digest,
};
