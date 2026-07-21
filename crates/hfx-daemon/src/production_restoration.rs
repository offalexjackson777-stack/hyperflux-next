// SPDX-License-Identifier: GPL-2.0-only

//! Production selection between a zero-I/O disabled restoration boundary and
//! the crash-safe file-backed coordinator.

use hfx_bridge::{
    DisabledRestorationSource, DurableRestorationRuntime, FilePersistenceConfig,
    FilePersistenceError, FilePersistenceStore, ReceiverRestorationSnapshot,
    RestorationProjectionError, RestorationRuntime, RestorationSnapshotSource,
};
use hfx_core::{
    BoundedEventLog, CompletedTransaction, DeviceStateAuthority, EventSink, LeaseManager,
    ProfileRegistry, ReceiverTransport, RestorationAuthority, RestorationError,
    RestoreAdvanceResult, RestoreGenerationRetirement, RestorePlanResult, RestoreRecord,
    RestoreTrigger, SessionAuthority, StableCommitOutcome, TransactionCoordinator,
};
use hfx_domain::{GenerationId, MonotonicMs, ReceiverId, RestoreClaimId, WallClockUnixMs};
use hfx_protocol::TransactionRequest;
use std::path::Path;

#[derive(Debug)]
pub enum ProductionRestoration {
    Disabled(DisabledRestorationSource),
    Durable(DurableRestorationRuntime<FilePersistenceStore>),
}

impl ProductionRestoration {
    #[must_use]
    pub const fn disabled() -> Self {
        Self::Disabled(DisabledRestorationSource)
    }

    /// Opens the exclusive crash-safe production store.
    ///
    /// # Errors
    ///
    /// Returns a path, authority, lock, schema, size, capacity, or I/O failure
    /// before a bridge socket or kernel writer is exposed.
    pub fn durable(
        path: &Path,
        max_receivers: usize,
        max_bytes: u64,
    ) -> Result<Self, FilePersistenceError> {
        let mut config = FilePersistenceConfig::new(path);
        config.max_receivers = max_receivers;
        config.max_bytes = max_bytes;
        FilePersistenceStore::open(config)
            .map(DurableRestorationRuntime::new)
            .map(Self::Durable)
    }

    #[must_use]
    pub const fn is_enabled(&self) -> bool {
        matches!(self, Self::Durable(_))
    }
}

impl RestorationSnapshotSource for ProductionRestoration {
    fn restoration(
        &self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
    ) -> Result<ReceiverRestorationSnapshot, RestorationProjectionError> {
        match self {
            Self::Disabled(runtime) => runtime.restoration(receiver_id, generation_id),
            Self::Durable(runtime) => runtime.restoration(receiver_id, generation_id),
        }
    }
}

impl RestorationRuntime for ProductionRestoration {
    fn set_enabled(
        &mut self,
        receiver_id: &ReceiverId,
        enabled: bool,
    ) -> Result<(), RestorationError> {
        match self {
            Self::Disabled(runtime) => runtime.set_enabled(receiver_id, enabled),
            Self::Durable(runtime) => runtime.set_enabled(receiver_id, enabled),
        }
    }

    fn pending_records(
        &self,
        receiver_id: &ReceiverId,
    ) -> Result<Vec<RestoreRecord>, RestorationError> {
        match self {
            Self::Disabled(runtime) => runtime.pending_records(receiver_id),
            Self::Durable(runtime) => runtime.pending_records(receiver_id),
        }
    }

    fn plan_restore(
        &mut self,
        trigger: &RestoreTrigger,
    ) -> Result<RestorePlanResult, RestorationError> {
        match self {
            Self::Disabled(runtime) => runtime.plan_restore(trigger),
            Self::Durable(runtime) => runtime.plan_restore(trigger),
        }
    }

    fn advance_claim<A, D, P, T, E>(
        &mut self,
        claim_id: &RestoreClaimId,
        authority: &RestorationAuthority,
        now: MonotonicMs,
        sessions: &A,
        devices: &D,
        profiles: &P,
        transport: &T,
        leases: &mut LeaseManager,
        transactions: &mut TransactionCoordinator,
        events: &mut BoundedEventLog,
        sink: &mut E,
    ) -> Result<RestoreAdvanceResult, RestorationError>
    where
        A: SessionAuthority,
        D: DeviceStateAuthority,
        P: ProfileRegistry,
        T: ReceiverTransport,
        E: EventSink,
    {
        match self {
            Self::Disabled(runtime) => runtime.advance_claim(
                claim_id,
                authority,
                now,
                sessions,
                devices,
                profiles,
                transport,
                leases,
                transactions,
                events,
                sink,
            ),
            Self::Durable(runtime) => runtime.advance_claim(
                claim_id,
                authority,
                now,
                sessions,
                devices,
                profiles,
                transport,
                leases,
                transactions,
                events,
                sink,
            ),
        }
    }

    fn dispatch_claim<A, D, P, T, E>(
        &mut self,
        claim_id: &RestoreClaimId,
        now: MonotonicMs,
        sessions: &A,
        devices: &D,
        profiles: &P,
        transport: &mut T,
        leases: &mut LeaseManager,
        transactions: &mut TransactionCoordinator,
        events: &mut BoundedEventLog,
        sink: &mut E,
    ) -> Result<RestoreRecord, RestorationError>
    where
        A: SessionAuthority,
        D: DeviceStateAuthority,
        P: ProfileRegistry,
        T: ReceiverTransport,
        E: EventSink,
    {
        match self {
            Self::Disabled(runtime) => runtime.dispatch_claim(
                claim_id,
                now,
                sessions,
                devices,
                profiles,
                transport,
                leases,
                transactions,
                events,
                sink,
            ),
            Self::Durable(runtime) => runtime.dispatch_claim(
                claim_id,
                now,
                sessions,
                devices,
                profiles,
                transport,
                leases,
                transactions,
                events,
                sink,
            ),
        }
    }

    fn capture_completed(
        &mut self,
        completed: &CompletedTransaction,
        captured_at: WallClockUnixMs,
    ) -> Result<StableCommitOutcome, RestorationError> {
        match self {
            Self::Disabled(runtime) => runtime.capture_completed(completed, captured_at),
            Self::Durable(runtime) => runtime.capture_completed(completed, captured_at),
        }
    }

    fn prepare_stable_dispatch(
        &mut self,
        request: &TransactionRequest,
        prepared_at: WallClockUnixMs,
    ) -> Result<(), RestorationError> {
        match self {
            Self::Disabled(runtime) => runtime.prepare_stable_dispatch(request, prepared_at),
            Self::Durable(runtime) => runtime.prepare_stable_dispatch(request, prepared_at),
        }
    }

    fn retire_generation<T, E>(
        &mut self,
        receiver_id: &ReceiverId,
        generation_id: GenerationId,
        now: MonotonicMs,
        transport: &T,
        leases: &mut LeaseManager,
        transactions: &TransactionCoordinator,
        events: &mut BoundedEventLog,
        sink: &mut E,
    ) -> Result<RestoreGenerationRetirement, RestorationError>
    where
        T: ReceiverTransport,
        E: EventSink,
    {
        match self {
            Self::Disabled(runtime) => runtime.retire_generation(
                receiver_id,
                generation_id,
                now,
                transport,
                leases,
                transactions,
                events,
                sink,
            ),
            Self::Durable(runtime) => runtime.retire_generation(
                receiver_id,
                generation_id,
                now,
                transport,
                leases,
                transactions,
                events,
                sink,
            ),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn disabled_runtime_does_not_create_a_state_file() {
        let root = std::env::temp_dir().join(format!(
            "hfx-disabled-restoration-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        fs::create_dir(&root).expect("root creates");
        let state = root.join("bridge-state.json");
        let runtime = ProductionRestoration::disabled();
        assert!(!runtime.is_enabled());
        assert!(!state.exists());
        fs::remove_dir(root).expect("root removes");
    }
}
