// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
    HFX_UAPI_NATIVE_CONTROL_TIMEOUT_NS, HFX_UAPI_NATIVE_DIGEST_BYTES,
    HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_MATCHED_RESPONSE,
    HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_MAY_HAVE_BEEN_SENT,
    HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_NOT_SENT, HFX_UAPI_NATIVE_REPORT_BYTES,
    HfxUapiNativeExchangeResult,
};
use sha2::{Digest, Sha256};
use std::time::Duration;

const NATIVE_REQUEST_DIGEST_DOMAIN: &[u8] = b"hyperflux-native-exchange-request-v1\0";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeEndpointLane {
    Pointer,
    Keyboard,
}

impl NativeEndpointLane {
    pub(crate) const fn as_uapi(self) -> u32 {
        match self {
            Self::Pointer => HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            Self::Keyboard => HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
        }
    }

    pub(crate) const fn from_uapi(value: u32) -> Option<Self> {
        match value {
            HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE => Some(Self::Pointer),
            HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE => Some(Self::Keyboard),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeExchangeRequest {
    pub dispatch_nonce: u64,
    pub endpoint: NativeEndpointLane,
    pub route_token_digest: [u8; HFX_UAPI_NATIVE_DIGEST_BYTES],
    pub report: [u8; HFX_UAPI_NATIVE_REPORT_BYTES],
    pub timeout: Duration,
}

impl NativeExchangeRequest {
    pub(crate) fn validate(&self) -> bool {
        let timeout_ns = self.timeout.as_nanos();
        self.dispatch_nonce != 0
            && self.route_token_digest.iter().any(|byte| *byte != 0)
            && timeout_ns != 0
            && timeout_ns <= u128::from(HFX_UAPI_NATIVE_CONTROL_TIMEOUT_NS)
    }

    pub(crate) fn request_digest(&self) -> [u8; HFX_UAPI_NATIVE_DIGEST_BYTES] {
        let mut hasher = Sha256::new();
        hasher.update(NATIVE_REQUEST_DIGEST_DOMAIN);
        hasher.update(self.endpoint.as_uapi().to_le_bytes());
        hasher.update(self.route_token_digest);
        hasher.update(self.report);
        hasher.finalize().into()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeExchangeCertainty {
    NotSent,
    MayHaveBeenSent,
    MatchedResponse,
}

impl NativeExchangeCertainty {
    #[must_use]
    pub const fn automatic_retry_safe(self) -> bool {
        matches!(self, Self::NotSent)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeExchangeOutcome {
    pub authorization_epoch: u64,
    pub kernel_sequence: u64,
    pub dispatch_nonce: u64,
    pub endpoint: NativeEndpointLane,
    pub route_token_digest: [u8; HFX_UAPI_NATIVE_DIGEST_BYTES],
    pub request_digest: [u8; HFX_UAPI_NATIVE_DIGEST_BYTES],
    pub certainty: NativeExchangeCertainty,
    pub transport_errno: i32,
    pub response: Option<[u8; HFX_UAPI_NATIVE_REPORT_BYTES]>,
}

impl NativeExchangeOutcome {
    pub(crate) fn decode(result: &HfxUapiNativeExchangeResult) -> Option<Self> {
        let endpoint = NativeEndpointLane::from_uapi(result.endpoint_slot)?;
        let (certainty, response) = match result.certainty {
            HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_NOT_SENT
                if result.kernel_sequence.0 == 0 && result.response_length == 0 =>
            {
                (NativeExchangeCertainty::NotSent, None)
            }
            HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_MAY_HAVE_BEEN_SENT
                if result.response_length == 0 =>
            {
                (NativeExchangeCertainty::MayHaveBeenSent, None)
            }
            HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_MATCHED_RESPONSE
                if result.kernel_sequence.0 != 0
                    && result.transport_errno == 0
                    && result.response_length
                        == u32::try_from(HFX_UAPI_NATIVE_REPORT_BYTES).ok()? =>
            {
                (
                    NativeExchangeCertainty::MatchedResponse,
                    Some(result.response),
                )
            }
            _ => return None,
        };
        Some(Self {
            authorization_epoch: result.authorization_epoch.0,
            kernel_sequence: result.kernel_sequence.0,
            dispatch_nonce: result.dispatch_nonce.0,
            endpoint,
            route_token_digest: result.route_token_digest,
            request_digest: result.request_digest,
            certainty,
            transport_errno: result.transport_errno,
            response,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::AlignedU64;

    fn request() -> NativeExchangeRequest {
        NativeExchangeRequest {
            dispatch_nonce: 7,
            endpoint: NativeEndpointLane::Pointer,
            route_token_digest: [0x41; HFX_UAPI_NATIVE_DIGEST_BYTES],
            report: [0x72; HFX_UAPI_NATIVE_REPORT_BYTES],
            timeout: Duration::from_millis(250),
        }
    }

    #[test]
    fn digest_is_domain_separated_and_bound_to_route_endpoint_and_report() {
        let first = request();
        let mut changed = first.clone();
        changed.endpoint = NativeEndpointLane::Keyboard;
        assert_ne!(first.request_digest(), changed.request_digest());
        changed = first.clone();
        changed.route_token_digest[0] ^= 1;
        assert_ne!(first.request_digest(), changed.request_digest());
        changed = first.clone();
        changed.report[89] ^= 1;
        assert_ne!(first.request_digest(), changed.request_digest());
    }

    #[test]
    fn malformed_or_contradictory_outcome_fails_closed() {
        let mut result = HfxUapiNativeExchangeResult {
            authorization_epoch: AlignedU64(4),
            kernel_sequence: AlignedU64(9),
            dispatch_nonce: AlignedU64(7),
            endpoint_slot: HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
            certainty: HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_MATCHED_RESPONSE,
            route_token_digest: [0x41; HFX_UAPI_NATIVE_DIGEST_BYTES],
            request_digest: request().request_digest(),
            response_length: 90,
            response: [0x72; HFX_UAPI_NATIVE_REPORT_BYTES],
            ..HfxUapiNativeExchangeResult::default()
        };
        assert_eq!(
            NativeExchangeOutcome::decode(&result)
                .expect("valid outcome")
                .certainty,
            NativeExchangeCertainty::MatchedResponse
        );
        result.response_length = 0;
        assert!(NativeExchangeOutcome::decode(&result).is_none());
    }
}
