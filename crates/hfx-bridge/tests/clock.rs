// SPDX-License-Identifier: GPL-2.0-only

use hfx_bridge::LinuxMonotonicClock;
use hfx_core::Clock;

#[test]
fn linux_monotonic_clock_never_moves_backwards_during_observation() {
    let clock = LinuxMonotonicClock;
    let first = clock.now();
    let second = clock.now();
    assert!(second >= first);
}
