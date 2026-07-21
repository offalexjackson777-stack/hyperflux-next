// SPDX-License-Identifier: GPL-2.0-only

use hfx_ops::{
    ActivationAction, RealSystemProbe, ServiceState, SystemController, SystemProbe, UpdateIntent,
    confirm_configuration, decide_post_update, load_update_intent, migrate_configuration,
    record_update_intent, remove_update_intent,
};
use hfx_runtime::{BRIDGE_CONFIGURATION_FILE_PATH, UPDATE_STATE_PATH};
use std::env;
use std::path::Path;
use std::thread;
use std::time::Duration;

fn usage() -> &'static str {
    "Usage: hyperflux-next-activate <fresh-install|pre-update|post-update|pre-remove>\n"
}

fn wait_for_bridge(probe: &RealSystemProbe) -> bool {
    for _ in 0..50 {
        let snapshot = probe.snapshot();
        if snapshot.service_state == ServiceState::Active && snapshot.bridge.is_some() {
            return true;
        }
        thread::sleep(Duration::from_millis(100));
    }
    false
}

fn fresh_install(probe: &RealSystemProbe) -> Result<(), String> {
    migrate_configuration(Path::new(BRIDGE_CONFIGURATION_FILE_PATH))
        .map_err(|error| format!("configuration setup failed: {error}"))?;
    record_update_intent(Path::new(UPDATE_STATE_PATH), &UpdateIntent::fresh_install())
        .map_err(|error| format!("fresh-install state failed: {error}"))?;
    let _ = probe;
    println!("HyperFlux Next is queued for conservative read-only activation.");
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
    )
    .map_err(|error| format!("post-update decision failed: {error}"))?;
    match decision.action {
        ActivationAction::EnableBridge => probe
            .enable_bridge()
            .map_err(|error| format!("bridge enable failed: {error}"))?,
        ActivationAction::ResumeBridge => probe
            .restart_bridge()
            .map_err(|error| format!("bridge restart failed: {error}"))?,
        ActivationAction::LeaveBridgeStopped => {}
        ActivationAction::ActivateDriver => {
            println!("HyperFlux Next driver activation is required.");
            println!(
                "Run hyperfluxctl doctor for the reboot or receiver-disconnect activation path."
            );
            remove_update_intent(Path::new(UPDATE_STATE_PATH))
                .map_err(|error| format!("post-update cleanup failed: {error}"))?;
            return Ok(());
        }
    }
    if matches!(
        decision.action,
        ActivationAction::EnableBridge | ActivationAction::ResumeBridge
    ) {
        if !wait_for_bridge(probe) {
            return Err("the bridge did not become ready after compatible activation".to_owned());
        }
        confirm_configuration(Path::new(BRIDGE_CONFIGURATION_FILE_PATH))
            .map_err(|error| format!("configuration confirmation failed: {error}"))?;
    }
    remove_update_intent(Path::new(UPDATE_STATE_PATH))
        .map_err(|error| format!("post-update cleanup failed: {error}"))?;
    println!("HyperFlux Next post-update activation completed.");
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
