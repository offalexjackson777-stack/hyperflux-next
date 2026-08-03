// SPDX-License-Identifier: GPL-2.0-only

use crate::NativeExchangeRejection;
use hfx_domain::NativeRouteToken;
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fmt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeInvalidationJournalError {
    InvalidCapacity,
}

impl fmt::Display for NativeInvalidationJournalError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("native route invalidation capacity is invalid")
    }
}

impl std::error::Error for NativeInvalidationJournalError {}

/// Bounded route-token tombstones plus one coalesced scheduler notification.
///
/// Tombstones prevent an old generation token from regaining authority. The
/// pending set is independent: consuming a scheduler notification does not
/// erase the rejection reason used by later exact request replays.
#[derive(Clone, Debug)]
pub(crate) struct NativeInvalidationJournal {
    capacity: usize,
    order: VecDeque<NativeRouteToken>,
    reasons: BTreeMap<NativeRouteToken, NativeExchangeRejection>,
    pending: BTreeSet<NativeRouteToken>,
}

impl NativeInvalidationJournal {
    pub(crate) fn new(capacity: usize) -> Result<Self, NativeInvalidationJournalError> {
        if !(1..=4096).contains(&capacity) {
            return Err(NativeInvalidationJournalError::InvalidCapacity);
        }
        Ok(Self {
            capacity,
            order: VecDeque::with_capacity(capacity),
            reasons: BTreeMap::new(),
            pending: BTreeSet::new(),
        })
    }

    pub(crate) fn invalidate(
        &mut self,
        route_token: NativeRouteToken,
        rejection: NativeExchangeRejection,
    ) {
        if self.reasons.contains_key(&route_token) {
            self.reasons.insert(route_token.clone(), rejection);
            self.pending.insert(route_token);
            return;
        }
        if self.reasons.len() == self.capacity
            && let Some(expired) = self.order.pop_front()
        {
            self.reasons.remove(&expired);
            self.pending.remove(&expired);
        }
        self.order.push_back(route_token.clone());
        self.reasons.insert(route_token.clone(), rejection);
        self.pending.insert(route_token);
    }

    #[must_use]
    pub(crate) fn rejection(
        &self,
        route_token: &NativeRouteToken,
    ) -> Option<NativeExchangeRejection> {
        self.reasons.get(route_token).copied()
    }

    pub(crate) fn retire(&mut self, route_token: &NativeRouteToken) {
        if self.reasons.remove(route_token).is_none() {
            return;
        }
        self.order.retain(|retained| retained != route_token);
        self.pending.remove(route_token);
    }

    pub(crate) fn take_pending(&mut self) -> Vec<(NativeRouteToken, NativeExchangeRejection)> {
        let pending = std::mem::take(&mut self.pending);
        pending
            .into_iter()
            .filter_map(|route_token| {
                self.reasons
                    .get(&route_token)
                    .copied()
                    .map(|rejection| (route_token, rejection))
            })
            .collect()
    }

    #[cfg(test)]
    #[must_use]
    pub(crate) fn len(&self) -> usize {
        self.reasons.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn token(fill: char) -> NativeRouteToken {
        NativeRouteToken::try_from(fill.to_string().repeat(64)).expect("token is canonical")
    }

    #[test]
    fn scheduler_notifications_are_coalesced_without_erasing_tombstones() {
        let mut journal = NativeInvalidationJournal::new(2).expect("capacity is valid");
        let route = token('a');
        journal.invalidate(route.clone(), NativeExchangeRejection::RouteUnavailable);
        journal.invalidate(route.clone(), NativeExchangeRejection::StaleGeneration);

        assert_eq!(
            journal.take_pending(),
            vec![(route.clone(), NativeExchangeRejection::StaleGeneration)]
        );
        assert!(journal.take_pending().is_empty());
        assert_eq!(
            journal.rejection(&route),
            Some(NativeExchangeRejection::StaleGeneration)
        );
    }

    #[test]
    fn tombstones_are_bounded_and_current_authority_can_retire_one() {
        let mut journal = NativeInvalidationJournal::new(2).expect("capacity is valid");
        let first = token('a');
        let second = token('b');
        let third = token('c');
        journal.invalidate(first.clone(), NativeExchangeRejection::RouteUnavailable);
        journal.invalidate(second.clone(), NativeExchangeRejection::RouteUnavailable);
        journal.invalidate(third.clone(), NativeExchangeRejection::RouteWithdrawn);

        assert_eq!(journal.len(), 2);
        assert_eq!(journal.rejection(&first), None);
        journal.retire(&second);
        assert_eq!(journal.len(), 1);
        assert_eq!(journal.rejection(&second), None);
    }
}
