// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

use hfx_daemon::{ProductionServicePaths, run_production_service};
use hfx_runtime::{BRIDGE_CONFIGURATION_FILE_PATH, PRODUCT_DISPLAY_NAME, PRODUCT_VERSION};
use signal_hook::{consts::TERM_SIGNALS, flag, low_level};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("HFX-SERVICE-001: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), String> {
    let configuration = parse_arguments()?;
    let termination = Arc::new(AtomicBool::new(false));
    let mut registrations = Vec::new();
    for signal in TERM_SIGNALS {
        registrations.push(
            flag::register(*signal, Arc::clone(&termination))
                .map_err(|_| "termination signal registration failed".to_owned())?,
        );
    }
    let mut paths = ProductionServicePaths::linux();
    paths.configuration = configuration;
    let result =
        run_production_service(paths, termination.as_ref()).map_err(|error| error.to_string());
    for registration in registrations {
        let _ = low_level::unregister(registration);
    }
    result.map(|_| ())
}

fn parse_arguments() -> Result<PathBuf, String> {
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    match arguments.as_slice() {
        [flag] if flag == "--help" => {
            println!(
                "{PRODUCT_DISPLAY_NAME} bridge {PRODUCT_VERSION}\n\nUsage: hyperflux-next-bridge --config PATH"
            );
            std::process::exit(0);
        }
        [flag] if flag == "--version" => {
            println!("{PRODUCT_VERSION}");
            std::process::exit(0);
        }
        [flag, path] if flag == "--config" => {
            let path = PathBuf::from(path);
            if path.is_absolute() {
                Ok(path)
            } else {
                Err("--config requires an absolute path".to_owned())
            }
        }
        [] => Ok(PathBuf::from(BRIDGE_CONFIGURATION_FILE_PATH)),
        _ => Err("usage: hyperflux-next-bridge --config PATH".to_owned()),
    }
}
