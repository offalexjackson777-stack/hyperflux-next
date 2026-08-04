// SPDX-License-Identifier: GPL-2.0-only

use std::fmt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct VirtualClock {
    now_ms: u64,
}

impl VirtualClock {
    #[must_use]
    pub const fn new(now_ms: u64) -> Self {
        Self { now_ms }
    }

    #[must_use]
    pub const fn now_ms(self) -> u64 {
        self.now_ms
    }

    /// Advances to an absolute virtual timestamp.
    ///
    /// # Errors
    ///
    /// Returns [`ClockError::WentBackwards`] when the target predates the
    /// current virtual timestamp.
    pub fn advance_to(&mut self, target_ms: u64) -> Result<(), ClockError> {
        if target_ms < self.now_ms {
            return Err(ClockError::WentBackwards {
                current_ms: self.now_ms,
                target_ms,
            });
        }
        self.now_ms = target_ms;
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ClockError {
    WentBackwards { current_ms: u64, target_ms: u64 },
}

impl fmt::Display for ClockError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::WentBackwards {
                current_ms,
                target_ms,
            } => write!(
                formatter,
                "virtual time cannot move from {current_ms} ms to {target_ms} ms"
            ),
        }
    }
}

impl std::error::Error for ClockError {}
