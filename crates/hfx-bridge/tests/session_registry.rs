// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{AuthorizedSession, SessionRegistry, SessionRegistryError};
use hfx_core::SessionAuthority;
use hfx_domain::{AuthorizationEpoch, ClientId, ProtocolVersion, QueueCapacity, SessionId};

fn id<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test identity must be valid")
}

fn authorization(client: &str, session: &str, epoch: u64) -> AuthorizedSession {
    AuthorizedSession {
        client_id: id::<ClientId>(client),
        selected_version: ProtocolVersion::try_from(2).expect("version must be valid"),
        session_id: id::<SessionId>(session),
        authorization_epoch: AuthorizationEpoch::try_from(epoch).expect("epoch must be valid"),
    }
}

fn registry(capacity: u16) -> SessionRegistry {
    SessionRegistry::new(QueueCapacity::try_from(capacity).expect("capacity must be valid"))
}

#[test]
fn capacity_and_identity_failures_leave_existing_authority_intact() {
    let mut registry = registry(1);
    let first = authorization("client-a", "session-a", 1);
    registry
        .register(first.clone())
        .expect("first session must register");

    assert_eq!(
        registry.register(authorization("client-b", "session-a", 2)),
        Err(SessionRegistryError::DuplicateSessionIdentity)
    );
    assert_eq!(
        registry.register(authorization("client-a", "session-b", 2)),
        Err(SessionRegistryError::ClientAlreadyConnected)
    );
    assert_eq!(
        registry.register(authorization("client-b", "session-b", 2)),
        Err(SessionRegistryError::CapacityExhausted)
    );

    assert_eq!(registry.len(), 1);
    assert!(registry.authorizes(&first.session_id, first.authorization_epoch));
}

#[test]
fn revocation_removes_exact_authority_and_allows_clean_client_reconnect() {
    let mut registry = registry(2);
    let first = authorization("client-a", "session-a", 4);
    registry
        .register(first.clone())
        .expect("first session must register");
    assert!(!registry.authorizes(
        &first.session_id,
        AuthorizationEpoch::try_from(5).expect("epoch must be valid")
    ));

    assert_eq!(registry.revoke(&first.session_id), Some(first.clone()));
    assert!(!registry.authorizes(&first.session_id, first.authorization_epoch));
    assert!(!registry.contains_client(&first.client_id));

    let replacement = authorization("client-a", "session-b", 6);
    registry
        .register(replacement.clone())
        .expect("revoked client identity may reconnect");
    assert!(registry.authorizes(&replacement.session_id, replacement.authorization_epoch));
}

#[test]
fn revoke_client_is_exact_and_unknown_revoke_is_a_noop() {
    let mut registry = registry(2);
    let first = authorization("client-a", "session-a", 1);
    let second = authorization("client-b", "session-b", 2);
    registry
        .register(first.clone())
        .expect("first session must register");
    registry
        .register(second.clone())
        .expect("second session must register");

    assert_eq!(registry.revoke_client(&first.client_id), Some(first));
    assert!(registry.authorizes(&second.session_id, second.authorization_epoch));
    assert_eq!(
        registry.revoke_client(&id::<ClientId>("unknown-client")),
        None
    );
    assert_eq!(registry.len(), 1);
}
