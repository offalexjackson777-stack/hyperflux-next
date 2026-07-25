// SPDX-License-Identifier: GPL-2.0-only

use hfx_protocol::NativeRouteInventory;
use serde::ser::SerializeStruct as _;
use serde::{Serialize, Serializer};
use std::env;
use std::fmt;
use std::fs;
use std::io;
use std::os::unix::fs::{DirBuilderExt as _, PermissionsExt as _};
use std::path::{Path, PathBuf};

const OBSERVATION_SCHEMA_REF: &str =
    "https://hyperflux.dev/schemas/native-route-observation-v1.json";
const OBSERVATION_SCHEMA: &str = "hyperflux-native-route-observation-v1";
const OBSERVATION_SOURCE: &str = "bridge-native-route-v7";
const MAX_NATIVE_OBSERVATIONS: usize = 128;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeObservationError {
    EmptyInventory,
    OversizedInventory,
    UnsupportedDeviceKind,
    InvalidTimestamp,
    InvalidOutputDirectory,
    OutputExists,
    Serialization,
    Io,
}

impl fmt::Display for NativeObservationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::EmptyInventory => "the bridge returned no native receiver routes",
            Self::OversizedInventory => "the bridge returned too many native receiver routes",
            Self::UnsupportedDeviceKind => "a native route has an unsupported device kind",
            Self::InvalidTimestamp => "the native observation timestamp is invalid",
            Self::InvalidOutputDirectory => "the native observation directory is unsafe",
            Self::OutputExists => "a native observation destination already exists",
            Self::Serialization => "the native observation could not be serialized",
            Self::Io => "the native observation could not be written privately",
        })
    }
}

impl std::error::Error for NativeObservationError {}

impl From<io::Error> for NativeObservationError {
    fn from(_: io::Error) -> Self {
        Self::Io
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct NativeObservationPrivacy;

impl Serialize for NativeObservationPrivacy {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let mut state = serializer.serialize_struct("NativeObservationPrivacy", 4)?;
        state.serialize_field("serials_included", &false)?;
        state.serialize_field("route_tokens_included", &false)?;
        state.serialize_field("host_paths_included", &false)?;
        state.serialize_field("raw_reports_included", &false)?;
        state.end()
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct NativeRouteObservation {
    #[serde(rename = "$schema")]
    pub schema_ref: &'static str,
    pub schema: &'static str,
    pub observed_at: String,
    pub source: &'static str,
    pub receiver_generation: u64,
    pub vendor_id: u16,
    pub product_id: u16,
    pub device_kind: String,
    pub route_kind: &'static str,
    pub route_state: String,
    pub access: String,
    pub protocol_family: String,
    pub report_bytes: u16,
    pub privacy: NativeObservationPrivacy,
}

/// Removes route tokens and host-local identity while preserving onboarding facts.
///
/// # Errors
///
/// Returns a typed error for an empty, oversized, or non-mouse/keyboard inventory.
pub fn build_native_route_observations(
    inventory: &NativeRouteInventory,
    observed_at: &str,
) -> Result<Vec<NativeRouteObservation>, NativeObservationError> {
    if inventory.routes.is_empty() {
        return Err(NativeObservationError::EmptyInventory);
    }
    if inventory.routes.len() > MAX_NATIVE_OBSERVATIONS {
        return Err(NativeObservationError::OversizedInventory);
    }
    if !canonical_timestamp(observed_at) {
        return Err(NativeObservationError::InvalidTimestamp);
    }
    inventory
        .routes
        .iter()
        .map(|route| {
            if !matches!(route.device_kind.as_str(), "mouse" | "keyboard") {
                return Err(NativeObservationError::UnsupportedDeviceKind);
            }
            Ok(NativeRouteObservation {
                schema_ref: OBSERVATION_SCHEMA_REF,
                schema: OBSERVATION_SCHEMA,
                observed_at: observed_at.to_owned(),
                source: OBSERVATION_SOURCE,
                receiver_generation: route.generation_id.get(),
                vendor_id: route.vendor_id.get(),
                product_id: route.product_id.get(),
                device_kind: route.device_kind.as_str().to_owned(),
                route_kind: "hyperflux-wireless",
                route_state: route.route_state.as_str().to_owned(),
                access: route.access.as_str().to_owned(),
                protocol_family: route.protocol_family.as_str().to_owned(),
                report_bytes: route.report_bytes.get(),
                privacy: NativeObservationPrivacy,
            })
        })
        .collect()
}

/// Returns the private per-user cache location for onboarding observations.
///
/// # Errors
///
/// Refuses to guess a relative destination when no user cache root is known.
pub fn default_native_observation_directory() -> Result<PathBuf, NativeObservationError> {
    let cache = env::var_os("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".cache")))
        .filter(|path| path.is_absolute())
        .ok_or(NativeObservationError::InvalidOutputDirectory)?;
    Ok(cache.join("hyperflux-next/native-onboarding"))
}

/// Writes each sanitized route as a new mode-0600 artifact.
///
/// # Errors
///
/// Refuses symbolic output roots, existing destinations, and partial overwrite.
pub fn write_native_route_observations(
    directory: &Path,
    observations: &[NativeRouteObservation],
) -> Result<Vec<PathBuf>, NativeObservationError> {
    if observations.is_empty() {
        return Err(NativeObservationError::EmptyInventory);
    }
    if observations.len() > MAX_NATIVE_OBSERVATIONS {
        return Err(NativeObservationError::OversizedInventory);
    }
    if directory.as_os_str().is_empty()
        || (!directory.is_absolute() && directory.components().count() > 1)
        || fs::symlink_metadata(directory).is_ok_and(|metadata| metadata.file_type().is_symlink())
    {
        return Err(NativeObservationError::InvalidOutputDirectory);
    }
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true).mode(0o700);
    builder.create(directory)?;
    fs::set_permissions(directory, fs::Permissions::from_mode(0o700))?;
    let stamp: String = observations[0]
        .observed_at
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect();
    let mut destinations = Vec::with_capacity(observations.len());
    for (index, observation) in observations.iter().enumerate() {
        let filename = format!(
            "{stamp}-g{}-{}-{:04x}{:04x}-{index}.json",
            observation.receiver_generation,
            observation.device_kind,
            observation.vendor_id,
            observation.product_id,
        );
        let destination = directory.join(filename);
        if destination.exists() || destination.is_symlink() {
            return Err(NativeObservationError::OutputExists);
        }
        let temporary = directory.join(format!(".{}.{}.tmp", index, std::process::id()));
        if temporary.exists() || temporary.is_symlink() {
            return Err(NativeObservationError::OutputExists);
        }
        let mut payload = serde_json::to_vec_pretty(observation)
            .map_err(|_| NativeObservationError::Serialization)?;
        payload.push(b'\n');
        crate::probe::write_private_file(&temporary, &payload)?;
        if let Err(error) = fs::hard_link(&temporary, &destination) {
            let _ = fs::remove_file(&temporary);
            return Err(if error.kind() == io::ErrorKind::AlreadyExists {
                NativeObservationError::OutputExists
            } else {
                NativeObservationError::Io
            });
        }
        if fs::remove_file(&temporary).is_err() {
            let _ = fs::remove_file(&destination);
            return Err(NativeObservationError::Io);
        }
        destinations.push(destination);
    }
    fs::File::open(directory)?.sync_all()?;
    Ok(destinations)
}

fn canonical_timestamp(value: &str) -> bool {
    value.len() == 20
        && value.ends_with('Z')
        && value.bytes().enumerate().all(|(index, byte)| match index {
            4 | 7 => byte == b'-',
            10 => byte == b'T',
            13 | 16 => byte == b':',
            19 => byte == b'Z',
            _ => byte.is_ascii_digit(),
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use hfx_domain::{
        DeviceKind, EndpointId, GenerationId, LogicalDeviceId, NativeProtocolFamily,
        NativeReportByteCount, NativeRouteAccess, NativeRouteState, ProductId, ProjectionRevision,
        ReceiverId, SequenceNumber, StreamEpoch, StreamId, VendorId,
    };
    use hfx_protocol::{EventCursor, NativeRoute};
    use std::time::{SystemTime, UNIX_EPOCH};

    fn text<T>(value: &str) -> T
    where
        T: TryFrom<String>,
        T::Error: fmt::Debug,
    {
        T::try_from(value.to_owned()).expect("test value is valid")
    }

    fn inventory() -> NativeRouteInventory {
        NativeRouteInventory {
            cursor: EventCursor {
                stream_id: text::<StreamId>("native-routes"),
                stream_epoch: StreamEpoch::try_from(1_u64).expect("epoch is valid"),
                projection_revision: ProjectionRevision::try_from(1_u32)
                    .expect("projection is valid"),
                sequence: SequenceNumber::try_from(1_u64).expect("sequence is valid"),
            },
            routes: vec![NativeRoute {
                receiver_id: text::<ReceiverId>("receiver-private-id"),
                generation_id: GenerationId::try_from(7_u64).expect("generation is valid"),
                device_id: text::<LogicalDeviceId>("device-private-id"),
                endpoint_id: text::<EndpointId>("endpoint-private-id"),
                device_kind: DeviceKind::Mouse,
                vendor_id: VendorId::try_from(0x1532_u16).expect("vendor is valid"),
                product_id: ProductId::try_from(0x007b_u16).expect("product is valid"),
                route_state: NativeRouteState::Available,
                access: NativeRouteAccess::Candidate,
                protocol_family: NativeProtocolFamily::try_from(
                    "razer-feature-report-v1".to_owned(),
                )
                .expect("protocol is valid"),
                report_bytes: NativeReportByteCount::try_from(90_u16)
                    .expect("report size is valid"),
                route_token: None,
                resource: None,
            }],
        }
    }

    #[test]
    fn observation_strips_every_private_route_field() {
        let observations = build_native_route_observations(&inventory(), "2026-07-25T00:00:00Z")
            .expect("observation builds");
        let serialized = serde_json::to_string(&observations).expect("observation serializes");
        assert!(!serialized.contains("receiver-private-id"));
        assert!(!serialized.contains("device-private-id"));
        assert!(!serialized.contains("endpoint-private-id"));
        assert!(!serialized.contains("\"route_token\":"));
        assert_eq!(observations[0].product_id, 0x007b);
        assert!(serialized.contains("\"raw_reports_included\":false"));
    }

    #[test]
    fn writer_uses_private_mode_and_refuses_overwrite() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock is valid")
            .as_nanos();
        let directory = env::temp_dir().join(format!(
            "hyperflux-native-observation-{}-{unique}",
            std::process::id()
        ));
        let observations = build_native_route_observations(&inventory(), "2026-07-25T00:00:00Z")
            .expect("observation builds");
        let paths =
            write_native_route_observations(&directory, &observations).expect("observation writes");
        assert_eq!(
            fs::metadata(&paths[0])
                .expect("metadata exists")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        assert_eq!(
            write_native_route_observations(&directory, &observations),
            Err(NativeObservationError::OutputExists)
        );
        fs::remove_dir_all(directory).expect("fixture cleans up");
    }
}
