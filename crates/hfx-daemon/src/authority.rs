// SPDX-License-Identifier: GPL-2.0-only

use crate::entropy::{EntropyUnavailable, fill_random};
use hfx_domain::{ProfileDigest, ProfileKind};
use hfx_profiles::{RuntimeProfile, RuntimeProfileCatalog};
use sha2::{Digest, Sha256};
use std::fmt;

pub const DAEMON_NONCE_BYTES: usize = 32;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WriterAuthorityError {
    InvalidReceiverProfile,
    EntropyUnavailable,
}

impl fmt::Display for WriterAuthorityError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidReceiverProfile => "writer authority receiver profile is invalid",
            Self::EntropyUnavailable => "writer authority entropy is unavailable",
        })
    }
}

impl std::error::Error for WriterAuthorityError {}

/// Derives the complete software-qualified authority for one receiver family.
/// Current pairing is intentionally absent: compatible children may appear or
/// disappear without changing the process's universal profile authority.
///
/// # Errors
///
/// Rejects a non-receiver profile or receiver without a protocol family.
pub fn derive_capability_digest(
    catalog: &RuntimeProfileCatalog,
    receiver: &RuntimeProfile,
) -> Result<[u8; 32], WriterAuthorityError> {
    if receiver.profile_kind != ProfileKind::Receiver || receiver.protocol_family.is_none() {
        return Err(WriterAuthorityError::InvalidReceiverProfile);
    }
    let protocol_family = receiver
        .protocol_family
        .ok_or(WriterAuthorityError::InvalidReceiverProfile)?;
    let child_digests = catalog
        .iter()
        .filter(|profile| {
            profile.profile_kind == ProfileKind::Child
                && receiver
                    .supported_child_kinds
                    .contains(&profile.device_kind)
                && profile.receiver_protocols.contains(&protocol_family)
        })
        .map(|profile| profile.runtime_digest.clone())
        .collect::<Vec<_>>();
    Ok(digest_material(&receiver.runtime_digest, &child_digests))
}

/// Creates one nonzero process nonce for all generation-scoped kernel
/// sessions owned by this daemon instance.
///
/// # Errors
///
/// Fails closed when operating-system entropy is unavailable.
pub fn generate_daemon_nonce() -> Result<[u8; DAEMON_NONCE_BYTES], WriterAuthorityError> {
    let mut nonce = [0_u8; DAEMON_NONCE_BYTES];
    fill_random(&mut nonce)
        .map_err(|EntropyUnavailable| WriterAuthorityError::EntropyUnavailable)?;
    if nonce.iter().all(|byte| *byte == 0) {
        return Err(WriterAuthorityError::EntropyUnavailable);
    }
    Ok(nonce)
}

fn digest_material(receiver: &ProfileDigest, children: &[ProfileDigest]) -> [u8; 32] {
    let mut children = children.to_vec();
    children.sort();
    let mut digest = Sha256::new();
    digest.update(b"hyperflux-capability-authority-v1\0");
    update_text(&mut digest, receiver.as_str());
    digest.update(
        u64::try_from(children.len())
            .unwrap_or(u64::MAX)
            .to_be_bytes(),
    );
    for child in &children {
        update_text(&mut digest, child.as_str());
    }
    digest.finalize().into()
}

fn update_text(digest: &mut Sha256, value: &str) {
    digest.update(u64::try_from(value.len()).unwrap_or(u64::MAX).to_be_bytes());
    digest.update(value.as_bytes());
}

#[cfg(test)]
mod tests {
    use super::*;

    fn profile_digest(value: char) -> ProfileDigest {
        ProfileDigest::try_from(value.to_string().repeat(64)).expect("digest is valid")
    }

    #[test]
    fn capability_digest_is_order_independent_and_profile_sensitive() {
        let receiver = profile_digest('a');
        let child_b = profile_digest('b');
        let child_c = profile_digest('c');
        assert_eq!(
            digest_material(&receiver, &[child_b.clone(), child_c.clone()]),
            digest_material(&receiver, &[child_c, child_b.clone()])
        );
        assert_ne!(
            digest_material(&receiver, &[child_b]),
            digest_material(&receiver, &[])
        );
    }

    #[test]
    fn generated_catalog_produces_nonzero_universal_authority() {
        let catalog = RuntimeProfileCatalog::load().expect("catalog loads");
        let receiver = catalog
            .iter()
            .find(|profile| profile.profile_kind == ProfileKind::Receiver)
            .expect("receiver profile exists");
        let digest = derive_capability_digest(&catalog, receiver).expect("authority derives");
        assert!(digest.iter().any(|byte| *byte != 0));
    }

    #[test]
    fn process_nonce_is_nonzero() {
        let nonce = generate_daemon_nonce().expect("entropy is available");
        assert!(nonce.iter().any(|byte| *byte != 0));
    }
}
