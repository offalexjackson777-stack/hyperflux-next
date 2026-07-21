// SPDX-License-Identifier: GPL-2.0-only

use crate::{
    HFX_UAPI_IOCTL_MAGIC, HfxUapiBeginSession, HfxUapiEndSession, HfxUapiInfo,
    HfxUapiReadObservations, HfxUapiSubmit, HfxUapiTransactionResult,
};
use rustix::io;
use rustix::ioctl::{Getter, Setter, Updater, ioctl, opcode};
use std::os::fd::AsFd;

const GET_INFO: rustix::ioctl::Opcode = opcode::read::<HfxUapiInfo>(HFX_UAPI_IOCTL_MAGIC, 0x00);
const BEGIN_SESSION: rustix::ioctl::Opcode =
    opcode::read_write::<HfxUapiBeginSession>(HFX_UAPI_IOCTL_MAGIC, 0x01);
const END_SESSION: rustix::ioctl::Opcode =
    opcode::write::<HfxUapiEndSession>(HFX_UAPI_IOCTL_MAGIC, 0x02);
const SUBMIT: rustix::ioctl::Opcode =
    opcode::read_write::<HfxUapiSubmit>(HFX_UAPI_IOCTL_MAGIC, 0x03);
const GET_TRANSACTION_RESULT: rustix::ioctl::Opcode =
    opcode::read_write::<HfxUapiTransactionResult>(HFX_UAPI_IOCTL_MAGIC, 0x04);
const READ_OBSERVATIONS: rustix::ioctl::Opcode =
    opcode::read_write::<HfxUapiReadObservations>(HFX_UAPI_IOCTL_MAGIC, 0x05);

pub fn get_info(fd: impl AsFd) -> io::Result<HfxUapiInfo> {
    // SAFETY: the opcode is generated from the canonical UAPI type and the
    // getter owns an exactly matching output allocation.
    unsafe { ioctl(fd, Getter::<GET_INFO, HfxUapiInfo>::new()) }
}

pub fn begin_session(fd: impl AsFd, request: &mut HfxUapiBeginSession) -> io::Result<()> {
    // SAFETY: the read/write opcode and value type come from the same generated UAPI record.
    unsafe {
        ioctl(
            fd,
            Updater::<BEGIN_SESSION, HfxUapiBeginSession>::new(request),
        )
    }
}

pub fn end_session(fd: impl AsFd, request: HfxUapiEndSession) -> io::Result<()> {
    // SAFETY: the write opcode and copied input type come from the same generated UAPI record.
    unsafe { ioctl(fd, Setter::<END_SESSION, HfxUapiEndSession>::new(request)) }
}

pub fn submit(fd: impl AsFd, request: &mut HfxUapiSubmit) -> io::Result<()> {
    // SAFETY: the read/write opcode and value type come from the same generated UAPI record.
    unsafe { ioctl(fd, Updater::<SUBMIT, HfxUapiSubmit>::new(request)) }
}

pub fn get_transaction_result(
    fd: impl AsFd,
    request: &mut HfxUapiTransactionResult,
) -> io::Result<()> {
    // SAFETY: the read/write opcode and value type come from the same generated UAPI record.
    unsafe {
        ioctl(
            fd,
            Updater::<GET_TRANSACTION_RESULT, HfxUapiTransactionResult>::new(request),
        )
    }
}

pub fn read_observations(fd: impl AsFd, request: &mut HfxUapiReadObservations) -> io::Result<()> {
    // SAFETY: the read/write opcode and value type come from the same generated UAPI record.
    unsafe {
        ioctl(
            fd,
            Updater::<READ_OBSERVATIONS, HfxUapiReadObservations>::new(request),
        )
    }
}
