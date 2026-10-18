// SPDX-License-Identifier: GPL-2.0-only

use super::*;
use std::fs;
use std::os::unix::fs::{PermissionsExt as _, symlink};
use std::time::{SystemTime, UNIX_EPOCH};

fn temporary_root(label: &str) -> std::path::PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    let root = std::env::temp_dir().join(format!(
        "hyperflux-component-{label}-{}-{nonce}",
        std::process::id()
    ));
    fs::create_dir(&root).expect("create root");
    root
}

fn descriptor(version: &str) -> String {
    format!(
        "{{\"$schema\":\"{DESCRIPTOR_SCHEMA_REF}\",\"schema\":\"{DESCRIPTOR_SCHEMA}\",\"component_id\":\"full\",\"product_version\":\"{version}\",\"capabilities\":[\"openrazer-native-route\",\"openrgb-plugin\"]}}\n"
    )
}

fn probe_fixture(path: &Path, version: &str) -> FullComponentSnapshot {
    let metadata = fs::metadata(path).expect("metadata");
    probe_with_ownership(path, version, metadata.uid(), metadata.gid())
}

#[test]
fn absent_descriptor_is_optional_not_installed() {
    let root = temporary_root("absent");
    assert_eq!(
        probe_full_component(&root.join("missing.json"), "2.0.0"),
        FullComponentSnapshot::not_installed()
    );
    fs::remove_dir(root).expect("remove root");
}

#[test]
fn exact_descriptor_is_ready_and_drift_fails_closed() {
    let root = temporary_root("ready");
    let path = root.join("full.json");
    fs::write(&path, descriptor("2.0.0")).expect("write descriptor");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).expect("mode");
    assert_eq!(
        probe_fixture(&path, "2.0.0"),
        FullComponentSnapshot::ready("2.0.0")
    );
    assert_eq!(
        probe_fixture(&path, "2.0.1").state,
        FullComponentState::VersionMismatch
    );

    fs::write(
        &path,
        descriptor("2.0.0").replace(",\"openrgb-plugin\"", ""),
    )
    .expect("write route-only descriptor");
    assert_eq!(
        probe_fixture(&path, "2.0.0"),
        FullComponentSnapshot::ready_without_openrgb_plugin("2.0.0")
    );

    for drift in [
        descriptor("2.0.0").replace("openrgb-plugin", "unexpected"),
        descriptor("2.0.0").replace("\"openrazer-native-route\",", ""),
        descriptor("2.0.0").replace(
            "\"openrazer-native-route\",\"openrgb-plugin\"",
            "\"openrgb-plugin\",\"openrazer-native-route\"",
        ),
        descriptor("2.0.0").replace(
            "\"openrgb-plugin\"",
            "\"openrgb-plugin\",\"openrgb-plugin\"",
        ),
        descriptor("2.0.0").replace("\"openrazer-native-route\",\"openrgb-plugin\"", ""),
    ] {
        fs::write(&path, drift).expect("write drift");
        assert_eq!(
            probe_fixture(&path, "2.0.0").state,
            FullComponentState::CapabilitiesIncomplete
        );
    }
    fs::remove_dir_all(root).expect("remove root");
}

#[test]
fn unsafe_metadata_and_links_are_invalid() {
    let root = temporary_root("metadata");
    let path = root.join("full.json");
    fs::write(&path, descriptor("2.0.0")).expect("write descriptor");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).expect("mode");
    assert_eq!(
        probe_fixture(&path, "2.0.0").state,
        FullComponentState::DescriptorInvalid
    );
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).expect("mode");
    let link = root.join("link.json");
    symlink(&path, &link).expect("link");
    assert_eq!(
        probe_with_ownership(&link, "2.0.0", 0, 0).state,
        FullComponentState::DescriptorInvalid
    );
    fs::remove_dir_all(root).expect("remove root");
}
