// SPDX-License-Identifier: GPL-2.0-only

use crate::qualification::{StageProgress, apply_stage_results};
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

#[derive(Debug, Default)]
struct ServerState {
    revision: u64,
    identity_fingerprint: Option<[u8; 32]>,
    run_id: Option<String>,
    results: BTreeMap<String, StageProgress>,
}

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
}

impl Default for QualificationServerConfig {
    fn default() -> Self {
        Self {
            assets: PathBuf::from("/usr/share/hyperflux-next/qualification-console"),
            port: DEFAULT_PORT,
        }
    }
}

#[derive(Debug)]
pub enum QualificationServerError {
    AssetRootInvalid,
    CatalogInvalid,
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
            Self::BindFailed(_) => "the local qualification endpoint could not be opened",
            Self::ConnectionFailed(_) => "the local qualification endpoint stopped unexpectedly",
        })
    }
}

impl std::error::Error for QualificationServerError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::BindFailed(error) | Self::ConnectionFailed(error) => Some(error),
            Self::AssetRootInvalid | Self::CatalogInvalid => None,
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
    let probe = RealSystemProbe::default();
    let mut state = ServerState::default();
    for connection in listener.incoming() {
        let mut stream = connection.map_err(QualificationServerError::ConnectionFailed)?;
        let _ = handle_connection(
            &mut stream,
            &asset_root,
            config.port,
            &probe,
            &catalog,
            &mut state,
        );
    }
    Ok(())
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
        let view = live_view(probe, catalog, state)?;
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
        return invoke_action(stream, &request, probe, catalog, state);
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
            ..RunnerCapabilities::default()
        },
        state.revision.max(1),
        qualification_generated_at(),
    );
    let fingerprint = identity_fingerprint(&view)?;
    match state.identity_fingerprint {
        None => {
            state.revision = 1;
            state.identity_fingerprint = Some(fingerprint);
        }
        Some(previous) if previous != fingerprint => {
            state.revision = state.revision.saturating_add(1);
            state.identity_fingerprint = Some(fingerprint);
            state.run_id = None;
            state.results.clear();
        }
        Some(_) => {}
    }
    view.view_revision = state.revision;
    apply_stage_results(&mut view, &state.results, state.run_id.as_deref());
    Ok(view)
}

fn identity_fingerprint(view: &QualificationView) -> io::Result<[u8; 32]> {
    let receivers = view
        .receivers
        .iter()
        .map(|receiver| ReceiverIdentityFingerprint {
            receiver_id: &receiver.receiver_id,
            generation_id: receiver.generation_id,
            profile: receiver
                .profile
                .as_ref()
                .map(|profile| (profile.id.as_str(), profile.digest.as_str())),
            devices: receiver
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
                .collect(),
        })
        .collect();
    let encoded =
        serde_json::to_vec(&IdentityFingerprint { receivers }).map_err(io::Error::other)?;
    Ok(Sha256::digest(encoded).into())
}

fn invoke_action(
    stream: &mut TcpStream,
    request: &HttpRequest,
    probe: &RealSystemProbe,
    catalog: &RuntimeProfileCatalog,
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
    let view = live_view(probe, catalog, state)?;
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
    if action.risk != RiskLevel::ReadOnly || selected_stage.risk != RiskLevel::ReadOnly {
        return write_json_error(
            stream,
            HttpStatus::NotImplemented,
            "The supervised hardware runner is not installed; no hardware write was attempted.",
        );
    }
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
    let (status, summary) = read_only_outcome(&view, selected_stage, &input);
    let completed_at = qualification_generated_at();
    if state.run_id.is_none() {
        let timestamp = completed_at
            .chars()
            .filter(|character| !matches!(character, ':' | '-'))
            .collect::<String>();
        state.run_id = Some(format!("local-{timestamp}"));
    }
    state.results.insert(
        selected_stage.stage_id.clone(),
        StageProgress {
            status,
            result: StageResult {
                summary: summary.to_owned(),
                completed_at,
                evidence_refs: vec![format!("local-read-only:{}", selected_stage.stage_id)],
            },
        },
    );
    state.revision = state.revision.saturating_add(1);
    let response = live_view(probe, catalog, state)?;
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

fn read_only_outcome<'a>(
    view: &'a QualificationView,
    stage: &QualificationStage,
    input: &ActionInput,
) -> (StageStatus, &'a str) {
    if stage.stage_id.ends_with("-telemetry") {
        let device = view.plans.iter().find_map(|plan| {
            plan.groups
                .iter()
                .flat_map(|group| &group.stages)
                .any(|candidate| candidate.stage_id == stage.stage_id)
                .then(|| {
                    view.receivers
                        .iter()
                        .flat_map(|receiver| &receiver.devices)
                        .find(|device| device.device_id == plan.device_id)
                })
                .flatten()
        });
        let Some(device) = device else {
            return (
                StageStatus::Blocked,
                "The selected device is no longer present.",
            );
        };
        if device.presence != PresenceView::Active {
            return (
                StageStatus::Blocked,
                "Fresh active-device evidence was not available after the exercise step.",
            );
        }
        let battery_claimed = stage
            .capabilities
            .iter()
            .any(|capability| capability == "telemetry.battery-percent");
        if battery_claimed && !matches!(device.battery.availability, BatteryAvailability::Reported)
        {
            return (
                StageStatus::Blocked,
                "The profile claims battery telemetry, but no fresh value was available.",
            );
        }
    }
    let mut outcome = StageStatus::Passed;
    for prompt in &stage.observations {
        let Some(answer) = input.observations.get(&prompt.id) else {
            return (
                StageStatus::Blocked,
                "A required watched observation was not answered.",
            );
        };
        let Some(choice) = prompt.choices.iter().find(|choice| choice.id == *answer) else {
            return (
                StageStatus::Blocked,
                "A watched observation used an answer outside the declared choices.",
            );
        };
        match choice.outcome.as_str() {
            "fail" => outcome = StageStatus::Failed,
            "unclear" if outcome != StageStatus::Failed => outcome = StageStatus::Blocked,
            "pass" => {}
            _ => {
                return (
                    StageStatus::Blocked,
                    "A watched observation outcome is not recognized.",
                );
            }
        }
    }
    match outcome {
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
    }
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
    NotImplemented,
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
            Self::NotImplemented => "501 Not Implemented",
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
}
