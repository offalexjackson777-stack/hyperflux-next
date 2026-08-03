// SPDX-License-Identifier: GPL-2.0-only

use hfx_sim::{parse_shadow_fixture, run_shadow_comparison};
use std::env;
use std::fs;
use std::io::{self, Write as _};
use std::path::Path;
use std::process::ExitCode;

fn main() -> ExitCode {
    match execute() {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("hfx-shadow: {message}");
            ExitCode::FAILURE
        }
    }
}

fn execute() -> Result<(), String> {
    let arguments = env::args().collect::<Vec<_>>();
    if arguments.len() != 2 {
        return Err("usage: hfx-shadow FIXTURE.json".to_owned());
    }
    let path = Path::new(&arguments[1]);
    let metadata = fs::symlink_metadata(path).map_err(|error| error.to_string())?;
    if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
        return Err("fixture must be one regular, non-symbolic-link file".to_owned());
    }
    let bytes = fs::read(path).map_err(|error| error.to_string())?;
    let fixture = parse_shadow_fixture(&bytes).map_err(|error| error.to_string())?;
    let result = run_shadow_comparison(&fixture).map_err(|error| error.to_string())?;
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    serde_json::to_writer_pretty(&mut stdout, &result).map_err(|error| error.to_string())?;
    stdout.write_all(b"\n").map_err(|error| error.to_string())?;
    stdout.flush().map_err(|error| error.to_string())
}
