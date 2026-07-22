// SPDX-License-Identifier: GPL-2.0-only

use hfx_ops::{
    AssessmentState, QualificationServerConfig, RealSystemProbe, RunnerCapabilities,
    SystemController, SystemProbe, assess_system, build_qualification_view, build_support_bundle,
    preview_support_bundle, qualification_generated_at, render_doctor_text, render_status_text,
    serve_qualification_console, suggested_support_name, write_support_bundle,
};
use hfx_profiles::RuntimeProfileCatalog;
use std::env;
use std::path::PathBuf;

fn usage() -> &'static str {
    "Usage:\n  hyperfluxctl status [--json]\n  hyperfluxctl doctor [--explain] [--json] [--repair]\n  hyperfluxctl support-bundle --preview\n  hyperfluxctl support-bundle --output PATH\n  hyperfluxctl qualification view --json\n  hyperfluxctl qualification serve [--assets PATH]\n"
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
