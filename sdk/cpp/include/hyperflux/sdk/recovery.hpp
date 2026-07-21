// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "client.hpp"

#include <cstdint>
#include <memory>

namespace hyperflux::sdk
{

[[nodiscard]] bool is_connection_error(ErrorCode code) noexcept;

class ClientFactory
{
public:
    virtual ~ClientFactory() = default;

    [[nodiscard]] virtual Result<std::unique_ptr<ClientApi>> connect() = 0;
};

class UnixClientFactory final : public ClientFactory
{
public:
    UnixClientFactory(UnixChannelConfig channel_config, ClientConfig client_config);

    [[nodiscard]] Result<std::unique_ptr<ClientApi>> connect() override;

private:
    UnixChannelConfig channel_config_;
    ClientConfig client_config_;
};

/// Reconnects SDK transport without replaying ambiguous hardware operations.
class RecoveringClient final : public ClientApi
{
public:
    RecoveringClient(const RecoveringClient&) = delete;
    RecoveringClient& operator=(const RecoveringClient&) = delete;
    RecoveringClient(RecoveringClient&&) = delete;
    RecoveringClient& operator=(RecoveringClient&&) = delete;

    /// Creates a disconnected client. The first SDK operation establishes the
    /// connection, allowing application plugins to load before the service.
    [[nodiscard]] static Result<std::unique_ptr<RecoveringClient>> create(
        std::unique_ptr<ClientFactory> factory);

    /// Creates a client and verifies the first connection immediately.
    [[nodiscard]] static Result<std::unique_ptr<RecoveringClient>> connect(
        std::unique_ptr<ClientFactory> factory);

    [[nodiscard]] std::uint64_t connection_epoch() const noexcept override;
    [[nodiscard]] Result<TransactionId> next_transaction_id() override;
    [[nodiscard]] Result<v5::BridgeSnapshot> snapshot() override;
    [[nodiscard]] Result<v5::IntegrationView> integration_view() override;
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
    [[nodiscard]] Result<v5::EventBatch> subscribe(
        EventSubscription subscription) override;
    [[nodiscard]] Result<v5::DiagnosticSnapshot> diagnostics() override;

private:
    RecoveringClient(
        std::unique_ptr<ClientFactory> factory,
        std::unique_ptr<ClientApi> connection);

    [[nodiscard]] Result<void> ensure_connected();
    [[nodiscard]] Result<void> reconnect();
    [[nodiscard]] Result<v5::TransactionResult> unknown_outcome(
        TransactionId transaction_id) const;

    template<typename T, typename Operation>
    [[nodiscard]] Result<T> retry_read(Operation operation)
    {
        auto ready = ensure_connected();
        if(!ready)
        {
            return Result<T>::failure(ready.error());
        }
        auto result = operation();
        if(result || !is_connection_error(result.error().code))
        {
            return result;
        }
        auto reconnected = reconnect();
        if(!reconnected)
        {
            return Result<T>::failure(reconnected.error());
        }
        return operation();
    }

    std::unique_ptr<ClientFactory> factory_;
    std::unique_ptr<ClientApi> connection_;
    std::uint64_t connection_epoch_ = 0;
};

} // namespace hyperflux::sdk
