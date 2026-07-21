// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::{LinuxMonotonicClock, LinuxWallClock};
use hfx_core::{Clock, WallClock};

#[test]
fn linux_monotonic_clock_never_moves_backwards_during_observation() {
    let clock = LinuxMonotonicClock;
    let first = clock.now();
    let second = clock.now();
    assert!(second >= first);
}

#[test]
fn linux_wall_clock_reports_a_plausible_unix_timestamp() {
    let timestamp = LinuxWallClock.now_unix_ms();
    assert!(timestamp.get() > 1_700_000_000_000);
}
