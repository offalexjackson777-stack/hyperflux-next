// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod diagnostics;
mod events;
mod leases;
mod ports;

pub use diagnostics::{BoundedDiagnosticSink, DiagnosticRegistry, DiagnosticRegistryError};
pub use events::{BoundedEventLog, EventDraft, EventLogError};
pub use leases::{LeaseManager, LeaseManagerError};
pub use ports::{
    Clock, EventDelivery, EventSink, PersistedStableIntent, PersistenceStore, ProfileRegistry,
    ReceiverTransport, RestoreClaim, RestoreClaimDisposition, StableLighting, TransportDispatch,
    TransportReceipt, TransportTerminal,
};
