// SPDX-License-Identifier: GPL-2.0-only

use crate::ReceiverLifecycleMachine;
use hfx_domain::ReceiverId;
use std::collections::BTreeMap;
use std::fmt;

pub const DEFAULT_MAX_RECEIVERS: usize = 16;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ReceiverRegistryError {
    InvalidCapacity,
    CapacityExhausted,
    DuplicateReceiver(ReceiverId),
}

impl fmt::Display for ReceiverRegistryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidCapacity => formatter.write_str("receiver registry capacity is invalid"),
            Self::CapacityExhausted => {
                formatter.write_str("receiver registry capacity is exhausted")
            }
            Self::DuplicateReceiver(receiver_id) => {
                write!(formatter, "receiver is already registered: {receiver_id}")
            }
        }
    }
}

impl std::error::Error for ReceiverRegistryError {}

/// Bounded canonical owner of every receiver lifecycle machine.
#[derive(Clone, Debug)]
pub struct ReceiverLifecycleRegistry {
    capacity: usize,
    receivers: BTreeMap<ReceiverId, ReceiverLifecycleMachine>,
}

impl ReceiverLifecycleRegistry {
    /// Creates a registry no larger than the public bridge snapshot bound.
    ///
    /// # Errors
    ///
    /// Returns [`ReceiverRegistryError::InvalidCapacity`] for zero or an
    /// oversized bound.
    pub fn new(capacity: usize) -> Result<Self, ReceiverRegistryError> {
        if !(1..=DEFAULT_MAX_RECEIVERS).contains(&capacity) {
            return Err(ReceiverRegistryError::InvalidCapacity);
        }
        Ok(Self {
            capacity,
            receivers: BTreeMap::new(),
        })
    }

    /// Registers one stable receiver identity without replacing old state.
    ///
    /// # Errors
    ///
    /// Duplicate identity and capacity exhaustion leave the registry intact.
    pub fn register(
        &mut self,
        machine: ReceiverLifecycleMachine,
    ) -> Result<(), ReceiverRegistryError> {
        let receiver_id = machine.receiver_id().clone();
        if self.receivers.contains_key(&receiver_id) {
            return Err(ReceiverRegistryError::DuplicateReceiver(receiver_id));
        }
        if self.receivers.len() == self.capacity {
            return Err(ReceiverRegistryError::CapacityExhausted);
        }
        self.receivers.insert(receiver_id, machine);
        Ok(())
    }

    #[must_use]
    pub fn get(&self, receiver_id: &ReceiverId) -> Option<&ReceiverLifecycleMachine> {
        self.receivers.get(receiver_id)
    }

    #[must_use]
    pub fn get_mut(&mut self, receiver_id: &ReceiverId) -> Option<&mut ReceiverLifecycleMachine> {
        self.receivers.get_mut(receiver_id)
    }

    #[must_use]
    pub fn iter(&self) -> impl ExactSizeIterator<Item = &ReceiverLifecycleMachine> {
        self.receivers.values()
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.receivers.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.receivers.is_empty()
    }
}

impl Default for ReceiverLifecycleRegistry {
    fn default() -> Self {
        Self {
            capacity: DEFAULT_MAX_RECEIVERS,
            receivers: BTreeMap::new(),
        }
    }
}
