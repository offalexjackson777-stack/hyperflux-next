// SPDX-License-Identifier: GPL-2.0-only

use crate::{NativeChecksumAlgorithm, NativeExchangeRecord, NativeRouteSelectorField};
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeRouteLane {
    Pointer,
    Keyboard,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct NativeRouteRewrite {
    pub application_selector: u8,
    pub receiver_selector: u8,
    pub translated: bool,
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
    if let Some(selector) = profile.route_selector
        && (!matches!(selector.field, NativeRouteSelectorField::TransactionId)
            || selector.pointer_lane == 0
            || selector.keyboard_lane == 0
            || selector.pointer_lane == selector.keyboard_lane
            || !selector.restore_original_response)
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

/// Rewrites only the receiver-profile route selector while retaining the
/// application-owned command bytes and original selector for response repair.
///
/// # Errors
///
/// Returns a typed structural error before mutating malformed input.
pub fn route_native_request(
    profile: &NativeExchangeRecord,
    lane: NativeRouteLane,
    report: &mut [u8],
) -> Result<NativeRouteRewrite, NativeReportValidationError> {
    validate_native_request(profile, report)?;
    let offset = usize::from(profile.report_format.transaction_id.offset);
    let application_selector = report[offset];
    let Some(selector) = profile.route_selector else {
        return Ok(NativeRouteRewrite {
            application_selector,
            receiver_selector: application_selector,
            translated: false,
        });
    };
    let receiver_selector = match lane {
        NativeRouteLane::Pointer => selector.pointer_lane,
        NativeRouteLane::Keyboard => selector.keyboard_lane,
    };
    let translated = application_selector != receiver_selector;
    if translated {
        report[offset] = receiver_selector;
        update_checksum_if_covered(profile, offset, report);
        validate_native_request(profile, report)?;
    }
    Ok(NativeRouteRewrite {
        application_selector,
        receiver_selector,
        translated,
    })
}

/// Restores the caller's transaction selector after a routed response matched.
///
/// # Errors
///
/// Returns a typed structural error when the response or profile is invalid.
pub fn restore_native_response(
    profile: &NativeExchangeRecord,
    rewrite: NativeRouteRewrite,
    report: &mut [u8],
) -> Result<(), NativeReportValidationError> {
    validate_native_response(profile, report)?;
    if !rewrite.translated {
        return Ok(());
    }
    let Some(selector) = profile.route_selector else {
        return Err(NativeReportValidationError::InvalidProfile);
    };
    if !selector.restore_original_response {
        return Err(NativeReportValidationError::InvalidProfile);
    }
    let offset = usize::from(profile.report_format.transaction_id.offset);
    if report[offset] != rewrite.receiver_selector {
        return Err(NativeReportValidationError::InvalidProfile);
    }
    report[offset] = rewrite.application_selector;
    update_checksum_if_covered(profile, offset, report);
    validate_native_response(profile, report)
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

fn update_checksum_if_covered(
    profile: &NativeExchangeRecord,
    changed_offset: usize,
    report: &mut [u8],
) {
    let checksum = profile.report_format.checksum;
    if changed_offset < usize::from(checksum.start_offset)
        || changed_offset >= usize::from(checksum.end_offset_exclusive)
    {
        return;
    }
    let value = match checksum.algorithm {
        NativeChecksumAlgorithm::Xor8 => report
            [usize::from(checksum.start_offset)..usize::from(checksum.end_offset_exclusive)]
            .iter()
            .fold(0_u8, |current, byte| current ^ byte),
    };
    report[usize::from(checksum.result_offset)] = value;
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        NativeByteRangeRecord, NativeByteValueRecord, NativeChecksumRecord, NativeDataSizeRecord,
        NativeMatchRangeRecord, NativeReportFormatRecord, NativeRouteSelectorRecord,
        NativeTransactionIdRecord,
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
            route_selector: Some(NativeRouteSelectorRecord {
                field: NativeRouteSelectorField::TransactionId,
                pointer_lane: 0x1f,
                keyboard_lane: 0x9f,
                restore_original_response: true,
            }),
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
    fn every_device_status_is_matched_without_interpretation() {
        let request = report(0, 0);
        for status in 1..=5 {
            let response = report(status, 0);
            assert_eq!(
                native_response_matches(&profile(0), &request, &response),
                Ok(true),
                "device status {status} must remain an upstream semantic"
            );
        }
    }

    #[test]
    fn request_only_status_zero_is_not_a_response() {
        let request = report(0, 0);
        let response = report(0, 0);
        assert_eq!(
            native_response_matches(&profile(0), &request, &response),
            Err(NativeReportValidationError::InvalidResponseStatus)
        );
    }

    #[test]
    fn route_selector_preserves_pointer_and_translates_keyboard() {
        let pointer_profile = profile(0xff);
        let mut pointer = report(0, 0x1f);
        let pointer_rewrite =
            route_native_request(&pointer_profile, NativeRouteLane::Pointer, &mut pointer)
                .expect("pointer route");
        assert_eq!(pointer[1], 0x1f);
        assert!(!pointer_rewrite.translated);

        let mut keyboard = report(0, 0xff);
        let rewrite =
            route_native_request(&pointer_profile, NativeRouteLane::Keyboard, &mut keyboard)
                .expect("keyboard route");
        assert_eq!(keyboard[1], 0x9f);
        assert!(rewrite.translated);

        let mut response = report(2, 0x9f);
        restore_native_response(&pointer_profile, rewrite, &mut response)
            .expect("response restoration");
        assert_eq!(response[1], 0xff);
    }

    #[test]
    fn route_selector_recomputes_checksum_when_selected_field_is_covered() {
        let mut covered = profile(0xff);
        covered.report_format.transaction_id.offset = 6;
        let mut request = report(0, 0x00);
        let before = request[88];
        let rewrite = route_native_request(&covered, NativeRouteLane::Keyboard, &mut request)
            .expect("covered route");
        assert_eq!(request[6], 0x9f);
        assert_ne!(request[88], before);
        assert_eq!(validate_native_request(&covered, &request), Ok(()));

        let mut response = request;
        response[0] = 2;
        restore_native_response(&covered, rewrite, &mut response)
            .expect("covered response restoration");
        assert_eq!(response[6], 0x1f);
        assert_eq!(validate_native_response(&covered, &response), Ok(()));
    }
}
