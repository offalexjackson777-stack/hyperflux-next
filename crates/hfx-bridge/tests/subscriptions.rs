// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{
    AuthorizedSession, RuntimeIdentityIssuer, SessionIdentityError, SessionIdentitySource,
    SubscriptionRegistry, SubscriptionRegistryError,
};
use hfx_domain::{
    AuthorizationEpoch, ClientId, EventBatchLimit, ProtocolVersion, SessionId, SubscriptionId,
};
use hfx_protocol::SubscriptionRequest;

fn text<T>(value: &str) -> T
where
    T: TryFrom<String>,
    T::Error: std::fmt::Debug,
{
    T::try_from(value.to_owned()).expect("test identity is canonical")
}

fn session(client: &str, session: &str) -> AuthorizedSession {
    AuthorizedSession {
        client_id: text::<ClientId>(client),
        selected_version: ProtocolVersion::try_from(2_u16).expect("version is canonical"),
        session_id: text::<SessionId>(session),
        authorization_epoch: AuthorizationEpoch::try_from(1_u64)
            .expect("authorization epoch is canonical"),
    }
}

fn request(subscription_id: Option<&str>) -> SubscriptionRequest {
    SubscriptionRequest {
        client_id: text("client-a"),
        subscription_id: subscription_id.map(text::<SubscriptionId>),
        expected_cursor: None,
        max_events: EventBatchLimit::try_from(16_u16).expect("event limit is canonical"),
    }
}

#[derive(Debug)]
struct DeterministicSource(u8);

impl SessionIdentitySource for DeterministicSource {
    fn fill_bytes(&mut self, destination: &mut [u8]) -> Result<(), SessionIdentityError> {
        for byte in destination {
            *byte = self.0;
            self.0 = self.0.wrapping_add(1);
        }
        Ok(())
    }
}

fn identities() -> RuntimeIdentityIssuer {
    RuntimeIdentityIssuer::new(&mut DeterministicSource(0)).expect("issuer initializes")
}

#[test]
fn initial_subscription_is_idempotent_and_exactly_session_bound() {
    let mut subscriptions = SubscriptionRegistry::new(2).expect("registry initializes");
    let mut identities = identities();
    let first_session = session("client-a", "session-a");
    let first = subscriptions
        .resolve(&first_session, &request(None), &mut identities)
        .expect("initial subscription succeeds");
    let replay = subscriptions
        .resolve(&first_session, &request(None), &mut identities)
        .expect("initial replay is idempotent");
    assert_eq!(first, replay);

    let mut continuation = request(Some(first.as_str()));
    continuation.client_id = first_session.client_id.clone();
    assert_eq!(
        subscriptions
            .resolve(&first_session, &continuation, &mut identities)
            .expect("exact continuation succeeds"),
        first
    );
    assert_eq!(subscriptions.len(), 1);

    assert_eq!(
        subscriptions.resolve(
            &session("client-a", "session-new"),
            &request(None),
            &mut identities
        ),
        Err(SubscriptionRegistryError::SubscriptionMismatch)
    );
}

#[test]
fn disconnect_removes_exact_subscription_and_old_identity_cannot_resume() {
    let mut subscriptions = SubscriptionRegistry::new(1).expect("registry initializes");
    let mut identities = identities();
    let first_session = session("client-a", "session-a");
    let issued = subscriptions
        .resolve(&first_session, &request(None), &mut identities)
        .expect("initial subscription succeeds");
    assert!(subscriptions.revoke_session(&first_session));
    assert!(subscriptions.is_empty());

    let new_session = session("client-a", "session-new");
    assert_eq!(
        subscriptions.resolve(
            &new_session,
            &request(Some(issued.as_str())),
            &mut identities
        ),
        Err(SubscriptionRegistryError::UnknownSubscription)
    );
    assert!(subscriptions.is_empty());
}
