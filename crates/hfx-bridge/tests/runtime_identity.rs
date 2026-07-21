// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{
    RuntimeIdentityError, RuntimeIdentityIssuer, SessionIdentityError, SessionIdentitySource,
};

#[derive(Debug)]
struct DeterministicSource {
    next: u8,
    fail: bool,
}

impl SessionIdentitySource for DeterministicSource {
    fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError> {
        if self.fail {
            return Err(SessionIdentityError::EntropyUnavailable);
        }
        for byte in destination {
            *byte = self.next;
            self.next = self.next.wrapping_add(1);
        }
        Ok(())
    }
}

#[test]
fn runtime_identities_share_one_nonrepeating_process_sequence() {
    let mut source = DeterministicSource {
        next: 0,
        fail: false,
    };
    let mut issuer = RuntimeIdentityIssuer::new(&mut source).expect("issuer initializes");

    let lease = issuer.lease_id().expect("lease identity is issued");
    let subscription = issuer
        .subscription_id()
        .expect("subscription identity is issued");
    let nonce = issuer.dispatch_nonce().expect("dispatch nonce is issued");

    assert_eq!(
        lease.as_str(),
        "lease-000102030405060708090a0b0c0d0e0f-0000000000000001"
    );
    assert_eq!(
        subscription.as_str(),
        "subscription-000102030405060708090a0b0c0d0e0f-0000000000000002"
    );
    assert_eq!(nonce.get(), 3);
}

#[test]
fn application_and_restoration_dispatches_cannot_reserve_the_same_nonce() {
    let mut source = DeterministicSource {
        next: 0x20,
        fail: false,
    };
    let mut issuer = RuntimeIdentityIssuer::new(&mut source).expect("issuer initializes");

    let application_first = issuer.dispatch_nonce().expect("application nonce");
    let restoration_first = issuer.dispatch_nonce().expect("restoration nonce");
    let _unrelated_lease = issuer.lease_id().expect("interleaved lease identity");
    let restoration_retry = issuer.dispatch_nonce().expect("restoration retry nonce");
    let application_second = issuer.dispatch_nonce().expect("second application nonce");

    assert_eq!(application_first.get(), 1);
    assert_eq!(restoration_first.get(), 2);
    assert_eq!(restoration_retry.get(), 4);
    assert_eq!(application_second.get(), 5);
    assert!(
        [
            application_first.get(),
            restoration_first.get(),
            restoration_retry.get(),
            application_second.get(),
        ]
        .windows(2)
        .all(|pair| pair[0] < pair[1])
    );
}

#[test]
fn entropy_failure_creates_no_partially_initialized_issuer() {
    let mut source = DeterministicSource {
        next: 0,
        fail: true,
    };
    assert!(matches!(
        RuntimeIdentityIssuer::new(&mut source),
        Err(RuntimeIdentityError::Entropy(
            SessionIdentityError::EntropyUnavailable
        ))
    ));
}
