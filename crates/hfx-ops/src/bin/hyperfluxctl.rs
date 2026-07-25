// SPDX-License-Identifier: GPL-2.0-only

use hfx_ops::{
    AssessmentState, DriverState, QualificationConfigurationOutcome, QualificationServerConfig,
    RealSystemProbe, RunnerCapabilities, SystemController, SystemProbe, assess_system,
    build_native_route_observations, build_qualification_view, build_support_bundle,
    default_native_observation_directory, enable_qualification_configuration,
    preview_support_bundle, qualification_generated_at, render_doctor_text, render_status_text,
    restore_qualification_configuration, serve_qualification_console, suggested_support_name,
    write_native_route_observations, write_support_bundle,
};
use hfx_profiles::RuntimeProfileCatalog;
use hfx_runtime::BRIDGE_CONFIGURATION_FILE_PATH;
use std::env;
use std::path::PathBuf;

fn usage() -> &'static str {
    "Usage:\n  hyperfluxctl status [--json]\n  hyperfluxctl doctor [--explain] [--json] [--repair]\n  hyperfluxctl support-bundle --preview\n  hyperfluxctl support-bundle --output PATH\n  hyperfluxctl qualification view --json\n  hyperfluxctl qualification prepare\n  hyperfluxctl qualification restore\n  hyperfluxctl qualification serve [--assets PATH]\n  hyperfluxctl native observe [--output-dir PATH]\n"
}

fn json<T: serde::Serialize>(value: &T) -> Result<String, String> {
    serde_json::to_string_pretty(value).map_err(|_| "structured output failed".to_owned())
}

fn doctor(arguments: &[String]) -> Result<i32, String> {
    let explain = arguments.iter().any(|argument| argument == "--explain");
    let as_json = arguments.iter().any(|argument| argument == "--json");
    let repair = arguments.iter().any(|argument| argument == "--repair");
    if arguments
        .iter()
        .any(|argument| !matches!(argument.as_str(), "--explain" | "--json" | "--repair"))
    {
        return Err(usage().to_owned());
    }
    let probe = RealSystemProbe::default();
    let mut assessment = assess_system(&probe.snapshot());
    if repair
        && assessment.driver != hfx_ops::DriverState::ActivationPending
        && assessment.findings.len() == 1
        && assessment.findings[0].code == "HFX-SERVICE-001"
    {
        probe
            .restart_bridge()
            .map_err(|error| format!("safe service repair failed: {error}"))?;
        assessment = assess_system(&probe.snapshot());
    }
    if as_json {
        println!("{}", json(&assessment)?);
    } else {
        print!("{}", render_doctor_text(&assessment, explain));
    }
    Ok(if assessment.state == AssessmentState::Ready {
        0
    } else {
        2
    })
}

fn status(arguments: &[String]) -> Result<i32, String> {
    if arguments.iter().any(|argument| argument != "--json") || arguments.len() > 1 {
        return Err(usage().to_owned());
    }
    let assessment = assess_system(&RealSystemProbe::default().snapshot());
    if arguments == ["--json"] {
        println!("{}", json(&assessment)?);
    } else {
        print!("{}", render_status_text(&assessment));
    }
    Ok(0)
}

fn support_bundle(arguments: &[String]) -> Result<i32, String> {
    let snapshot = RealSystemProbe::default().snapshot();
    match arguments {
        [preview] if preview == "--preview" => {
            println!("{}", json(&preview_support_bundle(&snapshot))?);
            Ok(0)
        }
        [output, path] if output == "--output" => {
            let bundle = build_support_bundle(&snapshot);
            let destination = PathBuf::from(path);
            write_support_bundle(&destination, &bundle)
                .map_err(|error| format!("support bundle failed: {error}"))?;
            println!("Support bundle written: {}", destination.display());
            Ok(0)
        }
        [] => {
            let bundle = build_support_bundle(&snapshot);
            let destination = PathBuf::from(suggested_support_name(&bundle));
            write_support_bundle(&destination, &bundle)
                .map_err(|error| format!("support bundle failed: {error}"))?;
            println!("Support bundle written: {}", destination.display());
            Ok(0)
        }
        _ => Err(usage().to_owned()),
    }
}

fn qualification(arguments: &[String]) -> Result<i32, String> {
    if arguments == ["view", "--json"] {
        let probe = RealSystemProbe::default();
        let system = probe.snapshot();
        let integration = probe.qualification_integration().ok();
        let catalog = RuntimeProfileCatalog::load()
            .map_err(|_| "installed profile catalog is invalid".to_owned())?;
        let view = build_qualification_view(
            &system,
            integration.as_ref(),
            &catalog,
            RunnerCapabilities::default(),
            1,
            qualification_generated_at(),
        );
        println!("{}", json(&view)?);
        return Ok(0);
    }
    if arguments == ["prepare"] {
        return prepare_qualification_configuration();
    }
    if arguments == ["restore"] {
        return restore_prequalification_configuration();
    }
    let config = match arguments {
        [serve] if serve == "serve" => QualificationServerConfig::default(),
        [serve, assets, path] if serve == "serve" && assets == "--assets" => {
            QualificationServerConfig {
                assets: PathBuf::from(path),
                ..QualificationServerConfig::default()
            }
        }
        _ => return Err(usage().to_owned()),
    };
    println!(
        "HyperFlux qualification console: http://127.0.0.1:{}",
        config.port
    );
    println!("Local only. No upload is configured.");
    serve_qualification_console(&config).map_err(|error| error.to_string())?;
    Ok(0)
}

fn native(arguments: &[String]) -> Result<i32, String> {
    let output = match arguments {
        [observe] if observe == "observe" => default_native_observation_directory()
            .map_err(|error| format!("native route observation failed: {error}"))?,
        [observe, option, path] if observe == "observe" && option == "--output-dir" => {
            PathBuf::from(path)
        }
        _ => return Err(usage().to_owned()),
    };
    let probe = RealSystemProbe::default();
    let inventory = probe
        .native_route_inventory()
        .map_err(|error| format!("native route observation failed: {error}"))?;
    let observations = build_native_route_observations(&inventory, &qualification_generated_at())
        .map_err(|error| format!("native route observation failed: {error}"))?;
    let paths = write_native_route_observations(&output, &observations)
        .map_err(|error| format!("native route observation failed: {error}"))?;
    println!("Native receiver routes observed: {}", paths.len());
    println!("No device command or hardware write was executed.");
    for path in paths {
        println!("Privacy-safe observation: {}", path.display());
    }
    Ok(0)
}

fn prepare_qualification_configuration() -> Result<i32, String> {
    require_root_for_qualification_configuration()?;
    let probe = RealSystemProbe::default();
    let assessment = assess_system(&probe.snapshot());
    if assessment.driver == DriverState::ActivationPending {
        return Err(
            "driver activation is pending; run hyperfluxctl doctor and complete its one action before enabling qualification"
                .to_owned(),
        );
    }
    let path = std::path::Path::new(BRIDGE_CONFIGURATION_FILE_PATH);
    let outcome = enable_qualification_configuration(path)
        .map_err(|error| format!("qualification configuration failed: {error}"))?;
    if let Err(error) = probe.restart_bridge() {
        let restored = if matches!(outcome, QualificationConfigurationOutcome::Enabled { .. }) {
            let _ = restore_qualification_configuration(path);
            let _ = probe.restart_bridge();
            true
        } else {
            false
        };
        let recovery = if restored {
            "the previous configuration was restored"
        } else {
            "the already-enabled configuration was left unchanged"
        };
        return Err(format!(
            "qualification bridge start failed and {recovery}: {error}"
        ));
    }
    match outcome {
        QualificationConfigurationOutcome::AlreadyEnabled => {
            println!("Qualification mode was already enabled; the bridge restarted cleanly.");
        }
        QualificationConfigurationOutcome::Enabled { rollback_path } => {
            println!("Qualification mode enabled.");
            println!("Previous configuration: {}", rollback_path.display());
        }
        QualificationConfigurationOutcome::Restored => unreachable!("prepare cannot restore"),
    }
    println!("No lighting request or hardware lifecycle action was executed.");
    println!("Next: hyperfluxctl qualification serve");
    Ok(0)
}

fn restore_prequalification_configuration() -> Result<i32, String> {
    require_root_for_qualification_configuration()?;
    let outcome =
        restore_qualification_configuration(std::path::Path::new(BRIDGE_CONFIGURATION_FILE_PATH))
            .map_err(|error| format!("qualification configuration restore failed: {error}"))?;
    RealSystemProbe::default()
        .restart_bridge()
        .map_err(|error| {
            format!("previous configuration restored, but bridge restart failed: {error}")
        })?;
    debug_assert_eq!(outcome, QualificationConfigurationOutcome::Restored);
    println!("Previous bridge configuration restored; the bridge restarted cleanly.");
    println!("No lighting request or hardware lifecycle action was executed.");
    Ok(0)
}

fn require_root_for_qualification_configuration() -> Result<(), String> {
    if rustix::process::geteuid().as_raw() == 0 {
        Ok(())
    } else {
        Err("qualification configuration requires sudo; no configuration was changed".to_owned())
    }
}

fn run() -> Result<i32, String> {
    let arguments: Vec<String> = env::args().skip(1).collect();
    let Some((command, rest)) = arguments.split_first() else {
        return Err(usage().to_owned());
    };
    match command.as_str() {
        "doctor" => doctor(rest),
        "status" => status(rest),
        "support-bundle" => support_bundle(rest),
        "qualification" => qualification(rest),
        "native" => native(rest),
        "--help" | "-h" | "help" => {
            print!("{}", usage());
            Ok(0)
        }
        _ => Err(usage().to_owned()),
    }
}

fn main() {
    match run() {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("hyperfluxctl: {error}");
            std::process::exit(1);
        }
    }
}
