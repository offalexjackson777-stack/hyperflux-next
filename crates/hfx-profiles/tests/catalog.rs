// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{DeviceKind, ProfileKind, RouteKind};
use hfx_profiles::{
    PROFILES, RuntimeProfileCatalog, child_profile_by_product_id, profile_by_id,
    receiver_profile_by_usb_id,
};

#[test]
fn receiver_and_children_resolve_independently() {
    let receiver = receiver_profile_by_usb_id(0x1532, 0x00cf).expect("receiver is qualified");
    let mouse = child_profile_by_product_id(0x00cd).expect("mouse is qualified");
    let keyboard = child_profile_by_product_id(0x0296).expect("keyboard is qualified");

    assert_eq!(receiver.profile_kind, ProfileKind::Receiver);
    assert_eq!(receiver.transport_backend_id, Some(1));
    assert_eq!(receiver.protocol_family, Some("razer-hyperflux-v2"));
    assert_eq!(
        receiver.supported_child_kinds,
        &[DeviceKind::Keyboard, DeviceKind::Mouse]
    );
    assert!(!receiver.exact_child_combinations);
    assert_eq!(mouse.device_kind, DeviceKind::Mouse);
    assert_eq!(mouse.transport_backend_id, None);
    assert_eq!(mouse.receiver_protocols, &["razer-hyperflux-v2"]);
    assert_eq!(mouse.routes, &[RouteKind::HyperfluxWireless]);
    assert!(mouse.required_sibling_kinds.is_empty());
    assert_eq!(keyboard.device_kind, DeviceKind::Keyboard);
    assert_ne!(mouse.id, keyboard.id);
}

#[test]
fn unknown_child_has_no_implicit_profile() {
    assert!(child_profile_by_product_id(0xffff).is_none());
}

#[test]
fn runtime_catalog_is_typed_profile_local_and_canonical() {
    let catalog = RuntimeProfileCatalog::load().expect("generated catalog is valid");
    assert_eq!(catalog.iter().len(), PROFILES.len());
    let mouse = catalog
        .child(hfx_domain::ProductId::try_from(0x00cd_u16).expect("product id is valid"))
        .expect("mouse profile resolves");
    assert_eq!(
        mouse.profile_id.as_str(),
        "child.razer.basilisk-v3-pro-35k.00cd"
    );
    assert_eq!(mouse.runtime_digest.as_str().len(), 64);
    let presentation = mouse
        .presentation
        .as_ref()
        .expect("qualified child presentation exists");
    assert_eq!(presentation.upstream_id, "openrgb");
    assert_eq!(presentation.project_version, "1.0rc3");
    assert_eq!(presentation.transport_variant, "wireless");
    assert_eq!(
        mouse
            .lighting
            .as_ref()
            .expect("mouse lighting exists")
            .application_slot_count
            .get(),
        13
    );
    assert_eq!(
        mouse
            .lighting
            .as_ref()
            .expect("mouse lighting exists")
            .carrier_count
            .get(),
        13
    );
    assert!(
        mouse
            .capabilities
            .windows(2)
            .all(|pair| pair[0].id < pair[1].id)
    );
    assert!(
        catalog
            .child(hfx_domain::ProductId::try_from(0xffff_u16).expect("product id is valid"))
            .is_none()
    );
}

#[test]
fn surface_has_no_invented_usb_identity_or_lighting_map() {
    let surface = profile_by_id("surface.razer.hyperflux-v2-hard-edition")
        .expect("surface metadata is registered");
    assert_eq!(surface.profile_kind, ProfileKind::Surface);
    assert_eq!(surface.vendor_id, None);
    assert_eq!(surface.product_id, None);
    assert_eq!(surface.lighting, None);
    assert_eq!(surface.presentation, None);
    assert!(
        surface
            .capabilities
            .iter()
            .all(|capability| !capability.writable)
    );
}

#[test]
fn profile_catalog_contains_no_exact_pairing_record() {
    assert!(PROFILES.iter().all(|profile| !profile.id.contains("hw001")));
    assert_eq!(
        PROFILES
            .iter()
            .filter(|profile| profile.profile_kind == ProfileKind::Child)
            .count(),
        2
    );
}
