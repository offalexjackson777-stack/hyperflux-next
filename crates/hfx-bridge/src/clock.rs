// SPDX-License-Identifier: GPL-2.0-only

use hfx_core::{Clock, WallClock};
use hfx_domain::{MonotonicMs, WallClockUnixMs};
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

/// Linux realtime clock used only for durable timestamps and audit records.
#[derive(Clone, Copy, Debug, Default)]
pub struct LinuxWallClock;

impl WallClock for LinuxWallClock {
    fn now_unix_ms(&self) -> WallClockUnixMs {
        let timestamp = clock_gettime(ClockId::Realtime);
        let seconds = u64::try_from(timestamp.tv_sec)
            .expect("Linux CLOCK_REALTIME cannot predate the Unix epoch");
        let nanoseconds = u64::try_from(timestamp.tv_nsec)
            .expect("Linux CLOCK_REALTIME cannot return negative nanoseconds");
        let milliseconds = seconds
            .saturating_mul(1_000)
            .saturating_add(nanoseconds / 1_000_000);
        WallClockUnixMs::try_from(milliseconds)
            .expect("every unsigned millisecond value is a canonical wall-clock instant")
    }
}
