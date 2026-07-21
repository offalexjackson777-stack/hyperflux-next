// SPDX-License-Identifier: GPL-2.0-only

use crate::RequestIdentityError;
use hfx_protocol::{FrameError, RpcError};
use std::fmt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SdkMethod {
    Negotiate,
    Snapshot,
    IntegrationView,
    AcquireLease,
    RenewLease,
    ReleaseLease,
    SubmitTransaction,
    TransactionOutcome,
    Subscribe,
    Diagnostics,
}

impl SdkMethod {
    const fn label(self) -> &'static str {
        match self {
            Self::Negotiate => "negotiate",
            Self::Snapshot => "snapshot",
            Self::IntegrationView => "integration view",
            Self::AcquireLease => "acquire lease",
            Self::RenewLease => "renew lease",
            Self::ReleaseLease => "release lease",
            Self::SubmitTransaction => "submit transaction",
            Self::TransactionOutcome => "transaction outcome",
            Self::Subscribe => "subscribe",
            Self::Diagnostics => "diagnostics",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SdkError {
    Frame(FrameError),
    RequestIdentity(RequestIdentityError),
    ConnectionClosed,
    Server(RpcError),
    ResponseRequestMismatch,
    ServerInstanceMismatch,
    InvalidNegotiation,
    UnexpectedResponse { expected: SdkMethod },
}

impl fmt::Display for SdkError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Frame(error) => write!(formatter, "local bridge framing failed: {error}"),
            Self::RequestIdentity(error) => write!(formatter, "request identity failed: {error}"),
            Self::ConnectionClosed => formatter.write_str("the local bridge connection closed"),
            Self::Server(error) => write!(
                formatter,
                "the local bridge rejected the request ({})",
                error.finding_id
            ),
            Self::ResponseRequestMismatch => {
                formatter.write_str("the bridge response belongs to another request")
            }
            Self::ServerInstanceMismatch => {
                formatter.write_str("the bridge response belongs to another bridge instance")
            }
            Self::InvalidNegotiation => {
                formatter.write_str("the bridge returned an invalid protocol negotiation")
            }
            Self::UnexpectedResponse { expected } => {
                write!(
                    formatter,
                    "the bridge returned the wrong response to {}",
                    expected.label()
                )
            }
        }
    }
}

impl std::error::Error for SdkError {}

impl From<FrameError> for SdkError {
    fn from(error: FrameError) -> Self {
        Self::Frame(error)
    }
}

impl From<RequestIdentityError> for SdkError {
    fn from(error: RequestIdentityError) -> Self {
        Self::RequestIdentity(error)
    }
}
