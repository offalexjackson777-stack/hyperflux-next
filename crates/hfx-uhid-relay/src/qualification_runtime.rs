// SPDX-License-Identifier: GPL-2.0-only

use crate::qualification::{QualificationBridge, QualificationExchangeRecord};
use crate::runtime::RunningEndpoint;
use crate::{NativeRouteCatalog, NativeRouteRecord, RelayPaths, VirtualDeviceSpec};
use hfx_daemon::{
    EndpointCandidate, EndpointDiscovery, derive_capability_digest, generate_daemon_nonce,
};
use hfx_domain::{DeviceKind, GenerationId, ProfileId, ReceiverId};
use hfx_kernel_transport::{
    HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE, HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
    HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID, HFX_UAPI_OBSERVATION_KIND_IDENTITY_CONFLICT,
    KernelObservationReader, KernelReceiverTransport, KernelSessionMaterial, LinuxKernelIo,
    NativeEndpointLane, RawKernelObservation,
};
use hfx_profiles::RuntimeProfileCatalog;
use hfx_runtime::KERNEL_SESSION_DURATION_MS;
use rustix::fs::{Mode, OFlags, open};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::fmt;
use std::fs::{File, remove_file};
use std::io::{BufWriter, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

pub const NATIVE_QUALIFICATION_APPROVAL: &str =
    "approve HyperFlux Next restoration-isolated bounded read-only native parity probes";

const SOURCE_REVISION_BYTES: usize = 40;
const SHA256_HEX_BYTES: usize = 64;
const MAXIMUM_RUNTIME: Duration = Duration::from_mins(2);
const IDENTITY_WAIT: Duration = Duration::from_secs(2);
const LOOP_INTERVAL: Duration = Duration::from_millis(2);
const MAXIMUM_IDENTITY_BATCHES: usize = 64;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QualificationRunPaths {
    pub device_directory: PathBuf,
    pub sysfs_misc_directory: PathBuf,
    pub uhid_device: PathBuf,
    pub raw_directory: PathBuf,
    pub raw_output: PathBuf,
}

impl QualificationRunPaths {
    #[must_use]
    pub fn linux(raw_output: PathBuf) -> Self {
        Self {
            device_directory: PathBuf::from("/dev"),
            sysfs_misc_directory: PathBuf::from("/sys/class/misc"),
            uhid_device: PathBuf::from("/dev/uhid"),
            raw_directory: PathBuf::from("/dev/shm"),
            raw_output,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QualificationRunConfig {
    pub profile_id: String,
    pub expected_generation: u64,
    pub source_revision: String,
    pub package_sha256: String,
    pub approval_text: String,
    pub timeout: Duration,
    pub paths: QualificationRunPaths,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum QualificationRuntimeError {
    Approval,
    Binding,
    Catalog,
    Endpoint,
    Generation,
    Identity,
    Profile,
    RawOutput,
    Runtime,
    WriterBusy,
    Incomplete,
}

impl fmt::Display for QualificationRuntimeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Approval => "the exact read-only qualification approval is missing",
            Self::Binding => "qualification source or package binding is invalid",
            Self::Catalog => "the source-bound native qualification catalog is invalid",
            Self::Endpoint => "the exact receiver endpoint is unavailable",
            Self::Generation => "the receiver generation differs from the approved generation",
            Self::Identity => "the candidate child identity is absent or contradictory",
            Self::Profile => "the requested candidate profile is invalid",
            Self::RawOutput => "the private ephemeral qualification output is invalid",
            Self::Runtime => "the bounded native qualification runtime failed",
            Self::WriterBusy => "another receiver writer is active",
            Self::Incomplete => "the bounded read-only qualification probe set is incomplete",
        })
    }
}

impl std::error::Error for QualificationRuntimeError {}

#[derive(Serialize)]
struct RawQualificationDocument {
    schema: &'static str,
    source_revision: String,
    package_sha256: String,
    profile_id: String,
    receiver_generation: u64,
    read_only: bool,
    completed: bool,
    records: Vec<RawQualificationRecord>,
}

#[derive(Serialize)]
struct RawQualificationRecord {
    sequence: u64,
    method: Option<String>,
    attempt: u8,
    request_hex: String,
    certainty: crate::qualification::QualificationExchangeCertainty,
    transport_errno: i32,
    response_hex: Option<String>,
}

struct PrivateRawOutput {
    path: PathBuf,
    file: File,
    preserved: bool,
}

struct PreparedQualification {
    native_catalog: NativeRouteCatalog,
    route: NativeRouteRecord,
    transport: KernelReceiverTransport<LinuxKernelIo>,
    endpoint_lane: NativeEndpointLane,
}

impl PrivateRawOutput {
    fn create(paths: &QualificationRunPaths) -> Result<Self, QualificationRuntimeError> {
        validate_raw_output_path(paths)?;
        let descriptor = open(
            &paths.raw_output,
            OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::CLOEXEC | OFlags::NOFOLLOW,
            Mode::RUSR | Mode::WUSR,
        )
        .map_err(|_| QualificationRuntimeError::RawOutput)?;
        Ok(Self {
            path: paths.raw_output.clone(),
            file: File::from(descriptor),
            preserved: false,
        })
    }

    fn file_mut(&mut self) -> &mut File {
        &mut self.file
    }

    fn preserve(&mut self) {
        self.preserved = true;
    }
}

impl Drop for PrivateRawOutput {
    fn drop(&mut self) {
        if !self.preserved {
            let _ = remove_file(&self.path);
        }
    }
}

impl From<QualificationExchangeRecord> for RawQualificationRecord {
    fn from(value: QualificationExchangeRecord) -> Self {
        Self {
            sequence: value.sequence,
            method: value.method,
            attempt: value.attempt,
            request_hex: hex(&value.request),
            certainty: value.certainty,
            transport_errno: value.transport_errno,
            response_hex: value.response.map(|response| hex(&response)),
        }
    }
}

/// Runs one generation-bound, read-only candidate qualification endpoint.
///
/// # Errors
///
/// Fails closed before creating a UHID endpoint when approval, source binding,
/// receiver generation, writer exclusivity, or candidate identity is invalid.
pub fn run_qualification(
    config: &QualificationRunConfig,
    termination: &AtomicBool,
) -> Result<(), QualificationRuntimeError> {
    validate_config(config)?;
    let prepared = prepare_qualification(config)?;
    let mut raw_output = PrivateRawOutput::create(&config.paths)?;
    let (completed, records) = execute_qualification(config, termination, prepared)?;
    write_raw_document(raw_output.file_mut(), config, completed, records)?;
    raw_output.preserve();
    if completed {
        println!(
            "HFX_NATIVE_QUALIFIER_COMPLETE profile={}",
            config.profile_id
        );
        Ok(())
    } else {
        Err(QualificationRuntimeError::Incomplete)
    }
}

fn prepare_qualification(
    config: &QualificationRunConfig,
) -> Result<PreparedQualification, QualificationRuntimeError> {
    let native_catalog =
        NativeRouteCatalog::load().map_err(|_| QualificationRuntimeError::Catalog)?;
    let route = native_catalog
        .profile(&config.profile_id)
        .ok_or(QualificationRuntimeError::Profile)?
        .clone();
    let runtime_catalog =
        RuntimeProfileCatalog::load().map_err(|_| QualificationRuntimeError::Catalog)?;
    let child_profile_id = ProfileId::try_from(route.profile_id.as_str())
        .map_err(|_| QualificationRuntimeError::Profile)?;
    let child = runtime_catalog
        .child(route.wireless_product_id)
        .filter(|profile| profile.profile_id == child_profile_id)
        .ok_or(QualificationRuntimeError::Profile)?;
    if child.device_kind != route.device_kind {
        return Err(QualificationRuntimeError::Profile);
    }
    let (candidate, reader) = find_receiver_endpoint(config, &runtime_catalog)?;
    let endpoint_lane = endpoint_lane(route.device_kind)?;
    let transport = open_qualification_transport(
        config,
        &candidate,
        &reader,
        runtime_catalog,
        endpoint_lane,
        route.wireless_product_id.get(),
    )?;
    Ok(PreparedQualification {
        native_catalog,
        route,
        transport,
        endpoint_lane,
    })
}

fn find_receiver_endpoint(
    config: &QualificationRunConfig,
    runtime_catalog: &RuntimeProfileCatalog,
) -> Result<(EndpointCandidate, KernelObservationReader<LinuxKernelIo>), QualificationRuntimeError>
{
    let discovery = EndpointDiscovery::new(
        config.paths.device_directory.clone(),
        config.paths.sysfs_misc_directory.clone(),
    );
    let mut matching = discovery
        .scan()
        .map_err(|_| QualificationRuntimeError::Endpoint)?
        .into_iter()
        .filter_map(|candidate| {
            let reader = KernelObservationReader::open(&candidate.device_path).ok()?;
            let binding = reader.binding();
            (binding.generation_id.get() == config.expected_generation
                && runtime_catalog
                    .receiver(binding.vendor_id, binding.product_id)
                    .is_some())
            .then_some((candidate, reader))
        })
        .collect::<Vec<_>>();
    if matching.len() != 1 {
        return Err(QualificationRuntimeError::Endpoint);
    }
    matching.pop().ok_or(QualificationRuntimeError::Endpoint)
}

fn open_qualification_transport(
    config: &QualificationRunConfig,
    candidate: &EndpointCandidate,
    reader: &KernelObservationReader<LinuxKernelIo>,
    runtime_catalog: RuntimeProfileCatalog,
    endpoint_lane: NativeEndpointLane,
    expected_product_id: u16,
) -> Result<KernelReceiverTransport<LinuxKernelIo>, QualificationRuntimeError> {
    let binding = reader.binding();
    if binding.generation_id.get() != config.expected_generation {
        return Err(QualificationRuntimeError::Generation);
    }
    if binding.flags.writer_open() || binding.flags.session_active() {
        return Err(QualificationRuntimeError::WriterBusy);
    }
    if binding.flags.disconnecting() || binding.flags.suspended() || binding.bound_interfaces == 0 {
        return Err(QualificationRuntimeError::Endpoint);
    }
    let receiver = runtime_catalog
        .receiver(binding.vendor_id, binding.product_id)
        .ok_or(QualificationRuntimeError::Profile)?;
    let baseline = reader
        .read_observations(0)
        .map_err(|_| QualificationRuntimeError::Endpoint)?
        .latest_sequence;
    let generation = GenerationId::try_from(config.expected_generation)
        .map_err(|_| QualificationRuntimeError::Generation)?;
    let receiver_id = ReceiverId::try_from(format!("qualification-receiver-g{}", generation.get()))
        .map_err(|_| QualificationRuntimeError::Binding)?;
    let material = KernelSessionMaterial {
        receiver_id,
        generation_id: generation,
        receiver_profile_id: receiver.profile_id.clone(),
        receiver_profile_digest: receiver.runtime_digest.clone(),
        capability_digest: derive_capability_digest(&runtime_catalog, receiver)
            .map_err(|_| QualificationRuntimeError::Binding)?,
        daemon_nonce: generate_daemon_nonce().map_err(|_| QualificationRuntimeError::Binding)?,
        session_duration: Duration::from_millis(KERNEL_SESSION_DURATION_MS),
    };
    let transport =
        KernelReceiverTransport::open(&candidate.device_path, runtime_catalog, material)
            .map_err(|_| QualificationRuntimeError::WriterBusy)?;
    await_candidate_identity(&transport, baseline, endpoint_lane, expected_product_id)?;
    Ok(transport)
}

fn execute_qualification(
    config: &QualificationRunConfig,
    termination: &AtomicBool,
    prepared: PreparedQualification,
) -> Result<(bool, Vec<QualificationExchangeRecord>), QualificationRuntimeError> {
    let PreparedQualification {
        native_catalog,
        route,
        transport,
        endpoint_lane,
    } = prepared;
    let route_digest = qualification_route_digest(config, endpoint_lane);
    let bridge = QualificationBridge::new(
        transport,
        endpoint_lane,
        route_digest,
        route.qualification_probes.clone(),
        native_catalog.qualification_maximum_attempts(),
    )
    .ok_or(QualificationRuntimeError::Catalog)?;
    let device = VirtualDeviceSpec::new(
        route.model_name,
        route.vendor_id.get(),
        route.wireless_product_id.get(),
        native_catalog.descriptor().to_vec(),
        &qualification_instance_label(config),
    )
    .map_err(|_| QualificationRuntimeError::Profile)?;
    let relay_paths = RelayPaths {
        bridge_socket: PathBuf::new(),
        uhid_device: config.paths.uhid_device.clone(),
        hid_driver_root: PathBuf::new(),
    };
    let mut endpoint = RunningEndpoint::create(&relay_paths, bridge, device)
        .map_err(|_| QualificationRuntimeError::Runtime)?;
    println!(
        "HFX_NATIVE_QUALIFIER_READY profile={} methods={}",
        config.profile_id,
        route.qualification_probes.len()
    );
    std::io::stdout()
        .flush()
        .map_err(|_| QualificationRuntimeError::Runtime)?;

    let deadline = Instant::now() + config.timeout;
    let mut completed = false;
    while !termination.load(Ordering::Relaxed) && Instant::now() < deadline {
        endpoint
            .step()
            .map_err(|_| QualificationRuntimeError::Runtime)?;
        if endpoint.bridge_mut().all_methods_observed() {
            completed = true;
            break;
        }
        thread::sleep(LOOP_INTERVAL);
    }
    let records = endpoint.bridge_mut().records().to_vec();
    endpoint.destroy();
    Ok((completed, records))
}

fn validate_config(config: &QualificationRunConfig) -> Result<(), QualificationRuntimeError> {
    if config.approval_text != NATIVE_QUALIFICATION_APPROVAL {
        return Err(QualificationRuntimeError::Approval);
    }
    if config.expected_generation == 0
        || config.timeout.is_zero()
        || config.timeout > MAXIMUM_RUNTIME
        || !is_lower_hex(&config.source_revision, SOURCE_REVISION_BYTES)
        || !is_lower_hex(&config.package_sha256, SHA256_HEX_BYTES)
    {
        return Err(QualificationRuntimeError::Binding);
    }
    let embedded = option_env!("HFX_SOURCE_REVISION").ok_or(QualificationRuntimeError::Binding)?;
    if embedded != config.source_revision {
        return Err(QualificationRuntimeError::Binding);
    }
    validate_raw_output_path(&config.paths)
}

fn validate_raw_output_path(
    paths: &QualificationRunPaths,
) -> Result<(), QualificationRuntimeError> {
    let raw_directory = paths
        .raw_directory
        .canonicalize()
        .map_err(|_| QualificationRuntimeError::RawOutput)?;
    let parent = paths
        .raw_output
        .parent()
        .ok_or(QualificationRuntimeError::RawOutput)?
        .canonicalize()
        .map_err(|_| QualificationRuntimeError::RawOutput)?;
    let file_name = paths
        .raw_output
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or(QualificationRuntimeError::RawOutput)?;
    let has_json_extension = paths
        .raw_output
        .extension()
        .and_then(|value| value.to_str())
        .is_some_and(|extension| extension.eq_ignore_ascii_case("json"));
    if parent != raw_directory
        || !file_name.starts_with("hyperflux-next-native-parity-")
        || !has_json_extension
        || !file_name.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'-' | b'.')
        })
    {
        return Err(QualificationRuntimeError::RawOutput);
    }
    Ok(())
}

fn endpoint_lane(device_kind: DeviceKind) -> Result<NativeEndpointLane, QualificationRuntimeError> {
    match device_kind {
        DeviceKind::Mouse => Ok(NativeEndpointLane::Pointer),
        DeviceKind::Keyboard => Ok(NativeEndpointLane::Keyboard),
        _ => Err(QualificationRuntimeError::Profile),
    }
}

fn lane_number(endpoint: NativeEndpointLane) -> u32 {
    match endpoint {
        NativeEndpointLane::Pointer => HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
        NativeEndpointLane::Keyboard => HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE,
    }
}

fn await_candidate_identity(
    transport: &KernelReceiverTransport<LinuxKernelIo>,
    mut cursor: u64,
    endpoint: NativeEndpointLane,
    expected_product_id: u16,
) -> Result<(), QualificationRuntimeError> {
    let deadline = Instant::now() + IDENTITY_WAIT;
    for _ in 0..MAXIMUM_IDENTITY_BATCHES {
        let batch = transport
            .read_observations(cursor)
            .map_err(|_| QualificationRuntimeError::Identity)?;
        if batch.cursor_gap {
            return Err(QualificationRuntimeError::Identity);
        }
        if identity_is_exact(&batch.observations, endpoint, expected_product_id)? {
            return Ok(());
        }
        cursor = batch.latest_sequence.max(cursor);
        if Instant::now() >= deadline {
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }
    Err(QualificationRuntimeError::Identity)
}

fn identity_is_exact(
    observations: &[RawKernelObservation],
    endpoint: NativeEndpointLane,
    expected_product_id: u16,
) -> Result<bool, QualificationRuntimeError> {
    let lane = lane_number(endpoint);
    let mut found = false;
    for observation in observations
        .iter()
        .filter(|item| item.endpoint_slot == lane)
    {
        if observation.kind == HFX_UAPI_OBSERVATION_KIND_IDENTITY_CONFLICT {
            return Err(QualificationRuntimeError::Identity);
        }
        if observation.kind == HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID {
            if observation.value != u32::from(expected_product_id) {
                return Err(QualificationRuntimeError::Identity);
            }
            found = true;
        }
    }
    Ok(found)
}

fn qualification_route_digest(
    config: &QualificationRunConfig,
    endpoint: NativeEndpointLane,
) -> [u8; 32] {
    let mut digest = Sha256::new();
    digest.update(b"hyperflux-native-qualification-route-v1\0");
    digest.update(config.source_revision.as_bytes());
    digest.update(config.package_sha256.as_bytes());
    digest.update(config.expected_generation.to_le_bytes());
    digest.update(lane_number(endpoint).to_le_bytes());
    digest.update(config.profile_id.as_bytes());
    digest.finalize().into()
}

fn qualification_instance_label(config: &QualificationRunConfig) -> String {
    let mut digest = Sha256::new();
    digest.update(b"hyperflux-native-qualification-instance-v1\0");
    digest.update(config.source_revision.as_bytes());
    digest.update(config.expected_generation.to_le_bytes());
    digest.update(config.profile_id.as_bytes());
    let digest = digest.finalize();
    format!("qualify-{}", hex(&digest[..8]))
}

fn write_raw_document(
    file: &mut File,
    config: &QualificationRunConfig,
    completed: bool,
    records: Vec<QualificationExchangeRecord>,
) -> Result<(), QualificationRuntimeError> {
    let document = RawQualificationDocument {
        schema: "hyperflux-native-qualification-raw-v1",
        source_revision: config.source_revision.clone(),
        package_sha256: config.package_sha256.clone(),
        profile_id: config.profile_id.clone(),
        receiver_generation: config.expected_generation,
        read_only: true,
        completed,
        records: records.into_iter().map(Into::into).collect(),
    };
    let mut writer = BufWriter::new(file);
    serde_json::to_writer(&mut writer, &document)
        .map_err(|_| QualificationRuntimeError::RawOutput)?;
    writer
        .write_all(b"\n")
        .map_err(|_| QualificationRuntimeError::RawOutput)?;
    writer
        .flush()
        .map_err(|_| QualificationRuntimeError::RawOutput)?;
    writer
        .get_ref()
        .sync_all()
        .map_err(|_| QualificationRuntimeError::RawOutput)
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn hex(bytes: &[u8]) -> String {
    let mut value = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write as _;
        let _ = write!(value, "{byte:02x}");
    }
    value
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_ID: AtomicU64 = AtomicU64::new(1);

    struct TempDirectory(PathBuf);

    impl TempDirectory {
        fn create() -> Self {
            let id = TEST_ID.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir()
                .join(format!("hyperflux-native-raw-{}-{id}", std::process::id()));
            fs::create_dir(&path).expect("temporary directory is created");
            Self(path)
        }

        fn paths(&self, name: &str) -> QualificationRunPaths {
            QualificationRunPaths {
                device_directory: self.0.clone(),
                sysfs_misc_directory: self.0.clone(),
                uhid_device: self.0.join("uhid"),
                raw_directory: self.0.clone(),
                raw_output: self.0.join(name),
            }
        }
    }

    impl Drop for TempDirectory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn observation(kind: u32, lane: u32, value: u32) -> RawKernelObservation {
        RawKernelObservation {
            sequence: 1,
            observed_boottime_ns: 1,
            kind,
            endpoint_slot: lane,
            source: 1,
            confidence: 3,
            value,
            auxiliary: 0,
        }
    }

    #[test]
    fn exact_identity_accepts_only_the_target_lane_and_pid() {
        assert_eq!(
            identity_is_exact(
                &[observation(
                    HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID,
                    HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
                    0x00cd,
                )],
                NativeEndpointLane::Pointer,
                0x00cd,
            ),
            Ok(true)
        );
        assert_eq!(
            identity_is_exact(
                &[observation(
                    HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID,
                    HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
                    0x0296,
                )],
                NativeEndpointLane::Pointer,
                0x00cd,
            ),
            Err(QualificationRuntimeError::Identity)
        );
        assert_eq!(
            identity_is_exact(
                &[observation(
                    HFX_UAPI_OBSERVATION_KIND_IDENTITY_CONFLICT,
                    HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE,
                    0,
                )],
                NativeEndpointLane::Pointer,
                0x00cd,
            ),
            Err(QualificationRuntimeError::Identity)
        );
    }

    #[test]
    fn route_digest_is_bound_to_generation_profile_and_lane() {
        let paths = QualificationRunPaths::linux(PathBuf::from(
            "/dev/shm/hyperflux-next-native-parity-test.json",
        ));
        let config = QualificationRunConfig {
            profile_id: "child.razer.mouse.0001".to_owned(),
            expected_generation: 7,
            source_revision: "a".repeat(40),
            package_sha256: "b".repeat(64),
            approval_text: NATIVE_QUALIFICATION_APPROVAL.to_owned(),
            timeout: Duration::from_secs(1),
            paths,
        };
        let pointer = qualification_route_digest(&config, NativeEndpointLane::Pointer);
        let keyboard = qualification_route_digest(&config, NativeEndpointLane::Keyboard);
        let mut newer = config.clone();
        newer.expected_generation += 1;
        assert_ne!(pointer, keyboard);
        assert_ne!(
            pointer,
            qualification_route_digest(&newer, NativeEndpointLane::Pointer)
        );
        assert!(pointer.iter().any(|byte| *byte != 0));
    }

    #[test]
    fn private_raw_output_is_deleted_on_failure_and_kept_only_when_preserved() {
        let directory = TempDirectory::create();
        let failure_paths = directory.paths("hyperflux-next-native-parity-failure.json");
        {
            let _output = PrivateRawOutput::create(&failure_paths).expect("output is reserved");
            assert!(failure_paths.raw_output.is_file());
        }
        assert!(!failure_paths.raw_output.exists());

        let success_paths = directory.paths("hyperflux-next-native-parity-success.json");
        {
            let mut output = PrivateRawOutput::create(&success_paths).expect("output is reserved");
            output.preserve();
        }
        assert!(success_paths.raw_output.is_file());
        fs::remove_file(success_paths.raw_output).expect("preserved fixture is removed");
    }
}
