// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

use hfx_runtime::{PRODUCT_DISPLAY_NAME, PRODUCT_VERSION};
use hfx_uhid_relay::{QualificationRunConfig, QualificationRunPaths, run_qualification};
use signal_hook::{consts::TERM_SIGNALS, flag, low_level};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::time::Duration;

const USAGE: &str = "Usage: hyperflux-next-native-qualifier --profile PROFILE_ID --expected-generation N --source-revision SHA1 --package-sha256 SHA256 --raw-output /dev/shm/hyperflux-next-native-parity-NAME.json --timeout-seconds N --approval-text TEXT";

#[derive(Debug, Eq, PartialEq)]
enum Command {
    Run(Box<QualificationRunConfig>),
    Help,
    Version,
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("HFX-NATIVE-QUALIFIER-001: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), String> {
    match parse_arguments(std::env::args().skip(1))? {
        Command::Help => {
            println!("{PRODUCT_DISPLAY_NAME} native qualifier {PRODUCT_VERSION}\n\n{USAGE}");
            Ok(())
        }
        Command::Version => {
            println!("{PRODUCT_VERSION}");
            Ok(())
        }
        Command::Run(config) => run_with_signals(&config),
    }
}

fn run_with_signals(config: &QualificationRunConfig) -> Result<(), String> {
    let termination = Arc::new(AtomicBool::new(false));
    let mut registrations = Vec::new();
    for signal in TERM_SIGNALS {
        registrations.push(
            flag::register(*signal, Arc::clone(&termination))
                .map_err(|_| "termination signal registration failed".to_owned())?,
        );
    }
    let result = run_qualification(config, termination.as_ref()).map_err(|error| error.to_string());
    for registration in registrations {
        let _ = low_level::unregister(registration);
    }
    result
}

fn parse_arguments(arguments: impl IntoIterator<Item = String>) -> Result<Command, String> {
    let arguments = arguments.into_iter().collect::<Vec<_>>();
    match arguments.as_slice() {
        [flag] if flag == "--help" || flag == "-h" => return Ok(Command::Help),
        [flag] if flag == "--version" => return Ok(Command::Version),
        _ => {}
    }
    if arguments.len() % 2 != 0 {
        return Err(USAGE.to_owned());
    }
    let mut profile_id = None;
    let mut expected_generation = None;
    let mut source_revision = None;
    let mut package_sha256 = None;
    let mut raw_output = None;
    let mut timeout = None;
    let mut approval_text = None;
    for pair in arguments.chunks_exact(2) {
        let slot = match pair[0].as_str() {
            "--profile" => &mut profile_id,
            "--expected-generation" => &mut expected_generation,
            "--source-revision" => &mut source_revision,
            "--package-sha256" => &mut package_sha256,
            "--raw-output" => &mut raw_output,
            "--timeout-seconds" => &mut timeout,
            "--approval-text" => &mut approval_text,
            _ => return Err(format!("unknown option: {}\n{USAGE}", pair[0])),
        };
        if slot.replace(pair[1].clone()).is_some() {
            return Err(format!("{} may be provided only once\n{USAGE}", pair[0]));
        }
    }
    let profile_id = profile_id.ok_or_else(|| USAGE.to_owned())?;
    let expected_generation = expected_generation
        .ok_or_else(|| USAGE.to_owned())?
        .parse::<u64>()
        .map_err(|_| USAGE.to_owned())?;
    let source_revision = source_revision.ok_or_else(|| USAGE.to_owned())?;
    let package_sha256 = package_sha256.ok_or_else(|| USAGE.to_owned())?;
    let raw_output = PathBuf::from(raw_output.ok_or_else(|| USAGE.to_owned())?);
    if !raw_output.is_absolute() {
        return Err(USAGE.to_owned());
    }
    let timeout = timeout
        .ok_or_else(|| USAGE.to_owned())?
        .parse::<u64>()
        .map(Duration::from_secs)
        .map_err(|_| USAGE.to_owned())?;
    let approval_text = approval_text.ok_or_else(|| USAGE.to_owned())?;
    Ok(Command::Run(Box::new(QualificationRunConfig {
        profile_id,
        expected_generation,
        source_revision,
        package_sha256,
        approval_text,
        timeout,
        paths: QualificationRunPaths::linux(raw_output),
    })))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn valid_arguments() -> Vec<String> {
        [
            "--profile",
            "child.razer.mouse.0001",
            "--expected-generation",
            "7",
            "--source-revision",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "--package-sha256",
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "--raw-output",
            "/dev/shm/hyperflux-next-native-parity-test.json",
            "--timeout-seconds",
            "30",
            "--approval-text",
            "approve HyperFlux Next restoration-isolated bounded read-only native parity probes",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect()
    }

    #[test]
    fn exact_complete_argument_set_parses() {
        let Command::Run(config) = parse_arguments(valid_arguments()).expect("arguments parse")
        else {
            panic!("expected run command");
        };
        assert_eq!(config.expected_generation, 7);
        assert_eq!(config.timeout, Duration::from_secs(30));
        assert_eq!(
            config.paths.raw_output,
            PathBuf::from("/dev/shm/hyperflux-next-native-parity-test.json")
        );
    }

    #[test]
    fn missing_duplicate_unknown_and_relative_arguments_fail() {
        let mut missing = valid_arguments();
        missing.truncate(missing.len() - 2);
        let mut duplicate = valid_arguments();
        duplicate.extend(["--profile".to_owned(), "other".to_owned()]);
        let mut unknown = valid_arguments();
        unknown[0] = "--other".to_owned();
        let mut relative = valid_arguments();
        let output = relative
            .iter()
            .position(|value| value == "--raw-output")
            .expect("output option exists");
        relative[output + 1] = "relative.json".to_owned();
        for arguments in [missing, duplicate, unknown, relative] {
            assert!(parse_arguments(arguments).is_err());
        }
    }
}
