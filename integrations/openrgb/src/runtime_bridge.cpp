// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/runtime_bridge.hpp>

#include <utility>

namespace hyperflux::openrgb
{

ClientRuntimeBridge::ClientRuntimeBridge(sdk::Client client) : client_(std::move(client)) {}

sdk::Result<TransactionId> ClientRuntimeBridge::next_transaction_id()
{
    return client_.next_transaction_id();
}

sdk::Result<v5::IntegrationView> ClientRuntimeBridge::integration_view()
{
    return client_.integration_view();
}

sdk::Result<v5::LeaseResult> ClientRuntimeBridge::acquire_lease(
    std::vector<v5::ResourceKey> resources,
    LeaseDurationMs duration_ms)
{
    return client_.acquire_lease(std::move(resources), duration_ms);
}

sdk::Result<v5::LeaseResult> ClientRuntimeBridge::renew_lease(
    LeaseId lease_id,
    LeaseDurationMs duration_ms)
{
    return client_.renew_lease(std::move(lease_id), duration_ms);
}

sdk::Result<v5::LeaseResult> ClientRuntimeBridge::release_lease(LeaseId lease_id)
{
    return client_.release_lease(std::move(lease_id));
}

sdk::Result<v5::TransactionResult> ClientRuntimeBridge::submit_transaction(
    sdk::TransactionSubmission submission)
{
    return client_.submit_transaction(std::move(submission));
}

sdk::Result<v5::TransactionResult> ClientRuntimeBridge::transaction_outcome(
    TransactionId transaction_id)
{
    return client_.transaction_outcome(std::move(transaction_id));
}

sdk::Result<v5::EventBatch> ClientRuntimeBridge::subscribe(
    sdk::EventSubscription subscription)
{
    return client_.subscribe(std::move(subscription));
}

} // namespace hyperflux::openrgb
