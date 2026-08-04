// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{ClientId, FindingId, NativeRouteToken, ProtocolErrorKind, RequestId};
use hfx_protocol::{
    NativeExchangeNotSent, NativeExchangeOutcome, NativeExchangeRequest, NativeExchangeResult,
};
use std::collections::{BTreeMap, VecDeque};
use std::fmt;

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct NativeRequestKey {
    client_id: ClientId,
    request_id: RequestId,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct NativeExchangeRecord {
    request: NativeExchangeRequest,
    outcome: NativeExchangeOutcome,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeExchangeJournalError {
    InvalidCapacity,
    RequestIdentityReused,
}

impl fmt::Display for NativeExchangeJournalError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidCapacity => "native exchange journal capacity is invalid",
            Self::RequestIdentityReused => {
                "native exchange request identity was reused with different content"
            }
        })
    }
}

impl std::error::Error for NativeExchangeJournalError {}

/// Bounded process-local idempotency journal for synchronous native exchanges.
#[derive(Clone, Debug)]
pub struct NativeExchangeJournal {
    capacity: usize,
    order: VecDeque<NativeRequestKey>,
    records: BTreeMap<NativeRequestKey, NativeExchangeRecord>,
}

impl NativeExchangeJournal {
    /// Creates a bounded terminal native-exchange history.
    ///
    /// # Errors
    ///
    /// Returns [`NativeExchangeJournalError::InvalidCapacity`] for a zero or
    /// oversized bound.
    pub fn new(capacity: usize) -> Result<Self, NativeExchangeJournalError> {
        if !(1..=4096).contains(&capacity) {
            return Err(NativeExchangeJournalError::InvalidCapacity);
        }
        Ok(Self {
            capacity,
            order: VecDeque::with_capacity(capacity),
            records: BTreeMap::new(),
        })
    }

    /// Resolves an exact retained request without issuing a transport call.
    ///
    /// # Errors
    ///
    /// Returns [`NativeExchangeJournalError::RequestIdentityReused`] when the
    /// same client request identity names different content.
    pub fn retained(
        &self,
        request: &NativeExchangeRequest,
    ) -> Result<Option<NativeExchangeOutcome>, NativeExchangeJournalError> {
        let key = NativeRequestKey {
            client_id: request.client_id.clone(),
            request_id: request.request_id.clone(),
        };
        let Some(record) = self.records.get(&key) else {
            return Ok(None);
        };
        if record.request != *request {
            return Err(NativeExchangeJournalError::RequestIdentityReused);
        }
        Ok(Some(record.outcome.clone()))
    }

    /// Retains one terminal outcome, evicting the oldest bounded entry.
    ///
    /// # Errors
    ///
    /// Returns [`NativeExchangeJournalError::RequestIdentityReused`] for a
    /// contradictory request or terminal result.
    pub fn remember(
        &mut self,
        request: NativeExchangeRequest,
        outcome: NativeExchangeOutcome,
    ) -> Result<(), NativeExchangeJournalError> {
        let key = NativeRequestKey {
            client_id: request.client_id.clone(),
            request_id: request.request_id.clone(),
        };
        if let Some(record) = self.records.get(&key) {
            return if record.request == request && record.outcome == outcome {
                Ok(())
            } else {
                Err(NativeExchangeJournalError::RequestIdentityReused)
            };
        }
        if self.records.len() == self.capacity
            && let Some(expired) = self.order.pop_front()
        {
            self.records.remove(&expired);
        }
        self.order.push_back(key.clone());
        self.records
            .insert(key, NativeExchangeRecord { request, outcome });
        Ok(())
    }

    /// Removes cached response bytes for one invalidated route while keeping
    /// request-identity conflict protection.
    ///
    /// Exact retries receive a generation-bound `not-sent` tombstone. A
    /// contradictory reuse of the same client/request identity remains an
    /// error, so invalidation cannot reopen transport admission.
    pub fn invalidate_route(
        &mut self,
        route_token: &NativeRouteToken,
        error_kind: ProtocolErrorKind,
        finding_id: &FindingId,
    ) -> usize {
        let mut invalidated = 0;
        for record in self.records.values_mut() {
            if &record.request.route_token != route_token {
                continue;
            }
            record.outcome.result = NativeExchangeResult::NotSent(NativeExchangeNotSent {
                error_kind,
                finding_id: finding_id.clone(),
            });
            invalidated += 1;
        }
        invalidated
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.records.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.records.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use hfx_domain::{
        FindingId, LeaseId, NativeExchangeTimeoutMs, NativeReportHex, NativeRouteToken,
        ProtocolErrorKind,
    };
    use hfx_protocol::{
        NativeExchangeMatchedResponse, NativeExchangeNotSent, NativeExchangeResult,
    };

    fn text<T>(value: &str) -> T
    where
        T: TryFrom<String>,
        T::Error: fmt::Debug,
    {
        T::try_from(value.to_owned()).expect("test value is valid")
    }

    fn request(client: &str, request: &str, fill: char) -> NativeExchangeRequest {
        NativeExchangeRequest {
            request_id: text(request),
            client_id: text(client),
            lease_id: text::<LeaseId>("lease-1"),
            route_token: text::<NativeRouteToken>(&"a".repeat(64)),
            report: text::<NativeReportHex>(&fill.to_string().repeat(180)),
            timeout_ms: NativeExchangeTimeoutMs::try_from(100_u16).expect("timeout is valid"),
        }
    }

    fn outcome(request: &NativeExchangeRequest) -> NativeExchangeOutcome {
        NativeExchangeOutcome {
            request_id: request.request_id.clone(),
            route_token: request.route_token.clone(),
            result: NativeExchangeResult::NotSent(NativeExchangeNotSent {
                error_kind: ProtocolErrorKind::TransportFailure,
                finding_id: text::<FindingId>("HFX-TRANSPORT-001"),
            }),
        }
    }

    #[test]
    fn exact_replay_is_retained_and_contradictory_reuse_is_rejected() {
        let mut journal = NativeExchangeJournal::new(2).expect("capacity is valid");
        let original = request("client-1", "request-1", '0');
        let retained = outcome(&original);
        journal
            .remember(original.clone(), retained.clone())
            .expect("first result is retained");

        assert_eq!(journal.retained(&original), Ok(Some(retained)));
        assert_eq!(
            journal.retained(&request("client-1", "request-1", '1')),
            Err(NativeExchangeJournalError::RequestIdentityReused)
        );
    }

    #[test]
    fn terminal_history_is_bounded_per_client_and_request_identity() {
        let mut journal = NativeExchangeJournal::new(2).expect("capacity is valid");
        let first = request("client-1", "request-1", '0');
        let second = request("client-1", "request-2", '1');
        let third = request("client-2", "request-1", '2');
        for request in [&first, &second, &third] {
            journal
                .remember(request.clone(), outcome(request))
                .expect("result is retained");
        }

        assert_eq!(journal.len(), 2);
        assert_eq!(journal.retained(&first), Ok(None));
        assert!(
            journal
                .retained(&second)
                .expect("lookup succeeds")
                .is_some()
        );
        assert!(journal.retained(&third).expect("lookup succeeds").is_some());
    }

    #[test]
    fn route_invalidation_removes_response_bytes_without_reopening_request_identity() {
        let mut journal = NativeExchangeJournal::new(2).expect("capacity is valid");
        let original = request("client-1", "request-1", '0');
        let matched = NativeExchangeOutcome {
            request_id: original.request_id.clone(),
            route_token: original.route_token.clone(),
            result: NativeExchangeResult::MatchedResponse(NativeExchangeMatchedResponse {
                response: original.report.clone(),
            }),
        };
        journal
            .remember(original.clone(), matched)
            .expect("matched response is retained");

        let finding = text::<FindingId>("HFX-GENERATION-001");
        assert_eq!(
            journal.invalidate_route(
                &original.route_token,
                ProtocolErrorKind::StaleGeneration,
                &finding,
            ),
            1
        );
        assert!(matches!(
            journal.retained(&original),
            Ok(Some(NativeExchangeOutcome {
                result: NativeExchangeResult::NotSent(NativeExchangeNotSent {
                    error_kind: ProtocolErrorKind::StaleGeneration,
                    ..
                }),
                ..
            }))
        ));
        assert_eq!(
            journal.retained(&request("client-1", "request-1", '1')),
            Err(NativeExchangeJournalError::RequestIdentityReused)
        );
    }
}
