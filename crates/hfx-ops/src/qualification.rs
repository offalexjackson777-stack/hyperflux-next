// SPDX-License-Identifier: GPL-2.0-only

//! The installed, read-only `hyperfluxctl qualification` report.
//!
//! The report reuses the console's `hyperflux-local-qualification-v1` view.
//! Its inputs are only the bridge's `snapshot`, `diagnostics`, and
//! `integration-view` answers; it never sends an active device query and
//! never writes to hardware.

use hfx_device_qualification::{
    BatteryAvailability, CompanionState, DeviceView, QualificationPlan, QualificationView,
    ReceiverLifecycleView, ReceiverView, ReportFindingSeverity, SupportView,
    build_qualification_report, qualification_generated_at,
};
use hfx_profiles::RuntimeProfileCatalog;
use hfx_system_probe::{CommandRunner, RealSystemProbe, SystemProbe, write_private_file};
use std::fmt::Write as _;
use std::path::Path;

/// The report is bounded by the console contract; this is a safety margin
/// for the serialized form, not an expected size.
const MAX_QUALIFICATION_REPORT_BYTES: usize = 4 * 1024 * 1024;

#[derive(Debug)]
pub enum QualificationReportError {
    CatalogInvalid,
    Serialization,
    SizeBoundExceeded,
    Io(std::io::Error),
}

impl std::fmt::Display for QualificationReportError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::CatalogInvalid => formatter.write_str("the installed profile catalog is invalid"),
            Self::Serialization => formatter.write_str("the report could not be serialized"),
            Self::SizeBoundExceeded => formatter.write_str("the report exceeded its size bound"),
            Self::Io(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for QualificationReportError {}

impl From<std::io::Error> for QualificationReportError {
    fn from(value: std::io::Error) -> Self {
        Self::Io(value)
    }
}

/// Reads the installed system and the bridge projection and builds the
/// privacy-bounded qualification report.
///
/// # Errors
///
/// Returns a bounded failure when the compiled profile catalog is invalid.
pub fn collect_qualification_report<R: CommandRunner>(
    probe: &RealSystemProbe<R>,
) -> Result<QualificationView, QualificationReportError> {
    let catalog =
        RuntimeProfileCatalog::load().map_err(|_| QualificationReportError::CatalogInvalid)?;
    let system = probe.snapshot();
    let projection = system
        .bridge
        .as_ref()
        .and_then(|_| probe.integration_projection().ok());
    Ok(build_qualification_report(
        &system,
        projection
            .as_ref()
            .map(|value| (value.protocol_version, &value.view)),
        &catalog,
        qualification_generated_at(),
    ))
}

/// Serializes the report exactly as `--json` prints it.
///
/// # Errors
///
/// Returns a bounded serialization or size failure.
pub fn qualification_report_json(
    view: &QualificationView,
) -> Result<String, QualificationReportError> {
    let rendered = serde_json::to_string_pretty(view)
        .map_err(|_| QualificationReportError::Serialization)?
        + "\n";
    if rendered.len() > MAX_QUALIFICATION_REPORT_BYTES {
        return Err(QualificationReportError::SizeBoundExceeded);
    }
    Ok(rendered)
}

/// Writes the JSON report to a new owner-only file.
///
/// # Errors
///
/// Returns a bounded serialization failure, or the I/O failure when the path
/// already exists or cannot be created.
pub fn write_qualification_report(
    path: &Path,
    view: &QualificationView,
) -> Result<(), QualificationReportError> {
    write_private_file(path, qualification_report_json(view)?.as_bytes())?;
    Ok(())
}

#[must_use]
pub fn render_qualification_text(view: &QualificationView) -> String {
    let mut text = String::from("HyperFlux Next qualification report\n");
    let _ = writeln!(text, "State: {}", state_text(view.companion.state));
    let _ = writeln!(
        text,
        "Bridge protocol: {}",
        view.companion
            .bridge_protocol
            .map_or_else(|| "not reachable".to_owned(), |value| format!("v{value}"))
    );
    let _ = writeln!(text, "Receivers: {}", view.receivers.len());
    for receiver in &view.receivers {
        render_receiver(&mut text, receiver, &view.plans);
    }
    text.push_str("\nFirmware: not collected (reading it needs an active device query)\n");
    let findings = view
        .collection
        .as_ref()
        .map(|collection| collection.diagnostic_findings.as_slice())
        .unwrap_or_default();
    if findings.is_empty() {
        text.push_str("Bridge findings: none\n");
    } else {
        let findings = findings
            .iter()
            .map(|finding| {
                format!(
                    "{} ({})",
                    finding.finding_id,
                    severity_text(finding.severity)
                )
            })
            .collect::<Vec<_>>()
            .join(", ");
        let _ = writeln!(text, "Bridge findings: {findings}");
    }
    text.push_str("No network upload, active device query, or hardware write was made.\n");
    let _ = writeln!(text, "Next: {}", next_step(view.companion.state));
    text
}

fn render_receiver(text: &mut String, receiver: &ReceiverView, plans: &[QualificationPlan]) {
    let _ = writeln!(
        text,
        "\n{}: {}{}, {}",
        receiver.receiver_id,
        receiver.model_name.as_deref().unwrap_or("unknown receiver"),
        usb_identity(receiver.vendor_id, receiver.product_id),
        lifecycle_text(receiver.lifecycle)
    );
    if receiver.devices.is_empty() {
        text.push_str("  No paired devices reported.\n");
    }
    for device in &receiver.devices {
        render_device(text, receiver, device, plans);
    }
}

fn render_device(
    text: &mut String,
    receiver: &ReceiverView,
    device: &DeviceView,
    plans: &[QualificationPlan],
) {
    let _ = writeln!(
        text,
        "  {}: {} {}{}",
        device.device_id,
        device.kind,
        device.model_name.as_deref().unwrap_or("(unknown model)"),
        usb_identity(device.vendor_id, Some(device.product_id))
    );
    let _ = writeln!(
        text,
        "    Support: {}; presence: {}; battery: {}",
        support_text(device.support),
        serde_text(&device.presence),
        battery_text(device)
    );
    let _ = writeln!(
        text,
        "    Profile: {}",
        device
            .profile
            .as_ref()
            .map_or("none (this device is not tested yet)", |profile| profile
                .id
                .as_str())
    );
    let checks = plans
        .iter()
        .find(|plan| {
            plan.receiver_id == receiver.receiver_id
                && plan.generation_id == receiver.generation_id
                && plan.device_id == device.device_id
        })
        .map(|plan| {
            plan.groups
                .iter()
                .map(|group| group.stages.len())
                .sum::<usize>()
        });
    if let Some(checks) = checks {
        let _ = writeln!(text, "    Evidence checks: {checks}");
    }
}

fn usb_identity(vendor_id: Option<u16>, product_id: Option<u16>) -> String {
    match (vendor_id, product_id) {
        (Some(vendor), Some(product)) => format!(" ({vendor:04x}:{product:04x})"),
        (None, Some(product)) => format!(" (product {product:04x})"),
        _ => String::new(),
    }
}

fn battery_text(device: &DeviceView) -> String {
    match (device.battery.availability, device.battery.percentage) {
        (BatteryAvailability::Reported, Some(percentage)) => format!("{percentage}%"),
        (BatteryAvailability::Stale, Some(percentage)) => format!("{percentage}% (stale)"),
        (availability, _) => serde_text(&availability),
    }
}

fn serde_text<T: serde::Serialize>(value: &T) -> String {
    serde_json::to_value(value)
        .ok()
        .and_then(|value| value.as_str().map(str::to_owned))
        .unwrap_or_else(|| "unknown".to_owned())
}

const fn state_text(state: CompanionState) -> &'static str {
    match state {
        CompanionState::Ready => "ready",
        CompanionState::LifecycleTransition => "receiver is changing state",
        CompanionState::DriverPending => "the installed driver is not loaded yet",
        CompanionState::LegacyV2Detected => "an older HyperFlux installation is active",
        CompanionState::BridgeUnavailable => "the bridge is not ready",
        CompanionState::NoReceiver => "no receiver found",
    }
}

const fn next_step(state: CompanionState) -> &'static str {
    match state {
        CompanionState::Ready => {
            "attach the output of `hyperfluxctl qualification --json` to your hardware report."
        }
        CompanionState::NoReceiver => "plug in the receiver, then run this command again.",
        CompanionState::LifecycleTransition => "wait a few seconds, then run this command again.",
        CompanionState::DriverPending
        | CompanionState::LegacyV2Detected
        | CompanionState::BridgeUnavailable => "run `hyperfluxctl doctor --explain`.",
    }
}

const fn support_text(support: SupportView) -> &'static str {
    match support {
        SupportView::Unknown => "unknown",
        SupportView::Identified => "identified",
        SupportView::ProfileQualified => "profile-qualified",
        SupportView::ProductionQualified => "production-qualified",
    }
}

const fn lifecycle_text(lifecycle: ReceiverLifecycleView) -> &'static str {
    match lifecycle {
        ReceiverLifecycleView::Active => "active",
        ReceiverLifecycleView::Recovering => "recovering",
        ReceiverLifecycleView::Unavailable => "unavailable",
    }
}

const fn severity_text(severity: ReportFindingSeverity) -> &'static str {
    match severity {
        ReportFindingSeverity::Info => "info",
        ReportFindingSeverity::Warning => "warning",
        ReportFindingSeverity::Error => "error",
        ReportFindingSeverity::Critical => "critical",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use hfx_device_qualification::{BatteryView, ReportFinding};
    use hfx_system_probe::{CommandOutput, ProbeError};
    use std::fs;
    use std::os::unix::fs::PermissionsExt as _;
    use std::path::PathBuf;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    struct UnavailableRunner;

    impl CommandRunner for UnavailableRunner {
        fn run(
            &self,
            _program: &str,
            _arguments: &[&str],
            _timeout: Duration,
        ) -> Result<CommandOutput, ProbeError> {
            Err(ProbeError::CommandUnavailable)
        }
    }

    fn ready_report() -> QualificationView {
        serde_json::from_str(include_str!(
            "../../../apps/device-qualification/tests/fixtures/rust-qualified-report.json"
        ))
        .expect("qualification report fixture matches the view")
    }

    fn unique_path(prefix: &str) -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        std::env::temp_dir().join(format!("{prefix}-{unique}"))
    }

    #[test]
    fn report_errors_describe_the_failure_without_private_values() {
        assert_eq!(
            QualificationReportError::CatalogInvalid.to_string(),
            "the installed profile catalog is invalid"
        );
        assert_eq!(
            QualificationReportError::Serialization.to_string(),
            "the report could not be serialized"
        );
        assert_eq!(
            QualificationReportError::SizeBoundExceeded.to_string(),
            "the report exceeded its size bound"
        );
        let error = QualificationReportError::from(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            "already exists",
        ));
        assert!(matches!(error, QualificationReportError::Io(_)));
        assert_eq!(error.to_string(), "already exists");
    }

    #[test]
    fn report_file_is_private_and_never_overwrites() {
        let path = unique_path("hfx-qualification").with_extension("json");
        let report = ready_report();
        write_qualification_report(&path, &report).expect("write");
        assert_eq!(
            fs::metadata(&path).expect("metadata").permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            fs::read_to_string(&path).expect("read"),
            qualification_report_json(&report).expect("serialize")
        );
        assert!(matches!(
            write_qualification_report(&path, &report),
            Err(QualificationReportError::Io(_))
        ));
        fs::remove_file(path).expect("cleanup");
    }

    #[test]
    fn unreachable_bridge_collects_a_report_that_is_not_ready() {
        let probe = RealSystemProbe::with_paths(
            UnavailableRunner,
            unique_path("hfx-missing-modules"),
            unique_path("hfx-missing-bridge").with_extension("sock"),
            Duration::from_millis(10),
        );
        let report = collect_qualification_report(&probe).expect("catalog is valid");
        assert_ne!(report.companion.state, CompanionState::Ready);
        assert_eq!(report.companion.bridge_protocol, None);
        let collection = report.collection.as_ref().expect("collection");
        assert!(!collection.active_device_query_executed);
        let text = render_qualification_text(&report);
        assert!(text.contains("Bridge protocol: not reachable"));
        assert!(text.contains("Firmware: not collected"));
    }

    #[test]
    fn values_without_a_text_form_render_as_unknown() {
        assert_eq!(serde_text(&BatteryAvailability::Reported), "reported");
        assert_eq!(serde_text(&7_u8), "unknown");
    }

    #[test]
    fn every_companion_state_names_a_plain_next_step() {
        let mut report = ready_report();
        for (state, label, next) in [
            (CompanionState::Ready, "ready", "attach the output"),
            (
                CompanionState::LifecycleTransition,
                "receiver is changing state",
                "wait a few seconds",
            ),
            (
                CompanionState::DriverPending,
                "the installed driver is not loaded yet",
                "run `hyperfluxctl doctor --explain`.",
            ),
            (
                CompanionState::LegacyV2Detected,
                "an older HyperFlux installation is active",
                "run `hyperfluxctl doctor --explain`.",
            ),
            (
                CompanionState::BridgeUnavailable,
                "the bridge is not ready",
                "run `hyperfluxctl doctor --explain`.",
            ),
            (
                CompanionState::NoReceiver,
                "no receiver found",
                "plug in the receiver",
            ),
        ] {
            report.companion.state = state;
            let text = render_qualification_text(&report);
            assert!(text.contains(&format!("State: {label}\n")), "{text}");
            assert!(text.contains(&format!("Next: {next}")), "{text}");
        }
    }

    #[test]
    fn partial_identities_and_stale_values_render_plainly() {
        let mut report = ready_report();
        let receiver = &mut report.receivers[0];
        receiver.model_name = None;
        receiver.vendor_id = None;
        receiver.product_id = Some(0x00cf);
        receiver.lifecycle = ReceiverLifecycleView::Recovering;
        let device = &mut receiver.devices[0];
        device.model_name = None;
        device.vendor_id = None;
        device.profile = None;
        device.support = SupportView::Unknown;
        device.battery = BatteryView {
            availability: BatteryAvailability::Stale,
            percentage: Some(41),
        };
        let device = &mut receiver.devices[1];
        device.support = SupportView::Identified;
        device.battery = BatteryView {
            availability: BatteryAvailability::Unavailable,
            percentage: None,
        };
        let mut empty = receiver.clone();
        empty.receiver_id = "receiver-2".to_owned();
        empty.model_name = Some("Receiver".to_owned());
        empty.vendor_id = None;
        empty.product_id = None;
        empty.lifecycle = ReceiverLifecycleView::Unavailable;
        empty.devices.clear();
        report.receivers.push(empty);
        if let Some(collection) = report.collection.as_mut() {
            collection.diagnostic_findings = vec![
                ReportFinding {
                    finding_id: "HFX-SERVICE-002".to_owned(),
                    severity: ReportFindingSeverity::Info,
                },
                ReportFinding {
                    finding_id: "HFX-TRANSPORT-001".to_owned(),
                    severity: ReportFindingSeverity::Critical,
                },
            ];
        }
        let text = render_qualification_text(&report);
        for expected in [
            "receiver-1: unknown receiver (product 00cf), recovering",
            "(unknown model) (product 00cd)",
            "Support: unknown; presence: active; battery: 41% (stale)",
            "Profile: none (this device is not tested yet)",
            "Support: identified; presence: active; battery: unavailable",
            "receiver-2: Receiver, unavailable",
            "  No paired devices reported.",
            "Bridge findings: HFX-SERVICE-002 (info), HFX-TRANSPORT-001 (critical)",
        ] {
            assert!(text.contains(expected), "missing {expected:?} in {text}");
        }
        report.receivers[0].devices[0].support = SupportView::ProfileQualified;
        report.collection = None;
        let text = render_qualification_text(&report);
        assert!(text.contains("Support: profile-qualified;"));
        assert!(text.contains("Bridge findings: none\n"));
    }
}
