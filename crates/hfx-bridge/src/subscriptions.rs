// SPDX-License-Identifier: GPL-2.0-only

use crate::{AuthorizedSession, RuntimeIdentityError, RuntimeIdentityIssuer};
use hfx_domain::{ClientId, SessionId, SubscriptionId};
use hfx_protocol::SubscriptionRequest;
use std::collections::BTreeMap;
use std::fmt;

pub const DEFAULT_MAX_SUBSCRIPTIONS: usize = 256;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ActiveSubscription {
    pub subscription_id: SubscriptionId,
    pub client_id: ClientId,
    pub session_id: SessionId,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SubscriptionRegistryError {
    InvalidCapacity,
    CapacityExhausted,
    Identity(RuntimeIdentityError),
    UnknownSubscription,
    SubscriptionMismatch,
}

impl fmt::Display for SubscriptionRegistryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidCapacity => "subscription capacity is invalid",
            Self::CapacityExhausted => "subscription capacity is exhausted",
            Self::Identity(_) => "subscription identity generation failed",
            Self::UnknownSubscription => "subscription is not active for this client",
            Self::SubscriptionMismatch => "subscription belongs to another active session",
        })
    }
}

impl std::error::Error for SubscriptionRegistryError {}

/// Bounded one-subscription-per-client ownership registry.
#[derive(Clone, Debug)]
pub struct SubscriptionRegistry {
    capacity: usize,
    active: BTreeMap<ClientId, ActiveSubscription>,
}

impl SubscriptionRegistry {
    /// Creates a bounded registry.
    ///
    /// # Errors
    ///
    /// Returns an error when capacity is zero or exceeds the protocol queue bound.
    pub fn new(capacity: usize) -> Result<Self, SubscriptionRegistryError> {
        if !(1..=4096).contains(&capacity) {
            return Err(SubscriptionRegistryError::InvalidCapacity);
        }
        Ok(Self {
            capacity,
            active: BTreeMap::new(),
        })
    }

    /// Resolves an initial or continuing subscription for one live session.
    ///
    /// Repeating an initial request within the same session is idempotent.
    /// A supplied identity must match the exact client and internal session.
    ///
    /// # Errors
    ///
    /// Returns a typed capacity, identity, unknown, or ownership failure.
    pub fn resolve(
        &mut self,
        session: &AuthorizedSession,
        request: &SubscriptionRequest,
        identities: &mut RuntimeIdentityIssuer,
    ) -> Result<SubscriptionId, SubscriptionRegistryError> {
        if let Some(active) = self.active.get(&session.client_id) {
            if active.session_id != session.session_id {
                return Err(SubscriptionRegistryError::SubscriptionMismatch);
            }
            if request
                .subscription_id
                .as_ref()
                .is_none_or(|requested| requested == &active.subscription_id)
            {
                return Ok(active.subscription_id.clone());
            }
            return Err(SubscriptionRegistryError::SubscriptionMismatch);
        }
        if request.subscription_id.is_some() {
            return Err(SubscriptionRegistryError::UnknownSubscription);
        }
        if self.active.len() == self.capacity {
            return Err(SubscriptionRegistryError::CapacityExhausted);
        }
        let subscription_id = identities
            .subscription_id()
            .map_err(SubscriptionRegistryError::Identity)?;
        self.active.insert(
            session.client_id.clone(),
            ActiveSubscription {
                subscription_id: subscription_id.clone(),
                client_id: session.client_id.clone(),
                session_id: session.session_id.clone(),
            },
        );
        Ok(subscription_id)
    }

    pub fn revoke_session(&mut self, session: &AuthorizedSession) -> bool {
        if self
            .active
            .get(&session.client_id)
            .is_some_and(|active| active.session_id == session.session_id)
        {
            self.active.remove(&session.client_id);
            true
        } else {
            false
        }
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.active.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.active.is_empty()
    }
}

impl Default for SubscriptionRegistry {
    fn default() -> Self {
        Self {
            capacity: DEFAULT_MAX_SUBSCRIPTIONS,
            active: BTreeMap::new(),
        }
    }
}
