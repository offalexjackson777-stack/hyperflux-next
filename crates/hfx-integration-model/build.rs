// SPDX-License-Identifier: GPL-2.0-only

use sha2::{Digest, Sha256};
use std::env;
use std::fmt::Write as _;
use std::fs;
use std::path::{Path, PathBuf};

const SOURCE_CATALOG: &str = "integrations/native-route/source-validated.json";
const OPENRAZER_KNOWLEDGE: &str = "knowledge/upstreams/openrazer.json";
const OPENRGB_KNOWLEDGE: &str = "knowledge/upstreams/openrgb.json";

fn main() {
    let manifest_dir = PathBuf::from(
        env::var_os("CARGO_MANIFEST_DIR").expect("Cargo provides CARGO_MANIFEST_DIR"),
    );
    let repository = manifest_dir.join("../..");
    let output_dir = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo provides OUT_DIR"));

    let catalog_path = repository.join(SOURCE_CATALOG);
    let catalog = fs::read(&catalog_path).expect("source-validated catalog is readable");
    let document: serde_json::Value =
        serde_json::from_slice(&catalog).expect("source-validated catalog is valid JSON");
    let compact = serde_json::to_vec(&document).expect("source-validated catalog is serializable");
    fs::write(output_dir.join("source-validated.json"), compact)
        .expect("compact source-validated catalog can be written");

    emit_source_binding(
        &repository.join(OPENRAZER_KNOWLEDGE),
        "HFX_OPENRAZER_KNOWLEDGE_SHA256",
    );
    emit_source_binding(
        &repository.join(OPENRGB_KNOWLEDGE),
        "HFX_OPENRGB_KNOWLEDGE_SHA256",
    );
    println!("cargo:rerun-if-changed={}", catalog_path.display());
}

fn emit_source_binding(path: &Path, variable: &str) {
    let source = fs::read(path).expect("upstream knowledge catalog is readable");
    println!("cargo:rustc-env={variable}={}", hex_sha256(&source));
    println!("cargo:rerun-if-changed={}", path.display());
}

fn hex_sha256(value: &[u8]) -> String {
    let digest = Sha256::digest(value);
    let mut output = String::with_capacity(digest.len() * 2);
    for byte in digest {
        write!(output, "{byte:02x}").expect("writing to a String cannot fail");
    }
    output
}
