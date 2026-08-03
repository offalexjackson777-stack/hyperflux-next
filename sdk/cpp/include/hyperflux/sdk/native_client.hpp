// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "identity.hpp"
#include "native_channel.hpp"

#include <hyperflux/generated/protocol_v7_types.hpp>

#include <memory>
#include <optional>
#include <vector>

namespace hyperflux::sdk
{

struct NativeClientConfig
{
    ClientId client_id;
    ClientName client_name;
    std::vector<ProtocolFeatureId> required_features;
    std::vector<ProtocolFeatureId> optional_features;
};

struct NativeEventSubscription
{
    std::optional<SubscriptionId> subscription_id;
    std::optional<v7::EventCursor> expected_cursor;
    EventBatchLimit max_events;
};

struct NativeExchangeSubmission
{
    RequestId request_id;
    LeaseId lease_id;
    NativeRouteToken route_token;
    NativeReportHex report;
    NativeExchangeTimeoutMs timeout_ms;
};

class NativeClientApi
{
public:
    virtual ~NativeClientApi() = default;

    [[nodiscard]] virtual Result<v7::LeaseResult> acquire_lease(
        std::vector<v7::ResourceKey> resources,
        LeaseDurationMs duration_ms) = 0;
    [[nodiscard]] virtual Result<v7::LeaseResult> renew_lease(
        LeaseId lease_id,
        LeaseDurationMs duration_ms) = 0;
    [[nodiscard]] virtual Result<v7::LeaseResult> release_lease(LeaseId lease_id) = 0;
    [[nodiscard]] virtual Result<v7::NativeRouteInventory> native_enumerate() = 0;
    [[nodiscard]] virtual Result<NativeExchangeSubmission> prepare_native_exchange(
        LeaseId lease_id,
        NativeRouteToken route_token,
        NativeReportHex report,
        NativeExchangeTimeoutMs timeout_ms) = 0;
    [[nodiscard]] virtual Result<v7::NativeExchangeOutcome> native_transact(
        const NativeExchangeSubmission& submission) = 0;
    [[nodiscard]] virtual Result<v7::NativeRouteEventBatch> native_watch(
        NativeEventSubscription subscription) = 0;
};

/// Protocol-v7 client for exact receiver-backed native feature exchanges.
class NativeClient final : public NativeClientApi
{
public:
    NativeClient(const NativeClient&) = delete;
    NativeClient& operator=(const NativeClient&) = delete;
    NativeClient(NativeClient&&) noexcept = default;
    NativeClient& operator=(NativeClient&&) noexcept = default;

    [[nodiscard]] static Result<NativeClient> connect(
        std::unique_ptr<NativeRpcChannel> channel,
        std::unique_ptr<IdentitySource> identities,
        NativeClientConfig config);

    [[nodiscard]] static Result<NativeClient> connect_unix(
        const UnixChannelConfig& channel_config,
        NativeClientConfig client_config);

    [[nodiscard]] const v7::ServerHello& server_hello() const noexcept;
    [[nodiscard]] const ClientId& client_id() const noexcept;

    [[nodiscard]] Result<v7::LeaseResult> acquire_lease(
        std::vector<v7::ResourceKey> resources,
        LeaseDurationMs duration_ms) override;
    [[nodiscard]] Result<v7::LeaseResult> renew_lease(
        LeaseId lease_id,
        LeaseDurationMs duration_ms) override;
    [[nodiscard]] Result<v7::LeaseResult> release_lease(LeaseId lease_id) override;
    [[nodiscard]] Result<v7::NativeRouteInventory> native_enumerate() override;
    [[nodiscard]] Result<NativeExchangeSubmission> prepare_native_exchange(
        LeaseId lease_id,
        NativeRouteToken route_token,
        NativeReportHex report,
        NativeExchangeTimeoutMs timeout_ms) override;
    [[nodiscard]] Result<v7::NativeExchangeOutcome> native_transact(
        const NativeExchangeSubmission& submission) override;
    [[nodiscard]] Result<v7::NativeRouteEventBatch> native_watch(
        NativeEventSubscription subscription) override;

private:
    NativeClient(
        std::unique_ptr<NativeRpcChannel> channel,
        std::unique_ptr<IdentitySource> identities,
        ClientId client_id,
        v7::ServerHello hello);

    template<typename T>
    [[nodiscard]] v7::SessionRequestEnvelope<T> envelope(RequestId request_id, T params) const
    {
        return {
            std::move(request_id),
            hello_.protocol_session_id,
            hello_.negotiation_token,
            std::move(params),
        };
    }

    [[nodiscard]] Result<RequestId> next_request_id();
    [[nodiscard]] Result<v7::RpcResponse> exchange_checked(
        const v7::RpcRequest& request,
        const RequestId& request_id);

    std::unique_ptr<NativeRpcChannel> channel_;
    std::unique_ptr<IdentitySource> identities_;
    ClientId client_id_;
    v7::ServerHello hello_;
};

} // namespace hyperflux::sdk
