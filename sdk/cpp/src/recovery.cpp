// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/sdk/recovery.hpp>

#include <limits>
#include <string>
#include <utility>

namespace hyperflux::sdk
{
namespace
{

Error inactive_lease_error()
{
    return {
        ErrorCode::SessionInactive,
        "the bridge connection changed and invalidated the previous lighting lease",
        "HFX-OWNERSHIP-002",
    };
}

} // namespace

bool is_connection_error(ErrorCode code) noexcept
{
    switch(code)
    {
        case ErrorCode::SocketCreate:
        case ErrorCode::SocketPath:
        case ErrorCode::SocketConnect:
        case ErrorCode::SocketConfigure:
        case ErrorCode::WriteFailed:
        case ErrorCode::ReadFailed:
        case ErrorCode::TruncatedFrame:
        case ErrorCode::ServerInstanceChanged:
            return true;
        default:
            return false;
    }
}

UnixClientFactory::UnixClientFactory(
    UnixChannelConfig channel_config,
    ClientConfig client_config)
    : channel_config_(std::move(channel_config)),
      client_config_(std::move(client_config))
{
}

Result<std::unique_ptr<ClientApi>> UnixClientFactory::connect()
{
    auto client = Client::connect_unix(channel_config_, client_config_);
    if(!client)
    {
        return Result<std::unique_ptr<ClientApi>>::failure(client.error());
    }
    return Result<std::unique_ptr<ClientApi>>::success(
        std::make_unique<Client>(std::move(client).value()));
}

RecoveringClient::RecoveringClient(
    std::unique_ptr<ClientFactory> factory,
    std::unique_ptr<ClientApi> connection)
    : factory_(std::move(factory)), connection_(std::move(connection))
{
}

Result<std::unique_ptr<RecoveringClient>> RecoveringClient::connect(
    std::unique_ptr<ClientFactory> factory)
{
    auto client = create(std::move(factory));
    if(!client)
    {
        return client;
    }
    auto connected = client.value()->ensure_connected();
    if(!connected)
    {
        return Result<std::unique_ptr<RecoveringClient>>::failure(connected.error());
    }
    return client;
}

Result<std::unique_ptr<RecoveringClient>> RecoveringClient::create(
    std::unique_ptr<ClientFactory> factory)
{
    if(factory == nullptr)
    {
        return Result<std::unique_ptr<RecoveringClient>>::failure({
            ErrorCode::InvalidArgument,
            "recovering SDK client requires a connection factory",
            std::nullopt,
        });
    }
    return Result<std::unique_ptr<RecoveringClient>>::success(
        std::unique_ptr<RecoveringClient>(new RecoveringClient(
            std::move(factory),
            nullptr)));
}

std::uint64_t RecoveringClient::connection_epoch() const noexcept
{
    return connection_epoch_;
}

Result<void> RecoveringClient::ensure_connected()
{
    return connection_ == nullptr ? reconnect() : Result<void>::success();
}

Result<void> RecoveringClient::reconnect()
{
    if(connection_epoch_ == std::numeric_limits<std::uint64_t>::max())
    {
        return Result<void>::failure({
            ErrorCode::IdentityExhausted,
            "SDK connection epoch is exhausted",
            std::nullopt,
        });
    }
    auto candidate = factory_->connect();
    if(!candidate)
    {
        return Result<void>::failure(candidate.error());
    }
    connection_ = std::move(candidate).value();
    ++connection_epoch_;
    return Result<void>::success();
}

Result<TransactionId> RecoveringClient::next_transaction_id()
{
    return retry_read<TransactionId>([this] { return connection_->next_transaction_id(); });
}

Result<v5::BridgeSnapshot> RecoveringClient::snapshot()
{
    return retry_read<v5::BridgeSnapshot>([this] { return connection_->snapshot(); });
}

Result<v5::IntegrationView> RecoveringClient::integration_view()
{
    return retry_read<v5::IntegrationView>([this] { return connection_->integration_view(); });
}

Result<v5::LeaseResult> RecoveringClient::acquire_lease(
    std::vector<v5::ResourceKey> resources,
    LeaseDurationMs duration_ms)
{
    return retry_read<v5::LeaseResult>([this, &resources, duration_ms] {
        return connection_->acquire_lease(resources, duration_ms);
    });
}

Result<v5::LeaseResult> RecoveringClient::renew_lease(
    LeaseId lease_id,
    LeaseDurationMs duration_ms)
{
    auto ready = ensure_connected();
    if(!ready)
    {
        return Result<v5::LeaseResult>::failure(ready.error());
    }
    auto result = connection_->renew_lease(std::move(lease_id), duration_ms);
    if(result || !is_connection_error(result.error().code))
    {
        return result;
    }
    auto reconnected = reconnect();
    return reconnected ? Result<v5::LeaseResult>::failure(inactive_lease_error())
                       : Result<v5::LeaseResult>::failure(reconnected.error());
}

Result<v5::LeaseResult> RecoveringClient::release_lease(LeaseId lease_id)
{
    auto ready = ensure_connected();
    if(!ready)
    {
        return Result<v5::LeaseResult>::failure(ready.error());
    }
    auto result = connection_->release_lease(std::move(lease_id));
    if(result || !is_connection_error(result.error().code))
    {
        return result;
    }
    auto reconnected = reconnect();
    return reconnected ? Result<v5::LeaseResult>::failure(inactive_lease_error())
                       : Result<v5::LeaseResult>::failure(reconnected.error());
}

Result<v5::TransactionResult> RecoveringClient::submit_transaction(
    TransactionSubmission submission)
{
    auto ready = ensure_connected();
    if(!ready)
    {
        return Result<v5::TransactionResult>::failure(ready.error());
    }
    const auto transaction_id = submission.transaction_id;
    auto result = connection_->submit_transaction(std::move(submission));
    if(result || !is_connection_error(result.error().code))
    {
        return result;
    }
    if(!reconnect())
    {
        return unknown_outcome(transaction_id);
    }
    auto reconciled = connection_->transaction_outcome(transaction_id);
    return reconciled ? reconciled : unknown_outcome(transaction_id);
}

Result<v5::TransactionResult> RecoveringClient::transaction_outcome(
    TransactionId transaction_id)
{
    return retry_read<v5::TransactionResult>(
        [this, &transaction_id] {
            return connection_->transaction_outcome(transaction_id);
        });
}

Result<v5::EventBatch> RecoveringClient::subscribe(EventSubscription subscription)
{
    auto result = connection_->subscribe(subscription);
    if(result || !is_connection_error(result.error().code))
    {
        return result;
    }
    auto reconnected = reconnect();
    if(!reconnected)
    {
        return Result<v5::EventBatch>::failure(reconnected.error());
    }
    auto fresh = connection_->subscribe({
        std::nullopt,
        std::nullopt,
        subscription.max_events,
    });
    if(fresh)
    {
        fresh.value().cursor_gap = true;
        fresh.value().has_more = false;
    }
    return fresh;
}

Result<v5::DiagnosticSnapshot> RecoveringClient::diagnostics()
{
    return retry_read<v5::DiagnosticSnapshot>([this] { return connection_->diagnostics(); });
}

Result<v5::TransactionResult> RecoveringClient::unknown_outcome(
    TransactionId transaction_id) const
{
    return Result<v5::TransactionResult>::success(v5::TransactionResultUnavailable {{
        std::move(transaction_id),
        ProtocolErrorKind::OutcomeUnknown,
        FindingId::from("HFX-OUTCOME-001").value(),
    }});
}

} // namespace hyperflux::sdk
