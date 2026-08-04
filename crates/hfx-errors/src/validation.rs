// SPDX-License-Identifier: GPL-2.0-only

use std::fmt;

use crate::{
    ErrorCode, MAX_SAFE_DETAIL_FIELDS, SafeDetailFieldDescriptor, SafeDetailKind, error_by_code,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SafeDetailValue<'a> {
    Boolean(bool),
    Unsigned(u64),
    Decimal(&'a str),
    Text(&'a str),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SafeDetail<'a> {
    pub name: &'a str,
    pub value: SafeDetailValue<'a>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SafeDetailValidationError<'a> {
    TooManyFields,
    UnknownField(&'a str),
    DuplicateField(&'a str),
    MissingField(&'static str),
    InvalidValue(&'a str),
}

impl fmt::Display for SafeDetailValidationError<'_> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooManyFields => formatter.write_str("too many safe detail fields"),
            Self::UnknownField(name) => write!(formatter, "unknown safe detail field: {name}"),
            Self::DuplicateField(name) => write!(formatter, "duplicate safe detail field: {name}"),
            Self::MissingField(name) => write!(formatter, "missing safe detail field: {name}"),
            Self::InvalidValue(name) => write!(formatter, "invalid safe detail field: {name}"),
        }
    }
}

impl std::error::Error for SafeDetailValidationError<'_> {}

fn safe_identifier(value: &str) -> bool {
    let mut characters = value.bytes();
    let Some(first) = characters.next() else {
        return false;
    };
    first.is_ascii_alphanumeric()
        && characters.all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, b'.' | b'_' | b':' | b'-')
        })
}

fn canonical_decimal(value: &str, maximum: u64) -> bool {
    if value.is_empty()
        || !value.bytes().all(|character| character.is_ascii_digit())
        || (value.len() > 1 && value.starts_with('0'))
    {
        return false;
    }
    value.parse::<u64>().is_ok_and(|number| number <= maximum)
}

fn valid_value(field: &SafeDetailFieldDescriptor, value: SafeDetailValue<'_>) -> bool {
    match (field.kind, value) {
        (SafeDetailKind::Boolean, SafeDetailValue::Boolean(_)) => true,
        (SafeDetailKind::U16 | SafeDetailKind::U32, SafeDetailValue::Unsigned(number)) => {
            field.maximum_value.is_some_and(|maximum| number <= maximum)
        }
        (SafeDetailKind::U64Decimal, SafeDetailValue::Decimal(value)) => field
            .maximum_value
            .is_some_and(|maximum| canonical_decimal(value, maximum)),
        (SafeDetailKind::Identifier, SafeDetailValue::Text(value)) => {
            field
                .maximum_length
                .is_some_and(|maximum| value.len() <= maximum)
                && safe_identifier(value)
        }
        (SafeDetailKind::Text, SafeDetailValue::Text(value)) => {
            field
                .maximum_length
                .is_some_and(|maximum| !value.is_empty() && value.len() <= maximum)
                && value
                    .bytes()
                    .all(|character| (32..=126).contains(&character))
        }
        (SafeDetailKind::Enum, SafeDetailValue::Text(value)) => {
            field.allowed_values.contains(&value)
        }
        _ => false,
    }
}

/// Validates one finding's complete bounded safe-detail set.
///
/// # Errors
///
/// Returns an error for missing, duplicate, unknown, ill-typed, non-canonical, or out-of-bound
/// fields.
pub fn validate_safe_details<'a>(
    code: ErrorCode,
    details: &'a [SafeDetail<'a>],
) -> Result<(), SafeDetailValidationError<'a>> {
    if details.len() > MAX_SAFE_DETAIL_FIELDS {
        return Err(SafeDetailValidationError::TooManyFields);
    }
    let descriptor = error_by_code(code);
    for (index, detail) in details.iter().enumerate() {
        if details[..index]
            .iter()
            .any(|previous| previous.name == detail.name)
        {
            return Err(SafeDetailValidationError::DuplicateField(detail.name));
        }
        let Some(field) = descriptor
            .safe_detail_fields
            .iter()
            .find(|field| field.name == detail.name)
        else {
            return Err(SafeDetailValidationError::UnknownField(detail.name));
        };
        if !valid_value(field, detail.value) {
            return Err(SafeDetailValidationError::InvalidValue(detail.name));
        }
    }
    for field in descriptor.safe_detail_fields {
        if field.required && !details.iter().any(|detail| detail.name == field.name) {
            return Err(SafeDetailValidationError::MissingField(field.name));
        }
    }
    Ok(())
}
