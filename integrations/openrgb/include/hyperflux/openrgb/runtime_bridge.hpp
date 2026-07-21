// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <hyperflux/sdk/recovery.hpp>

#include <cstdint>
#include <memory>

namespace hyperflux::openrgb
{

/// Complete SDK surface required by the OpenRGB runtime.
///
/// Keeping this interface above `sdk::Client` makes generation, event, lease,
/// and transaction behavior independently testable without a live daemon.
class RuntimeBridge : public sdk::LightingBridge
{
public:
    ~RuntimeBridge() override = default;

    [[nodiscard]] virtual sdk::Result<v5::IntegrationView> integration_view() = 0;
    [[nodiscard]] virtual sdk::Result<v5::EventBatch> subscribe(
        sdk::EventSubscription subscription) = 0;
    [[nodiscard]] virtual std::uint64_t connection_epoch() const noexcept = 0;
};

/// Production adapter that preserves one serialized SDK connection.
class ClientRuntimeBridge final : public RuntimeBridge
{
public:
    [[nodiscard]] static sdk::Result<std::unique_ptr<ClientRuntimeBridge>> create(
        std::unique_ptr<sdk::ClientApi> client);
    explicit ClientRuntimeBridge(sdk::Client client);

    [[nodiscard]] sdk::Result<TransactionId> next_transaction_id() override;
    [[nodiscard]] sdk::Result<v5::IntegrationView> integration_view() override;
    [[nodiscard]] sdk::Result<v5::LeaseResult> acquire_lease(
        std::vector<v5::ResourceKey> resources,
        LeaseDurationMs duration_ms) override;
    [[nodiscard]] sdk::Result<v5::LeaseResult> renew_lease(
        LeaseId lease_id,
        LeaseDurationMs duration_ms) override;
    [[nodiscard]] sdk::Result<v5::LeaseResult> release_lease(
        LeaseId lease_id) override;
    [[nodiscard]] sdk::Result<v5::TransactionResult> submit_transaction(
        sdk::TransactionSubmission submission) override;
    [[nodiscard]] sdk::Result<v5::TransactionResult> transaction_outcome(
        TransactionId transaction_id) override;
    [[nodiscard]] sdk::Result<v5::EventBatch> subscribe(
        sdk::EventSubscription subscription) override;
    [[nodiscard]] std::uint64_t connection_epoch() const noexcept override;

private:
    struct ValidatedClientTag
    {
    };

    ClientRuntimeBridge(
        std::unique_ptr<sdk::ClientApi> client,
        ValidatedClientTag);

    std::unique_ptr<sdk::ClientApi> client_;
};

} // namespace hyperflux::openrgb
