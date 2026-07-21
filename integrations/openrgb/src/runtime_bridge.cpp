// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/runtime_bridge.hpp>

#include <utility>

namespace hyperflux::openrgb
{

sdk::Result<std::unique_ptr<ClientRuntimeBridge>> ClientRuntimeBridge::create(
    std::unique_ptr<sdk::ClientApi> client)
{
    if(client == nullptr)
    {
        return sdk::Result<std::unique_ptr<ClientRuntimeBridge>>::failure({
            sdk::ErrorCode::RuntimeConfiguration,
            "OpenRGB runtime bridge requires a connected SDK client",
            "HFX-RUNTIME-001",
        });
    }
    return sdk::Result<std::unique_ptr<ClientRuntimeBridge>>::success(
        std::unique_ptr<ClientRuntimeBridge>(new ClientRuntimeBridge(
            std::move(client),
            ValidatedClientTag {})));
}

ClientRuntimeBridge::ClientRuntimeBridge(
    std::unique_ptr<sdk::ClientApi> client,
    ValidatedClientTag)
    : client_(std::move(client))
{
}

ClientRuntimeBridge::ClientRuntimeBridge(sdk::Client client)
    : ClientRuntimeBridge(
          std::make_unique<sdk::Client>(std::move(client)),
          ValidatedClientTag {})
{
}

sdk::Result<TransactionId> ClientRuntimeBridge::next_transaction_id()
{
    return client_->next_transaction_id();
}

sdk::Result<v5::IntegrationView> ClientRuntimeBridge::integration_view()
{
    return client_->integration_view();
}

sdk::Result<v5::LeaseResult> ClientRuntimeBridge::acquire_lease(
    std::vector<v5::ResourceKey> resources,
    LeaseDurationMs duration_ms)
{
    return client_->acquire_lease(std::move(resources), duration_ms);
}

sdk::Result<v5::LeaseResult> ClientRuntimeBridge::renew_lease(
    LeaseId lease_id,
    LeaseDurationMs duration_ms)
{
    return client_->renew_lease(std::move(lease_id), duration_ms);
}

sdk::Result<v5::LeaseResult> ClientRuntimeBridge::release_lease(LeaseId lease_id)
{
    return client_->release_lease(std::move(lease_id));
}

sdk::Result<v5::TransactionResult> ClientRuntimeBridge::submit_transaction(
    sdk::TransactionSubmission submission)
{
    return client_->submit_transaction(std::move(submission));
}

sdk::Result<v5::TransactionResult> ClientRuntimeBridge::transaction_outcome(
    TransactionId transaction_id)
{
    return client_->transaction_outcome(std::move(transaction_id));
}

sdk::Result<v5::EventBatch> ClientRuntimeBridge::subscribe(
    sdk::EventSubscription subscription)
{
    return client_->subscribe(std::move(subscription));
}

std::uint64_t ClientRuntimeBridge::connection_epoch() const noexcept
{
    return client_->connection_epoch();
}

} // namespace hyperflux::openrgb
