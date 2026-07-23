// SPDX-License-Identifier: GPL-2.0-only

use crate::qualification::{StageProgress, apply_stage_results, resume_retry_ready};
use crate::qualification_lifecycle::{
    PreparedLifecycleAction, capture_lifecycle_run, current_suspend_stamp,
    identity_transition_matches, observe_lifecycle, overlay_lifecycle_run,
    prepare_lifecycle_checkpoint, reconcile_system_resume, recover_retryable_resume_failure,
    start_lifecycle_stage,
};
use crate::qualification_runner::{
    ObservationVerdict, PreparedLightingAction, new_transaction_id, prepare_lighting_checkpoint,
    prepare_lighting_stage, reconcile_pending_lighting,
};
use crate::qualification_state::{
    QualificationSessionState, QualificationStateError, archive_stale_state, default_state_path,
    load_state, save_state,
};
use crate::{
    BatteryAvailability, CompanionAction, PresenceView, QualificationStage, QualificationView,
    RealSystemProbe, RiskLevel, RunnerAvailability, RunnerCapabilities, StageResult, StageStatus,
    SystemProbe, build_qualification_view, qualification_generated_at,
};
use hfx_profiles::RuntimeProfileCatalog;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, SocketAddrV4, TcpListener, TcpStream};
use std::path::{Component, Path, PathBuf};
use std::time::Duration;

const DEFAULT_PORT: u16 = 47_821;
const MAX_REQUEST_BYTES: usize = 16 * 1_024;
const MAX_ASSET_BYTES: u64 = 2 * 1_024 * 1_024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(3);

type ServerState = QualificationSessionState;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
struct ActionInput {
    view_revision: u64,
    #[serde(default)]
    confirmation: Option<String>,
    #[serde(default)]
    observations: BTreeMap<String, String>,
}

#[derive(Serialize)]
struct IdentityFingerprint<'a> {
    receivers: Vec<ReceiverIdentityFingerprint<'a>>,
}

#[derive(Serialize)]
struct ReceiverIdentityFingerprint<'a> {
    receiver_id: &'a str,
    generation_id: u64,
    profile: Option<(&'a str, &'a str)>,
    devices: Vec<DeviceIdentityFingerprint<'a>>,
}

#[derive(Serialize)]
struct DeviceIdentityFingerprint<'a> {
    device_id: &'a str,
    product_id: u16,
    profile: Option<(&'a str, &'a str)>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QualificationServerConfig {
    pub assets: PathBuf,
    pub port: u16,
    pub state_file: Option<PathBuf>,
}

impl Default for QualificationServerConfig {
    fn default() -> Self {
        Self {
            assets: PathBuf::from("/usr/share/hyperflux-next/qualification-console"),
            port: DEFAULT_PORT,
            state_file: default_state_path().ok(),
        }
    }
}

#[derive(Debug)]
pub enum QualificationServerError {
    AssetRootInvalid,
    CatalogInvalid,
    StateUnavailable,
    BindFailed(io::Error),
    ConnectionFailed(io::Error),
}

impl fmt::Display for QualificationServerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::AssetRootInvalid => {
                "qualification console assets are missing or are not a readable directory"
            }
            Self::CatalogInvalid => "the installed profile catalog is invalid",
            Self::StateUnavailable => "the private qualification checkpoint store is unavailable",
            Self::BindFailed(_) => "the local qualification endpoint could not be opened",
            Self::ConnectionFailed(_) => "the local qualification endpoint stopped unexpectedly",
        })
    }
}

impl std::error::Error for QualificationServerError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::BindFailed(error) | Self::ConnectionFailed(error) => Some(error),
            Self::AssetRootInvalid | Self::CatalogInvalid | Self::StateUnavailable => None,
        }
    }
}

/// Serves the installed qualification UI and its read-only live projection
/// from one loopback origin. This function never binds a non-loopback address.
///
/// # Errors
///
/// Returns an error when assets are absent, the immutable profile catalog is
/// invalid, the loopback listener cannot bind, or listener acceptance fails.
pub fn serve_qualification_console(
    config: &QualificationServerConfig,
) -> Result<(), QualificationServerError> {
    let asset_root = canonical_asset_root(&config.assets)?;
    let catalog =
        RuntimeProfileCatalog::load().map_err(|_| QualificationServerError::CatalogInvalid)?;
    let listener = TcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, config.port))
        .map_err(QualificationServerError::BindFailed)?;
    let state_file = config
        .state_file
        .as_deref()
        .ok_or(QualificationServerError::StateUnavailable)?;
    let mut state = match load_state(state_file) {
        Ok(Some(state)) => state,
        Ok(None) => ServerState::default(),
        Err(QualificationStateError::Stale) => {
            archive_stale_state(state_file)
                .map_err(|_| QualificationServerError::StateUnavailable)?;
            ServerState::default()
        }
        Err(_) => return Err(QualificationServerError::StateUnavailable),
    };
    if recover_retryable_resume_state(&mut state)
        .map_err(|_| QualificationServerError::StateUnavailable)?
    {
        state.revision = state.revision.saturating_add(1);
    }
    save_state(state_file, &state).map_err(|_| QualificationServerError::StateUnavailable)?;
    let probe = RealSystemProbe::default();
    for connection in listener.incoming() {
        let mut stream = connection.map_err(QualificationServerError::ConnectionFailed)?;
        let _ = handle_connection(
            &mut stream,
            &asset_root,
            config.port,
            &probe,
            &catalog,
            state_file,
            &mut state,
        );
    }
    Ok(())
}

fn recover_retryable_resume_state(
    state: &mut ServerState,
) -> Result<bool, crate::qualification_lifecycle::LifecycleRunnerError> {
    let Some(run) = state.lifecycle_run.as_mut() else {
        return Ok(false);
    };
    for (stage_id, progress) in &mut state.results {
        if recover_retryable_resume_failure(run, stage_id, progress)? {
            return Ok(true);
        }
    }
    Ok(false)
}

fn canonical_asset_root(path: &Path) -> Result<PathBuf, QualificationServerError> {
    let root = fs::canonicalize(path).map_err(|_| QualificationServerError::AssetRootInvalid)?;
    if !root.is_dir() || !root.join("index.html").is_file() {
        return Err(QualificationServerError::AssetRootInvalid);
    }
    Ok(root)
}

fn handle_connection(
    stream: &mut TcpStream,
    asset_root: &Path,
    port: u16,
    probe: &RealSystemProbe,
    catalog: &RuntimeProfileCatalog,
    state_file: &Path,
    state: &mut ServerState,
) -> io::Result<()> {
    stream.set_read_timeout(Some(REQUEST_TIMEOUT))?;
    stream.set_write_timeout(Some(REQUEST_TIMEOUT))?;
    let request = match read_request(stream) {
        Ok(request) => request,
        Err(status) => return write_error(stream, status),
    };
    if !host_is_local(request.host.as_deref(), port)
        || !origin_is_local(request.origin.as_deref(), port)
    {
        return write_error(stream, HttpStatus::Forbidden);
    }
    let head_only = request.method == "HEAD";
    if request.path == "/v1/qualification/view" && matches!(request.method.as_str(), "GET" | "HEAD")
    {
        let view = live_view(probe, catalog, state_file, state)?;
        let body = serde_json::to_vec(&view).map_err(io::Error::other)?;
        return write_response(
            stream,
            HttpStatus::Ok,
            "application/json; charset=utf-8",
            &body,
            CachePolicy::NoStore,
            head_only,
        );
    }
    if request.path.starts_with("/v1/qualification/actions/") && request.method == "POST" {
        return invoke_action(stream, &request, probe, catalog, state_file, state);
    }
    if request.method != "GET" && request.method != "HEAD" {
        return write_error(stream, HttpStatus::MethodNotAllowed);
    }
    let Some(asset) = resolve_asset(asset_root, &request.path) else {
        return write_error(stream, HttpStatus::NotFound);
    };
    let metadata = fs::metadata(&asset)?;
    if !metadata.is_file() || metadata.len() > MAX_ASSET_BYTES {
        return write_error(stream, HttpStatus::NotFound);
    }
    let body = fs::read(&asset)?;
    let content_type = content_type(&asset);
    let cache = CachePolicy::NoCache;
    write_response(
        stream,
        HttpStatus::Ok,
        content_type,
        &body,
        cache,
        head_only,
    )
}

fn live_view(
    probe: &RealSystemProbe,
    catalog: &RuntimeProfileCatalog,
    state_file: &Path,
    state: &mut ServerState,
) -> io::Result<QualificationView> {
    let system = probe.snapshot();
    let integration = probe.qualification_integration().ok();
    let mut view = build_qualification_view(
        &system,
        integration.as_ref(),
        catalog,
        RunnerCapabilities {
            read_only_actions: RunnerAvailability::Available,
            supervised_lighting: RunnerAvailability::Available,
            device_lifecycle: RunnerAvailability::Available,
            system_lifecycle: RunnerAvailability::Available,
        },
        state.revision.max(1),
        qualification_generated_at(),
    );
    let fingerprint = identity_fingerprint(&view)?;
    let mut changed = refresh_identity_binding(state, fingerprint, &view);
    if reconcile_pending(probe, &view, state) {
        changed = true;
    }
    if reconcile_lifecycle_resume_state(integration.as_ref(), state) {
        changed = true;
    }
    if let Some(run) = &state.lifecycle_run {
        overlay_lifecycle_run(&mut view, run);
    }
    view.view_revision = state.revision;
    apply_stage_results(&mut view, &state.results, state.run_id.as_deref());
    view.companion.hardware_write_executed = state.hardware_write_executed;
    if changed {
        save_state(state_file, state).map_err(io::Error::other)?;
    }
    Ok(view)
}

fn refresh_identity_binding(
    state: &mut ServerState,
    fingerprint: [u8; 32],
    view: &QualificationView,
) -> bool {
    match state.identity_fingerprint {
        None => {
            state.revision = 1;
            state.identity_fingerprint = Some(fingerprint);
            true
        }
        Some(previous) if previous != fingerprint => {
            if state
                .lifecycle_run
                .as_ref()
                .is_some_and(|run| identity_transition_matches(view, run))
            {
                state.identity_fingerprint = Some(fingerprint);
                return true;
            }
            state.revision = state.revision.saturating_add(1);
            state.identity_fingerprint = Some(fingerprint);
            state.run_id = None;
            state.results.clear();
            state.lifecycle_run = None;
            if let Some(pending) = &state.pending_transaction {
                state.results.insert(
                    pending.stage_id.clone(),
                    StageProgress {
                        status: StageStatus::Running,
                        result: None,
                        checkpoint: None,
                    },
                );
            }
            true
        }
        Some(_) => false,
    }
}

fn reconcile_lifecycle_resume_state(
    integration: Option<&crate::BridgeIntegration>,
    session: &mut ServerState,
) -> bool {
    let Some(run) = session.lifecycle_run.clone() else {
        return false;
    };
    let Some(active) = &run.active else {
        return false;
    };
    let stage_id = active.stage_id.clone();
    let device_id = active.device_id.clone();
    let observation = observe_lifecycle(integration, &run, &device_id);
    let Ok(stamp) = current_suspend_stamp() else {
        return false;
    };
    let Ok(Some((run, transition))) = reconcile_system_resume(run, &observation, &stamp) else {
        return false;
    };
    session.lifecycle_run = Some(run);
    session.results.insert(stage_id, transition.progress);
    session.revision = session.revision.saturating_add(1);
    true
}

fn reconcile_pending(
    probe: &RealSystemProbe,
    view: &QualificationView,
    session: &mut ServerState,
) -> bool {
    let Some(pending) = session.pending_transaction.clone() else {
        return false;
    };
    let Some(stage) = view
        .plans
        .iter()
        .flat_map(|plan| &plan.groups)
        .flat_map(|group| &group.stages)
        .find(|stage| stage.stage_id == pending.stage_id)
    else {
        session.pending_transaction = None;
        session.results.clear();
        session.run_id = None;
        session.revision = session.revision.saturating_add(1);
        return true;
    };
    let Ok(Some(transition)) = reconcile_pending_lighting(probe, view, stage, &pending) else {
        return false;
    };
    session.hardware_write_executed |= transition.hardware_write_executed;
    session
        .results
        .insert(pending.stage_id, transition.progress);
    session.pending_transaction = None;
    session.revision = session.revision.saturating_add(1);
    true
}

fn identity_fingerprint(view: &QualificationView) -> io::Result<[u8; 32]> {
    let mut receivers = view
        .receivers
        .iter()
        .map(|receiver| {
            let mut devices = receiver
                .devices
                .iter()
                .map(|device| DeviceIdentityFingerprint {
                    device_id: &device.device_id,
                    product_id: device.product_id,
                    profile: device
                        .profile
                        .as_ref()
                        .map(|profile| (profile.id.as_str(), profile.digest.as_str())),
                })
                .collect::<Vec<_>>();
            devices.sort_by_key(|device| (device.device_id, device.product_id));
            ReceiverIdentityFingerprint {
                receiver_id: &receiver.receiver_id,
                generation_id: receiver.generation_id,
                profile: receiver
                    .profile
                    .as_ref()
                    .map(|profile| (profile.id.as_str(), profile.digest.as_str())),
                devices,
            }
        })
        .collect::<Vec<_>>();
    receivers.sort_by_key(|receiver| (receiver.receiver_id, receiver.generation_id));
    let encoded =
        serde_json::to_vec(&IdentityFingerprint { receivers }).map_err(io::Error::other)?;
    Ok(Sha256::digest(encoded).into())
}

fn invoke_action(
    stream: &mut TcpStream,
    request: &HttpRequest,
    probe: &RealSystemProbe,
    catalog: &RuntimeProfileCatalog,
    state_file: &Path,
    state: &mut ServerState,
) -> io::Result<()> {
    let input: ActionInput = match serde_json::from_slice(&request.body) {
        Ok(input) => input,
        Err(_) => {
            return write_json_error(
                stream,
                HttpStatus::BadRequest,
                "The action body does not match the qualification contract.",
            );
        }
    };
    let view = live_view(probe, catalog, state_file, state)?;
    if input.view_revision != view.view_revision {
        return write_json_error(
            stream,
            HttpStatus::Conflict,
            "The receiver generation or qualification state changed. Refresh before continuing.",
        );
    }
    let Some(action) = view
        .actions
        .iter()
        .find(|action| action.href == request.path && action.enabled)
    else {
        return write_json_error(
            stream,
            HttpStatus::Conflict,
            "This action is not available in the current qualification state.",
        );
    };
    let Some(selected_stage) = stage_for_action(&view, action) else {
        return write_json_error(
            stream,
            HttpStatus::Conflict,
            "This action has no current profile-bound stage.",
        );
    };
    if action
        .confirmation
        .as_ref()
        .is_some_and(|confirmation| input.confirmation.as_deref() != Some(&confirmation.phrase))
    {
        return write_json_error(
            stream,
            HttpStatus::Forbidden,
            "The exact authorization phrase was not supplied.",
        );
    }
    ensure_run_id(state);
    if let Err(failure) = apply_action(probe, &view, selected_stage, &input, state_file, state) {
        if failure.state_changed {
            state.revision = state.revision.saturating_add(1);
            save_state(state_file, state).map_err(io::Error::other)?;
        }
        return write_json_error(stream, failure.status, &failure.message);
    }
    state.revision = state.revision.saturating_add(1);
    commit_completed_state(state_file, state)?;
    let response = live_view(probe, catalog, state_file, state)?;
    let body = serde_json::to_vec(&response).map_err(io::Error::other)?;
    write_response(
        stream,
        HttpStatus::Ok,
        "application/json; charset=utf-8",
        &body,
        CachePolicy::NoStore,
        false,
    )
}

struct ActionFailure {
    status: HttpStatus,
    message: String,
    state_changed: bool,
}

impl ActionFailure {
    fn unchanged(status: HttpStatus, message: impl Into<String>) -> Self {
        Self {
            status,
            message: message.into(),
            state_changed: false,
        }
    }
}

fn apply_action(
    probe: &RealSystemProbe,
    view: &QualificationView,
    stage: &QualificationStage,
    input: &ActionInput,
    state_file: &Path,
    session: &mut ServerState,
) -> Result<(), ActionFailure> {
    match stage.risk {
        RiskLevel::ReadOnly => record_read_only_result(view, stage, input, session),
        RiskLevel::LightingWrite => {
            apply_lighting_action(probe, view, stage, input, state_file, session)
        }
        RiskLevel::DeviceLifecycle | RiskLevel::SystemLifecycle => {
            apply_lifecycle_action(probe, view, stage, input, state_file, session)
        }
    }
}

fn apply_lifecycle_action(
    probe: &RealSystemProbe,
    view: &QualificationView,
    stage: &QualificationStage,
    input: &ActionInput,
    state_file: &Path,
    session: &mut ServerState,
) -> Result<(), ActionFailure> {
    let run = match &session.lifecycle_run {
        Some(run) => run.clone(),
        None => capture_lifecycle_run(view, stage)
            .map_err(|error| ActionFailure::unchanged(HttpStatus::Conflict, error.to_string()))?,
    };
    let selected_device_id = run
        .active
        .as_ref()
        .map(|active| active.device_id.clone())
        .or_else(|| device_id_for_stage(view, stage).map(str::to_owned))
        .ok_or_else(|| {
            ActionFailure::unchanged(
                HttpStatus::Conflict,
                "The lifecycle stage has no exact selected controller.",
            )
        })?;
    let integration = probe.qualification_integration().ok();
    let observation = observe_lifecycle(integration.as_ref(), &run, &selected_device_id);
    let prepared = if stage.status == StageStatus::Ready {
        let (run, transition) = start_lifecycle_stage(run, stage, &observation)
            .map_err(|error| ActionFailure::unchanged(HttpStatus::Conflict, error.to_string()))?;
        PreparedLifecycleAction::Record { run, transition }
    } else if stage.status == StageStatus::AwaitingObservation || resume_retry_ready(stage) {
        let verdict = observation_verdict(stage, input)
            .map_err(|message| ActionFailure::unchanged(HttpStatus::BadRequest, message))?;
        let current = session.results.get(&stage.stage_id).ok_or_else(|| {
            ActionFailure::unchanged(
                HttpStatus::Conflict,
                "The watched lifecycle checkpoint is no longer available.",
            )
        })?;
        prepare_lifecycle_checkpoint(run, stage, current, verdict, &observation)
            .map_err(|error| ActionFailure::unchanged(HttpStatus::Conflict, error.to_string()))?
    } else {
        return Err(ActionFailure::unchanged(
            HttpStatus::Conflict,
            "This lifecycle stage is not at a runnable checkpoint.",
        ));
    };
    commit_lifecycle_action(prepared, probe, stage, state_file, session)
}

fn commit_lifecycle_action(
    prepared: PreparedLifecycleAction,
    probe: &RealSystemProbe,
    stage: &QualificationStage,
    state_file: &Path,
    session: &mut ServerState,
) -> Result<(), ActionFailure> {
    let (run, transition, requests_suspend) = match prepared {
        PreparedLifecycleAction::Record { run, transition } => (run, transition, false),
        PreparedLifecycleAction::Suspend { run, transition } => (run, transition, true),
    };
    let previous_run = session.lifecycle_run.clone();
    let previous_progress = session.results.get(&stage.stage_id).cloned();
    session.lifecycle_run = Some(run);
    session
        .results
        .insert(stage.stage_id.clone(), transition.progress);
    if !requests_suspend {
        return Ok(());
    }
    if save_state(state_file, session).is_err() {
        restore_lifecycle_state(session, &stage.stage_id, previous_run, previous_progress);
        return Err(ActionFailure::unchanged(
            HttpStatus::Conflict,
            "The private resume checkpoint could not be committed; suspend was not requested.",
        ));
    }
    if probe.request_system_suspend().is_err() {
        restore_lifecycle_state(session, &stage.stage_id, previous_run, previous_progress);
        let _ = save_state(state_file, session);
        return Err(ActionFailure::unchanged(
            HttpStatus::Conflict,
            "The operating system rejected the single suspend request.",
        ));
    }
    Ok(())
}

fn restore_lifecycle_state(
    session: &mut ServerState,
    stage_id: &str,
    previous_run: Option<crate::qualification_lifecycle::LifecycleRunState>,
    previous_progress: Option<StageProgress>,
) {
    session.lifecycle_run = previous_run;
    if let Some(progress) = previous_progress {
        session.results.insert(stage_id.to_owned(), progress);
    } else {
        session.results.remove(stage_id);
    }
}

fn device_id_for_stage<'a>(
    view: &'a QualificationView,
    stage: &QualificationStage,
) -> Option<&'a str> {
    view.plans.iter().find_map(|plan| {
        plan.groups
            .iter()
            .flat_map(|group| &group.stages)
            .any(|candidate| candidate.stage_id == stage.stage_id)
            .then_some(plan.device_id.as_str())
    })
}

fn record_read_only_result(
    view: &QualificationView,
    stage: &QualificationStage,
    input: &ActionInput,
    session: &mut ServerState,
) -> Result<(), ActionFailure> {
    let (status, summary) = read_only_outcome(view, stage, input)?;
    session.results.insert(
        stage.stage_id.clone(),
        StageProgress {
            status,
            result: Some(StageResult {
                summary: summary.to_owned(),
                completed_at: qualification_generated_at(),
                evidence_refs: vec![format!("local-read-only:{}", stage.stage_id)],
            }),
            checkpoint: None,
        },
    );
    Ok(())
}

fn apply_lighting_action(
    probe: &RealSystemProbe,
    view: &QualificationView,
    stage: &QualificationStage,
    input: &ActionInput,
    state_file: &Path,
    session: &mut ServerState,
) -> Result<(), ActionFailure> {
    let prepared = if stage.status == StageStatus::Ready {
        prepare_lighting_stage(probe, view, stage)
    } else if stage.status == StageStatus::AwaitingObservation {
        let verdict = observation_verdict(stage, input)
            .map_err(|message| ActionFailure::unchanged(HttpStatus::BadRequest, message))?;
        let current = session.results.get(&stage.stage_id).ok_or_else(|| {
            ActionFailure::unchanged(
                HttpStatus::Conflict,
                "The watched checkpoint is no longer available; no request was sent.",
            )
        })?;
        prepare_lighting_checkpoint(probe, view, stage, current, verdict)
    } else {
        return Err(ActionFailure::unchanged(
            HttpStatus::Conflict,
            "This lighting stage is not at a runnable checkpoint.",
        ));
    };
    let prepared = prepared
        .map_err(|error| ActionFailure::unchanged(HttpStatus::Conflict, error.to_string()))?;
    let transition = match prepared {
        PreparedLightingAction::Record(transition) => transition,
        PreparedLightingAction::Write(prepared) => {
            execute_prepared_lighting(&prepared, probe, stage, state_file, session)?
        }
    };
    session.hardware_write_executed |= transition.hardware_write_executed;
    session
        .results
        .insert(stage.stage_id.clone(), transition.progress);
    Ok(())
}

fn execute_prepared_lighting(
    prepared: &crate::qualification_runner::PreparedLightingWrite,
    probe: &RealSystemProbe,
    stage: &QualificationStage,
    state_file: &Path,
    session: &mut ServerState,
) -> Result<crate::qualification_runner::RunnerTransition, ActionFailure> {
    let transaction_id = new_transaction_id()
        .map_err(|error| ActionFailure::unchanged(HttpStatus::Conflict, error.to_string()))?;
    let prior = session.results.get(&stage.stage_id).cloned();
    let pending = prepared.pending(&stage.stage_id, transaction_id.clone());
    session.pending_transaction = Some(pending.clone());
    session.results.insert(
        stage.stage_id.clone(),
        StageProgress {
            status: StageStatus::Running,
            result: None,
            checkpoint: None,
        },
    );
    if save_state(state_file, session).is_err() {
        restore_prior_progress(session, &stage.stage_id, prior);
        return Err(ActionFailure::unchanged(
            HttpStatus::Conflict,
            "The private checkpoint could not be committed; no request was sent.",
        ));
    }
    match prepared.execute(probe, &transaction_id) {
        Ok(transition) => Ok(transition),
        Err(error) => {
            let retry_forbidden = error.retry_forbidden();
            session.hardware_write_executed |= error.hardware_write_executed();
            if !retry_forbidden {
                restore_prior_progress(session, &stage.stage_id, prior);
                if save_state(state_file, session).is_err() {
                    restore_pending_progress(session, pending);
                    return Err(ActionFailure::unchanged(
                        HttpStatus::Conflict,
                        "The request was not sent, but its private checkpoint could not be restored.",
                    ));
                }
            }
            Err(ActionFailure {
                status: HttpStatus::Conflict,
                message: error.to_string(),
                state_changed: retry_forbidden,
            })
        }
    }
}

fn restore_pending_progress(
    session: &mut ServerState,
    pending: crate::qualification_runner::PendingLightingTransaction,
) {
    session.results.insert(
        pending.stage_id.clone(),
        StageProgress {
            status: StageStatus::Running,
            result: None,
            checkpoint: None,
        },
    );
    session.pending_transaction = Some(pending);
}

fn restore_prior_progress(session: &mut ServerState, stage_id: &str, prior: Option<StageProgress>) {
    session.pending_transaction = None;
    if let Some(progress) = prior {
        session.results.insert(stage_id.to_owned(), progress);
    } else {
        session.results.remove(stage_id);
    }
}

fn ensure_run_id(state: &mut ServerState) {
    if state.run_id.is_none() {
        let timestamp = qualification_generated_at()
            .chars()
            .filter(|character| !matches!(character, ':' | '-'))
            .collect::<String>();
        state.run_id = Some(format!("local-{timestamp}"));
    }
}

fn commit_completed_state(state_file: &Path, state: &mut ServerState) -> io::Result<()> {
    let completed_pending = state.pending_transaction.take();
    if let Err(error) = save_state(state_file, state) {
        state.pending_transaction = completed_pending;
        return Err(io::Error::other(error));
    }
    Ok(())
}

fn observation_verdict(
    stage: &QualificationStage,
    input: &ActionInput,
) -> Result<ObservationVerdict, &'static str> {
    if input.observations.len() != stage.observations.len() {
        return Err("Answer every watched observation exactly once; no request was sent.");
    }
    let mut verdict = ObservationVerdict::Pass;
    for prompt in &stage.observations {
        let answer = input
            .observations
            .get(&prompt.id)
            .ok_or("Answer every watched observation exactly once; no request was sent.")?;
        let choice = prompt
            .choices
            .iter()
            .find(|choice| choice.id == *answer)
            .ok_or("A watched answer is outside the declared choices; no request was sent.")?;
        match choice.outcome.as_str() {
            "fail" => verdict = ObservationVerdict::Fail,
            "unclear" if verdict != ObservationVerdict::Fail => {
                verdict = ObservationVerdict::Unclear;
            }
            "pass" | "unclear" => {}
            _ => return Err("A watched answer has an invalid outcome; no request was sent."),
        }
    }
    Ok(verdict)
}

fn stage_for_action<'a>(
    view: &'a QualificationView,
    action: &CompanionAction,
) -> Option<&'a QualificationStage> {
    view.plans
        .iter()
        .flat_map(|plan| &plan.groups)
        .flat_map(|group| &group.stages)
        .find(|stage| stage.action_id.as_deref() == Some(action.id.as_str()))
}

fn read_only_outcome(
    view: &QualificationView,
    stage: &QualificationStage,
    input: &ActionInput,
) -> Result<(StageStatus, &'static str), ActionFailure> {
    if stage.stage_id.ends_with("-telemetry") {
        let device = view.plans.iter().find_map(|plan| {
            plan.groups
                .iter()
                .flat_map(|group| &group.stages)
                .any(|candidate| candidate.stage_id == stage.stage_id)
                .then(|| device_for_plan(view, plan))
                .flatten()
        });
        let Some(device) = device else {
            return Err(ActionFailure::unchanged(
                HttpStatus::Conflict,
                "The selected device is no longer present. Let it return, refresh, and retry.",
            ));
        };
        if device.presence != PresenceView::Active {
            return Err(ActionFailure::unchanged(
                HttpStatus::Conflict,
                "Fresh active-device evidence is not available yet. Exercise the device, refresh, and retry.",
            ));
        }
        let battery_claimed = stage
            .capabilities
            .iter()
            .any(|capability| capability == "telemetry.battery-percent");
        if battery_claimed && !matches!(device.battery.availability, BatteryAvailability::Reported)
        {
            return Err(ActionFailure::unchanged(
                HttpStatus::Conflict,
                "Fresh battery evidence is not available yet. Exercise or reconnect the device, refresh, and retry.",
            ));
        }
    }
    let mut outcome = StageStatus::Passed;
    for prompt in &stage.observations {
        let Some(answer) = input.observations.get(&prompt.id) else {
            return Err(ActionFailure::unchanged(
                HttpStatus::BadRequest,
                "A required watched observation was not answered.",
            ));
        };
        let Some(choice) = prompt.choices.iter().find(|choice| choice.id == *answer) else {
            return Err(ActionFailure::unchanged(
                HttpStatus::BadRequest,
                "A watched observation used an answer outside the declared choices.",
            ));
        };
        match choice.outcome.as_str() {
            "fail" => outcome = StageStatus::Failed,
            "unclear" if outcome != StageStatus::Failed => outcome = StageStatus::Blocked,
            "pass" => {}
            _ => {
                return Err(ActionFailure::unchanged(
                    HttpStatus::BadRequest,
                    "A watched observation outcome is not recognized.",
                ));
            }
        }
    }
    Ok(match outcome {
        StageStatus::Passed => (
            outcome,
            "The local companion recorded a complete read-only stage outcome.",
        ),
        StageStatus::Failed => (
            outcome,
            "The watched observation did not match the expected behavior.",
        ),
        _ => (
            StageStatus::Blocked,
            "The watched observation was inconclusive.",
        ),
    })
}

fn device_for_plan<'a>(
    view: &'a QualificationView,
    plan: &crate::QualificationPlan,
) -> Option<&'a crate::DeviceView> {
    view.receivers
        .iter()
        .find(|receiver| {
            receiver.receiver_id == plan.receiver_id && receiver.generation_id == plan.generation_id
        })?
        .devices
        .iter()
        .find(|device| device.device_id == plan.device_id)
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct HttpRequest {
    method: String,
    path: String,
    host: Option<String>,
    origin: Option<String>,
    content_type: Option<String>,
    content_length: usize,
    body: Vec<u8>,
}

fn read_request(stream: &mut TcpStream) -> Result<HttpRequest, HttpStatus> {
    let mut bytes = Vec::with_capacity(1_024);
    let mut chunk = [0_u8; 1_024];
    loop {
        let count = stream
            .read(&mut chunk)
            .map_err(|_| HttpStatus::BadRequest)?;
        if count == 0 {
            return Err(HttpStatus::BadRequest);
        }
        bytes.extend_from_slice(&chunk[..count]);
        if bytes.len() > MAX_REQUEST_BYTES {
            return Err(HttpStatus::RequestTooLarge);
        }
        if bytes.windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
    }
    let header_end = bytes
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or(HttpStatus::BadRequest)?;
    let headers = std::str::from_utf8(&bytes[..header_end]).map_err(|_| HttpStatus::BadRequest)?;
    let mut request = parse_request(headers)?;
    let body_start = header_end + 4;
    let total = body_start
        .checked_add(request.content_length)
        .ok_or(HttpStatus::RequestTooLarge)?;
    if total > MAX_REQUEST_BYTES {
        return Err(HttpStatus::RequestTooLarge);
    }
    while bytes.len() < total {
        let count = stream
            .read(&mut chunk)
            .map_err(|_| HttpStatus::BadRequest)?;
        if count == 0 {
            return Err(HttpStatus::BadRequest);
        }
        bytes.extend_from_slice(&chunk[..count]);
        if bytes.len() > MAX_REQUEST_BYTES {
            return Err(HttpStatus::RequestTooLarge);
        }
    }
    if bytes.len() != total {
        return Err(HttpStatus::BadRequest);
    }
    request.body = bytes[body_start..total].to_vec();
    if request.method == "POST" {
        if request.content_length == 0
            || !request
                .content_type
                .as_deref()
                .is_some_and(|value| value.eq_ignore_ascii_case("application/json"))
        {
            return Err(HttpStatus::UnsupportedMediaType);
        }
    } else if request.content_length != 0 {
        return Err(HttpStatus::BadRequest);
    }
    Ok(request)
}

fn parse_request(request: &str) -> Result<HttpRequest, HttpStatus> {
    let mut lines = request.split("\r\n");
    let mut start = lines.next().ok_or(HttpStatus::BadRequest)?.split(' ');
    let method = start.next().ok_or(HttpStatus::BadRequest)?;
    let path = start.next().ok_or(HttpStatus::BadRequest)?;
    let version = start.next().ok_or(HttpStatus::BadRequest)?;
    if start.next().is_some()
        || method.is_empty()
        || !method.bytes().all(|byte| byte.is_ascii_uppercase())
        || !path.starts_with('/')
        || path.bytes().any(|byte| matches!(byte, b'?' | b'#' | b'\\'))
        || version != "HTTP/1.1"
    {
        return Err(HttpStatus::BadRequest);
    }
    let mut host = None;
    let mut origin = None;
    let mut content_type = None;
    let mut content_length = None;
    for line in lines.take_while(|line| !line.is_empty()) {
        let Some((name, value)) = line.split_once(':') else {
            return Err(HttpStatus::BadRequest);
        };
        let value = value.trim();
        if name.eq_ignore_ascii_case("host") {
            if host.replace(value.to_owned()).is_some() {
                return Err(HttpStatus::BadRequest);
            }
        } else if name.eq_ignore_ascii_case("origin") {
            if origin.replace(value.to_owned()).is_some() {
                return Err(HttpStatus::BadRequest);
            }
        } else if name.eq_ignore_ascii_case("content-type") {
            if content_type.replace(value.to_owned()).is_some() {
                return Err(HttpStatus::BadRequest);
            }
        } else if name.eq_ignore_ascii_case("content-length") {
            let parsed = value.parse::<usize>().map_err(|_| HttpStatus::BadRequest)?;
            if parsed.to_string() != value || content_length.replace(parsed).is_some() {
                return Err(HttpStatus::BadRequest);
            }
        } else if name.eq_ignore_ascii_case("transfer-encoding") {
            return Err(HttpStatus::BadRequest);
        }
    }
    if host.is_none() {
        return Err(HttpStatus::BadRequest);
    }
    Ok(HttpRequest {
        method: method.to_owned(),
        path: path.to_owned(),
        host,
        origin,
        content_type,
        content_length: content_length.unwrap_or(0),
        body: Vec::new(),
    })
}

fn host_is_local(host: Option<&str>, port: u16) -> bool {
    let expected_ip = format!("127.0.0.1:{port}");
    let expected_name = format!("localhost:{port}");
    host.is_some_and(|value| value == expected_ip || value == expected_name)
}

fn origin_is_local(origin: Option<&str>, port: u16) -> bool {
    origin.is_none_or(|value| {
        value == format!("http://127.0.0.1:{port}") || value == format!("http://localhost:{port}")
    })
}

fn resolve_asset(asset_root: &Path, request_path: &str) -> Option<PathBuf> {
    let relative = if request_path == "/" {
        Path::new("index.html")
    } else {
        Path::new(request_path.strip_prefix('/')?)
    };
    if relative
        .components()
        .any(|component| !matches!(component, Component::Normal(_)))
    {
        return None;
    }
    let candidate = asset_root.join(relative);
    let canonical = fs::canonicalize(candidate).ok()?;
    canonical.starts_with(asset_root).then_some(canonical)
}

fn content_type(path: &Path) -> &'static str {
    match path.extension().and_then(|extension| extension.to_str()) {
        Some("html") => "text/html; charset=utf-8",
        Some("js") => "text/javascript; charset=utf-8",
        Some("css") => "text/css; charset=utf-8",
        Some("svg") => "image/svg+xml",
        Some("png") => "image/png",
        Some("webp") => "image/webp",
        Some("woff2") => "font/woff2",
        _ => "application/octet-stream",
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CachePolicy {
    NoStore,
    NoCache,
}

impl CachePolicy {
    const fn header(self) -> &'static str {
        match self {
            Self::NoStore => "no-store",
            Self::NoCache => "no-cache",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum HttpStatus {
    Ok,
    BadRequest,
    Forbidden,
    NotFound,
    MethodNotAllowed,
    Conflict,
    RequestTooLarge,
    UnsupportedMediaType,
}

impl HttpStatus {
    const fn line(self) -> &'static str {
        match self {
            Self::Ok => "200 OK",
            Self::BadRequest => "400 Bad Request",
            Self::Forbidden => "403 Forbidden",
            Self::NotFound => "404 Not Found",
            Self::MethodNotAllowed => "405 Method Not Allowed",
            Self::Conflict => "409 Conflict",
            Self::RequestTooLarge => "413 Content Too Large",
            Self::UnsupportedMediaType => "415 Unsupported Media Type",
        }
    }
}

fn write_error(stream: &mut TcpStream, status: HttpStatus) -> io::Result<()> {
    let body = format!("{{\"error\":\"{}\"}}", status.line());
    write_response(
        stream,
        status,
        "application/json; charset=utf-8",
        body.as_bytes(),
        CachePolicy::NoStore,
        false,
    )
}

fn write_json_error(stream: &mut TcpStream, status: HttpStatus, message: &str) -> io::Result<()> {
    let body =
        serde_json::to_vec(&serde_json::json!({ "error": message })).map_err(io::Error::other)?;
    write_response(
        stream,
        status,
        "application/json; charset=utf-8",
        &body,
        CachePolicy::NoStore,
        false,
    )
}

fn write_response(
    stream: &mut TcpStream,
    status: HttpStatus,
    content_type: &str,
    body: &[u8],
    cache: CachePolicy,
    head_only: bool,
) -> io::Result<()> {
    write!(
        stream,
        "HTTP/1.1 {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nCache-Control: {}\r\nContent-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'\r\nCross-Origin-Opener-Policy: same-origin\r\nCross-Origin-Resource-Policy: same-origin\r\nPermissions-Policy: bluetooth=(), camera=(), geolocation=(), microphone=(), payment=(), serial=(), usb=()\r\nReferrer-Policy: no-referrer\r\nX-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\nConnection: close\r\n\r\n",
        status.line(),
        content_type,
        body.len(),
        cache.header(),
    )?;
    if !head_only {
        stream.write_all(body)?;
    }
    stream.flush()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn qualification_fixture(name: &str) -> QualificationView {
        let fixture = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../apps/device-qualification/tests/fixtures")
            .join(name);
        serde_json::from_slice(&fs::read(fixture).expect("fixture reads")).expect("fixture parses")
    }

    fn pending_transaction() -> crate::qualification_runner::PendingLightingTransaction {
        crate::qualification_runner::PendingLightingTransaction {
            stage_id: "mouse-test-lighting".to_owned(),
            transaction_id: hfx_domain::TransactionId::try_from("qualification-test")
                .expect("transaction id"),
            step: crate::qualification_runner::LightingStep::LightingBlue,
            prior_progress: None,
        }
    }

    #[test]
    fn parser_accepts_only_bounded_http11_shapes() {
        let request =
            parse_request("GET /v1/qualification/view HTTP/1.1\r\nHost: 127.0.0.1:47821\r\n\r\n")
                .expect("request is valid");
        assert_eq!(request.method, "GET");
        assert_eq!(request.path, "/v1/qualification/view");
        let post = parse_request(
            "POST /v1/qualification/actions/run HTTP/1.1\r\nHost: 127.0.0.1:47821\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n",
        )
        .expect("post headers are valid");
        assert_eq!(post.content_length, 2);
        assert!(parse_request("GET / HTTP/1.0\r\nHost: localhost\r\n\r\n").is_err());
    }

    #[test]
    fn local_authority_checks_reject_remote_origins() {
        assert!(host_is_local(Some("127.0.0.1:47821"), 47_821));
        assert!(host_is_local(Some("localhost:47821"), 47_821));
        assert!(!host_is_local(Some("example.com"), 47_821));
        assert!(origin_is_local(None, 47_821));
        assert!(!origin_is_local(Some("https://example.com"), 47_821));
    }

    #[test]
    fn asset_resolution_stays_beneath_the_canonical_root() {
        let root = std::env::temp_dir().join(format!(
            "hyperflux-qualification-assets-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("assets")).expect("asset directory creates");
        fs::write(root.join("index.html"), "index").expect("index writes");
        fs::write(root.join("assets/app.js"), "app").expect("asset writes");
        let canonical = canonical_asset_root(&root).expect("root is valid");
        assert_eq!(
            resolve_asset(&canonical, "/assets/app.js"),
            Some(canonical.join("assets/app.js"))
        );
        assert_eq!(resolve_asset(&canonical, "/../secret"), None);
        fs::remove_dir_all(&root).expect("test root removes");
    }

    #[test]
    fn failed_final_commit_keeps_the_pending_transaction_for_reconciliation() {
        let parent = std::env::temp_dir().join(format!(
            "hyperflux-qualification-invalid-state-parent-{}",
            std::process::id()
        ));
        let _ = fs::remove_file(&parent);
        fs::write(&parent, "not a directory").expect("blocking parent writes");
        let pending = pending_transaction();
        let mut state = ServerState {
            pending_transaction: Some(pending.clone()),
            ..ServerState::default()
        };
        assert!(commit_completed_state(&parent.join("session.json"), &mut state).is_err());
        assert_eq!(state.pending_transaction, Some(pending));
        fs::remove_file(parent).expect("blocking parent removes");
    }

    #[test]
    fn failed_unsent_restore_keeps_the_pending_gate_in_memory() {
        let pending = pending_transaction();
        let mut state = ServerState {
            pending_transaction: Some(pending.clone()),
            results: BTreeMap::from([(
                pending.stage_id.clone(),
                StageProgress {
                    status: StageStatus::Running,
                    result: None,
                    checkpoint: None,
                },
            )]),
            ..ServerState::default()
        };
        restore_prior_progress(&mut state, &pending.stage_id, None);
        restore_pending_progress(&mut state, pending.clone());
        assert_eq!(state.pending_transaction, Some(pending.clone()));
        assert_eq!(
            state.results[&pending.stage_id].status,
            StageStatus::Running
        );
    }

    #[test]
    fn identity_change_invalidates_claims_but_preserves_uncertain_side_effects() {
        let pending = pending_transaction();
        let mut state = ServerState {
            identity_fingerprint: Some([1_u8; 32]),
            run_id: Some("run-old-generation".to_owned()),
            results: BTreeMap::from([(
                "mouse-test-profile".to_owned(),
                StageProgress {
                    status: StageStatus::Passed,
                    result: None,
                    checkpoint: None,
                },
            )]),
            pending_transaction: Some(pending.clone()),
            hardware_write_executed: true,
            ..ServerState::default()
        };
        assert!(refresh_identity_binding(
            &mut state,
            [2_u8; 32],
            &qualification_fixture("rust-qualified-mouse.json"),
        ));
        assert_eq!(state.results.len(), 1);
        assert_eq!(
            state.results["mouse-test-lighting"].status,
            StageStatus::Running
        );
        assert!(state.run_id.is_none());
        assert_eq!(state.pending_transaction, Some(pending));
        assert!(state.hardware_write_executed);
    }

    #[test]
    fn identity_fingerprint_ignores_inventory_order() {
        let mut view = qualification_fixture("rust-qualified-pair.json");
        let mut second = view.receivers[0].clone();
        second.receiver_id = "receiver-second".to_owned();
        second.generation_id = 11;
        view.receivers.push(second);
        let expected = identity_fingerprint(&view).expect("fingerprint computes");
        view.receivers.reverse();
        for receiver in &mut view.receivers {
            receiver.devices.reverse();
        }
        assert_eq!(
            identity_fingerprint(&view).expect("reordered fingerprint computes"),
            expected
        );
    }

    #[test]
    fn lifecycle_binding_accepts_only_its_expected_profile_bound_generation() {
        let mut view = qualification_fixture("rust-qualified-pair.json");
        for group in view.plans.iter_mut().flat_map(|plan| &mut plan.groups) {
            for stage in &mut group.stages {
                stage.status = if group.group_id == "lifecycle" {
                    StageStatus::Ready
                } else {
                    StageStatus::Passed
                };
            }
        }
        let lifecycle_stage = view.plans[0]
            .groups
            .iter()
            .find(|group| group.group_id == "lifecycle")
            .expect("lifecycle group")
            .stages[0]
            .clone();
        let mut run = capture_lifecycle_run(&view, &lifecycle_stage).expect("run captures");
        run.active = Some(crate::qualification_lifecycle::LifecycleStageState {
            stage_id: lifecycle_stage.stage_id.clone(),
            device_id: view.plans[0].device_id.clone(),
            device_kind: "mouse".to_owned(),
            phase: crate::qualification_lifecycle::LifecyclePhase::ReceiverReturn,
            baseline_generation_id: 7,
            evidence_refs: vec!["receiver-generation:7".to_owned()],
            suspend_stamp: None,
        });
        run.validate().expect("run is valid");
        let mut session = ServerState {
            identity_fingerprint: Some(identity_fingerprint(&view).expect("fingerprint")),
            run_id: Some("run-generation-7".to_owned()),
            results: BTreeMap::from([(
                lifecycle_stage.stage_id.clone(),
                StageProgress {
                    status: StageStatus::AwaitingObservation,
                    result: None,
                    checkpoint: None,
                },
            )]),
            lifecycle_run: Some(run),
            ..ServerState::default()
        };

        view.receivers[0].generation_id = 8;
        let renewed = identity_fingerprint(&view).expect("renewed fingerprint");
        assert!(refresh_identity_binding(&mut session, renewed, &view));
        assert!(session.lifecycle_run.is_some());
        assert!(!session.results.is_empty());

        view.receivers[0].devices[0]
            .profile
            .as_mut()
            .expect("profile exists")
            .digest = "0".repeat(64);
        let foreign = identity_fingerprint(&view).expect("foreign fingerprint");
        assert!(refresh_identity_binding(&mut session, foreign, &view));
        assert!(session.lifecycle_run.is_none());
        assert!(session.results.is_empty());
        assert!(session.run_id.is_none());
    }

    #[test]
    fn server_upgrades_a_measured_terminal_resume_failure_once() {
        let mut view = qualification_fixture("rust-qualified-pair.json");
        for group in view.plans.iter_mut().flat_map(|plan| &mut plan.groups) {
            for stage in &mut group.stages {
                stage.status = if group.group_id == "lifecycle" {
                    StageStatus::Ready
                } else {
                    StageStatus::Passed
                };
            }
        }
        let stage = view
            .plans
            .iter()
            .find(|plan| plan.device_id == "keyboard-test")
            .expect("keyboard plan exists")
            .groups
            .iter()
            .flat_map(|group| &group.stages)
            .find(|stage| stage.stage_id.ends_with("-lifecycle"))
            .expect("keyboard lifecycle exists")
            .clone();
        let mut run = capture_lifecycle_run(&view, &stage).expect("run captures");
        run.active = None;
        let mut session = ServerState {
            results: BTreeMap::from([(
                stage.stage_id.clone(),
                StageProgress {
                    status: StageStatus::Failed,
                    result: Some(StageResult {
                        summary: "A watched lifecycle result did not match.".to_owned(),
                        completed_at: "2026-07-23T11:50:45Z".to_owned(),
                        evidence_refs: vec![
                            "system-suspend:authorized".to_owned(),
                            "system-suspend-gap-ms:9496".to_owned(),
                        ],
                    }),
                    checkpoint: None,
                },
            )]),
            lifecycle_run: Some(run),
            ..ServerState::default()
        };

        assert!(recover_retryable_resume_state(&mut session).expect("state upgrades"));
        assert!(!recover_retryable_resume_state(&mut session).expect("upgrade is idempotent"));
        let progress = &session.results[&stage.stage_id];
        assert_eq!(progress.status, StageStatus::Failed);
        assert_eq!(
            progress
                .checkpoint
                .as_ref()
                .expect("retry checkpoint exists")
                .checkpoint_id,
            "system-suspend-authorization"
        );
    }

    #[test]
    fn transient_telemetry_gap_remains_retryable() {
        let mut view = qualification_fixture("rust-qualified-pair.json");
        let stage = view
            .plans
            .iter()
            .find(|plan| plan.device_id == "keyboard-test")
            .expect("keyboard plan exists")
            .groups
            .iter()
            .flat_map(|group| &group.stages)
            .find(|stage| stage.stage_id.ends_with("-telemetry"))
            .expect("telemetry stage exists")
            .clone();
        let keyboard = view
            .receivers
            .iter_mut()
            .flat_map(|receiver| &mut receiver.devices)
            .find(|device| device.device_id == "keyboard-test")
            .expect("keyboard device exists");
        keyboard.battery.availability = BatteryAvailability::Unknown;
        keyboard.battery.percentage = None;
        let input = ActionInput {
            view_revision: 1,
            confirmation: None,
            observations: BTreeMap::from([("controls".to_owned(), "pass".to_owned())]),
        };
        let mut session = ServerState::default();

        let failure = record_read_only_result(&view, &stage, &input, &mut session)
            .expect_err("missing telemetry remains retryable");
        assert!(matches!(failure.status, HttpStatus::Conflict));
        assert!(!failure.state_changed);
        assert!(failure.message.contains("refresh, and retry"));
        assert!(session.results.is_empty());

        let keyboard = view
            .receivers
            .iter_mut()
            .flat_map(|receiver| &mut receiver.devices)
            .find(|device| device.device_id == "keyboard-test")
            .expect("keyboard device exists");
        keyboard.battery.availability = BatteryAvailability::Reported;
        keyboard.battery.percentage = Some(20);
        assert!(record_read_only_result(&view, &stage, &input, &mut session).is_ok());
        assert_eq!(session.results[&stage.stage_id].status, StageStatus::Passed);
    }

    #[test]
    fn malformed_read_only_observation_does_not_poison_the_run() {
        let view = qualification_fixture("rust-qualified-pair.json");
        let stage = view
            .plans
            .iter()
            .find(|plan| plan.device_id == "keyboard-test")
            .expect("keyboard plan exists")
            .groups
            .iter()
            .flat_map(|group| &group.stages)
            .find(|stage| stage.stage_id.ends_with("-telemetry"))
            .expect("telemetry stage exists")
            .clone();
        let input = ActionInput {
            view_revision: 1,
            confirmation: None,
            observations: BTreeMap::new(),
        };
        let mut session = ServerState::default();

        let failure = record_read_only_result(&view, &stage, &input, &mut session)
            .expect_err("missing answer is rejected");
        assert!(matches!(failure.status, HttpStatus::BadRequest));
        assert!(!failure.state_changed);
        assert!(session.results.is_empty());
    }

    #[test]
    fn watched_answers_are_exact_and_reduce_to_one_verdict() {
        let stage = QualificationStage {
            stage_id: "mouse-test-lighting".to_owned(),
            title: "Lighting".to_owned(),
            description: "Lighting".to_owned(),
            kind: crate::StageKind::WatchedObservation,
            risk: RiskLevel::LightingWrite,
            status: StageStatus::AwaitingObservation,
            capabilities: Vec::new(),
            instructions: Vec::new(),
            observations: vec![crate::ObservationPrompt {
                id: "visual".to_owned(),
                prompt: "Does it match?".to_owned(),
                choices: vec![
                    crate::ObservationChoice {
                        id: "pass".to_owned(),
                        label: "Matches".to_owned(),
                        outcome: "pass".to_owned(),
                    },
                    crate::ObservationChoice {
                        id: "fail".to_owned(),
                        label: "Does not match".to_owned(),
                        outcome: "fail".to_owned(),
                    },
                    crate::ObservationChoice {
                        id: "unclear".to_owned(),
                        label: "Unclear".to_owned(),
                        outcome: "unclear".to_owned(),
                    },
                ],
            }],
            action_id: Some("run:mouse-test-lighting".to_owned()),
            result: None,
        };
        let input = |answer: &str| ActionInput {
            view_revision: 1,
            confirmation: None,
            observations: BTreeMap::from([("visual".to_owned(), answer.to_owned())]),
        };
        assert_eq!(
            observation_verdict(&stage, &input("pass")),
            Ok(ObservationVerdict::Pass)
        );
        assert_eq!(
            observation_verdict(&stage, &input("fail")),
            Ok(ObservationVerdict::Fail)
        );
        assert_eq!(
            observation_verdict(&stage, &input("unclear")),
            Ok(ObservationVerdict::Unclear)
        );
        assert!(observation_verdict(&stage, &input("typo")).is_err());
        assert!(
            observation_verdict(
                &stage,
                &ActionInput {
                    view_revision: 1,
                    confirmation: None,
                    observations: BTreeMap::new(),
                }
            )
            .is_err()
        );
    }
}
