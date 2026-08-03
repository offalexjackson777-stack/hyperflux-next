// SPDX-License-Identifier: GPL-2.0-only

use hfx_device_qualification::{
    QualificationServerConfig, RunnerCapabilities, build_native_route_observations,
    build_qualification_view, default_native_observation_directory, qualification_generated_at,
    serve_qualification_console, write_native_route_observations,
};
use hfx_profiles::RuntimeProfileCatalog;
use hfx_system_probe::{RealSystemProbe, SystemProbe};
use std::env;
use std::path::PathBuf;

fn usage() -> &'static str {
    "Usage:\n  hyperflux-next-device-qualification view --json\n  hyperflux-next-device-qualification serve [--assets PATH]\n  hyperflux-next-device-qualification native observe [--output-dir PATH]\n"
}

fn source_assets() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../apps/device-qualification")
}

fn render_json<T: serde::Serialize>(value: &T) -> Result<String, String> {
    serde_json::to_string_pretty(value).map_err(|_| "structured output failed".to_owned())
}

fn qualification_view() -> Result<i32, String> {
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
    println!("{}", render_json(&view)?);
    Ok(0)
}

fn serve(arguments: &[String]) -> Result<i32, String> {
    let assets = match arguments {
        [] => source_assets(),
        [option, path] if option == "--assets" => PathBuf::from(path),
        _ => return Err(usage().to_owned()),
    };
    let config = QualificationServerConfig {
        assets,
        ..QualificationServerConfig::default()
    };
    println!(
        "HyperFlux qualification console: http://127.0.0.1:{}",
        config.port
    );
    println!("Source-only local console. No upload is configured.");
    serve_qualification_console(&config).map_err(|error| error.to_string())?;
    Ok(0)
}

fn native_observe(arguments: &[String]) -> Result<i32, String> {
    let output = match arguments {
        [] => default_native_observation_directory()
            .map_err(|error| format!("native route observation failed: {error}"))?,
        [option, path] if option == "--output-dir" => PathBuf::from(path),
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
    Ok(0)
}

fn run_arguments(arguments: &[String]) -> Result<i32, String> {
    match arguments {
        [command, option] if command == "view" && option == "--json" => qualification_view(),
        [command, rest @ ..] if command == "serve" => serve(rest),
        [native, observe, rest @ ..] if native == "native" && observe == "observe" => {
            native_observe(rest)
        }
        [help] if matches!(help.as_str(), "--help" | "-h" | "help") => {
            print!("{}", usage());
            Ok(0)
        }
        _ => Err(usage().to_owned()),
    }
}

fn main() {
    let arguments = env::args().skip(1).collect::<Vec<_>>();
    match run_arguments(&arguments) {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("hyperflux-next-device-qualification: {error}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_console_surface_is_separate_from_installed_hyperfluxctl() {
        let value = usage();
        for command in ["view --json", "serve", "native observe"] {
            assert!(value.contains(command));
        }
        for forbidden in ["doctor", "support-bundle", "activate-disconnected-driver"] {
            assert!(!value.contains(forbidden));
        }
    }

    #[test]
    fn source_console_rejects_unknown_and_mutating_configuration_commands() {
        for arguments in [
            &["qualification", "prepare"][..],
            &["qualification", "restore"][..],
            &["native", "write"][..],
        ] {
            let arguments = arguments
                .iter()
                .map(|argument| (*argument).to_owned())
                .collect::<Vec<_>>();
            assert_eq!(run_arguments(&arguments), Err(usage().to_owned()));
        }
    }
}
