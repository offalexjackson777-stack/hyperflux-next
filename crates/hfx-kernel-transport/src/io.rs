// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    HfxUapiBeginSession, HfxUapiEndSession, HfxUapiInfo, HfxUapiReadObservations, HfxUapiSubmit,
    HfxUapiTransactionResult, ioctl_sys,
};
use rustix::fs::{Mode, OFlags, open};
use rustix::time::{ClockId, clock_gettime};
use std::fmt;
use std::fs::File;
use std::path::Path;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct KernelIoError {
    raw_os_error: Option<i32>,
}

impl KernelIoError {
    #[must_use]
    pub const fn from_raw_os_error(raw_os_error: i32) -> Self {
        Self {
            raw_os_error: Some(raw_os_error),
        }
    }

    #[must_use]
    pub const fn raw_os_error(self) -> Option<i32> {
        self.raw_os_error
    }

    fn from_rustix(error: rustix::io::Errno) -> Self {
        Self::from_raw_os_error(error.raw_os_error())
    }

    pub(crate) const fn invariant() -> Self {
        Self { raw_os_error: None }
    }
}

impl fmt::Display for KernelIoError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("kernel boundary operation failed")
    }
}

impl std::error::Error for KernelIoError {}

pub trait KernelIo {
    /// Returns the Linux boottime clock in nanoseconds.
    ///
    /// # Errors
    ///
    /// Returns an error when the clock cannot be represented.
    fn boottime_ns(&self) -> Result<u64, KernelIoError>;
    /// Reads the bounded receiver and session header.
    ///
    /// # Errors
    ///
    /// Returns an error when the kernel operation fails.
    fn get_info(&self) -> Result<HfxUapiInfo, KernelIoError>;
    /// Begins one generation-bound writer session.
    ///
    /// # Errors
    ///
    /// Returns an error when the kernel rejects or cannot complete admission.
    fn begin_session(&self, request: &mut HfxUapiBeginSession) -> Result<(), KernelIoError>;
    /// Ends one exact writer session.
    ///
    /// # Errors
    ///
    /// Returns an error when the kernel cannot revoke the exact session.
    fn end_session(&self, request: HfxUapiEndSession) -> Result<(), KernelIoError>;
    /// Submits one bounded, already encoded transaction.
    ///
    /// # Errors
    ///
    /// Returns an error for validation, authorization, or transport failure.
    fn submit(&self, request: &mut HfxUapiSubmit) -> Result<(), KernelIoError>;
    /// Reconciles one exact kernel transaction identity.
    ///
    /// # Errors
    ///
    /// Returns an error when the result lookup cannot complete.
    fn get_transaction_result(
        &self,
        request: &mut HfxUapiTransactionResult,
    ) -> Result<(), KernelIoError>;
    /// Reads the next bounded passive-observation batch.
    ///
    /// # Errors
    ///
    /// Returns an error when the passive read cannot complete.
    fn read_observations(&self, request: &mut HfxUapiReadObservations)
    -> Result<(), KernelIoError>;
}

#[derive(Debug)]
pub struct LinuxKernelIo {
    file: File,
}

impl LinuxKernelIo {
    /// Opens one generation-scoped kernel receiver endpoint for passive reads.
    ///
    /// # Errors
    ///
    /// Returns a bounded error that does not include the private device path.
    pub fn open_read_only(path: &Path) -> Result<Self, KernelIoError> {
        open(
            path,
            OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
            Mode::empty(),
        )
        .map(|file| Self { file: file.into() })
        .map_err(KernelIoError::from_rustix)
    }

    /// Opens one generation-scoped kernel receiver endpoint for an admitted
    /// writer session.
    ///
    /// # Errors
    ///
    /// Returns a bounded error that does not include the private device path.
    pub fn open_read_write(path: &Path) -> Result<Self, KernelIoError> {
        open(
            path,
            OFlags::RDWR | OFlags::CLOEXEC | OFlags::NOFOLLOW,
            Mode::empty(),
        )
        .map(|file| Self { file: file.into() })
        .map_err(KernelIoError::from_rustix)
    }
}

impl KernelIo for LinuxKernelIo {
    fn boottime_ns(&self) -> Result<u64, KernelIoError> {
        let value = clock_gettime(ClockId::Boottime);
        let seconds = u64::try_from(value.tv_sec).map_err(|_| KernelIoError::invariant())?;
        let nanoseconds = u64::try_from(value.tv_nsec).map_err(|_| KernelIoError::invariant())?;
        seconds
            .checked_mul(1_000_000_000)
            .and_then(|base| base.checked_add(nanoseconds))
            .ok_or_else(KernelIoError::invariant)
    }

    fn get_info(&self) -> Result<HfxUapiInfo, KernelIoError> {
        ioctl_sys::get_info(&self.file).map_err(KernelIoError::from_rustix)
    }

    fn begin_session(&self, request: &mut HfxUapiBeginSession) -> Result<(), KernelIoError> {
        ioctl_sys::begin_session(&self.file, request).map_err(KernelIoError::from_rustix)
    }

    fn end_session(&self, request: HfxUapiEndSession) -> Result<(), KernelIoError> {
        ioctl_sys::end_session(&self.file, request).map_err(KernelIoError::from_rustix)
    }

    fn submit(&self, request: &mut HfxUapiSubmit) -> Result<(), KernelIoError> {
        ioctl_sys::submit(&self.file, request).map_err(KernelIoError::from_rustix)
    }

    fn get_transaction_result(
        &self,
        request: &mut HfxUapiTransactionResult,
    ) -> Result<(), KernelIoError> {
        ioctl_sys::get_transaction_result(&self.file, request).map_err(KernelIoError::from_rustix)
    }

    fn read_observations(
        &self,
        request: &mut HfxUapiReadObservations,
    ) -> Result<(), KernelIoError> {
        ioctl_sys::read_observations(&self.file, request).map_err(KernelIoError::from_rustix)
    }
}
