// SPDX-License-Identifier: GPL-2.0-only

use hfx_ops::{
    AssessmentState, CompanionState, RealSystemProbe, SystemProbe, assess_system,
    build_support_bundle, collect_qualification_report, preview_support_bundle,
    qualification_report_json, render_doctor_text, render_qualification_text, render_status_text,
    suggested_support_name, write_qualification_report, write_support_bundle,
};
use std::env;
use std::path::PathBuf;

fn usage() -> &'static str {
    "Usage:\n  hyperfluxctl status [--json]\n  hyperfluxctl doctor [--explain] [--json]\n  hyperfluxctl support-bundle --preview\n  hyperfluxctl support-bundle [--output PATH]\n  hyperfluxctl qualification [--json] [--output PATH]\n"
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

struct QualificationArguments {
    as_json: bool,
    output: Option<PathBuf>,
}

fn qualification_arguments(arguments: &[String]) -> Option<QualificationArguments> {
    let mut parsed = QualificationArguments {
        as_json: false,
        output: None,
    };
    let mut remaining = arguments.iter();
    while let Some(argument) = remaining.next() {
        match argument.as_str() {
            "--json" if !parsed.as_json => parsed.as_json = true,
            "--output" if parsed.output.is_none() => {
                let path = remaining.next().filter(|path| !path.starts_with("--"))?;
                parsed.output = Some(PathBuf::from(path));
            }
            _ => return None,
        }
    }
    Some(parsed)
}

fn qualification(arguments: &[String]) -> Result<i32, String> {
    let arguments = qualification_arguments(arguments).ok_or_else(|| usage().to_owned())?;
    let report = collect_qualification_report(&RealSystemProbe::default())
        .map_err(|error| format!("qualification report failed: {error}"))?;
    if let Some(destination) = &arguments.output {
        write_qualification_report(destination, &report)
            .map_err(|error| format!("qualification report failed: {error}"))?;
    }
    if arguments.as_json {
        print!(
            "{}",
            qualification_report_json(&report)
                .map_err(|error| format!("qualification report failed: {error}"))?
        );
    } else {
        print!("{}", render_qualification_text(&report));
        if let Some(destination) = &arguments.output {
            println!("Report written: {}", destination.display());
        }
    }
    Ok(if report.companion.state == CompanionState::Ready {
        0
    } else {
        2
    })
}

fn run_arguments(arguments: &[String]) -> Result<i32, String> {
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
        for command in ["status", "doctor", "support-bundle", "qualification"] {
            assert!(value.contains(command));
        }
        for forbidden in [
            "--repair",
            "qualification serve",
            "qualification view",
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
            &["qualification", "serve"][..],
            &["qualification", "--output"][..],
            &["qualification", "--output", "--json"][..],
            &["qualification", "--json", "--json"][..],
            &["qualification", "--output", "a.json", "--output", "b.json"][..],
            &["native", "observe"][..],
        ] {
            let arguments = arguments
                .iter()
                .map(|argument| (*argument).to_owned())
                .collect::<Vec<_>>();
            assert_eq!(run_arguments(&arguments), Err(usage().to_owned()));
        }
    }

    #[test]
    fn qualification_accepts_json_and_output_in_either_order() {
        for (arguments, as_json, output) in [
            (&[][..], false, None),
            (&["--json"][..], true, None),
            (&["--output", "report.json"][..], false, Some("report.json")),
            (
                &["--json", "--output", "report.json"][..],
                true,
                Some("report.json"),
            ),
            (
                &["--output", "report.json", "--json"][..],
                true,
                Some("report.json"),
            ),
        ] {
            let arguments = arguments
                .iter()
                .map(|argument| (*argument).to_owned())
                .collect::<Vec<_>>();
            let parsed = qualification_arguments(&arguments).expect("arguments are valid");
            assert_eq!(parsed.as_json, as_json);
            assert_eq!(parsed.output, output.map(PathBuf::from));
        }
    }
}
