// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{ClientId, LeaseDurationMs, LeaseId, LeaseState, MonotonicMs, RequestId};
use hfx_protocol::{
    LeaseConflict, LeaseGrant, LeaseRequest, LeaseResult, ProtocolValidationError,
    ReleaseLeaseRequest, RenewLeaseRequest, ResourceKey, validate_lease_request,
};
use std::collections::{BTreeMap, VecDeque};
use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq)]
struct RequestRecord {
    key: (ClientId, RequestId),
    request: LeaseOperation,
    result: LeaseResult,
    pinned_lease: Option<LeaseId>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum LeaseOperation {
    Acquire(LeaseRequest),
    Renew(RenewLeaseRequest),
    Release(ReleaseLeaseRequest),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum LeaseManagerError {
    InvalidCapacity,
    InvalidRequest(ProtocolValidationError),
    RequestIdReused,
    LeaseCapacity,
    HistoryCapacity,
    ClockOverflow,
    UnknownLease,
    NotOwner,
}

impl fmt::Display for LeaseManagerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidCapacity => "lease manager capacity is invalid",
            Self::InvalidRequest(_) => "lease request is structurally invalid",
            Self::RequestIdReused => "request identity was reused with different content",
            Self::LeaseCapacity => "lease capacity is exhausted",
            Self::HistoryCapacity => "idempotency history capacity is exhausted",
            Self::ClockOverflow => "lease expiry exceeds monotonic time range",
            Self::UnknownLease => "lease is unknown or expired",
            Self::NotOwner => "client does not own the lease",
        })
    }
}

impl std::error::Error for LeaseManagerError {}

#[derive(Clone, Debug)]
pub struct LeaseManager {
    max_leases: usize,
    max_history: usize,
    leases: BTreeMap<LeaseId, LeaseGrant>,
    owners: BTreeMap<ResourceKey, LeaseId>,
    history: VecDeque<RequestRecord>,
}

impl LeaseManager {
    /// Creates a bounded lease manager.
    ///
    /// # Errors
    ///
    /// Returns an error when either capacity is zero or history cannot retain
    /// at least every active lease decision.
    pub fn new(max_leases: usize, max_history: usize) -> Result<Self, LeaseManagerError> {
        if max_leases == 0 || max_history < max_leases {
            return Err(LeaseManagerError::InvalidCapacity);
        }
        Ok(Self {
            max_leases,
            max_history,
            leases: BTreeMap::new(),
            owners: BTreeMap::new(),
            history: VecDeque::new(),
        })
    }

    /// Acquires an entire resource set or none of it.
    ///
    /// # Errors
    ///
    /// Returns an error for malformed requests, conflicting idempotency use,
    /// exhausted bounds, duplicate lease ids, or monotonic overflow.
    pub fn acquire(
        &mut self,
        request: LeaseRequest,
        lease_id: LeaseId,
        now: MonotonicMs,
    ) -> Result<LeaseResult, LeaseManagerError> {
        self.expire(now);
        let operation = LeaseOperation::Acquire(request);
        let LeaseOperation::Acquire(request) = &operation else {
            unreachable!("operation was constructed as acquire");
        };
        validate_lease_request(request).map_err(LeaseManagerError::InvalidRequest)?;
        let key = (request.client_id.clone(), request.request_id.clone());
        if let Some(record) = self.history.iter().find(|record| record.key == key) {
            if record.request == operation {
                return Ok(record.result.clone());
            }
            return Err(LeaseManagerError::RequestIdReused);
        }
        if let Some((resource, owner)) = request.resources.iter().find_map(|resource| {
            let lease_id = self.owners.get(resource)?;
            let grant = self.leases.get(lease_id)?;
            Some((resource.clone(), grant.client_id.clone()))
        }) {
            let result = LeaseResult::Conflict(LeaseConflict {
                conflicting_client: owner,
                conflicting_resource: resource,
            });
            self.reserve_history_slot()?;
            self.remember(RequestRecord {
                key,
                request: operation,
                result: result.clone(),
                pinned_lease: None,
            });
            return Ok(result);
        }

        if self.leases.contains_key(&lease_id) || self.leases.len() >= self.max_leases {
            return Err(LeaseManagerError::LeaseCapacity);
        }
        self.reserve_history_slot()?;

        let expires = now
            .get()
            .checked_add(u64::from(request.duration_ms.get()))
            .ok_or(LeaseManagerError::ClockOverflow)?;
        let expires_at_ms =
            MonotonicMs::try_from(expires).map_err(|_| LeaseManagerError::ClockOverflow)?;
        let grant = LeaseGrant {
            lease_id: lease_id.clone(),
            client_id: request.client_id.clone(),
            resources: request.resources.clone(),
            expires_at_ms,
            state: LeaseState::Granted,
        };
        for resource in &grant.resources {
            self.owners.insert(resource.clone(), lease_id.clone());
        }
        self.leases.insert(lease_id.clone(), grant.clone());
        let result = LeaseResult::Granted(grant);
        self.remember(RequestRecord {
            key,
            request: operation,
            result: result.clone(),
            pinned_lease: Some(lease_id),
        });
        Ok(result)
    }

    /// Idempotently renews a currently owned lease using the protocol request
    /// identity. Replaying the exact request returns its retained expiry and
    /// never extends the lease a second time.
    ///
    /// # Errors
    ///
    /// Returns an error for request-identity reuse, exhausted history, an
    /// unknown lease, a non-owner, or monotonic overflow.
    pub fn renew_request(
        &mut self,
        request: RenewLeaseRequest,
        now: MonotonicMs,
    ) -> Result<LeaseResult, LeaseManagerError> {
        self.expire(now);
        let operation = LeaseOperation::Renew(request);
        let LeaseOperation::Renew(request) = &operation else {
            unreachable!("operation was constructed as renew");
        };
        let key = (request.client_id.clone(), request.request_id.clone());
        if let Some(record) = self.history.iter().find(|record| record.key == key) {
            if record.request == operation {
                return Ok(record.result.clone());
            }
            return Err(LeaseManagerError::RequestIdReused);
        }

        let grant = self
            .leases
            .get(&request.lease_id)
            .ok_or(LeaseManagerError::UnknownLease)?;
        if grant.client_id != request.client_id {
            return Err(LeaseManagerError::NotOwner);
        }
        let expires = now
            .get()
            .checked_add(u64::from(request.duration_ms.get()))
            .ok_or(LeaseManagerError::ClockOverflow)?;
        let expires_at_ms =
            MonotonicMs::try_from(expires).map_err(|_| LeaseManagerError::ClockOverflow)?;
        self.reserve_history_slot()?;

        let grant = self
            .leases
            .get_mut(&request.lease_id)
            .ok_or(LeaseManagerError::UnknownLease)?;
        grant.expires_at_ms = expires_at_ms;
        grant.state = LeaseState::Renewed;
        let result = LeaseResult::Granted(grant.clone());
        let pinned_lease = request.lease_id.clone();
        self.remember(RequestRecord {
            key,
            request: operation,
            result: result.clone(),
            pinned_lease: Some(pinned_lease),
        });
        Ok(result)
    }

    /// Idempotently releases a currently owned lease using the protocol
    /// request identity. Replaying the exact request returns the retained
    /// released grant even though the live lease no longer exists.
    ///
    /// # Errors
    ///
    /// Returns an error for request-identity reuse, exhausted history, an
    /// unknown lease, or a non-owner.
    pub fn release_request(
        &mut self,
        request: ReleaseLeaseRequest,
        now: MonotonicMs,
    ) -> Result<LeaseResult, LeaseManagerError> {
        self.expire(now);
        let operation = LeaseOperation::Release(request);
        let LeaseOperation::Release(request) = &operation else {
            unreachable!("operation was constructed as release");
        };
        let key = (request.client_id.clone(), request.request_id.clone());
        if let Some(record) = self.history.iter().find(|record| record.key == key) {
            if record.request == operation {
                return Ok(record.result.clone());
            }
            return Err(LeaseManagerError::RequestIdReused);
        }

        let grant = self
            .leases
            .get(&request.lease_id)
            .ok_or(LeaseManagerError::UnknownLease)?;
        if grant.client_id != request.client_id {
            return Err(LeaseManagerError::NotOwner);
        }
        self.reserve_history_slot()?;

        let mut released = self
            .remove_lease(&request.lease_id)
            .ok_or(LeaseManagerError::UnknownLease)?;
        released.state = LeaseState::Released;
        let result = LeaseResult::Granted(released);
        self.remember(RequestRecord {
            key,
            request: operation,
            result: result.clone(),
            pinned_lease: None,
        });
        Ok(result)
    }

    /// Renews a currently owned lease without changing its resource set.
    ///
    /// # Errors
    ///
    /// Returns an error for an unknown lease, a non-owner, or monotonic overflow.
    pub fn renew(
        &mut self,
        client_id: &ClientId,
        lease_id: &LeaseId,
        duration_ms: LeaseDurationMs,
        now: MonotonicMs,
    ) -> Result<LeaseGrant, LeaseManagerError> {
        self.expire(now);
        let grant = self
            .leases
            .get_mut(lease_id)
            .ok_or(LeaseManagerError::UnknownLease)?;
        if &grant.client_id != client_id {
            return Err(LeaseManagerError::NotOwner);
        }
        let expires = now
            .get()
            .checked_add(u64::from(duration_ms.get()))
            .ok_or(LeaseManagerError::ClockOverflow)?;
        grant.expires_at_ms =
            MonotonicMs::try_from(expires).map_err(|_| LeaseManagerError::ClockOverflow)?;
        grant.state = LeaseState::Renewed;
        Ok(grant.clone())
    }

    /// Releases a lease only for its owning client.
    ///
    /// # Errors
    ///
    /// Returns an error for an unknown lease or non-owner.
    pub fn release(
        &mut self,
        client_id: &ClientId,
        lease_id: &LeaseId,
        now: MonotonicMs,
    ) -> Result<LeaseGrant, LeaseManagerError> {
        self.expire(now);
        let grant = self
            .leases
            .get(lease_id)
            .ok_or(LeaseManagerError::UnknownLease)?;
        if &grant.client_id != client_id {
            return Err(LeaseManagerError::NotOwner);
        }
        let mut released = self
            .remove_lease(lease_id)
            .ok_or(LeaseManagerError::UnknownLease)?;
        released.state = LeaseState::Released;
        Ok(released)
    }

    pub fn release_client(&mut self, client_id: &ClientId) -> Vec<LeaseGrant> {
        let ids = self
            .leases
            .iter()
            .filter(|(_, grant)| &grant.client_id == client_id)
            .map(|(id, _)| id.clone())
            .collect::<Vec<_>>();
        ids.iter()
            .filter_map(|id| self.remove_lease(id))
            .map(|mut grant| {
                grant.state = LeaseState::Revoked;
                grant
            })
            .collect()
    }

    pub fn invalidate_generation(
        &mut self,
        receiver_id: &hfx_domain::ReceiverId,
        generation_id: hfx_domain::GenerationId,
    ) -> Vec<LeaseGrant> {
        let ids = self
            .leases
            .iter()
            .filter(|(_, grant)| {
                grant.resources.iter().any(|resource| {
                    &resource.receiver_id == receiver_id && resource.generation_id == generation_id
                })
            })
            .map(|(id, _)| id.clone())
            .collect::<Vec<_>>();
        ids.iter()
            .filter_map(|id| self.remove_lease(id))
            .map(|mut grant| {
                grant.state = LeaseState::Revoked;
                grant
            })
            .collect()
    }

    #[must_use]
    pub fn owns(
        &self,
        client_id: &ClientId,
        lease_id: &LeaseId,
        resources: &[ResourceKey],
        now: MonotonicMs,
    ) -> bool {
        self.leases.get(lease_id).is_some_and(|grant| {
            &grant.client_id == client_id
                && grant.expires_at_ms > now
                && resources
                    .iter()
                    .all(|resource| grant.resources.contains(resource))
        })
    }

    fn expire(&mut self, now: MonotonicMs) {
        let expired = self
            .leases
            .iter()
            .filter(|(_, grant)| grant.expires_at_ms <= now)
            .map(|(id, _)| id.clone())
            .collect::<Vec<_>>();
        for id in expired {
            let _ = self.remove_lease(&id);
        }
    }

    fn remove_lease(&mut self, lease_id: &LeaseId) -> Option<LeaseGrant> {
        let grant = self.leases.remove(lease_id)?;
        for resource in &grant.resources {
            self.owners.remove(resource);
        }
        for record in &mut self.history {
            if record.pinned_lease.as_ref() == Some(lease_id) {
                record.pinned_lease = None;
            }
        }
        Some(grant)
    }

    fn reserve_history_slot(&mut self) -> Result<(), LeaseManagerError> {
        while self.history.len() >= self.max_history {
            let position = self
                .history
                .iter()
                .position(|entry| entry.pinned_lease.is_none())
                .ok_or(LeaseManagerError::HistoryCapacity)?;
            self.history.remove(position);
        }
        Ok(())
    }

    fn remember(&mut self, record: RequestRecord) {
        debug_assert!(self.history.len() < self.max_history);
        self.history.push_back(record);
    }
}
