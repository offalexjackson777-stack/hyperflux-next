// SPDX-License-Identifier: GPL-2.0-only

use hfx_integration_model::{ADAPTERS, UPSTREAMS, adapter_by_id, upstream_by_id};

#[test]
fn generated_catalog_keeps_upstreams_exact_and_network_free() {
    let openrgb = upstream_by_id("openrgb").expect("OpenRGB pin exists");
    assert_eq!(openrgb.version, "1.0rc3");
    assert_eq!(openrgb.commit, "6fbcf62d7694e7b92fd0a5884b40b92984fbd1b0");
    assert_eq!(UPSTREAMS.len(), 3);
}

#[test]
fn adapters_require_canonical_protocol_v5_views_and_distinct_coexistence_contracts() {
    assert!(
        ADAPTERS
            .iter()
            .all(|adapter| adapter.sdk_protocol_versions == [5])
    );
    assert_eq!(
        adapter_by_id("openrgb-native")
            .expect("OpenRGB adapter exists")
            .coexistence_policy,
        "application-plugin"
    );
    assert_eq!(
        adapter_by_id("openrazer-compatibility")
            .expect("OpenRazer compatibility adapter exists")
            .coexistence_policy,
        "private-explicit-service-only"
    );
}
