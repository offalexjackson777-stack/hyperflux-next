// SPDX-License-Identifier: GPL-2.0-only

//! The read-only hardware qualification report written by the installed
//! `hyperfluxctl qualification` command.
//!
//! The report is the console's `hyperflux-local-qualification-v1` view built
//! by the same producer, plus one `collection` declaration. Its inputs are
//! only the bridge's `snapshot`, `diagnostics`, and `integration-view`
//! answers. The installation-stable receiver and device identifiers, and the
//! receiver generation that the driver seeds at random for each load, are
//! replaced with ordinals before the view is built, so the stage identifiers
//! derived from them cannot link two reports to one computer.

use crate::SystemSnapshot;
use crate::qualification::{QualificationView, RunnerCapabilities, build_projected_view};
use hfx_domain::{
    ErrorSeverity, GenerationId, LogicalDeviceId, PrivacyClass, ProtocolVersion, ReceiverId,
};
use hfx_profiles::RuntimeProfileCatalog;
use hfx_protocol::{
    DeviceInventoryView, DiagnosticFinding, IntegrationReceiverView, IntegrationView,
};
use serde::{Deserialize, Serialize};

pub const QUALIFICATION_REPORT_PRODUCER: &str = "hyperfluxctl qualification";
const REPORT_VIEW_REVISION: u64 = 1;
const REPORT_GENERATION: u64 = 1;
const MAX_REPORT_RECEIVERS: usize = 8;
const MAX_REPORT_DEVICES: usize = 16;
const MAX_REPORT_PLANS: usize = 32;
const MAX_REPORT_FINDINGS: usize = 32;

/// One read-only bridge request whose answer the report used.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ReportBridgeRequest {
    Snapshot,
    Diagnostics,
    IntegrationView,
}

/// Firmware is reachable only through an active native device query, which
/// the report never sends.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum FirmwareCollection {
    NotCollected,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ReportFindingSeverity {
    Info,
    Warning,
    Error,
    Critical,
}

/// A bridge finding reduced to its error catalog code and severity; its
/// free-text cause, explanation, and action are left out.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ReportFinding {
    pub finding_id: String,
    pub severity: ReportFindingSeverity,
}

/// What a `hyperfluxctl qualification` report read and what it left out.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QualificationCollection {
    pub producer: String,
    pub bridge_requests: Vec<ReportBridgeRequest>,
    pub active_device_query_executed: bool,
    pub firmware: FirmwareCollection,
    pub diagnostic_findings: Vec<ReportFinding>,
}

/// Builds the bounded, privacy-safe qualification report from the system
/// probe (which carries the bridge `snapshot` and `diagnostics` answers) and
/// the bridge's `integration-view` projection when it was available.
#[must_use]
pub fn build_qualification_report(
    system: &SystemSnapshot,
    projection: Option<(ProtocolVersion, &IntegrationView)>,
    catalog: &RuntimeProfileCatalog,
    generated_at: String,
) -> QualificationView {
    let ordinal = projection.map(|(version, view)| (version, ordinal_projection(view)));
    let mut view = build_projected_view(
        system,
        ordinal.as_ref().map(|(version, view)| (*version, view)),
        catalog,
        RunnerCapabilities::default(),
        REPORT_VIEW_REVISION,
        generated_at,
    );
    view.plans.truncate(MAX_REPORT_PLANS);
    let mut bridge_requests = Vec::new();
    if system.bridge.is_some() {
        bridge_requests.push(ReportBridgeRequest::Snapshot);
        bridge_requests.push(ReportBridgeRequest::Diagnostics);
    }
    if projection.is_some() {
        bridge_requests.push(ReportBridgeRequest::IntegrationView);
    }
    view.collection = Some(QualificationCollection {
        producer: QUALIFICATION_REPORT_PRODUCER.to_owned(),
        bridge_requests,
        active_device_query_executed: false,
        firmware: FirmwareCollection::NotCollected,
        diagnostic_findings: system
            .bridge
            .iter()
            .flat_map(|bridge| &bridge.diagnostics.findings)
            .filter_map(report_finding)
            .take(MAX_REPORT_FINDINGS)
            .collect(),
    });
    view
}

fn ordinal_projection(view: &IntegrationView) -> IntegrationView {
    IntegrationView {
        cursor: view.cursor.clone(),
        receivers: view
            .receivers
            .iter()
            .take(MAX_REPORT_RECEIVERS)
            .enumerate()
            .filter_map(|(index, receiver)| ordinal_receiver(index, receiver))
            .collect(),
    }
}

fn ordinal_receiver(
    index: usize,
    receiver: &IntegrationReceiverView,
) -> Option<IntegrationReceiverView> {
    let receiver_id = ReceiverId::try_from(format!("receiver-{}", index + 1)).ok()?;
    let generation_id = GenerationId::try_from(REPORT_GENERATION).ok()?;
    let inventory = receiver
        .inventory
        .iter()
        .take(MAX_REPORT_DEVICES)
        .enumerate()
        .filter_map(|(position, device)| ordinal_device(position, device))
        .collect();
    Some(IntegrationReceiverView {
        receiver_id,
        generation_id,
        profile: receiver.profile.clone(),
        model_name: receiver.model_name.clone(),
        lifecycle: receiver.lifecycle,
        stable_restore_enabled: receiver.stable_restore_enabled,
        restore_state: receiver.restore_state,
        inventory,
        controllers: Vec::new(),
    })
}

fn ordinal_device(position: usize, device: &DeviceInventoryView) -> Option<DeviceInventoryView> {
    Some(DeviceInventoryView {
        device_id: LogicalDeviceId::try_from(format!("device-{}", position + 1)).ok()?,
        device_kind: device.device_kind,
        product_id: device.product_id,
        profile: device.profile.clone(),
        model_name: device.model_name.clone(),
        pairing: device.pairing,
        presence: device.presence,
        availability: device.availability,
        support_level: device.support_level,
        endpoints: Vec::new(),
        battery: device.battery.clone(),
        capabilities: device.capabilities.clone(),
    })
}

fn report_finding(finding: &DiagnosticFinding) -> Option<ReportFinding> {
    if !matches!(
        finding.privacy,
        PrivacyClass::Public | PrivacyClass::PublicSummary
    ) {
        return None;
    }
    let code = hfx_errors::ERRORS
        .iter()
        .map(|error| error.code.as_str())
        .find(|code| *code == finding.finding_id.as_str())?;
    Some(ReportFinding {
        finding_id: code.to_owned(),
        severity: match finding.severity {
            ErrorSeverity::Info => ReportFindingSeverity::Info,
            ErrorSeverity::Warning => ReportFindingSeverity::Warning,
            ErrorSeverity::Error => ReportFindingSeverity::Error,
            ErrorSeverity::Critical => ReportFindingSeverity::Critical,
        },
    })
}
