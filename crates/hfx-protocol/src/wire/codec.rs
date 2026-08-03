// SPDX-License-Identifier: GPL-2.0-only

//! Size-bounded JSON encoding and decoding for protocol wire values.

use super::ProtocolWireError;
use crate::MAX_WIRE_MESSAGE_BYTES;
use serde::{Serialize, de::DeserializeOwned};
use std::io::{self, Write};

pub(super) fn decode<T: DeserializeOwned>(bytes: &[u8]) -> Result<T, ProtocolWireError> {
    if bytes.len() > MAX_WIRE_MESSAGE_BYTES {
        return Err(ProtocolWireError::MessageTooLarge);
    }
    serde_json::from_slice(bytes).map_err(|_| ProtocolWireError::MalformedJson)
}

struct BoundedEncoder {
    bytes: Vec<u8>,
    exceeded: bool,
}

impl BoundedEncoder {
    fn new() -> Self {
        Self {
            bytes: Vec::with_capacity(4096),
            exceeded: false,
        }
    }
}

impl Write for BoundedEncoder {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let Some(next_length) = self.bytes.len().checked_add(bytes.len()) else {
            self.exceeded = true;
            return Err(io::Error::other("bounded protocol encoding overflow"));
        };
        if next_length > MAX_WIRE_MESSAGE_BYTES {
            self.exceeded = true;
            return Err(io::Error::other("bounded protocol encoding exceeded"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

pub(super) fn encode<T: Serialize>(value: &T) -> Result<Vec<u8>, ProtocolWireError> {
    let mut encoded = BoundedEncoder::new();
    if serde_json::to_writer(&mut encoded, value).is_err() {
        return Err(if encoded.exceeded {
            ProtocolWireError::MessageTooLarge
        } else {
            ProtocolWireError::VersionTranslation
        });
    }
    if encoded.bytes.is_empty() {
        return Err(ProtocolWireError::VersionTranslation);
    }
    Ok(encoded.bytes)
}
