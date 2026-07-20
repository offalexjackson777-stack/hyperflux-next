// SPDX-License-Identifier: GPL-2.0-only

use hfx_core::Clock;
use hfx_domain::MonotonicMs;
use rustix::time::{ClockId, clock_gettime};

/// Linux monotonic clock used for bridge-local deadlines and lease expiry.
#[derive(Clone, Copy, Debug, Default)]
pub struct LinuxMonotonicClock;

impl Clock for LinuxMonotonicClock {
    fn now(&self) -> MonotonicMs {
        let timestamp = clock_gettime(ClockId::Monotonic);
        let seconds = u64::try_from(timestamp.tv_sec)
            .expect("Linux CLOCK_MONOTONIC cannot return negative seconds");
        let nanoseconds = u64::try_from(timestamp.tv_nsec)
            .expect("Linux CLOCK_MONOTONIC cannot return negative nanoseconds");
        let milliseconds = seconds
            .saturating_mul(1_000)
            .saturating_add(nanoseconds / 1_000_000);
        MonotonicMs::try_from(milliseconds)
            .expect("every unsigned millisecond value is a canonical monotonic instant")
    }
}
