// SPDX-License-Identifier: GPL-2.0-only

use crate::bridge::{BridgeExchange, BridgeExchangeResult};
use crate::uhid::FEATURE_REPORT_BYTES;
use sha2::{Digest, Sha256};
use std::fmt;

const SUCCESS_STATUS: u8 = 0x02;
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

impl SerialQuerySpec {
    /// Creates one source-bound read-only serial query.
    ///
    /// # Errors
    ///
    /// Rejects an invalid report shape or a normalized request digest that no
    /// longer matches the upstream-derived catalog.
    pub fn new(
        command_class: u8,
        command_id: u8,
        data_size: u8,
        arguments: Vec<u8>,
        normalized_sha256: String,
    ) -> Result<Self, SerialQueryError> {
        if usize::from(data_size) > MAX_ARGUMENT_BYTES {
            return Err(SerialQueryError::InvalidDataSize);
        }
        if arguments.len() != usize::from(data_size) {
            return Err(SerialQueryError::InvalidArguments);
        }
        let query = Self {
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
        report[5] = self.data_size;
        report[6] = self.command_class;
        report[7] = self.command_id;
        let end = ARGUMENT_OFFSET + self.arguments.len();
        report[ARGUMENT_OFFSET..end].copy_from_slice(&self.arguments);
        report
    }

    #[must_use]
    pub fn fingerprint_response(
        &self,
        request: &[u8; FEATURE_REPORT_BYTES],
        response: &[u8; FEATURE_REPORT_BYTES],
    ) -> Option<SerialFingerprint> {
        if response[0] != SUCCESS_STATUS
            || response[1] != request[1]
            || response[2..4] != request[2..4]
            || response[5] != self.data_size
            || response[6] != self.command_class
            || response[7] != self.command_id
            || response[CRC_OFFSET] != report_crc(response)
            || response[RESERVED_OFFSET] != 0
        {
            return None;
        }
        let end = ARGUMENT_OFFSET + usize::from(self.data_size);
        fingerprint_serial_bytes(&response[ARGUMENT_OFFSET..end])
    }

    #[must_use]
    pub fn query<B: BridgeExchange>(&self, bridge: &mut B) -> Option<SerialFingerprint> {
        let request = self.request();
        match bridge.exchange(request) {
            BridgeExchangeResult::MatchedResponse(response) => {
                self.fingerprint_response(&request, &response)
            }
            BridgeExchangeResult::NotSent | BridgeExchangeResult::MayHaveBeenSent => None,
        }
    }
}

#[must_use]
pub fn fingerprint_serial_bytes(value: &[u8]) -> Option<SerialFingerprint> {
    let value = trim_serial(value);
    if value.is_empty() || value.len() > MAX_ARGUMENT_BYTES || value.contains(&0) {
        return None;
    }
    let mut hasher = Sha256::new();
    hasher.update(FINGERPRINT_DOMAIN);
    hasher.update(value);
    Some(SerialFingerprint(hasher.finalize().into()))
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
            0,
            0x82,
            22,
            vec![0; 22],
            "1ee6772a2962f0e212d4db1ce62dfc328155bb7e508a50940059c1a89e1afcaf".to_owned(),
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

    #[test]
    fn source_bound_request_matches_the_imported_digest() {
        let query = query();
        assert_eq!(
            hex_sha256(&query.normalized_request()),
            "1ee6772a2962f0e212d4db1ce62dfc328155bb7e508a50940059c1a89e1afcaf"
        );
        assert_eq!(query.request()[CRC_OFFSET], 0x94);
    }

    #[test]
    fn direct_and_receiver_serials_share_only_a_redacted_fingerprint() {
        let query = query();
        let response = response(&query, b"SERIAL-EXAMPLE");
        let from_route = query
            .fingerprint_response(&query.request(), &response)
            .expect("response contains a serial");
        let from_sysfs =
            fingerprint_serial_bytes(b"SERIAL-EXAMPLE\n").expect("sysfs serial is normalized");
        assert_eq!(from_route, from_sysfs);
        assert_eq!(format!("{from_route:?}"), "SerialFingerprint(<redacted>)");
    }

    #[test]
    fn malformed_empty_and_transport_failure_results_are_not_identities() {
        let query = query();
        let empty = response(&query, &[0; 22]);
        assert!(
            query
                .fingerprint_response(&query.request(), &empty)
                .is_none()
        );

        let mut malformed = response(&query, b"SERIAL-EXAMPLE");
        malformed[7] ^= 1;
        malformed[CRC_OFFSET] = report_crc(&malformed);
        assert!(
            query
                .fingerprint_response(&query.request(), &malformed)
                .is_none()
        );

        let mut bridge = FakeBridge(BridgeExchangeResult::NotSent);
        assert!(query.query(&mut bridge).is_none());
    }
}
