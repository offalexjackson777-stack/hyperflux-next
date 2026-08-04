// SPDX-License-Identifier: GPL-2.0-only

use rustix::rand::{GetRandomFlags, getrandom};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct EntropyUnavailable;

pub(crate) fn fill_random(destination: &mut [u8]) -> Result<(), EntropyUnavailable> {
    let mut filled = 0;
    while filled < destination.len() {
        match getrandom(&mut destination[filled..], GetRandomFlags::empty()) {
            Ok(0) => return Err(EntropyUnavailable),
            Ok(count) => filled += count,
            Err(rustix::io::Errno::INTR) => {}
            Err(_) => return Err(EntropyUnavailable),
        }
    }
    Ok(())
}
