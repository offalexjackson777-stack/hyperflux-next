// SPDX-License-Identifier: GPL-2.0-only

use std::collections::{BTreeMap, VecDeque};
use std::fmt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum FairRouteQueueError {
    InvalidCapacity,
    Full,
}

impl fmt::Display for FairRouteQueueError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidCapacity => "native route queue capacity is invalid",
            Self::Full => "native route queue is full",
        })
    }
}

impl std::error::Error for FairRouteQueueError {}

/// Bounded route-local FIFO scheduler with fair round-robin lane selection.
///
/// A route enters the rotation exactly once while it has queued work. Popping
/// one request moves a still-nonempty route to the back, so a busy child cannot
/// consume a sibling child's turn.
#[derive(Clone, Debug)]
pub(crate) struct FairRouteQueue<K, T> {
    capacity: usize,
    len: usize,
    rotation: VecDeque<K>,
    lanes: BTreeMap<K, VecDeque<T>>,
}

impl<K, T> FairRouteQueue<K, T>
where
    K: Clone + Ord,
{
    pub(crate) fn new(capacity: usize) -> Result<Self, FairRouteQueueError> {
        if !(1..=4096).contains(&capacity) {
            return Err(FairRouteQueueError::InvalidCapacity);
        }
        Ok(Self {
            capacity,
            len: 0,
            rotation: VecDeque::with_capacity(capacity),
            lanes: BTreeMap::new(),
        })
    }

    pub(crate) fn admit(&mut self, route: K, item: T) -> Result<(), (FairRouteQueueError, T)> {
        if self.len == self.capacity {
            return Err((FairRouteQueueError::Full, item));
        }
        let lane = self.lanes.entry(route.clone()).or_default();
        if lane.is_empty() {
            self.rotation.push_back(route);
        }
        lane.push_back(item);
        self.len += 1;
        Ok(())
    }

    pub(crate) fn pop_next(&mut self) -> Option<T> {
        let route = self.rotation.pop_front()?;
        let lane = self
            .lanes
            .get_mut(&route)
            .expect("rotated native route owns a lane");
        let item = lane
            .pop_front()
            .expect("rotated native route owns queued work");
        self.len -= 1;
        if lane.is_empty() {
            self.lanes.remove(&route);
        } else {
            self.rotation.push_back(route);
        }
        Some(item)
    }

    pub(crate) fn remove_where<F>(&mut self, mut predicate: F) -> Vec<T>
    where
        F: FnMut(&T) -> bool,
    {
        let mut removed = Vec::new();
        for lane in self.lanes.values_mut() {
            let mut retained = VecDeque::with_capacity(lane.len());
            while let Some(item) = lane.pop_front() {
                if predicate(&item) {
                    self.len -= 1;
                    removed.push(item);
                } else {
                    retained.push_back(item);
                }
            }
            *lane = retained;
        }
        self.lanes.retain(|_, lane| !lane.is_empty());
        self.rotation.retain(|route| self.lanes.contains_key(route));
        removed
    }

    pub(crate) fn drain(&mut self) -> Vec<T> {
        let mut drained = Vec::with_capacity(self.len);
        while let Some(item) = self.pop_next() {
            drained.push(item);
        }
        drained
    }

    #[must_use]
    #[cfg(test)]
    pub(crate) const fn len(&self) -> usize {
        self.len
    }

    #[must_use]
    pub(crate) const fn is_empty(&self) -> bool {
        self.len == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    struct Item {
        route: char,
        sequence: u8,
        connection: u8,
    }

    fn item(route: char, sequence: u8, connection: u8) -> Item {
        Item {
            route,
            sequence,
            connection,
        }
    }

    #[test]
    fn route_fifo_and_sibling_round_robin_are_simultaneously_preserved() {
        let mut queue = FairRouteQueue::new(8).expect("queue is bounded");
        for next in [
            item('m', 1, 1),
            item('m', 2, 1),
            item('k', 1, 2),
            item('m', 3, 1),
            item('k', 2, 2),
        ] {
            queue
                .admit(next.route, next)
                .expect("fixture remains within capacity");
        }

        assert_eq!(
            queue.drain(),
            vec![
                item('m', 1, 1),
                item('k', 1, 2),
                item('m', 2, 1),
                item('k', 2, 2),
                item('m', 3, 1),
            ]
        );
    }

    #[test]
    fn admission_is_exactly_bounded_and_returns_the_unadmitted_item() {
        let mut queue = FairRouteQueue::new(2).expect("queue is bounded");
        queue
            .admit('m', item('m', 1, 1))
            .expect("first item is admitted");
        queue
            .admit('k', item('k', 1, 2))
            .expect("second item is admitted");
        let rejected = item('m', 2, 1);
        assert_eq!(
            queue.admit('m', rejected),
            Err((FairRouteQueueError::Full, rejected))
        );
        assert_eq!(queue.len(), 2);
    }

    #[test]
    fn connection_cancellation_keeps_other_lanes_ordered_and_fair() {
        let mut queue = FairRouteQueue::new(8).expect("queue is bounded");
        for next in [
            item('m', 1, 1),
            item('k', 1, 2),
            item('m', 2, 1),
            item('k', 2, 2),
        ] {
            queue
                .admit(next.route, next)
                .expect("fixture remains within capacity");
        }

        assert_eq!(
            queue.remove_where(|pending| pending.connection == 1),
            vec![item('m', 1, 1), item('m', 2, 1)]
        );
        assert_eq!(queue.drain(), vec![item('k', 1, 2), item('k', 2, 2)]);
        assert!(queue.is_empty());
    }
}
