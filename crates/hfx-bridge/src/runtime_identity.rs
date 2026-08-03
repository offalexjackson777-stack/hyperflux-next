// SPDX-License-Identifier: GPL-2.0-only

use crate::{SessionIdentityError, SessionIdentitySource};
use hfx_domain::{
    DispatchNonce, LeaseId, NativeRouteToken, RequestId, SubscriptionId, TransactionId,
};
use sha2::{Digest, Sha256};
use std::fmt;

const PROCESS_PREFIX_BYTES: usize = 16;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RuntimeIdentityError {
    Entropy(SessionIdentityError),
    SequenceExhausted,
    InvalidGeneratedIdentity,
}

impl fmt::Display for RuntimeIdentityError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Entropy(_) => "runtime identity entropy is unavailable",
            Self::SequenceExhausted => "runtime identity sequence is exhausted",
            Self::InvalidGeneratedIdentity => "generated runtime identity is invalid",
        })
    }
}

impl std::error::Error for RuntimeIdentityError {}

/// Collision-free process-scoped identities for runtime-owned objects.
///
/// The random prefix separates bridge processes. The checked sequence makes
/// identities deterministic and non-repeating inside one process without
/// relying on an unbounded collision registry.
#[derive(Clone, Debug)]
pub struct RuntimeIdentityIssuer {
    process_prefix: String,
    next_sequence: u64,
}

impl RuntimeIdentityIssuer {
    /// Creates one issuer from operating-system or deterministic test entropy.
    ///
    /// # Errors
    ///
    /// Returns an error when the complete process prefix cannot be initialized.
    pub fn new<S: SessionIdentitySource>(source: &mut S) -> Result<Self, RuntimeIdentityError> {
        let mut bytes = [0_u8; PROCESS_PREFIX_BYTES];
        source
            .fill_bytes(&mut bytes)
            .map_err(RuntimeIdentityError::Entropy)?;
        let mut process_prefix = String::with_capacity(PROCESS_PREFIX_BYTES * 2);
        for byte in bytes {
            use std::fmt::Write as _;
            write!(process_prefix, "{byte:02x}")
                .map_err(|_| RuntimeIdentityError::InvalidGeneratedIdentity)?;
        }
        Ok(Self {
            process_prefix,
            next_sequence: 1,
        })
    }

    /// Issues one process-unique lease identity.
    ///
    /// # Errors
    ///
    /// Returns an error only after the finite process sequence is exhausted.
    pub fn lease_id(&mut self) -> Result<LeaseId, RuntimeIdentityError> {
        let sequence = self.take_sequence()?;
        self.string_identity("lease", sequence)
    }

    /// Issues one process-unique event subscription identity.
    ///
    /// # Errors
    ///
    /// Returns an error only after the finite process sequence is exhausted.
    pub fn subscription_id(&mut self) -> Result<SubscriptionId, RuntimeIdentityError> {
        let sequence = self.take_sequence()?;
        self.string_identity("subscription", sequence)
    }

    /// Issues one process-unique request identity for bridge-owned replay.
    ///
    /// # Errors
    ///
    /// Returns an error only after the finite process sequence is exhausted.
    pub fn request_id(&mut self) -> Result<RequestId, RuntimeIdentityError> {
        let sequence = self.take_sequence()?;
        self.string_identity("request", sequence)
    }

    /// Issues one process-unique transaction identity for bridge-owned replay.
    ///
    /// # Errors
    ///
    /// Returns an error only after the finite process sequence is exhausted.
    pub fn transaction_id(&mut self) -> Result<TransactionId, RuntimeIdentityError> {
        let sequence = self.take_sequence()?;
        self.string_identity("transaction", sequence)
    }

    /// Issues one nonzero dispatch nonce that cannot repeat in this process.
    ///
    /// # Errors
    ///
    /// Returns an error only after the finite process sequence is exhausted.
    pub fn dispatch_nonce(&mut self) -> Result<DispatchNonce, RuntimeIdentityError> {
        let sequence = self.take_sequence()?;
        DispatchNonce::try_from(sequence)
            .map_err(|_| RuntimeIdentityError::InvalidGeneratedIdentity)
    }

    /// Issues one opaque process-local native route token.
    ///
    /// The token contains no host path or hardware serial. Its digest binds a
    /// kernel exchange while lease and generation checks remain the authority.
    ///
    /// # Errors
    ///
    /// Returns an error only after the finite process sequence is exhausted or
    /// if the generated token violates the domain contract.
    pub fn native_route_token(&mut self) -> Result<NativeRouteToken, RuntimeIdentityError> {
        let sequence = self.take_sequence()?;
        let mut hasher = Sha256::new();
        hasher.update(b"hyperflux-native-route-token-v1\0");
        hasher.update(self.process_prefix.as_bytes());
        hasher.update(sequence.to_be_bytes());
        let digest = hasher.finalize();
        let mut value = String::with_capacity(64);
        for byte in digest {
            use std::fmt::Write as _;
            write!(value, "{byte:02x}")
                .map_err(|_| RuntimeIdentityError::InvalidGeneratedIdentity)?;
        }
        NativeRouteToken::try_from(value)
            .map_err(|_| RuntimeIdentityError::InvalidGeneratedIdentity)
    }

    fn take_sequence(&mut self) -> Result<u64, RuntimeIdentityError> {
        let current = self.next_sequence;
        self.next_sequence = current
            .checked_add(1)
            .ok_or(RuntimeIdentityError::SequenceExhausted)?;
        Ok(current)
    }

    fn string_identity<T>(&self, kind: &str, sequence: u64) -> Result<T, RuntimeIdentityError>
    where
        T: TryFrom<String>,
    {
        T::try_from(format!("{kind}-{}-{sequence:016x}", self.process_prefix))
            .map_err(|_| RuntimeIdentityError::InvalidGeneratedIdentity)
    }
}
