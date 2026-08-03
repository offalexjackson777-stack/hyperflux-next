// SPDX-License-Identifier: GPL-2.0-only

use crate::AuthorizedSession;
use hfx_core::SessionAuthority;
use hfx_domain::{AuthorizationEpoch, ClientId, QueueCapacity, SessionId};
use std::collections::BTreeMap;
use std::fmt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SessionRegistryError {
    CapacityExhausted,
    DuplicateSessionIdentity,
    ClientAlreadyConnected,
}

impl fmt::Display for SessionRegistryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::CapacityExhausted => "active bridge session capacity is exhausted",
            Self::DuplicateSessionIdentity => "bridge session identity is already active",
            Self::ClientAlreadyConnected => "client identity already owns an active connection",
        })
    }
}

impl std::error::Error for SessionRegistryError {}

/// Bounded bridge-wide authority for negotiated connections.
///
/// Client identities are unique while active. Without that rule, two local
/// connections choosing the same client ID could operate each other's leases
/// even though their protocol credentials and internal sessions differ.
#[derive(Clone, Debug)]
pub struct SessionRegistry {
    capacity: usize,
    sessions: BTreeMap<SessionId, AuthorizedSession>,
    clients: BTreeMap<ClientId, SessionId>,
}

impl SessionRegistry {
    #[must_use]
    pub fn new(capacity: QueueCapacity) -> Self {
        Self {
            capacity: usize::from(capacity.get()),
            sessions: BTreeMap::new(),
            clients: BTreeMap::new(),
        }
    }

    /// Registers one negotiated session atomically.
    ///
    /// # Errors
    ///
    /// Returns an error without mutation when the bound is full, the internal
    /// session identity collides, or the client already has a live connection.
    pub fn register(
        &mut self,
        authorization: AuthorizedSession,
    ) -> Result<(), SessionRegistryError> {
        if self.sessions.contains_key(&authorization.session_id) {
            return Err(SessionRegistryError::DuplicateSessionIdentity);
        }
        if self.clients.contains_key(&authorization.client_id) {
            return Err(SessionRegistryError::ClientAlreadyConnected);
        }
        if self.sessions.len() >= self.capacity {
            return Err(SessionRegistryError::CapacityExhausted);
        }
        self.clients.insert(
            authorization.client_id.clone(),
            authorization.session_id.clone(),
        );
        self.sessions
            .insert(authorization.session_id.clone(), authorization);
        Ok(())
    }

    pub fn revoke(&mut self, session_id: &SessionId) -> Option<AuthorizedSession> {
        let authorization = self.sessions.remove(session_id)?;
        self.clients.remove(&authorization.client_id);
        Some(authorization)
    }

    pub fn revoke_client(&mut self, client_id: &ClientId) -> Option<AuthorizedSession> {
        let session_id = self.clients.get(client_id)?.clone();
        self.revoke(&session_id)
    }

    #[must_use]
    pub fn contains_client(&self, client_id: &ClientId) -> bool {
        self.clients.contains_key(client_id)
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.sessions.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.sessions.is_empty()
    }
}

impl SessionAuthority for SessionRegistry {
    fn authorizes(&self, session_id: &SessionId, authorization_epoch: AuthorizationEpoch) -> bool {
        self.sessions
            .get(session_id)
            .is_some_and(|session| session.authorization_epoch == authorization_epoch)
    }
}
