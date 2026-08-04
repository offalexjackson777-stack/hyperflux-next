// SPDX-License-Identifier: GPL-2.0-only

use std::str::FromStr;

use hfx_errors::{
    ERRORS, ErrorCode, MAX_ERROR_COUNT, REMEDIATIONS, RetryPolicy, SafeDetail,
    SafeDetailValidationError, SafeDetailValue, SideEffectCertaintyPolicy, error_by_code,
    remediation_by_id, validate_safe_details,
};

#[test]
fn every_generated_identity_resolves_to_one_descriptor() {
    assert!(!ERRORS.is_empty());
    assert!(ERRORS.len() <= MAX_ERROR_COUNT);
    for descriptor in ERRORS {
        assert_eq!(error_by_code(descriptor.code), descriptor);
        assert_eq!(
            ErrorCode::from_str(descriptor.code.as_str()),
            Ok(descriptor.code)
        );
        assert_eq!(
            remediation_by_id(descriptor.remediation_id).id,
            descriptor.remediation_id
        );
    }
    assert!(REMEDIATIONS.iter().all(|remediation| {
        ERRORS
            .iter()
            .any(|descriptor| descriptor.remediation_id == remediation.id)
    }));
}

#[test]
fn retry_policy_never_replays_uncertain_hardware_transport() {
    for descriptor in ERRORS {
        if matches!(
            descriptor.side_effect_certainty_policy,
            SideEffectCertaintyPolicy::Possible | SideEffectCertaintyPolicy::Partial
        ) {
            assert_eq!(descriptor.retry_policy, RetryPolicy::OutcomeLookupOnly);
        }
        if descriptor.retry_policy == RetryPolicy::BoundedBackoff {
            assert_eq!(
                descriptor.side_effect_certainty_policy,
                SideEffectCertaintyPolicy::MustBeNone
            );
        }
    }
}

#[test]
fn safe_details_accept_only_declared_bounded_public_values() {
    let valid = [
        SafeDetail {
            name: "client_max_version",
            value: SafeDetailValue::Unsigned(1),
        },
        SafeDetail {
            name: "client_min_version",
            value: SafeDetailValue::Unsigned(1),
        },
        SafeDetail {
            name: "server_max_version",
            value: SafeDetailValue::Unsigned(1),
        },
        SafeDetail {
            name: "server_min_version",
            value: SafeDetailValue::Unsigned(1),
        },
    ];
    validate_safe_details(ErrorCode::HfxProtocol001, &valid).expect("bounded details are valid");

    let missing = &valid[..3];
    assert_eq!(
        validate_safe_details(ErrorCode::HfxProtocol001, missing),
        Err(SafeDetailValidationError::MissingField(
            "server_min_version"
        ))
    );

    let unknown = [SafeDetail {
        name: "raw_payload",
        value: SafeDetailValue::Text("forbidden"),
    }];
    assert_eq!(
        validate_safe_details(ErrorCode::HfxTransport002, &unknown),
        Err(SafeDetailValidationError::UnknownField("raw_payload"))
    );
}

#[test]
fn decimal_details_require_canonical_cross_language_wire_strings() {
    let canonical = [
        SafeDetail {
            name: "active_generation",
            value: SafeDetailValue::Decimal("18446744073709551615"),
        },
        SafeDetail {
            name: "requested_generation",
            value: SafeDetailValue::Decimal("1"),
        },
    ];
    validate_safe_details(ErrorCode::HfxGeneration001, &canonical)
        .expect("canonical u64 strings are valid");

    let noncanonical = [
        canonical[0],
        SafeDetail {
            name: "requested_generation",
            value: SafeDetailValue::Decimal("01"),
        },
    ];
    assert_eq!(
        validate_safe_details(ErrorCode::HfxGeneration001, &noncanonical),
        Err(SafeDetailValidationError::InvalidValue(
            "requested_generation"
        ))
    );
}

#[test]
fn wire_codes_are_stable_strings_and_reject_unknown_values() {
    let text = serde_json::to_string(&ErrorCode::HfxTransport002).expect("serialize code");
    assert_eq!(text, r#""HFX-TRANSPORT-002""#);
    assert!(serde_json::from_str::<ErrorCode>(r#""HFX-TRANSPORT-999""#).is_err());
}
