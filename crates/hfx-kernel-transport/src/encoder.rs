// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    HFX_UAPI_FRAME_KIND_USB_CLASS_SET_REPORT, HFX_UAPI_MAX_FRAME_BYTES, HFX_UAPI_MAX_FRAMES,
    HfxUapiFrame, KernelTransportError, KernelTransportErrorKind,
};
use hfx_core::TransportDispatch;
use hfx_domain::DeviceKind;
use hfx_profiles::{RuntimeLightingTopology, RuntimeProfileCatalog};
use hfx_protocol::{DeviceProfileBinding, LightingFrame, RgbColor};
use std::collections::BTreeSet;

const HW001_BACKEND_ID: u32 = 1;
const HW001_REPORT_BYTES: usize = 90;
const HW001_CHECKSUM_INDEX: usize = 88;
const HW001_COLOR_OFFSET: usize = 13;
const HW001_MAX_COLUMNS: usize = 25;
const HW001_MAX_ROWS: usize = 8;
const HW001_MOUSE_COMMAND: u8 = 0x2c;
const HW001_KEYBOARD_COMMAND: u8 = 0x38;
const HW001_PASS_COUNT: usize = 2;
const HW001_INTER_FRAME_DELAY_US: u32 = 2_500;
const HW001_BETWEEN_PASS_DELAY_US: u32 = 50_000;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EncodedTransaction {
    frames: Vec<HfxUapiFrame>,
    semantic_sources: Vec<usize>,
    semantic_frame_count: usize,
}

impl EncodedTransaction {
    #[must_use]
    pub fn frames(&self) -> &[HfxUapiFrame] {
        &self.frames
    }

    #[must_use]
    pub const fn semantic_frame_count(&self) -> usize {
        self.semantic_frame_count
    }

    #[must_use]
    pub fn semantic_frames_completed(&self, physical_frames_completed: usize) -> usize {
        let completed = physical_frames_completed.min(self.frames.len());
        (0..self.semantic_frame_count)
            .filter(|source| {
                let planned = self
                    .semantic_sources
                    .iter()
                    .filter(|candidate| *candidate == source)
                    .count();
                let delivered = self.semantic_sources[..completed]
                    .iter()
                    .filter(|candidate| *candidate == source)
                    .count();
                planned > 0 && delivered == planned
            })
            .count()
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct ReceiverFrameEncoderRegistry;

impl ReceiverFrameEncoderRegistry {
    /// Encodes one admitted semantic dispatch with its profile-selected backend.
    ///
    /// # Errors
    ///
    /// Returns a fail-closed error for unknown backends, stale profile
    /// bindings, unsupported geometry, or malformed semantic frames.
    pub fn encode(
        &self,
        catalog: &RuntimeProfileCatalog,
        receiver_protocol_family: &str,
        backend_id: u32,
        dispatch: &TransportDispatch,
    ) -> Result<EncodedTransaction, KernelTransportError> {
        match backend_id {
            HW001_BACKEND_ID => encode_hw001(catalog, receiver_protocol_family, dispatch),
            _ => Err(KernelTransportError::safe(
                KernelTransportErrorKind::UnsupportedBackend,
            )),
        }
    }
}

#[derive(Clone, Debug)]
struct PreparedDevice {
    kind: DeviceKind,
    semantic_index: usize,
    topology: RuntimeLightingTopology,
    carriers: Vec<[u8; 3]>,
}

fn encode_hw001(
    catalog: &RuntimeProfileCatalog,
    receiver_protocol_family: &str,
    dispatch: &TransportDispatch,
) -> Result<EncodedTransaction, KernelTransportError> {
    if dispatch.frames.is_empty() || dispatch.frames.len() != dispatch.device_profiles.len() {
        return Err(invalid_dispatch());
    }
    let mut bound_devices = BTreeSet::new();
    for binding in &dispatch.device_profiles {
        if !bound_devices.insert(binding.device_id.clone()) {
            return Err(invalid_dispatch());
        }
    }

    let mut prepared = Vec::with_capacity(dispatch.frames.len());
    let mut kinds = BTreeSet::new();
    for (semantic_index, frame) in dispatch.frames.iter().enumerate() {
        let expected_index = u32::try_from(semantic_index).map_err(|_| invalid_dispatch())?;
        if frame.frame_index.get() != expected_index {
            return Err(invalid_dispatch());
        }
        let binding = binding_for_frame(&dispatch.device_profiles, frame)?;
        let profile = catalog
            .profile(&binding.profile_id)
            .ok_or_else(profile_mismatch)?;
        if profile.runtime_digest != binding.profile_digest
            || !profile
                .receiver_protocols
                .contains(&receiver_protocol_family)
            || profile.lighting.is_none()
            || binding.application_slot_count.get()
                != u16::try_from(frame.colors.len()).map_err(|_| invalid_dispatch())?
        {
            return Err(profile_mismatch());
        }
        if !kinds.insert(profile.device_kind) {
            return Err(invalid_dispatch());
        }
        let topology = profile.lighting.clone().ok_or_else(profile_mismatch)?;
        validate_hw001_topology(profile.device_kind, &topology)?;
        prepared.push(PreparedDevice {
            kind: profile.device_kind,
            semantic_index,
            carriers: translate_colors(&topology, &frame.colors)?,
            topology,
        });
    }
    if bound_devices.len() != prepared.len() {
        return Err(invalid_dispatch());
    }

    prepared.sort_by_key(|device| match device.kind {
        DeviceKind::Mouse => (0_u8, device.semantic_index),
        DeviceKind::Keyboard => (1_u8, device.semantic_index),
        _ => (2_u8, device.semantic_index),
    });
    build_hw001_transaction(&prepared, dispatch.frames.len())
}

fn binding_for_frame<'a>(
    bindings: &'a [DeviceProfileBinding],
    frame: &LightingFrame,
) -> Result<&'a DeviceProfileBinding, KernelTransportError> {
    bindings
        .iter()
        .find(|binding| binding.device_id == frame.device_id)
        .ok_or_else(invalid_dispatch)
}

fn validate_hw001_topology(
    kind: DeviceKind,
    topology: &RuntimeLightingTopology,
) -> Result<(), KernelTransportError> {
    let carriers = usize::from(topology.carrier_count.get());
    let rows = usize::from(topology.rows);
    let columns = usize::from(topology.columns);
    let valid = match kind {
        DeviceKind::Mouse => {
            rows == 1 && columns == carriers && (1..=HW001_MAX_COLUMNS).contains(&columns)
        }
        DeviceKind::Keyboard => {
            (1..=HW001_MAX_ROWS).contains(&rows)
                && (1..=HW001_MAX_COLUMNS).contains(&columns)
                && rows.checked_mul(columns) == Some(carriers)
        }
        DeviceKind::Receiver | DeviceKind::Mat | DeviceKind::Unknown => false,
    };
    if valid {
        Ok(())
    } else {
        Err(KernelTransportError::safe(
            KernelTransportErrorKind::Encoding,
        ))
    }
}

fn translate_colors(
    topology: &RuntimeLightingTopology,
    colors: &[RgbColor],
) -> Result<Vec<[u8; 3]>, KernelTransportError> {
    if colors.len() != usize::from(topology.application_slot_count.get())
        || colors.len() != topology.application_index_to_carrier.len()
    {
        return Err(profile_mismatch());
    }
    let mut carriers = vec![[0_u8; 3]; usize::from(topology.carrier_count.get())];
    for (color, carrier) in colors.iter().zip(&topology.application_index_to_carrier) {
        let destination = carriers
            .get_mut(usize::from(carrier.get()))
            .ok_or_else(profile_mismatch)?;
        *destination = [color.red.get(), color.green.get(), color.blue.get()];
    }
    Ok(carriers)
}

fn build_hw001_transaction(
    devices: &[PreparedDevice],
    semantic_frame_count: usize,
) -> Result<EncodedTransaction, KernelTransportError> {
    let frames_per_pass = devices.iter().try_fold(0_usize, |count, device| {
        count.checked_add(match device.kind {
            DeviceKind::Mouse => 1,
            DeviceKind::Keyboard => usize::from(device.topology.rows),
            _ => return None,
        })
    });
    let frames_per_pass = frames_per_pass.ok_or_else(encoding_error)?;
    let frame_count = frames_per_pass
        .checked_mul(HW001_PASS_COUNT)
        .ok_or_else(encoding_error)?;
    if frame_count == 0 || frame_count > HFX_UAPI_MAX_FRAMES {
        return Err(encoding_error());
    }

    let mut frames = Vec::with_capacity(frame_count);
    let mut semantic_sources = Vec::with_capacity(frame_count);
    for pass in 0..HW001_PASS_COUNT {
        let pass_start = frames.len();
        for device in devices {
            match device.kind {
                DeviceKind::Mouse => {
                    frames.push(mouse_frame(&device.carriers)?);
                    semantic_sources.push(device.semantic_index);
                }
                DeviceKind::Keyboard => {
                    for selector in 0..device.topology.rows {
                        frames.push(keyboard_frame(
                            selector,
                            device.topology.columns,
                            &device.carriers,
                        )?);
                        semantic_sources.push(device.semantic_index);
                    }
                }
                _ => return Err(encoding_error()),
            }
        }
        let pass_end = frames.len();
        for frame in &mut frames[pass_start..pass_end.saturating_sub(1)] {
            frame.delay_after_us = HW001_INTER_FRAME_DELAY_US;
        }
        if pass + 1 < HW001_PASS_COUNT {
            let last = frames.last_mut().ok_or_else(encoding_error)?;
            last.delay_after_us = HW001_BETWEEN_PASS_DELAY_US;
        }
    }
    Ok(EncodedTransaction {
        frames,
        semantic_sources,
        semantic_frame_count,
    })
}

fn mouse_frame(carriers: &[[u8; 3]]) -> Result<HfxUapiFrame, KernelTransportError> {
    let mut payload = [0_u8; HFX_UAPI_MAX_FRAME_BYTES];
    payload[5] = HW001_MOUSE_COMMAND;
    payload[6] = 0x0f;
    payload[7] = 0x03;
    write_colors(&mut payload, carriers)?;
    frame(payload)
}

fn keyboard_frame(
    selector: u16,
    columns: u16,
    carriers: &[[u8; 3]],
) -> Result<HfxUapiFrame, KernelTransportError> {
    let selector_index = usize::from(selector);
    let column_count = usize::from(columns);
    let start = selector_index
        .checked_mul(column_count)
        .ok_or_else(encoding_error)?;
    let end = start.checked_add(column_count).ok_or_else(encoding_error)?;
    let row = carriers.get(start..end).ok_or_else(encoding_error)?;
    let selector = u8::try_from(selector).map_err(|_| encoding_error())?;
    let mut payload = [0_u8; HFX_UAPI_MAX_FRAME_BYTES];
    payload[1] = 0x80_u8.checked_add(selector).ok_or_else(encoding_error)?;
    payload[5] = HW001_KEYBOARD_COMMAND;
    payload[6] = 0x0f;
    payload[7] = 0x03;
    payload[10] = selector;
    write_colors(&mut payload, row)?;
    frame(payload)
}

fn write_colors(
    payload: &mut [u8; HFX_UAPI_MAX_FRAME_BYTES],
    colors: &[[u8; 3]],
) -> Result<(), KernelTransportError> {
    let count_minus_one = colors.len().checked_sub(1).ok_or_else(encoding_error)?;
    payload[12] = u8::try_from(count_minus_one).map_err(|_| encoding_error())?;
    for (index, color) in colors.iter().enumerate() {
        let offset = HW001_COLOR_OFFSET
            .checked_add(index.checked_mul(3).ok_or_else(encoding_error)?)
            .ok_or_else(encoding_error)?;
        let destination = payload
            .get_mut(offset..offset + 3)
            .ok_or_else(encoding_error)?;
        destination.copy_from_slice(color);
    }
    payload[HW001_CHECKSUM_INDEX] = payload[2..HW001_CHECKSUM_INDEX]
        .iter()
        .fold(0_u8, |checksum, value| checksum ^ value);
    Ok(())
}

fn frame(payload: [u8; HFX_UAPI_MAX_FRAME_BYTES]) -> Result<HfxUapiFrame, KernelTransportError> {
    Ok(HfxUapiFrame {
        backend_id: HW001_BACKEND_ID,
        kind: u16::try_from(HFX_UAPI_FRAME_KIND_USB_CLASS_SET_REPORT)
            .map_err(|_| encoding_error())?,
        payload_length: u16::try_from(HW001_REPORT_BYTES).map_err(|_| encoding_error())?,
        delay_after_us: 0,
        flags: 0,
        payload,
    })
}

fn invalid_dispatch() -> KernelTransportError {
    KernelTransportError::safe(KernelTransportErrorKind::InvalidDispatch)
}

fn profile_mismatch() -> KernelTransportError {
    KernelTransportError::safe(KernelTransportErrorKind::ProfileMismatch)
}

fn encoding_error() -> KernelTransportError {
    KernelTransportError::safe(KernelTransportErrorKind::Encoding)
}
