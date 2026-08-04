// SPDX-License-Identifier: GPL-2.0-only

use hfx_domain::RequestId;
use rustix::io::Errno;
use rustix::rand::{GetRandomFlags, getrandom};
use std::fmt;

const REQUEST_ID_BYTES: usize = 16;
const HEX: &[u8; 16] = b"0123456789abcdef";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RequestIdentityError {
    EntropyUnavailable,
    InvalidGeneratedIdentity,
}

impl fmt::Display for RequestIdentityError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::EntropyUnavailable => "operating-system identity entropy is unavailable",
            Self::InvalidGeneratedIdentity => "generated request identity is invalid",
        })
    }
}

impl std::error::Error for RequestIdentityError {}

pub trait RequestIdentitySource {
    /// Returns one process-local unique request identity.
    ///
    /// # Errors
    ///
    /// Returns an error when a strong identity cannot be generated.
    fn next_request_id(&mut self) -> Result<RequestId, RequestIdentityError>;
}

#[derive(Clone, Copy, Debug, Default)]
pub struct KernelRequestIdentitySource;

impl RequestIdentitySource for KernelRequestIdentitySource {
    fn next_request_id(&mut self) -> Result<RequestId, RequestIdentityError> {
        let mut bytes = [0_u8; REQUEST_ID_BYTES];
        let mut filled = 0;
        while filled < bytes.len() {
            match getrandom(&mut bytes[filled..], GetRandomFlags::empty()) {
                Ok(0) => return Err(RequestIdentityError::EntropyUnavailable),
                Ok(count) => filled += count,
                Err(Errno::INTR) => {}
                Err(_) => return Err(RequestIdentityError::EntropyUnavailable),
            }
        }

        let mut value = String::with_capacity("sdk-request-".len() + bytes.len() * 2);
        value.push_str("sdk-request-");
        for byte in bytes {
            value.push(char::from(HEX[usize::from(byte >> 4)]));
            value.push(char::from(HEX[usize::from(byte & 0x0f)]));
        }
        RequestId::try_from(value).map_err(|_| RequestIdentityError::InvalidGeneratedIdentity)
    }
}
