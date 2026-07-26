// SPDX-License-Identifier: GPL-2.0-only

use hfx_integration_model::OPENRAZER_NATIVE_VERIFIED_UPSTREAM_VERSION;
use hfx_ops::{
    ActivationAction, OpenRazerDriverState, OpenRazerRuntimeState, RealSystemProbe, ServiceState,
    SystemController, SystemProbe, UpdateIntent, confirm_configuration, decide_post_update,
    load_update_intent, migrate_configuration, record_update_intent, remove_update_intent,
};
use hfx_runtime::{BRIDGE_CONFIGURATION_FILE_PATH, UPDATE_STATE_PATH};
use std::env;
use std::path::Path;
use std::thread;
use std::time::Duration;

fn usage() -> &'static str {
    "Usage: hyperflux-next-activate <fresh-install|pre-update|post-update|prepare-start|check-openrazer-driver|check-openrazer-runtime|reconcile-openrazer-runtime|report-openrazer-runtime|confirm-start|pre-remove>\n"
}

fn ensure_openrazer_driver(snapshot: &hfx_ops::SystemSnapshot) -> Result<(), String> {
    match snapshot.openrazer_driver.state() {
        OpenRazerDriverState::Ready => Ok(()),
        OpenRazerDriverState::ActivationPending => Err(
            "an older OpenRazer route module is active; reboot before starting the native relay"
                .to_owned(),
        ),
        OpenRazerDriverState::CapabilityUnavailable => {
            let module = snapshot
                .openrazer_driver
                .blocking_module()
                .map_or("unknown", |value| value.module_name.as_str());
            Err(format!(
                "the installed OpenRazer route module {module} lacks the HyperFlux native transport capability"
            ))
        }
        OpenRazerDriverState::Unavailable => {
            let module = snapshot
                .openrazer_driver
                .blocking_module()
                .map_or("unknown", |value| value.module_name.as_str());
            Err(format!(
                "the installed OpenRazer route module {module} is unavailable"
            ))
        }
    }
}

fn ensure_openrazer_runtime(snapshot: &hfx_ops::SystemSnapshot) -> Result<(), String> {
    match snapshot.openrazer_runtime.state {
        OpenRazerRuntimeState::Verified => Ok(()),
        OpenRazerRuntimeState::NotInstalled => {
            Err("the verified OpenRazer runtime is not installed".to_owned())
        }
        OpenRazerRuntimeState::ReverificationRequired => Err(format!(
            "the installed OpenRazer runtime is outside verified source line {OPENRAZER_NATIVE_VERIFIED_UPSTREAM_VERSION}; rebase and reverification are required"
        )),
        OpenRazerRuntimeState::Unverified => Err(
            "the installed OpenRazer runtime source line could not be verified; rebase and reverification are required"
                .to_owned(),
        ),
    }
}

fn report_openrazer_runtime(probe: &RealSystemProbe) {
    let snapshot = probe.snapshot();
    match snapshot.openrazer_runtime.state {
        OpenRazerRuntimeState::Verified => println!(
            "HyperFlux Next: OpenRazer remains on verified upstream source line {OPENRAZER_NATIVE_VERIFIED_UPSTREAM_VERSION}; package release changes are compatible."
        ),
        OpenRazerRuntimeState::NotInstalled => {
            println!(
                "HyperFlux Next: OpenRazer is not installed; no native-route action is required."
            );
        }
        OpenRazerRuntimeState::ReverificationRequired => {
            let observed = snapshot
                .openrazer_runtime
                .observed_upstream_version
                .as_deref()
                .unwrap_or("unavailable");
            println!(
                "HyperFlux Next: OpenRazer {observed} requires a native-route rebase and reverification; the relay will be skipped on its next start."
            );
            println!("Run hyperfluxctl doctor for the verified source line and safe action.");
        }
        OpenRazerRuntimeState::Unverified => {
            println!(
                "HyperFlux Next: the OpenRazer source line could not be verified; the relay will be skipped on its next start."
            );
            println!("Run hyperfluxctl doctor for the safe action.");
        }
    }
}

fn reconcile_openrazer_runtime(probe: &RealSystemProbe) -> Result<(), String> {
    report_openrazer_runtime(probe);
    probe
        .reconcile_openrazer_relay()
        .map_err(|error| format!("OpenRazer relay reconciliation failed: {error}"))
}

fn wait_for_bridge_and_confirmation(probe: &RealSystemProbe) -> bool {
    for _ in 0..50 {
        let snapshot = probe.snapshot();
        if snapshot.service_state == ServiceState::Active
            && snapshot.bridge.is_some()
            && !Path::new(UPDATE_STATE_PATH).exists()
        {
            return true;
        }
        thread::sleep(Duration::from_millis(100));
    }
    false
}

fn fresh_install(probe: &RealSystemProbe) -> Result<(), String> {
    migrate_configuration(Path::new(BRIDGE_CONFIGURATION_FILE_PATH))
        .map_err(|error| format!("configuration setup failed: {error}"))?;
    confirm_configuration(Path::new(BRIDGE_CONFIGURATION_FILE_PATH))
        .map_err(|error| format!("configuration confirmation failed: {error}"))?;
    let _ = probe;
    println!("HyperFlux Next installed with conservative read-only defaults.");
    println!("The bridge remains disabled until the user explicitly enables it.");
    println!("OpenRazer and Polychromatic use the standard openrazer access group.");
    println!("For the desktop user, run: sudo gpasswd -a \"$USER\" openrazer");
    println!("Then sign out and back in once so the session receives the group.");
    Ok(())
}

fn pre_update(probe: &RealSystemProbe) -> Result<(), String> {
    let snapshot = probe.snapshot();
    let active = snapshot.service_state == ServiceState::Active;
    let intent = UpdateIntent::new(active, snapshot.loaded_module_identity);
    record_update_intent(Path::new(UPDATE_STATE_PATH), &intent)
        .map_err(|error| format!("pre-update state failed: {error}"))?;
    if active {
        probe
            .stop_bridge()
            .map_err(|error| format!("bridge stop failed: {error}"))?;
    }
    println!("HyperFlux Next preserved the pre-update service state.");
    Ok(())
}

fn post_update(probe: &RealSystemProbe) -> Result<(), String> {
    let intent = load_update_intent(Path::new(UPDATE_STATE_PATH))
        .map_err(|error| format!("post-update state failed: {error}"))?;
    let snapshot = probe.snapshot();
    let decision = decide_post_update(
        intent.as_ref(),
        snapshot.installed_module_identity.as_deref(),
        snapshot.loaded_module_identity.as_deref(),
        snapshot.openrazer_driver.state(),
    )
    .map_err(|error| format!("post-update decision failed: {error}"))?;
    match decision.action {
        ActivationAction::ResumeBridge => probe
            .restart_bridge()
            .map_err(|error| format!("bridge restart failed: {error}"))?,
        ActivationAction::LeaveBridgeStopped => {}
        ActivationAction::ActivateDriver => {
            println!("HyperFlux Next driver activation is required.");
            println!(
                "Run hyperfluxctl doctor for the reboot or receiver-disconnect activation path."
            );
            return Ok(());
        }
        ActivationAction::ActivateOpenRazerDriver => {
            println!("OpenRazer native-route driver activation is required.");
            println!("Reboot, then run hyperfluxctl doctor before starting the bridge.");
            return Ok(());
        }
        ActivationAction::RepairOpenRazerDriver => {
            println!("The OpenRazer native-route driver installation is incomplete.");
            println!("Run hyperfluxctl doctor for the exact missing module and safe action.");
            return Ok(());
        }
    }
    if matches!(decision.action, ActivationAction::ResumeBridge)
        && !wait_for_bridge_and_confirmation(probe)
    {
        return Err("the bridge did not become ready after compatible activation".to_owned());
    }
    println!("HyperFlux Next post-update activation completed.");
    Ok(())
}

fn prepare_start(probe: &RealSystemProbe) -> Result<(), String> {
    load_update_intent(Path::new(UPDATE_STATE_PATH))
        .map_err(|error| format!("start preparation state failed: {error}"))?;
    let snapshot = probe.snapshot();
    let installed = snapshot
        .installed_module_identity
        .as_deref()
        .ok_or_else(|| "the installed kernel module is unavailable".to_owned())?;
    if snapshot
        .loaded_module_identity
        .as_deref()
        .is_some_and(|loaded| loaded != installed)
    {
        return Err(
            "a newer installed kernel module must be activated before the bridge starts".to_owned(),
        );
    }
    ensure_openrazer_driver(&snapshot)?;
    migrate_configuration(Path::new(BRIDGE_CONFIGURATION_FILE_PATH))
        .map_err(|error| format!("configuration migration failed: {error}"))?;
    println!("HyperFlux Next start preparation completed.");
    Ok(())
}

fn check_openrazer_driver(probe: &RealSystemProbe) -> Result<(), String> {
    ensure_openrazer_driver(&probe.snapshot())
}

fn check_openrazer_runtime(probe: &RealSystemProbe) -> Result<(), String> {
    ensure_openrazer_runtime(&probe.snapshot())
}

fn confirm_start(_probe: &RealSystemProbe) -> Result<(), String> {
    confirm_configuration(Path::new(BRIDGE_CONFIGURATION_FILE_PATH))
        .map_err(|error| format!("configuration confirmation failed: {error}"))?;
    remove_update_intent(Path::new(UPDATE_STATE_PATH))
        .map_err(|error| format!("update-state confirmation failed: {error}"))?;
    println!("HyperFlux Next start confirmation completed.");
    Ok(())
}

fn pre_remove(probe: &RealSystemProbe) -> Result<(), String> {
    if probe.snapshot().service_state == ServiceState::Active {
        probe
            .stop_bridge()
            .map_err(|error| format!("bridge stop failed: {error}"))?;
    }
    remove_update_intent(Path::new(UPDATE_STATE_PATH))
        .map_err(|error| format!("remove cleanup failed: {error}"))?;
    Ok(())
}

fn run() -> Result<(), String> {
    let mut arguments = env::args().skip(1);
    let command = arguments.next().ok_or_else(|| usage().to_owned())?;
    if arguments.next().is_some() {
        return Err(usage().to_owned());
    }
    let probe = RealSystemProbe::default();
    match command.as_str() {
        "fresh-install" => fresh_install(&probe),
        "pre-update" => pre_update(&probe),
        "post-update" => post_update(&probe),
        "prepare-start" => prepare_start(&probe),
        "check-openrazer-driver" => check_openrazer_driver(&probe),
        "check-openrazer-runtime" => check_openrazer_runtime(&probe),
        "reconcile-openrazer-runtime" => reconcile_openrazer_runtime(&probe),
        "report-openrazer-runtime" => {
            report_openrazer_runtime(&probe);
            Ok(())
        }
        "confirm-start" => confirm_start(&probe),
        "pre-remove" => pre_remove(&probe),
        _ => Err(usage().to_owned()),
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("hyperflux-next-activate: {error}");
        std::process::exit(1);
    }
}
