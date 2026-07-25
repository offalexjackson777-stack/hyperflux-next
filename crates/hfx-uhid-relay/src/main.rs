// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

use hfx_runtime::{PRODUCT_DISPLAY_NAME, PRODUCT_VERSION};
use hfx_uhid_relay::{RelayPaths, run_relay};
use signal_hook::{consts::TERM_SIGNALS, flag, low_level};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;

const USAGE: &str = "Usage: hyperflux-next-uhid-relay [--socket ABSOLUTE_PATH] [--uhid ABSOLUTE_PATH] [--hid-driver-root ABSOLUTE_PATH]";

#[derive(Debug, Eq, PartialEq)]
enum Command {
    Run(RelayPaths),
    Help,
    Version,
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("HFX-OPENRAZER-RELAY-001: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), String> {
    match parse_arguments(std::env::args().skip(1))? {
        Command::Help => {
            println!("{PRODUCT_DISPLAY_NAME} OpenRazer relay {PRODUCT_VERSION}\n\n{USAGE}");
            Ok(())
        }
        Command::Version => {
            println!("{PRODUCT_VERSION}");
            Ok(())
        }
        Command::Run(paths) => run_with_signals(&paths),
    }
}

fn run_with_signals(paths: &RelayPaths) -> Result<(), String> {
    let termination = Arc::new(AtomicBool::new(false));
    let mut registrations = Vec::new();
    for signal in TERM_SIGNALS {
        registrations.push(
            flag::register(*signal, Arc::clone(&termination))
                .map_err(|_| "termination signal registration failed".to_owned())?,
        );
    }
    let result = run_relay(paths, termination.as_ref()).map_err(|error| error.to_string());
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

    let mut paths = RelayPaths::linux();
    let mut socket_seen = false;
    let mut uhid_seen = false;
    let mut hid_driver_root_seen = false;
    let mut index = 0;
    while index < arguments.len() {
        let flag = &arguments[index];
        let value = arguments
            .get(index + 1)
            .ok_or_else(|| format!("{flag} requires an absolute path\n{USAGE}"))?;
        let path = PathBuf::from(value);
        if !path.is_absolute() {
            return Err(format!("{flag} requires an absolute path\n{USAGE}"));
        }
        match flag.as_str() {
            "--socket" if !socket_seen => {
                paths.bridge_socket = path;
                socket_seen = true;
            }
            "--uhid" if !uhid_seen => {
                paths.uhid_device = path;
                uhid_seen = true;
            }
            "--hid-driver-root" if !hid_driver_root_seen => {
                paths.hid_driver_root = path;
                hid_driver_root_seen = true;
            }
            "--socket" | "--uhid" | "--hid-driver-root" => {
                return Err(format!("{flag} may be provided only once\n{USAGE}"));
            }
            _ => return Err(format!("unknown option: {flag}\n{USAGE}")),
        }
        index += 2;
    }
    Ok(Command::Run(paths))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_owned()).collect()
    }

    #[test]
    fn defaults_to_production_paths() {
        assert_eq!(
            parse_arguments(Vec::new()).expect("arguments parse"),
            Command::Run(RelayPaths::linux())
        );
    }

    #[test]
    fn accepts_absolute_overrides_in_either_order() {
        let command = parse_arguments(strings(&[
            "--uhid",
            "/tmp/test-uhid",
            "--hid-driver-root",
            "/tmp/test-hid-drivers",
            "--socket",
            "/tmp/test.sock",
        ]))
        .expect("arguments parse");
        assert_eq!(
            command,
            Command::Run(RelayPaths {
                bridge_socket: PathBuf::from("/tmp/test.sock"),
                uhid_device: PathBuf::from("/tmp/test-uhid"),
                hid_driver_root: PathBuf::from("/tmp/test-hid-drivers"),
            })
        );
    }

    #[test]
    fn rejects_relative_missing_duplicate_and_unknown_values() {
        for arguments in [
            strings(&["--socket", "relative.sock"]),
            strings(&["--uhid"]),
            strings(&["--socket", "/a", "--socket", "/b"]),
            strings(&["--hid-driver-root", "/a", "--hid-driver-root", "/b"]),
            strings(&["--other", "/tmp/value"]),
        ] {
            assert!(parse_arguments(arguments).is_err());
        }
    }

    #[test]
    fn help_and_version_are_side_effect_free_commands() {
        assert_eq!(
            parse_arguments(strings(&["--help"])).expect("help parses"),
            Command::Help
        );
        assert_eq!(
            parse_arguments(strings(&["--version"])).expect("version parses"),
            Command::Version
        );
    }
}
