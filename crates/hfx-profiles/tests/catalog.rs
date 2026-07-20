// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::{DeviceKind, ProfileKind};
use hfx_profiles::{
    PROFILES, child_profile_by_product_id, profile_by_id, receiver_profile_by_usb_id,
};

#[test]
fn receiver_and_children_resolve_independently() {
    let receiver = receiver_profile_by_usb_id(0x1532, 0x00cf).expect("receiver is qualified");
    let mouse = child_profile_by_product_id(0x00cd).expect("mouse is qualified");
    let keyboard = child_profile_by_product_id(0x0296).expect("keyboard is qualified");

    assert_eq!(receiver.profile_kind, ProfileKind::Receiver);
    assert_eq!(mouse.device_kind, DeviceKind::Mouse);
    assert_eq!(keyboard.device_kind, DeviceKind::Keyboard);
    assert_ne!(mouse.id, keyboard.id);
}

#[test]
fn unknown_child_has_no_implicit_profile() {
    assert!(child_profile_by_product_id(0xffff).is_none());
}

#[test]
fn surface_has_no_invented_usb_identity_or_lighting_map() {
    let surface = profile_by_id("surface.razer.hyperflux-v2-hard-edition")
        .expect("surface metadata is registered");
    assert_eq!(surface.profile_kind, ProfileKind::Surface);
    assert_eq!(surface.vendor_id, None);
    assert_eq!(surface.product_id, None);
    assert_eq!(surface.lighting, None);
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
