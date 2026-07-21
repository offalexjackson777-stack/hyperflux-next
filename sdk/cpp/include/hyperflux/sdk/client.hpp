// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "channel.hpp"
#include "identity.hpp"
#include "result.hpp"

#include <hyperflux/generated/protocol_v5_types.hpp>

#include <memory>
#include <optional>
#include <vector>

namespace hyperflux::sdk
{

struct ClientConfig
{
    ClientId client_id;
    ClientName client_name;
    std::vector<ProtocolFeatureId> required_features;
    std::vector<ProtocolFeatureId> optional_features;
};

struct EventSubscription
{
    std::optional<SubscriptionId> subscription_id;
    std::optional<v5::EventCursor> expected_cursor;
    EventBatchLimit max_events;
};

struct TransactionSubmission
{
    TransactionId transaction_id;
    LeaseId lease_id;
    ReceiverId receiver_id;
    GenerationId generation_id;
    ProfileId receiver_profile_id;
    ProfileDigest receiver_profile_digest;
    std::vector<v5::DeviceProfileBinding> device_profiles;
    TransactionClass transaction_class;
    std::vector<v5::StableLightingIntent> stable_intents;
    MonotonicMs deadline_ms;
    std::vector<v5::ResourceKey> resources;
    std::vector<v5::LightingFrame> frames;
};

class LightingBridge
{
public:
    virtual ~LightingBridge() = default;

    [[nodiscard]] virtual Result<TransactionId> next_transaction_id() = 0;
    [[nodiscard]] virtual Result<v5::LeaseResult> acquire_lease(
        std::vector<v5::ResourceKey> resources,
        LeaseDurationMs duration_ms) = 0;
    [[nodiscard]] virtual Result<v5::LeaseResult> renew_lease(
        LeaseId lease_id,
        LeaseDurationMs duration_ms) = 0;
    [[nodiscard]] virtual Result<v5::LeaseResult> release_lease(LeaseId lease_id) = 0;
    [[nodiscard]] virtual Result<v5::TransactionResult> submit_transaction(
        TransactionSubmission submission) = 0;
    [[nodiscard]] virtual Result<v5::TransactionResult> transaction_outcome(
        TransactionId transaction_id) = 0;
};

class Client final : public LightingBridge
{
public:
    Client(const Client&) = delete;
    Client& operator=(const Client&) = delete;
    Client(Client&&) noexcept = default;
    Client& operator=(Client&&) noexcept = default;

    [[nodiscard]] static Result<Client> connect(
        std::unique_ptr<RpcChannel> channel,
        std::unique_ptr<IdentitySource> identities,
        ClientConfig config);

    [[nodiscard]] static Result<Client> connect_unix(
        const UnixChannelConfig& channel_config,
        ClientConfig client_config);

    [[nodiscard]] const v5::ServerHello& server_hello() const noexcept;
    [[nodiscard]] const ClientId& client_id() const noexcept;

    [[nodiscard]] Result<TransactionId> next_transaction_id() override;
    [[nodiscard]] Result<v5::BridgeSnapshot> snapshot();
    [[nodiscard]] Result<v5::IntegrationView> integration_view();
    [[nodiscard]] Result<v5::LeaseResult> acquire_lease(
        std::vector<v5::ResourceKey> resources,
        LeaseDurationMs duration_ms) override;
    [[nodiscard]] Result<v5::LeaseResult> renew_lease(
        LeaseId lease_id,
        LeaseDurationMs duration_ms) override;
    [[nodiscard]] Result<v5::LeaseResult> release_lease(LeaseId lease_id) override;
    [[nodiscard]] Result<v5::TransactionResult> submit_transaction(
        TransactionSubmission submission) override;
    [[nodiscard]] Result<v5::TransactionResult> transaction_outcome(
        TransactionId transaction_id) override;
    [[nodiscard]] Result<v5::EventBatch> subscribe(EventSubscription subscription);
    [[nodiscard]] Result<v5::DiagnosticSnapshot> diagnostics();

private:
    Client(
        std::unique_ptr<RpcChannel> channel,
        std::unique_ptr<IdentitySource> identities,
        ClientId client_id,
        v5::ServerHello hello);

    template<typename T>
    [[nodiscard]] v5::SessionRequestEnvelope<T> envelope(RequestId request_id, T params) const
    {
        return {
            std::move(request_id),
            hello_.protocol_session_id,
            hello_.negotiation_token,
            std::move(params),
        };
    }

    [[nodiscard]] Result<RequestId> next_request_id();
    [[nodiscard]] Result<v5::RpcResponse> exchange_checked(
        const v5::RpcRequest& request,
        const RequestId& request_id);

    std::unique_ptr<RpcChannel> channel_;
    std::unique_ptr<IdentitySource> identities_;
    ClientId client_id_;
    v5::ServerHello hello_;
};

} // namespace hyperflux::sdk
