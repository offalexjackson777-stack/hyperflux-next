// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{FindingId, QueueCapacity, SequenceNumber};
use hfx_protocol::{DiagnosticFinding, DiagnosticSnapshot};
use std::collections::{BTreeMap, VecDeque};
use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DiagnosticRegistryError {
    InvalidCapacity,
    CapacityExhausted,
}

impl fmt::Display for DiagnosticRegistryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidCapacity => "diagnostic capacity is invalid",
            Self::CapacityExhausted => "diagnostic capacity is exhausted",
        })
    }
}

impl std::error::Error for DiagnosticRegistryError {}

#[derive(Clone, Debug)]
pub struct DiagnosticRegistry {
    capacity: usize,
    active: BTreeMap<FindingId, DiagnosticFinding>,
}

impl DiagnosticRegistry {
    /// Creates a bounded active-finding registry.
    ///
    /// # Errors
    ///
    /// Returns an error when capacity is outside the protocol bound.
    pub fn new(capacity: usize) -> Result<Self, DiagnosticRegistryError> {
        if !(1..=128).contains(&capacity) {
            return Err(DiagnosticRegistryError::InvalidCapacity);
        }
        Ok(Self {
            capacity,
            active: BTreeMap::new(),
        })
    }

    /// Inserts or replaces one active finding.
    ///
    /// # Errors
    ///
    /// Returns an error when a new finding would exceed the fixed bound.
    pub fn raise(&mut self, finding: DiagnosticFinding) -> Result<(), DiagnosticRegistryError> {
        if !self.active.contains_key(&finding.finding_id) && self.active.len() == self.capacity {
            return Err(DiagnosticRegistryError::CapacityExhausted);
        }
        self.active.insert(finding.finding_id.clone(), finding);
        Ok(())
    }

    pub fn clear(&mut self, finding_id: &FindingId) -> bool {
        self.active.remove(finding_id).is_some()
    }

    #[must_use]
    pub fn snapshot(
        &self,
        sequence: SequenceNumber,
        event_buffer_capacity: QueueCapacity,
        transaction_queue_capacity: QueueCapacity,
    ) -> DiagnosticSnapshot {
        DiagnosticSnapshot {
            sequence,
            findings: self.active.values().cloned().collect(),
            event_buffer_capacity,
            transaction_queue_capacity,
        }
    }
}

#[derive(Clone, Debug)]
pub struct BoundedDiagnosticSink<T> {
    capacity: usize,
    entries: VecDeque<T>,
    dropped: u64,
}

impl<T> BoundedDiagnosticSink<T> {
    /// Creates a nonblocking in-memory sink.
    ///
    /// # Errors
    ///
    /// Returns an error when capacity is zero.
    pub fn new(capacity: usize) -> Result<Self, DiagnosticRegistryError> {
        if capacity == 0 {
            return Err(DiagnosticRegistryError::InvalidCapacity);
        }
        Ok(Self {
            capacity,
            entries: VecDeque::with_capacity(capacity),
            dropped: 0,
        })
    }

    pub fn try_push(&mut self, entry: T) {
        if self.entries.len() == self.capacity {
            self.entries.pop_front();
            self.dropped = self.dropped.saturating_add(1);
        }
        self.entries.push_back(entry);
    }

    #[must_use]
    pub const fn dropped(&self) -> u64 {
        self.dropped
    }

    pub fn iter(&self) -> impl Iterator<Item = &T> {
        self.entries.iter()
    }
}
