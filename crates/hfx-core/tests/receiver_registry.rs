// SPDX-License-Identifier: GPL-2.0-only

mod common;

use common::text;
use hfx_core::{
    LifecycleLimits, ReceiverLifecycleMachine, ReceiverLifecycleRegistry, ReceiverRegistryError,
};

fn machine(receiver_id: &str) -> ReceiverLifecycleMachine {
    ReceiverLifecycleMachine::new(text(receiver_id), LifecycleLimits::default())
        .expect("test lifecycle machine is valid")
}

#[test]
fn registry_is_bounded_canonical_and_never_replaces_existing_state() {
    assert!(matches!(
        ReceiverLifecycleRegistry::new(0),
        Err(ReceiverRegistryError::InvalidCapacity)
    ));
    let mut registry = ReceiverLifecycleRegistry::new(2).expect("registry bound is valid");
    registry
        .register(machine("receiver-b"))
        .expect("first receiver fits");
    registry
        .register(machine("receiver-a"))
        .expect("second receiver fits");

    assert_eq!(
        registry
            .iter()
            .map(|machine| machine.receiver_id().as_str())
            .collect::<Vec<_>>(),
        vec!["receiver-a", "receiver-b"]
    );
    assert!(registry.get(&text("receiver-a")).is_some());
    assert!(registry.get_mut(&text("receiver-b")).is_some());
    assert_eq!(registry.len(), 2);
    assert!(!registry.is_empty());

    assert_eq!(
        registry.register(machine("receiver-a")),
        Err(ReceiverRegistryError::DuplicateReceiver(text("receiver-a")))
    );
    assert_eq!(
        registry.register(machine("receiver-c")),
        Err(ReceiverRegistryError::CapacityExhausted)
    );
    assert_eq!(registry.len(), 2);
}
