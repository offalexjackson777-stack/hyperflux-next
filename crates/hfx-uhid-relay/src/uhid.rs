// SPDX-License-Identifier: GPL-2.0-only

use std::fmt;

pub const FEATURE_REPORT_BYTES: usize = 90;
pub const HYPERFLUX_PHYS_PREFIX: &str = "hyperflux-next/native/v1/";
pub const TRANSPORT_ENVELOPE_MAGIC: [u8; 8] = *b"HFXNERR1";

const BUS_VIRTUAL: u16 = 0x06;
const HID_MAX_DESCRIPTOR_SIZE: usize = 4_096;
const UHID_DATA_MAX: usize = 4_096;
pub const UHID_EVENT_BYTES: usize = 4_376;
const UHID_DESTROY: u32 = 1;
const UHID_START: u32 = 2;
const UHID_STOP: u32 = 3;
const UHID_OPEN: u32 = 4;
const UHID_CLOSE: u32 = 5;
const UHID_OUTPUT: u32 = 6;
const UHID_GET_REPORT: u32 = 9;
const UHID_GET_REPORT_REPLY: u32 = 10;
const UHID_CREATE2: u32 = 11;
const UHID_SET_REPORT: u32 = 13;
const UHID_SET_REPORT_REPLY: u32 = 14;
const UHID_FEATURE_REPORT: u8 = 0;
const EIO: u16 = 5;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct VirtualDeviceSpec {
    pub name: String,
    pub phys: String,
    pub vendor_id: u16,
    pub product_id: u16,
    pub version: u32,
    pub country: u32,
    pub descriptor: Vec<u8>,
}

impl VirtualDeviceSpec {
    /// Creates one feature-only virtual transport identity.
    ///
    /// # Errors
    ///
    /// Rejects strings that do not fit the fixed UHID ABI, embedded NULs,
    /// invalid descriptors, and an empty instance label.
    pub fn new(
        name: String,
        vendor_id: u16,
        product_id: u16,
        descriptor: Vec<u8>,
        instance_label: &str,
    ) -> Result<Self, UhidCodecError> {
        if name.is_empty()
            || name.len() >= 128
            || name.as_bytes().contains(&0)
            || instance_label.is_empty()
            || instance_label.len() > 24
            || !instance_label
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
            || descriptor.is_empty()
            || descriptor.len() > HID_MAX_DESCRIPTOR_SIZE
            || !is_feature_only_descriptor(&descriptor)
        {
            return Err(UhidCodecError::InvalidDeviceSpec);
        }
        let phys = format!("{HYPERFLUX_PHYS_PREFIX}{instance_label}");
        if phys.len() >= 64 {
            return Err(UhidCodecError::InvalidDeviceSpec);
        }
        Ok(Self {
            name,
            phys,
            vendor_id,
            product_id,
            version: 0x0100,
            country: 0,
            descriptor,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum UhidEvent {
    Start {
        flags: u64,
    },
    Stop,
    Open,
    Close,
    GetReport {
        id: u32,
        report_number: u8,
        report_type: u8,
    },
    SetReport {
        id: u32,
        report_number: u8,
        report_type: u8,
        data: Vec<u8>,
    },
    Output,
    Unknown {
        event_type: u32,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum UhidReply {
    Create(VirtualDeviceSpec),
    Destroy,
    GetReport { id: u32, data: Option<Vec<u8>> },
    SetReport { id: u32, success: bool },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TransportEnvelopeKind {
    NotSent,
    MayHaveBeenSent,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum UhidCodecError {
    InvalidDeviceSpec,
    EventTooShort,
    InvalidEvent,
    InvalidReply,
}

impl fmt::Display for UhidCodecError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidDeviceSpec => "virtual HID device specification is invalid",
            Self::EventTooShort => "UHID event is shorter than its typed payload",
            Self::InvalidEvent => "UHID event payload is invalid",
            Self::InvalidReply => "UHID reply payload is invalid",
        })
    }
}

impl std::error::Error for UhidCodecError {}

impl UhidEvent {
    /// Decodes one kernel UHID event. The kernel may return a short event; only
    /// bytes required by the selected payload are mandatory.
    ///
    /// # Errors
    ///
    /// Rejects truncated typed payloads and impossible report lengths.
    pub fn decode(bytes: &[u8]) -> Result<Self, UhidCodecError> {
        let event_type = read_u32(bytes, 0).ok_or(UhidCodecError::EventTooShort)?;
        match event_type {
            UHID_START => Ok(Self::Start {
                flags: read_u64(bytes, 4).ok_or(UhidCodecError::EventTooShort)?,
            }),
            UHID_STOP => Ok(Self::Stop),
            UHID_OPEN => Ok(Self::Open),
            UHID_CLOSE => Ok(Self::Close),
            UHID_OUTPUT => Ok(Self::Output),
            UHID_GET_REPORT => {
                if bytes.len() < 10 {
                    return Err(UhidCodecError::EventTooShort);
                }
                Ok(Self::GetReport {
                    id: read_u32(bytes, 4).ok_or(UhidCodecError::EventTooShort)?,
                    report_number: bytes[8],
                    report_type: bytes[9],
                })
            }
            UHID_SET_REPORT => {
                if bytes.len() < 12 {
                    return Err(UhidCodecError::EventTooShort);
                }
                let size = usize::from(read_u16(bytes, 10).ok_or(UhidCodecError::EventTooShort)?);
                if size > UHID_DATA_MAX || bytes.len() < 12 + size {
                    return Err(UhidCodecError::InvalidEvent);
                }
                Ok(Self::SetReport {
                    id: read_u32(bytes, 4).ok_or(UhidCodecError::EventTooShort)?,
                    report_number: bytes[8],
                    report_type: bytes[9],
                    data: bytes[12..12 + size].to_vec(),
                })
            }
            _ => Ok(Self::Unknown { event_type }),
        }
    }
}

impl UhidReply {
    /// Encodes one userspace-to-kernel UHID reply with the complete stable ABI
    /// size. Reserved bytes remain zero.
    ///
    /// # Errors
    ///
    /// Rejects oversized response payloads or invalid device specifications.
    pub fn encode(&self) -> Result<Vec<u8>, UhidCodecError> {
        let mut bytes = vec![0_u8; UHID_EVENT_BYTES];
        match self {
            Self::Create(spec) => {
                validate_spec(spec)?;
                write_u32(&mut bytes, 0, UHID_CREATE2);
                write_c_string(&mut bytes[4..132], &spec.name)?;
                write_c_string(&mut bytes[132..196], &spec.phys)?;
                write_u16(
                    &mut bytes,
                    260,
                    u16::try_from(spec.descriptor.len())
                        .map_err(|_| UhidCodecError::InvalidReply)?,
                );
                write_u16(&mut bytes, 262, BUS_VIRTUAL);
                write_u32(&mut bytes, 264, u32::from(spec.vendor_id));
                write_u32(&mut bytes, 268, u32::from(spec.product_id));
                write_u32(&mut bytes, 272, spec.version);
                write_u32(&mut bytes, 276, spec.country);
                bytes[280..280 + spec.descriptor.len()].copy_from_slice(&spec.descriptor);
            }
            Self::Destroy => write_u32(&mut bytes, 0, UHID_DESTROY),
            Self::GetReport { id, data } => {
                write_u32(&mut bytes, 0, UHID_GET_REPORT_REPLY);
                write_u32(&mut bytes, 4, *id);
                match data {
                    Some(data) if data.len() <= UHID_DATA_MAX => {
                        write_u16(&mut bytes, 8, 0);
                        write_u16(
                            &mut bytes,
                            10,
                            u16::try_from(data.len()).map_err(|_| UhidCodecError::InvalidReply)?,
                        );
                        bytes[12..12 + data.len()].copy_from_slice(data);
                    }
                    Some(_) => return Err(UhidCodecError::InvalidReply),
                    None => write_u16(&mut bytes, 8, EIO),
                }
            }
            Self::SetReport { id, success } => {
                write_u32(&mut bytes, 0, UHID_SET_REPORT_REPLY);
                write_u32(&mut bytes, 4, *id);
                write_u16(&mut bytes, 8, if *success { 0 } else { EIO });
            }
        }
        Ok(bytes)
    }
}

#[must_use]
pub fn encode_transport_envelope(kind: TransportEnvelopeKind) -> [u8; FEATURE_REPORT_BYTES] {
    let mut report = [0_u8; FEATURE_REPORT_BYTES];
    report[..TRANSPORT_ENVELOPE_MAGIC.len()].copy_from_slice(&TRANSPORT_ENVELOPE_MAGIC);
    report[8] = 1;
    report[9] = match kind {
        TransportEnvelopeKind::NotSent => 1,
        TransportEnvelopeKind::MayHaveBeenSent => 2,
    };
    report[88] = 0x5a;
    report[89] = 0xa5;
    report
}

#[must_use]
pub fn decode_transport_envelope(report: &[u8]) -> Option<TransportEnvelopeKind> {
    if report.len() != FEATURE_REPORT_BYTES
        || report[..TRANSPORT_ENVELOPE_MAGIC.len()] != TRANSPORT_ENVELOPE_MAGIC
        || report[8] != 1
        || report[10..88].iter().any(|byte| *byte != 0)
        || report[88] != 0x5a
        || report[89] != 0xa5
    {
        return None;
    }
    match report[9] {
        1 => Some(TransportEnvelopeKind::NotSent),
        2 => Some(TransportEnvelopeKind::MayHaveBeenSent),
        _ => None,
    }
}

#[must_use]
pub const fn is_feature_request(report_number: u8, report_type: u8) -> bool {
    report_number == 0 && report_type == UHID_FEATURE_REPORT
}

fn validate_spec(spec: &VirtualDeviceSpec) -> Result<(), UhidCodecError> {
    if spec.name.is_empty()
        || spec.name.len() >= 128
        || spec.name.as_bytes().contains(&0)
        || !spec.phys.starts_with(HYPERFLUX_PHYS_PREFIX)
        || spec.phys.len() >= 64
        || spec.phys.as_bytes().contains(&0)
        || spec.descriptor.is_empty()
        || spec.descriptor.len() > HID_MAX_DESCRIPTOR_SIZE
        || !is_feature_only_descriptor(&spec.descriptor)
    {
        Err(UhidCodecError::InvalidDeviceSpec)
    } else {
        Ok(())
    }
}

fn is_feature_only_descriptor(descriptor: &[u8]) -> bool {
    let mut offset = 0;
    let mut collection_depth = 0_u16;
    let mut feature_items = 0_u16;

    while offset < descriptor.len() {
        let prefix = descriptor[offset];
        if prefix == 0xfe {
            return false;
        }
        let payload_bytes = match prefix & 0x03 {
            0 => 0,
            1 => 1,
            2 => 2,
            3 => 4,
            _ => unreachable!(),
        };
        let Some(next) = offset.checked_add(1 + payload_bytes) else {
            return false;
        };
        if next > descriptor.len() {
            return false;
        }

        let item_type = (prefix >> 2) & 0x03;
        let tag = prefix >> 4;
        if item_type == 0 {
            match tag {
                0x0a if payload_bytes > 0 => {
                    let Some(next_depth) = collection_depth.checked_add(1) else {
                        return false;
                    };
                    collection_depth = next_depth;
                }
                0x0b if payload_bytes > 0 && collection_depth > 0 => {
                    let Some(next_items) = feature_items.checked_add(1) else {
                        return false;
                    };
                    feature_items = next_items;
                }
                0x0c if payload_bytes == 0 && collection_depth > 0 => {
                    collection_depth -= 1;
                }
                _ => return false,
            }
        }
        offset = next;
    }

    collection_depth == 0 && feature_items > 0
}

fn write_c_string(target: &mut [u8], value: &str) -> Result<(), UhidCodecError> {
    if value.len() >= target.len() || value.as_bytes().contains(&0) {
        return Err(UhidCodecError::InvalidReply);
    }
    target[..value.len()].copy_from_slice(value.as_bytes());
    Ok(())
}

fn read_u16(bytes: &[u8], offset: usize) -> Option<u16> {
    let value: [u8; 2] = bytes.get(offset..offset + 2)?.try_into().ok()?;
    Some(u16::from_ne_bytes(value))
}

fn read_u32(bytes: &[u8], offset: usize) -> Option<u32> {
    let value: [u8; 4] = bytes.get(offset..offset + 4)?.try_into().ok()?;
    Some(u32::from_ne_bytes(value))
}

fn read_u64(bytes: &[u8], offset: usize) -> Option<u64> {
    let value: [u8; 8] = bytes.get(offset..offset + 8)?.try_into().ok()?;
    Some(u64::from_ne_bytes(value))
}

fn write_u16(bytes: &mut [u8], offset: usize, value: u16) {
    bytes[offset..offset + 2].copy_from_slice(&value.to_ne_bytes());
}

fn write_u32(bytes: &mut [u8], offset: usize, value: u32) {
    bytes[offset..offset + 4].copy_from_slice(&value.to_ne_bytes());
}

#[cfg(test)]
mod tests {
    use super::*;

    const FEATURE_ONLY_DESCRIPTOR: &[u8] = &[
        0x06, 0x00, 0xff, 0x09, 0x01, 0xa1, 0x01, 0x15, 0x00, 0x26, 0xff, 0x00, 0x75, 0x08, 0x95,
        0x5a, 0xb1, 0x02, 0xc0,
    ];

    #[test]
    fn feature_only_descriptor_cannot_create_an_input_node() {
        assert!(is_feature_only_descriptor(FEATURE_ONLY_DESCRIPTOR));
        for main_item in [0x81, 0x91] {
            let mut descriptor = FEATURE_ONLY_DESCRIPTOR.to_vec();
            descriptor.splice(18..18, [main_item, 0x02]);
            assert!(!is_feature_only_descriptor(&descriptor));
            assert_eq!(
                VirtualDeviceSpec::new(
                    "Razer test".to_string(),
                    0x1532,
                    1,
                    descriptor,
                    "test-route",
                ),
                Err(UhidCodecError::InvalidDeviceSpec)
            );
        }
    }

    #[test]
    fn create_encoding_keeps_the_trusted_virtual_feature_identity() {
        let spec = VirtualDeviceSpec::new(
            "Razer test".to_string(),
            0x1532,
            0x1234,
            FEATURE_ONLY_DESCRIPTOR.to_vec(),
            "test-route",
        )
        .expect("feature-only descriptor is valid");
        let encoded = UhidReply::Create(spec).encode().expect("create encodes");
        assert_eq!(read_u32(&encoded, 0), Some(UHID_CREATE2));
        assert_eq!(read_u16(&encoded, 262), Some(BUS_VIRTUAL));
        assert_eq!(read_u32(&encoded, 264), Some(0x1532));
        assert_eq!(read_u32(&encoded, 268), Some(0x1234));
    }
}
