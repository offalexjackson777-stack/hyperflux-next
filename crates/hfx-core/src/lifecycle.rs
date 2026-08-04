// SPDX-License-Identifier: GPL-2.0-only

//! Generation-bound receiver and logical-device lifecycle policy.
//!
//! This facade preserves the established public lifecycle surface while the
//! fact model, receiver machine, and return-epoch policy remain independently
//! reviewable.

#[path = "lifecycle/facts.rs"]
mod facts;
#[path = "lifecycle/machine.rs"]
mod machine;
#[path = "lifecycle/return_epoch.rs"]
mod return_epoch;

#[allow(unused_imports)]
pub use facts::*;
#[allow(unused_imports)]
pub use machine::*;
#[allow(unused_imports)]
pub use return_epoch::*;
