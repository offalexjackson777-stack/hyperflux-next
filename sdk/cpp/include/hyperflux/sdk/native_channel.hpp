// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "channel.hpp"

#include <hyperflux/generated/protocol_v7_types.hpp>

#include <cstdint>
#include <memory>

namespace hyperflux::sdk
{

/// Protocol-v7 channel kept separate from the stable protocol-v5 lighting API.
class NativeRpcChannel
{
public:
    virtual ~NativeRpcChannel() = default;

    [[nodiscard]] virtual Result<v7::RpcResponse> exchange(
        const v7::RpcRequest& request) = 0;
};

class NativeUnixRpcChannel final : public NativeRpcChannel
{
public:
    ~NativeUnixRpcChannel() override;
    NativeUnixRpcChannel(const NativeUnixRpcChannel&) = delete;
    NativeUnixRpcChannel& operator=(const NativeUnixRpcChannel&) = delete;
    NativeUnixRpcChannel(NativeUnixRpcChannel&& other) noexcept;
    NativeUnixRpcChannel& operator=(NativeUnixRpcChannel&& other) noexcept;

    [[nodiscard]] static Result<std::unique_ptr<NativeUnixRpcChannel>> connect(
        const UnixChannelConfig& config);

    [[nodiscard]] static Result<std::unique_ptr<NativeUnixRpcChannel>> adopt_connected_socket(
        int file_descriptor,
        std::uint32_t timeout_ms);

    [[nodiscard]] Result<v7::RpcResponse> exchange(
        const v7::RpcRequest& request) override;

private:
    explicit NativeUnixRpcChannel(int file_descriptor);

    int file_descriptor_ = -1;
};

} // namespace hyperflux::sdk
