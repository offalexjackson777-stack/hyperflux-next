// SPDX-License-Identifier: GPL-2.0-only

use crate::bridge::{BridgeExchange, BridgeExchangeFailure, BridgeExchangeResult};
use crate::uhid::FEATURE_REPORT_BYTES;
use sha2::{Digest, Sha256};
use std::fmt;

const BUSY_STATUS: u8 = 0x01;
const SUCCESS_STATUS: u8 = 0x02;
const FAILURE_STATUS: u8 = 0x03;
const TIMEOUT_STATUS: u8 = 0x04;
const UNSUPPORTED_STATUS: u8 = 0x05;
const ARGUMENT_OFFSET: usize = 8;
const CRC_OFFSET: usize = 88;
const RESERVED_OFFSET: usize = 89;
const MAX_ARGUMENT_BYTES: usize = CRC_OFFSET - ARGUMENT_OFFSET;
const FINGERPRINT_DOMAIN: &[u8] = b"hyperflux-native-serial-fingerprint-v1\0";

/// A non-reversible, process-local comparison value. The source serial is
/// never retained after construction and this type deliberately redacts its
/// `Debug` representation.
#[derive(Clone, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct SerialFingerprint([u8; 32]);

impl fmt::Debug for SerialFingerprint {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("SerialFingerprint(<redacted>)")
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SerialQuerySpec {
    transaction_id: u8,
    command_class: u8,
    command_id: u8,
    data_size: u8,
    arguments: Vec<u8>,
    normalized_sha256: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SerialQueryError {
    InvalidDataSize,
    InvalidArguments,
    InvalidDigest,
}

impl fmt::Display for SerialQueryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidDataSize => "serial query data size is outside the Razer report",
            Self::InvalidArguments => "serial query arguments do not match its data size",
            Self::InvalidDigest => "serial query no longer matches its source-bound digest",
        })
    }
}

impl std::error::Error for SerialQueryError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PublicationFailureDisposition {
    Retryable,
    Blocked,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PublicationIdentityFailure {
    Bridge(BridgeExchangeFailure),
    Status(u8),
    TransactionMismatch,
    HeaderMismatch,
    DataSizeMismatch,
    ClassMismatch,
    CommandMismatch,
    CrcMismatch,
    ReservedMismatch,
    EmptySerial,
    InvalidSerial,
    IdentityMismatch,
    DirectStateUnavailable,
    UhidOpen,
    UhidRead,
    UhidWrite,
    UhidProtocol,
    UhidStartTimeout,
    KernelBinding,
}

impl PublicationIdentityFailure {
    #[must_use]
    pub const fn disposition(self) -> PublicationFailureDisposition {
        match self {
            Self::Bridge(
                BridgeExchangeFailure::LeaseConflict
                | BridgeExchangeFailure::LeaseRejected
                | BridgeExchangeFailure::LeaseSdk
                | BridgeExchangeFailure::PrepareSdk
                | BridgeExchangeFailure::TransactSdk
                | BridgeExchangeFailure::TransportNotSent
                | BridgeExchangeFailure::TransportMayHaveBeenSent,
            )
            | Self::Status(BUSY_STATUS | FAILURE_STATUS | TIMEOUT_STATUS)
            | Self::IdentityMismatch
            | Self::DirectStateUnavailable
            // A driver hand-over (hid-generic re-probed by a late-loading
            // OpenRazer module) stops the device; recreating it recovers.
            | Self::KernelBinding => PublicationFailureDisposition::Retryable,
            Self::Bridge(
                BridgeExchangeFailure::MissingRouteAuthority
                | BridgeExchangeFailure::RequestEncoding
                | BridgeExchangeFailure::TimeoutEncoding
                | BridgeExchangeFailure::ResponseEncoding,
            )
            | Self::Status(_)
            | Self::TransactionMismatch
            | Self::HeaderMismatch
            | Self::DataSizeMismatch
            | Self::ClassMismatch
            | Self::CommandMismatch
            | Self::CrcMismatch
            | Self::ReservedMismatch
            | Self::EmptySerial
            | Self::InvalidSerial
            | Self::UhidOpen
            | Self::UhidRead
            | Self::UhidWrite
            | Self::UhidProtocol
            | Self::UhidStartTimeout => PublicationFailureDisposition::Blocked,
        }
    }

    #[must_use]
    pub const fn stage(self) -> &'static str {
        match self {
            Self::Bridge(
                BridgeExchangeFailure::LeaseConflict
                | BridgeExchangeFailure::LeaseRejected
                | BridgeExchangeFailure::LeaseSdk,
            ) => "lease",
            Self::Bridge(
                BridgeExchangeFailure::PrepareSdk
                | BridgeExchangeFailure::RequestEncoding
                | BridgeExchangeFailure::TimeoutEncoding,
            ) => "prepare",
            Self::Bridge(
                BridgeExchangeFailure::TransactSdk
                | BridgeExchangeFailure::TransportNotSent
                | BridgeExchangeFailure::TransportMayHaveBeenSent,
            ) => "transport",
            Self::Bridge(
                BridgeExchangeFailure::MissingRouteAuthority
                | BridgeExchangeFailure::ResponseEncoding,
            )
            | Self::Status(_)
            | Self::TransactionMismatch
            | Self::HeaderMismatch
            | Self::DataSizeMismatch
            | Self::ClassMismatch
            | Self::CommandMismatch
            | Self::CrcMismatch
            | Self::ReservedMismatch
            | Self::EmptySerial
            | Self::InvalidSerial
            | Self::IdentityMismatch => "identity",
            Self::DirectStateUnavailable => "direct-route",
            Self::UhidOpen | Self::UhidWrite => "uhid-create",
            Self::UhidRead | Self::UhidProtocol => "uhid-runtime",
            Self::UhidStartTimeout => "uhid-start",
            Self::KernelBinding => "kernel-binding",
        }
    }

    #[must_use]
    pub const fn class(self) -> &'static str {
        match self {
            Self::Bridge(BridgeExchangeFailure::LeaseConflict) => "lease-conflict",
            Self::Bridge(BridgeExchangeFailure::LeaseRejected) => "lease-rejected",
            Self::Bridge(BridgeExchangeFailure::LeaseSdk) => "lease-sdk",
            Self::Bridge(BridgeExchangeFailure::PrepareSdk) => "prepare-sdk-not-sent",
            Self::Bridge(BridgeExchangeFailure::TransactSdk) => "transact-sdk-may-have-been-sent",
            Self::Bridge(BridgeExchangeFailure::TransportNotSent) => "not-sent",
            Self::Bridge(BridgeExchangeFailure::TransportMayHaveBeenSent) => "may-have-been-sent",
            Self::Bridge(BridgeExchangeFailure::MissingRouteAuthority) => {
                "missing-route-authority-not-sent"
            }
            Self::Bridge(BridgeExchangeFailure::RequestEncoding) => "request-encoding-not-sent",
            Self::Bridge(BridgeExchangeFailure::TimeoutEncoding) => "timeout-encoding-not-sent",
            Self::Bridge(BridgeExchangeFailure::ResponseEncoding) => {
                "response-encoding-may-have-been-sent"
            }
            Self::Status(BUSY_STATUS) => "status-busy",
            Self::Status(FAILURE_STATUS) => "status-failure",
            Self::Status(TIMEOUT_STATUS) => "status-timeout",
            Self::Status(UNSUPPORTED_STATUS) => "status-unsupported",
            Self::Status(_) => "status-other",
            Self::TransactionMismatch => "transaction-mismatch",
            Self::HeaderMismatch => "header-mismatch",
            Self::DataSizeMismatch => "data-size-mismatch",
            Self::ClassMismatch => "class-mismatch",
            Self::CommandMismatch => "command-mismatch",
            Self::CrcMismatch => "crc-mismatch",
            Self::ReservedMismatch => "reserved-mismatch",
            Self::EmptySerial => "empty-serial",
            Self::InvalidSerial => "invalid-serial",
            Self::IdentityMismatch => "identity-mismatch",
            Self::DirectStateUnavailable => "state-unavailable",
            Self::UhidOpen => "uhid-open",
            Self::UhidRead => "uhid-read",
            Self::UhidWrite => "uhid-write",
            Self::UhidProtocol => "uhid-protocol",
            Self::UhidStartTimeout => "missing-uhid-start",
            Self::KernelBinding => "kernel-driver-binding",
        }
    }

    #[must_use]
    pub const fn status(self) -> Option<u8> {
        match self {
            Self::Status(status) => Some(status),
            _ => None,
        }
    }
}

impl SerialQuerySpec {
    /// Creates one source-bound read-only serial query.
    ///
    /// # Errors
    ///
    /// Rejects an invalid report shape or a normalized request digest that no
    /// longer matches the upstream-derived catalog.
    pub fn new(
        transaction_id: u8,
        command_class: u8,
        command_id: u8,
        data_size: u8,
        arguments: Vec<u8>,
        normalized_sha256: String,
    ) -> Result<Self, SerialQueryError> {
        if data_size == 0 || usize::from(data_size) > MAX_ARGUMENT_BYTES {
            return Err(SerialQueryError::InvalidDataSize);
        }
        if transaction_id == 0 || command_id < 0x80 || arguments.len() != usize::from(data_size) {
            return Err(SerialQueryError::InvalidArguments);
        }
        let query = Self {
            transaction_id,
            command_class,
            command_id,
            data_size,
            arguments,
            normalized_sha256,
        };
        if query.normalized_sha256.len() != 64
            || query.normalized_sha256 != hex_sha256(&query.normalized_request())
        {
            return Err(SerialQueryError::InvalidDigest);
        }
        Ok(query)
    }

    #[must_use]
    pub fn request(&self) -> [u8; FEATURE_REPORT_BYTES] {
        let mut report = self.normalized_request();
        report[CRC_OFFSET] = report_crc(&report);
        report
    }

    fn normalized_request(&self) -> [u8; FEATURE_REPORT_BYTES] {
        let mut report = [0_u8; FEATURE_REPORT_BYTES];
        report[1] = self.transaction_id;
        report[5] = self.data_size;
        report[6] = self.command_class;
        report[7] = self.command_id;
        let end = ARGUMENT_OFFSET + self.arguments.len();
        report[ARGUMENT_OFFSET..end].copy_from_slice(&self.arguments);
        report
    }

    /// Validates one matched response against the exact source-derived request.
    ///
    /// # Errors
    ///
    /// Returns a typed status, framing, integrity, or serial-data failure.
    pub fn fingerprint_response(
        &self,
        request: &[u8; FEATURE_REPORT_BYTES],
        response: &[u8; FEATURE_REPORT_BYTES],
    ) -> Result<SerialFingerprint, PublicationIdentityFailure> {
        if response[1] != request[1] {
            return Err(PublicationIdentityFailure::TransactionMismatch);
        }
        if response[2..5] != request[2..5] {
            return Err(PublicationIdentityFailure::HeaderMismatch);
        }
        if response[5] != self.data_size {
            return Err(PublicationIdentityFailure::DataSizeMismatch);
        }
        if response[6] != self.command_class {
            return Err(PublicationIdentityFailure::ClassMismatch);
        }
        if response[7] != self.command_id {
            return Err(PublicationIdentityFailure::CommandMismatch);
        }
        if response[CRC_OFFSET] != report_crc(response) {
            return Err(PublicationIdentityFailure::CrcMismatch);
        }
        if response[RESERVED_OFFSET] != 0 {
            return Err(PublicationIdentityFailure::ReservedMismatch);
        }
        if response[0] != SUCCESS_STATUS {
            return Err(PublicationIdentityFailure::Status(response[0]));
        }
        let end = ARGUMENT_OFFSET + usize::from(self.data_size);
        classify_serial_bytes(&response[ARGUMENT_OFFSET..end])
    }

    /// Performs one source-derived serial identity exchange.
    ///
    /// # Errors
    ///
    /// Returns a typed bridge or matched-response validation failure.
    pub fn query<B: BridgeExchange>(
        &self,
        bridge: &mut B,
    ) -> Result<SerialFingerprint, PublicationIdentityFailure> {
        let request = self.request();
        match bridge.exchange(request) {
            BridgeExchangeResult::MatchedResponse(response) => {
                self.fingerprint_response(&request, &response)
            }
            BridgeExchangeResult::NotSent(failure)
            | BridgeExchangeResult::MayHaveBeenSent(failure) => {
                Err(PublicationIdentityFailure::Bridge(failure))
            }
        }
    }
}

#[must_use]
pub fn fingerprint_serial_bytes(value: &[u8]) -> Option<SerialFingerprint> {
    classify_serial_bytes(value).ok()
}

fn classify_serial_bytes(value: &[u8]) -> Result<SerialFingerprint, PublicationIdentityFailure> {
    let value = trim_serial(value);
    if value.is_empty() {
        return Err(PublicationIdentityFailure::EmptySerial);
    }
    if value.len() > MAX_ARGUMENT_BYTES || !value.iter().all(u8::is_ascii_graphic) {
        return Err(PublicationIdentityFailure::InvalidSerial);
    }
    let mut hasher = Sha256::new();
    hasher.update(FINGERPRINT_DOMAIN);
    hasher.update(value);
    Ok(SerialFingerprint(hasher.finalize().into()))
}

fn trim_serial(mut value: &[u8]) -> &[u8] {
    while value
        .first()
        .is_some_and(|byte| *byte == 0 || byte.is_ascii_whitespace())
    {
        value = &value[1..];
    }
    while value
        .last()
        .is_some_and(|byte| *byte == 0 || byte.is_ascii_whitespace())
    {
        value = &value[..value.len() - 1];
    }
    value
}

fn report_crc(report: &[u8; FEATURE_REPORT_BYTES]) -> u8 {
    report[2..CRC_OFFSET]
        .iter()
        .copied()
        .fold(0_u8, |value, byte| value ^ byte)
}

fn hex_sha256(value: &[u8]) -> String {
    let digest = Sha256::digest(value);
    let mut output = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        let _ = write!(output, "{byte:02x}");
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FakeBridge(BridgeExchangeResult);

    impl BridgeExchange for FakeBridge {
        fn exchange(&mut self, _report: [u8; FEATURE_REPORT_BYTES]) -> BridgeExchangeResult {
            self.0
        }
    }

    fn query() -> SerialQuerySpec {
        SerialQuerySpec::new(
            0x1f,
            0,
            0x82,
            22,
            vec![0; 22],
            "9a1a0fefcff4fc7a1fd8c8b8a6d00f674a801e74d127d247deb81a1db0a4bc2f".to_owned(),
        )
        .expect("source-bound serial query is valid")
    }

    fn response(query: &SerialQuerySpec, serial: &[u8]) -> [u8; FEATURE_REPORT_BYTES] {
        let request = query.request();
        let mut response = request;
        response[0] = SUCCESS_STATUS;
        response[ARGUMENT_OFFSET..ARGUMENT_OFFSET + serial.len()].copy_from_slice(serial);
        response[CRC_OFFSET] = report_crc(&response);
        response
    }

    fn next_property_value(state: &mut u64) -> u64 {
        *state ^= *state << 13;
        *state ^= *state >> 7;
        *state ^= *state << 17;
        *state
    }

    #[test]
    fn source_bound_request_includes_the_imported_transaction_and_digest() {
        let query = query();
        assert_eq!(query.request()[1], 0x1f);
        assert_eq!(
            hex_sha256(&query.normalized_request()),
            "9a1a0fefcff4fc7a1fd8c8b8a6d00f674a801e74d127d247deb81a1db0a4bc2f"
        );
        assert_eq!(query.request()[CRC_OFFSET], 0x94);
        assert!(matches!(
            SerialQuerySpec::new(
                0,
                0,
                0x82,
                22,
                vec![0; 22],
                "9a1a0fefcff4fc7a1fd8c8b8a6d00f674a801e74d127d247deb81a1db0a4bc2f".to_owned(),
            ),
            Err(SerialQueryError::InvalidArguments)
        ));
    }

    #[test]
    fn direct_and_receiver_serials_share_only_a_redacted_fingerprint() {
        let query = query();
        let response = response(&query, b"SERIAL-EXAMPLE");
        let from_route = query
            .fingerprint_response(&query.request(), &response)
            .expect("response contains a valid serial");
        let from_sysfs =
            fingerprint_serial_bytes(b"SERIAL-EXAMPLE\n").expect("sysfs serial is normalized");
        assert_eq!(from_route, from_sysfs);
        assert_eq!(format!("{from_route:?}"), "SerialFingerprint(<redacted>)");
    }

    #[test]
    fn status_and_transport_failures_preserve_exact_retry_semantics() {
        let query = query();
        for (status, disposition, class) in [
            (
                BUSY_STATUS,
                PublicationFailureDisposition::Retryable,
                "status-busy",
            ),
            (
                FAILURE_STATUS,
                PublicationFailureDisposition::Retryable,
                "status-failure",
            ),
            (
                TIMEOUT_STATUS,
                PublicationFailureDisposition::Retryable,
                "status-timeout",
            ),
            (
                UNSUPPORTED_STATUS,
                PublicationFailureDisposition::Blocked,
                "status-unsupported",
            ),
            (0x7f, PublicationFailureDisposition::Blocked, "status-other"),
        ] {
            let mut value = response(&query, b"SERIAL-EXAMPLE");
            value[0] = status;
            let failure = query
                .fingerprint_response(&query.request(), &value)
                .expect_err("non-success status cannot identify a route");
            assert_eq!(failure.status(), Some(status));
            assert_eq!(failure.disposition(), disposition);
            assert_eq!(failure.class(), class);
        }

        for (result, disposition, class) in [
            (
                BridgeExchangeResult::NotSent(BridgeExchangeFailure::LeaseConflict),
                PublicationFailureDisposition::Retryable,
                "lease-conflict",
            ),
            (
                BridgeExchangeResult::NotSent(BridgeExchangeFailure::PrepareSdk),
                PublicationFailureDisposition::Retryable,
                "prepare-sdk-not-sent",
            ),
            (
                BridgeExchangeResult::MayHaveBeenSent(BridgeExchangeFailure::TransactSdk),
                PublicationFailureDisposition::Retryable,
                "transact-sdk-may-have-been-sent",
            ),
            (
                BridgeExchangeResult::NotSent(BridgeExchangeFailure::TransportNotSent),
                PublicationFailureDisposition::Retryable,
                "not-sent",
            ),
            (
                BridgeExchangeResult::MayHaveBeenSent(
                    BridgeExchangeFailure::TransportMayHaveBeenSent,
                ),
                PublicationFailureDisposition::Retryable,
                "may-have-been-sent",
            ),
            (
                BridgeExchangeResult::MayHaveBeenSent(BridgeExchangeFailure::ResponseEncoding),
                PublicationFailureDisposition::Blocked,
                "response-encoding-may-have-been-sent",
            ),
        ] {
            let mut bridge = FakeBridge(result);
            let failure = query.query(&mut bridge).expect_err("transport failed");
            assert_eq!(failure.disposition(), disposition);
            assert_eq!(failure.class(), class);
            assert!(!format!("{failure:?}").contains("SERIAL"));
        }
    }

    #[test]
    fn every_structural_mismatch_is_distinct_and_blocking() {
        let query = query();
        let request = query.request();
        let valid = response(&query, b"SERIAL-EXAMPLE");
        let mut mutations = Vec::new();

        let mut value = valid;
        value[1] ^= 1;
        mutations.push((value, PublicationIdentityFailure::TransactionMismatch));

        let mut value = valid;
        value[2] ^= 1;
        value[CRC_OFFSET] = report_crc(&value);
        mutations.push((value, PublicationIdentityFailure::HeaderMismatch));

        let mut value = valid;
        value[5] ^= 1;
        value[CRC_OFFSET] = report_crc(&value);
        mutations.push((value, PublicationIdentityFailure::DataSizeMismatch));

        let mut value = valid;
        value[6] ^= 1;
        value[CRC_OFFSET] = report_crc(&value);
        mutations.push((value, PublicationIdentityFailure::ClassMismatch));

        let mut value = valid;
        value[7] ^= 1;
        value[CRC_OFFSET] = report_crc(&value);
        mutations.push((value, PublicationIdentityFailure::CommandMismatch));

        let mut value = valid;
        value[CRC_OFFSET] ^= 1;
        mutations.push((value, PublicationIdentityFailure::CrcMismatch));

        let mut value = valid;
        value[RESERVED_OFFSET] = 1;
        mutations.push((value, PublicationIdentityFailure::ReservedMismatch));

        for (value, expected) in mutations {
            let failure = query
                .fingerprint_response(&request, &value)
                .expect_err("malformed response cannot identify a route");
            assert_eq!(failure, expected);
            assert_eq!(
                failure.disposition(),
                PublicationFailureDisposition::Blocked
            );
        }
    }

    #[test]
    fn empty_and_non_ascii_serials_are_distinct_private_failures() {
        let query = query();
        let empty = response(&query, &[0; 22]);
        assert_eq!(
            query.fingerprint_response(&query.request(), &empty),
            Err(PublicationIdentityFailure::EmptySerial)
        );

        let invalid = response(&query, b"SERIAL\x01VALUE");
        assert_eq!(
            query.fingerprint_response(&query.request(), &invalid),
            Err(PublicationIdentityFailure::InvalidSerial)
        );
    }

    #[test]
    fn seeded_response_mutations_are_accepted_only_as_valid_private_serials() {
        let query = query();
        let request = query.request();
        let canonical = response(&query, b"SERIAL-EXAMPLE");
        let mut state = 0xbb67_ae85_84ca_a73b_u64;
        for iteration in 0..2048_u16 {
            let mut candidate = canonical;
            let mutations = usize::from(iteration % 3 + 1);
            for _ in 0..mutations {
                let value = next_property_value(&mut state);
                let index =
                    usize::try_from(value).expect("host usize contains u64") % FEATURE_REPORT_BYTES;
                candidate[index] ^=
                    u8::try_from(((value >> 32) & 0xff) | 1).expect("masked byte fits u8");
            }
            if iteration % 2 == 0 {
                candidate[CRC_OFFSET] = report_crc(&candidate);
            }

            match query.fingerprint_response(&request, &candidate) {
                Ok(fingerprint) => {
                    assert_eq!(candidate[0], SUCCESS_STATUS);
                    assert_eq!(candidate[1..8], request[1..8]);
                    assert_eq!(candidate[CRC_OFFSET], report_crc(&candidate));
                    assert_eq!(candidate[RESERVED_OFFSET], 0);
                    let serial = &candidate[ARGUMENT_OFFSET..ARGUMENT_OFFSET + 22];
                    assert_eq!(fingerprint_serial_bytes(serial), Some(fingerprint));
                }
                Err(failure) => {
                    assert!(!failure.class().is_empty());
                    assert!(!format!("{failure:?}").contains("SERIAL-EXAMPLE"));
                }
            }
        }
    }
}
