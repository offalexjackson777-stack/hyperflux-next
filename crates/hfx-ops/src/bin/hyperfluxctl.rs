// SPDX-License-Identifier: GPL-2.0-only

use hfx_ops::{
    AssessmentState, RealSystemProbe, SystemProbe, assess_system, build_support_bundle,
    preview_support_bundle, render_doctor_text, render_status_text, suggested_support_name,
    write_support_bundle,
};
use std::env;
use std::path::PathBuf;

fn usage() -> &'static str {
    "Usage:\n  hyperfluxctl status [--json]\n  hyperfluxctl doctor [--explain] [--json]\n  hyperfluxctl support-bundle --preview\n  hyperfluxctl support-bundle --output PATH\n"
}

fn json<T: serde::Serialize>(value: &T) -> Result<String, String> {
    serde_json::to_string_pretty(value).map_err(|_| "structured output failed".to_owned())
}

fn doctor(arguments: &[String]) -> Result<i32, String> {
    let explain = arguments.iter().any(|argument| argument == "--explain");
    let as_json = arguments.iter().any(|argument| argument == "--json");
    if arguments
        .iter()
        .any(|argument| !matches!(argument.as_str(), "--explain" | "--json"))
    {
        return Err(usage().to_owned());
    }
    let probe = RealSystemProbe::default();
    let assessment = assess_system(&probe.snapshot());
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

fn run_arguments(arguments: &[String]) -> Result<i32, String> {
    let Some((command, rest)) = arguments.split_first() else {
        return Err(usage().to_owned());
    };
    match command.as_str() {
        "doctor" => doctor(rest),
        "status" => status(rest),
        "support-bundle" => support_bundle(rest),
        "--help" | "-h" | "help" => {
            print!("{}", usage());
            Ok(0)
        }
        _ => Err(usage().to_owned()),
    }
}

fn run() -> Result<i32, String> {
    run_arguments(&env::args().skip(1).collect::<Vec<_>>())
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn installed_cli_exposes_only_read_only_inspection_and_bounded_support() {
        let value = usage();
        assert_eq!(
            value,
            include_str!("../../../../tests/fixtures/cli/hyperfluxctl-usage.txt")
        );
        for command in ["status", "doctor", "support-bundle"] {
            assert!(value.contains(command));
        }
        for forbidden in [
            "--repair",
            "qualification",
            "native observe",
            "activate-disconnected-driver",
        ] {
            assert!(!value.contains(forbidden));
        }
    }

    #[test]
    fn removed_runtime_commands_fail_before_constructing_a_probe() {
        for arguments in [
            &["doctor", "--repair"][..],
            &["qualification", "view", "--json"][..],
            &["native", "observe"][..],
        ] {
            let arguments = arguments
                .iter()
                .map(|argument| (*argument).to_owned())
                .collect::<Vec<_>>();
            assert_eq!(run_arguments(&arguments), Err(usage().to_owned()));
        }
    }
}
