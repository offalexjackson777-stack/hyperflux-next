// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod coordinator;
mod diagnostics;
mod events;
mod leases;
mod lifecycle;
mod ports;
mod transactions;

pub use coordinator::{
    DispatchResult, SubmissionBinding, SubmissionResult, TransactionCoordinator,
    TransactionCoordinatorError,
};
pub use diagnostics::{BoundedDiagnosticSink, DiagnosticRegistry, DiagnosticRegistryError};
pub use events::{BoundedEventLog, EventDraft, EventLogError};
pub use leases::{LeaseManager, LeaseManagerError};
pub use lifecycle::{
    ChildIdentity, DEFAULT_MAX_DEVICE_ENDPOINTS, DEFAULT_MAX_LIFECYCLE_DEVICES, DeviceLifecycle,
    EndpointIdentity, EndpointLifecycle, LifecycleError, LifecycleFact, LifecycleLimits,
    ObservationStamp, ReceiverGenerationLifecycle, ReceiverLifecycleMachine,
};
pub use ports::{
    Clock, EventDelivery, EventSink, PersistedStableIntent, PersistenceStore, ProfileRegistry,
    ReceiverTransport, RestoreClaim, RestoreClaimDisposition, SessionAuthority, StableLighting,
    TransportDispatch, TransportFailure, TransportFailureFacts, TransportReceipt,
    TransportTerminal,
};
pub use transactions::{
    BoundedOutcomeJournal, BoundedTransactionQueue, DequeueDecision, OutcomeJournalError,
    OutcomeLookup, QueueDecision, QueuedTransaction, RequestDigestError, RequestReplay,
    TransactionMachine, TransactionQueueError, TransactionTransitionError,
    canonical_request_digest,
};
