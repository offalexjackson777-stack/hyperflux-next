// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "result.hpp"

#include <hyperflux/generated/protocol_v5_types.hpp>

#include <cstdint>
#include <memory>
#include <optional>
#include <string>

namespace hyperflux::sdk
{

class RpcChannel
{
public:
    virtual ~RpcChannel() = default;

    [[nodiscard]] virtual Result<v5::RpcResponse> exchange(const v5::RpcRequest& request) = 0;
};

struct UnixChannelConfig
{
    std::string socket_path;
    std::uint32_t timeout_ms = 5'000;
    std::optional<std::uint32_t> expected_peer_uid;
};

class UnixRpcChannel final : public RpcChannel
{
public:
    ~UnixRpcChannel() override;
    UnixRpcChannel(const UnixRpcChannel&) = delete;
    UnixRpcChannel& operator=(const UnixRpcChannel&) = delete;
    UnixRpcChannel(UnixRpcChannel&& other) noexcept;
    UnixRpcChannel& operator=(UnixRpcChannel&& other) noexcept;

    [[nodiscard]] static Result<std::unique_ptr<UnixRpcChannel>> connect(
        const UnixChannelConfig& config);

    // Takes ownership of one already-connected local stream socket. This is
    // useful for supervised runtimes and deterministic socket-pair tests.
    [[nodiscard]] static Result<std::unique_ptr<UnixRpcChannel>> adopt_connected_socket(
        int file_descriptor,
        std::uint32_t timeout_ms);

    [[nodiscard]] Result<v5::RpcResponse> exchange(const v5::RpcRequest& request) override;

private:
    explicit UnixRpcChannel(int file_descriptor);

    int file_descriptor_ = -1;
};

} // namespace hyperflux::sdk
