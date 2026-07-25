// SPDX-License-Identifier: GPL-2.0-only

use crate::{NativeChecksumAlgorithm, NativeExchangeRecord};
use std::fmt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeReportValidationError {
    InvalidProfile,
    InvalidLength,
    InvalidRequestStatus,
    InvalidResponseStatus,
    InvalidProtocolType,
    InvalidDataSize,
    InvalidChecksum,
    InvalidTrailer,
}

impl fmt::Display for NativeReportValidationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidProfile => "native report profile is invalid",
            Self::InvalidLength => "native report length is invalid",
            Self::InvalidRequestStatus => "native request status is invalid",
            Self::InvalidResponseStatus => "native response status is invalid",
            Self::InvalidProtocolType => "native report protocol type is invalid",
            Self::InvalidDataSize => "native report data size is invalid",
            Self::InvalidChecksum => "native report checksum is invalid",
            Self::InvalidTrailer => "native report trailer is invalid",
        })
    }
}

impl std::error::Error for NativeReportValidationError {}

/// Validates one generated native-exchange record before runtime use.
///
/// # Errors
///
/// Returns [`NativeReportValidationError::InvalidProfile`] when an offset,
/// checksum range, or response match range escapes the declared report.
pub fn validate_native_exchange_record(
    profile: &NativeExchangeRecord,
) -> Result<(), NativeReportValidationError> {
    let report_bytes = usize::from(profile.report_bytes);
    let format = profile.report_format;
    let offsets = [
        format.request_status.offset,
        format.response_status.offset,
        format.protocol_type.offset,
        format.data_size.offset,
        format.checksum.result_offset,
        format.trailer.offset,
        format.transaction_id.offset,
    ];
    if report_bytes == 0
        || offsets
            .iter()
            .any(|offset| usize::from(*offset) >= report_bytes)
        || format.checksum.start_offset >= format.checksum.end_offset_exclusive
        || usize::from(format.checksum.end_offset_exclusive) > report_bytes
        || (format.checksum.result_offset >= format.checksum.start_offset
            && format.checksum.result_offset < format.checksum.end_offset_exclusive)
        || profile.match_ranges.is_empty()
    {
        return Err(NativeReportValidationError::InvalidProfile);
    }
    let mut previous_end = 0_usize;
    for range in profile.match_ranges {
        let offset = usize::from(range.offset);
        let length = usize::from(range.length);
        if length == 0 || offset < previous_end || offset + length > report_bytes {
            return Err(NativeReportValidationError::InvalidProfile);
        }
        previous_end = offset + length;
    }
    Ok(())
}

/// Validates one opaque request against its generated protocol format.
///
/// # Errors
///
/// Returns a typed structural error before any transport may observe malformed
/// request bytes.
pub fn validate_native_request(
    profile: &NativeExchangeRecord,
    report: &[u8],
) -> Result<(), NativeReportValidationError> {
    validate_native_exchange_record(profile)?;
    validate_report_length(profile, report)?;
    let format = profile.report_format;
    if report[usize::from(format.request_status.offset)] != format.request_status.value {
        return Err(NativeReportValidationError::InvalidRequestStatus);
    }
    validate_common_report(profile, report)
}

/// Validates one response against its generated protocol format.
///
/// Response status remains an upstream-device semantic. The tunnel validates
/// framing and returns failure statuses unchanged to the owning application.
///
/// # Errors
///
/// Returns a typed structural error for malformed response bytes.
pub fn validate_native_response(
    profile: &NativeExchangeRecord,
    report: &[u8],
) -> Result<(), NativeReportValidationError> {
    validate_native_exchange_record(profile)?;
    validate_report_length(profile, report)?;
    let status = profile.report_format.response_status;
    let response_status = report[usize::from(status.offset)];
    if response_status < status.minimum || response_status > status.maximum {
        return Err(NativeReportValidationError::InvalidResponseStatus);
    }
    validate_common_report(profile, report)
}

/// Checks one structurally valid response against declarative request fields.
///
/// # Errors
///
/// Returns a typed structural error when either report is malformed. A valid
/// but unrelated response returns `Ok(false)`.
pub fn native_response_matches(
    profile: &NativeExchangeRecord,
    request: &[u8],
    response: &[u8],
) -> Result<bool, NativeReportValidationError> {
    validate_native_request(profile, request)?;
    validate_native_response(profile, response)?;
    let transaction = profile.report_format.transaction_id;
    let transaction_difference =
        request[usize::from(transaction.offset)] ^ response[usize::from(transaction.offset)];
    if transaction_difference & transaction.match_mask != 0 {
        return Ok(false);
    }
    Ok(profile.match_ranges.iter().all(|range| {
        let start = usize::from(range.offset);
        let end = start + usize::from(range.length);
        request[start..end] == response[start..end]
    }))
}

fn validate_report_length(
    profile: &NativeExchangeRecord,
    report: &[u8],
) -> Result<(), NativeReportValidationError> {
    if report.len() == usize::from(profile.report_bytes) {
        Ok(())
    } else {
        Err(NativeReportValidationError::InvalidLength)
    }
}

fn validate_common_report(
    profile: &NativeExchangeRecord,
    report: &[u8],
) -> Result<(), NativeReportValidationError> {
    let format = profile.report_format;
    if report[usize::from(format.protocol_type.offset)] != format.protocol_type.value {
        return Err(NativeReportValidationError::InvalidProtocolType);
    }
    if report[usize::from(format.data_size.offset)] > format.data_size.maximum {
        return Err(NativeReportValidationError::InvalidDataSize);
    }
    let checksum = match format.checksum.algorithm {
        NativeChecksumAlgorithm::Xor8 => report[usize::from(format.checksum.start_offset)
            ..usize::from(format.checksum.end_offset_exclusive)]
            .iter()
            .fold(0_u8, |value, byte| value ^ byte),
    };
    if checksum != report[usize::from(format.checksum.result_offset)] {
        return Err(NativeReportValidationError::InvalidChecksum);
    }
    if report[usize::from(format.trailer.offset)] != format.trailer.value {
        return Err(NativeReportValidationError::InvalidTrailer);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        NativeByteRangeRecord, NativeByteValueRecord, NativeChecksumRecord, NativeDataSizeRecord,
        NativeMatchRangeRecord, NativeReportFormatRecord, NativeTransactionIdRecord,
    };

    const MATCH_RANGES: &[NativeMatchRangeRecord] = &[
        NativeMatchRangeRecord {
            offset: 2,
            length: 2,
        },
        NativeMatchRangeRecord {
            offset: 6,
            length: 2,
        },
    ];

    const fn profile(transaction_mask: u8) -> NativeExchangeRecord {
        NativeExchangeRecord {
            protocol_family: "razer-feature-report-v1",
            report_bytes: 90,
            set_report_index: 0,
            response_report_index: 0,
            initial_response_delay_us: 8_000,
            response_retry_delay_us: 10_000,
            max_response_attempts: 5,
            report_format: NativeReportFormatRecord {
                request_status: NativeByteValueRecord {
                    offset: 0,
                    value: 0,
                },
                response_status: NativeByteRangeRecord {
                    offset: 0,
                    minimum: 1,
                    maximum: 5,
                },
                protocol_type: NativeByteValueRecord {
                    offset: 4,
                    value: 0,
                },
                data_size: NativeDataSizeRecord {
                    offset: 5,
                    maximum: 80,
                },
                checksum: NativeChecksumRecord {
                    algorithm: NativeChecksumAlgorithm::Xor8,
                    start_offset: 2,
                    end_offset_exclusive: 88,
                    result_offset: 88,
                },
                trailer: NativeByteValueRecord {
                    offset: 89,
                    value: 0,
                },
                transaction_id: NativeTransactionIdRecord {
                    offset: 1,
                    match_mask: transaction_mask,
                },
            },
            match_ranges: MATCH_RANGES,
        }
    }

    fn report(status: u8, transaction: u8) -> [u8; 90] {
        let mut report = [0_u8; 90];
        report[0] = status;
        report[1] = transaction;
        report[5] = 2;
        report[6] = 0x1f;
        report[7] = 0x03;
        report[8] = 0xaa;
        report[9] = 0x55;
        report[88] = report[2..88].iter().fold(0_u8, |value, byte| value ^ byte);
        report
    }

    #[test]
    fn valid_request_and_response_match_declarative_fields() {
        let request = report(0, 0x4f);
        let response = report(2, 0x4f);
        assert_eq!(validate_native_request(&profile(0xff), &request), Ok(()));
        assert_eq!(validate_native_response(&profile(0xff), &response), Ok(()));
        assert_eq!(
            native_response_matches(&profile(0xff), &request, &response),
            Ok(true)
        );
    }

    #[test]
    fn malformed_fields_fail_before_matching() {
        let valid = report(0, 0);
        for (offset, expected) in [
            (0, NativeReportValidationError::InvalidRequestStatus),
            (4, NativeReportValidationError::InvalidProtocolType),
            (5, NativeReportValidationError::InvalidDataSize),
            (88, NativeReportValidationError::InvalidChecksum),
            (89, NativeReportValidationError::InvalidTrailer),
        ] {
            let mut malformed = valid;
            malformed[offset] = if offset == 5 { 81 } else { 1 };
            assert_eq!(
                validate_native_request(&profile(0), &malformed),
                Err(expected)
            );
        }
    }

    #[test]
    fn response_matching_honors_optional_transaction_mask() {
        let request = report(0, 0x10);
        let response = report(2, 0x20);
        assert_eq!(
            native_response_matches(&profile(0), &request, &response),
            Ok(true)
        );
        assert_eq!(
            native_response_matches(&profile(0xff), &request, &response),
            Ok(false)
        );
    }

    #[test]
    fn malformed_response_is_not_a_match() {
        let request = report(0, 0);
        let mut response = report(2, 0);
        response[88] ^= 1;
        assert_eq!(
            native_response_matches(&profile(0), &request, &response),
            Err(NativeReportValidationError::InvalidChecksum)
        );
    }

    #[test]
    fn nonterminal_response_status_is_not_a_match() {
        let request = report(0, 0);
        let response = report(0, 0);
        assert_eq!(
            native_response_matches(&profile(0), &request, &response),
            Err(NativeReportValidationError::InvalidResponseStatus)
        );
    }
}
