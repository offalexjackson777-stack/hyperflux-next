// SPDX-License-Identifier: GPL-2.0-only

use hfx_integration_model::{ADAPTERS, UPSTREAMS, adapter_by_id, upstream_by_id};

#[test]
fn generated_catalog_keeps_upstreams_exact_and_network_free() {
    let openrgb = upstream_by_id("openrgb").expect("OpenRGB pin exists");
    assert_eq!(openrgb.version, "1.1");
    assert_eq!(openrgb.commit, "8e16de27858cf2be6af54a33ce003df74cb9fd3e");
    assert_eq!(UPSTREAMS.len(), 3);
}

#[test]
fn bridge_owned_adapters_bind_exact_protocol_generations() {
    assert_eq!(ADAPTERS.len(), 2);
    for (id, versions) in [
        ("openrazer-native-route", &[7_u16][..]),
        ("openrgb-native", &[7_u16][..]),
    ] {
        assert_eq!(
            adapter_by_id(id)
                .unwrap_or_else(|| panic!("{id} adapter exists"))
                .sdk_protocol_versions,
            versions
        );
    }
    assert_eq!(
        adapter_by_id("openrgb-native")
            .expect("OpenRGB adapter exists")
            .coexistence_policy,
        "application-plugin"
    );
    let native =
        adapter_by_id("openrazer-native-route").expect("OpenRazer native-route adapter exists");
    assert_eq!(native.kind, "transport-adapter");
    assert_eq!(native.coexistence_policy, "upstream-transport-extension");
    assert!(native.feature_families.contains(&"native-control"));
    assert!(adapter_by_id("openrazer-compatibility").is_none());
    assert!(adapter_by_id("polychromatic-native").is_none());
}
