// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <optional>
#include <string>

namespace hyperflux::sdk
{

enum class ErrorCode
{
    InvalidArgument,
    IdentityUnavailable,
    IdentityExhausted,
    SocketCreate,
    SocketPath,
    SocketConnect,
    SocketConfigure,
    PeerCredentialMismatch,
    WriteFailed,
    ReadFailed,
    TruncatedFrame,
    InvalidFrame,
    PayloadTooLarge,
    InvalidJson,
    InvalidProtocol,
    NegotiationFailed,
    RequiredFeatureMissing,
    UnexpectedResponse,
    ResponseRequestMismatch,
    ServerInstanceChanged,
    ServerRejected,
    InvalidController,
    InvalidLightingFrame,
    MixedReceiverGeneration,
    OwnershipConflict,
    LeaseRejected,
    SessionInactive,
    ClockUnavailable,
    RuntimeConfiguration,
    RuntimeNotInitialized,
};

struct Error
{
    ErrorCode code;
    std::string message;
    std::optional<std::string> finding_id;

    friend bool operator==(const Error&, const Error&) = default;
};

} // namespace hyperflux::sdk
