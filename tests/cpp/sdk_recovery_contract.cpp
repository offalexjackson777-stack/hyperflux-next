// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/sdk.hpp>

#include <cstdint>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <variant>
#include <vector>

namespace
{

template<typename T>
T text(std::string_view value)
{
    auto decoded = T::from(value);
    if(!decoded.has_value())
    {
        throw std::runtime_error("invalid test string domain value");
    }
    return *decoded;
}

template<typename T>
T number(std::uint64_t value)
{
    auto decoded = T::from(static_cast<typename T::value_type>(value));
    if(!decoded.has_value())
    {
        throw std::runtime_error("invalid test numeric domain value");
    }
    return *decoded;
}

hyperflux::v5::EventCursor cursor(std::uint64_t sequence)
{
    using namespace hyperflux;
    return {
        text<StreamId>("stream-1"),
        number<StreamEpoch>(1),
        number<ProjectionRevision>(1),
        number<SequenceNumber>(sequence),
    };
}

hyperflux::sdk::Error failure(
    hyperflux::sdk::ErrorCode code,
    std::string message = "injected contract failure")
{
    return {code, std::move(message), std::nullopt};
}

enum class Scenario
{
    ReadRetry,
    AcquireRetry,
    AmbiguousWriteRetained,
    AmbiguousWriteReconnectFailure,
    StaleLease,
    SubscriptionReset,
    ServerRejection,
};

struct Counters
{
    std::uint64_t connections = 0;
    std::uint64_t integration_reads = 0;
    std::uint64_t acquisitions = 0;
    std::uint64_t renewals = 0;
    std::uint64_t submissions = 0;
    std::uint64_t outcome_lookups = 0;
    std::uint64_t subscriptions = 0;
    bool fresh_subscription_seen = false;
};

class FaultClient final : public hyperflux::sdk::ClientApi
{
public:
    FaultClient(Scenario scenario, std::uint64_t connection, std::shared_ptr<Counters> counters)
        : scenario_(scenario), connection_(connection), counters_(std::move(counters))
    {
    }

    std::uint64_t connection_epoch() const noexcept override
    {
        return 1;
    }

    hyperflux::sdk::Result<hyperflux::TransactionId> next_transaction_id() override
    {
        return hyperflux::sdk::Result<hyperflux::TransactionId>::success(
            text<hyperflux::TransactionId>("transaction-1"));
    }

    hyperflux::sdk::Result<hyperflux::v5::BridgeSnapshot> snapshot() override
    {
        return hyperflux::sdk::Result<hyperflux::v5::BridgeSnapshot>::success(
            {cursor(connection_), {}});
    }

    hyperflux::sdk::Result<hyperflux::v5::IntegrationView> integration_view() override
    {
        using namespace hyperflux;
        ++counters_->integration_reads;
        if(scenario_ == Scenario::ReadRetry && connection_ == 1)
        {
            return sdk::Result<v5::IntegrationView>::failure(
                failure(sdk::ErrorCode::ReadFailed));
        }
        if(scenario_ == Scenario::ServerRejection)
        {
            return sdk::Result<v5::IntegrationView>::failure(
                failure(sdk::ErrorCode::ServerRejected));
        }
        return sdk::Result<v5::IntegrationView>::success({cursor(connection_), {}});
    }

    hyperflux::sdk::Result<hyperflux::v5::LeaseResult> acquire_lease(
        std::vector<hyperflux::v5::ResourceKey> resources,
        hyperflux::LeaseDurationMs) override
    {
        using namespace hyperflux;
        ++counters_->acquisitions;
        if(scenario_ == Scenario::AcquireRetry && connection_ == 1)
        {
            return sdk::Result<v5::LeaseResult>::failure(
                failure(sdk::ErrorCode::WriteFailed));
        }
        return sdk::Result<v5::LeaseResult>::success(v5::LeaseResultGranted {{
            text<LeaseId>("lease-1"),
            text<ClientId>("contract-client"),
            std::move(resources),
            number<MonotonicMs>(10'000),
            LeaseState::Granted,
        }});
    }

    hyperflux::sdk::Result<hyperflux::v5::LeaseResult> renew_lease(
        hyperflux::LeaseId,
        hyperflux::LeaseDurationMs) override
    {
        using namespace hyperflux;
        ++counters_->renewals;
        if(scenario_ == Scenario::StaleLease && connection_ == 1)
        {
            return sdk::Result<v5::LeaseResult>::failure(
                failure(sdk::ErrorCode::ReadFailed));
        }
        return sdk::Result<v5::LeaseResult>::failure(
            failure(sdk::ErrorCode::LeaseRejected));
    }

    hyperflux::sdk::Result<hyperflux::v5::LeaseResult> release_lease(
        hyperflux::LeaseId) override
    {
        return hyperflux::sdk::Result<hyperflux::v5::LeaseResult>::failure(
            failure(hyperflux::sdk::ErrorCode::LeaseRejected));
    }

    hyperflux::sdk::Result<hyperflux::v5::TransactionResult> submit_transaction(
        hyperflux::sdk::TransactionSubmission) override
    {
        using namespace hyperflux;
        ++counters_->submissions;
        if(scenario_ == Scenario::AmbiguousWriteRetained
           || scenario_ == Scenario::AmbiguousWriteReconnectFailure)
        {
            return sdk::Result<v5::TransactionResult>::failure(
                failure(sdk::ErrorCode::ReadFailed));
        }
        return sdk::Result<v5::TransactionResult>::failure(
            failure(sdk::ErrorCode::ServerRejected));
    }

    hyperflux::sdk::Result<hyperflux::v5::TransactionResult> transaction_outcome(
        hyperflux::TransactionId transaction_id) override
    {
        using namespace hyperflux;
        ++counters_->outcome_lookups;
        return sdk::Result<v5::TransactionResult>::success(v5::TransactionResultTerminal {{
            text<RequestId>("request-1"),
            text<RequestDigest>(std::string(64, 'a')),
            std::move(transaction_id),
            text<ReceiverId>("receiver-1"),
            number<GenerationId>(1),
            TransactionState::Succeeded,
            number<FrameCount>(1),
            number<DeliveredFrameCount>(1),
            SideEffectCertainty::Committed,
            true,
            false,
            DeviceApplicationState::Confirmed,
            number<SequenceNumber>(3),
            std::nullopt,
            std::nullopt,
        }});
    }

    hyperflux::sdk::Result<hyperflux::v5::EventBatch> subscribe(
        hyperflux::sdk::EventSubscription subscription) override
    {
        using namespace hyperflux;
        ++counters_->subscriptions;
        if(scenario_ == Scenario::SubscriptionReset && connection_ == 1)
        {
            return sdk::Result<v5::EventBatch>::failure(
                failure(sdk::ErrorCode::TruncatedFrame));
        }
        counters_->fresh_subscription_seen = !subscription.subscription_id.has_value()
            && !subscription.expected_cursor.has_value();
        return sdk::Result<v5::EventBatch>::success({
            text<SubscriptionId>("subscription-2"),
            cursor(2),
            {},
            number<SequenceNumber>(0),
            number<SequenceNumber>(2),
            number<DroppedEventCount>(0),
            false,
            false,
        });
    }

    hyperflux::sdk::Result<hyperflux::v5::DiagnosticSnapshot> diagnostics() override
    {
        using namespace hyperflux;
        return sdk::Result<v5::DiagnosticSnapshot>::success({
            number<SequenceNumber>(0),
            {},
            number<QueueCapacity>(64),
            number<QueueCapacity>(64),
        });
    }

private:
    Scenario scenario_;
    std::uint64_t connection_;
    std::shared_ptr<Counters> counters_;
};

class FaultFactory final : public hyperflux::sdk::ClientFactory
{
public:
    FaultFactory(Scenario scenario, std::shared_ptr<Counters> counters)
        : scenario_(scenario), counters_(std::move(counters))
    {
    }

    hyperflux::sdk::Result<std::unique_ptr<hyperflux::sdk::ClientApi>> connect() override
    {
        using namespace hyperflux;
        ++counters_->connections;
        if(scenario_ == Scenario::AmbiguousWriteReconnectFailure
           && counters_->connections > 1)
        {
            return sdk::Result<std::unique_ptr<sdk::ClientApi>>::failure(
                failure(sdk::ErrorCode::SocketConnect));
        }
        return sdk::Result<std::unique_ptr<sdk::ClientApi>>::success(
            std::make_unique<FaultClient>(scenario_, counters_->connections, counters_));
    }

private:
    Scenario scenario_;
    std::shared_ptr<Counters> counters_;
};

struct Fixture
{
    std::shared_ptr<Counters> counters;
    std::unique_ptr<hyperflux::sdk::RecoveringClient> client;
};

Fixture fixture(Scenario scenario)
{
    auto counters = std::make_shared<Counters>();
    auto connected = hyperflux::sdk::RecoveringClient::connect(
        std::make_unique<FaultFactory>(scenario, counters));
    if(!connected)
    {
        throw std::runtime_error("fault client did not connect");
    }
    return {std::move(counters), std::move(connected).value()};
}

hyperflux::v5::ResourceKey resource()
{
    using namespace hyperflux;
    return {
        text<ReceiverId>("receiver-1"),
        number<GenerationId>(1),
        text<LogicalDeviceId>("mouse"),
        ResourceKind::Lighting,
    };
}

hyperflux::sdk::TransactionSubmission submission()
{
    using namespace hyperflux;
    return {
        text<TransactionId>("transaction-1"),
        text<LeaseId>("lease-1"),
        text<ReceiverId>("receiver-1"),
        number<GenerationId>(1),
        text<ProfileId>("receiver.test"),
        text<ProfileDigest>(std::string(64, 'b')),
        {},
        TransactionClass::EffectFrame,
        {},
        number<MonotonicMs>(5'000),
        {resource()},
        {},
    };
}

} // namespace

int main()
{
    using namespace hyperflux;

    auto reads = fixture(Scenario::ReadRetry);
    const auto view = reads.client->integration_view();
    if(!view || reads.client->connection_epoch() != 2 || reads.counters->connections != 2
       || reads.counters->integration_reads != 2)
    {
        return 1;
    }

    auto acquisitions = fixture(Scenario::AcquireRetry);
    const auto acquired = acquisitions.client->acquire_lease(
        {resource()},
        number<LeaseDurationMs>(30'000));
    if(!acquired || acquisitions.client->connection_epoch() != 2
       || acquisitions.counters->acquisitions != 2)
    {
        return 2;
    }

    auto retained = fixture(Scenario::AmbiguousWriteRetained);
    const auto reconciled = retained.client->submit_transaction(submission());
    if(!reconciled || !std::holds_alternative<v5::TransactionResultTerminal>(reconciled.value())
       || retained.counters->submissions != 1 || retained.counters->outcome_lookups != 1
       || retained.client->connection_epoch() != 2)
    {
        return 3;
    }

    auto unknown = fixture(Scenario::AmbiguousWriteReconnectFailure);
    const auto unresolved = unknown.client->submit_transaction(submission());
    const auto* unavailable = unresolved
        ? std::get_if<v5::TransactionResultUnavailable>(&unresolved.value())
        : nullptr;
    if(unavailable == nullptr || unavailable->detail.transaction_id.value() != "transaction-1"
       || unavailable->detail.error_kind != ProtocolErrorKind::OutcomeUnknown
       || unavailable->detail.finding_id.value() != "HFX-OUTCOME-001"
       || unknown.counters->submissions != 1 || unknown.counters->connections != 2
       || unknown.client->connection_epoch() != 1)
    {
        return 4;
    }

    auto stale = fixture(Scenario::StaleLease);
    const auto renewed = stale.client->renew_lease(
        text<LeaseId>("lease-1"),
        number<LeaseDurationMs>(30'000));
    if(renewed || renewed.error().code != sdk::ErrorCode::SessionInactive
       || stale.counters->renewals != 1 || stale.client->connection_epoch() != 2)
    {
        return 5;
    }

    auto events = fixture(Scenario::SubscriptionReset);
    const auto batch = events.client->subscribe({
        text<SubscriptionId>("subscription-1"),
        cursor(1),
        number<EventBatchLimit>(32),
    });
    if(!batch || !batch.value().cursor_gap || batch.value().has_more
       || !events.counters->fresh_subscription_seen || events.counters->subscriptions != 2
       || events.client->connection_epoch() != 2)
    {
        return 6;
    }

    auto rejected = fixture(Scenario::ServerRejection);
    const auto rejection = rejected.client->integration_view();
    if(rejection || rejection.error().code != sdk::ErrorCode::ServerRejected
       || rejected.counters->connections != 1 || rejected.counters->integration_reads != 1
       || rejected.client->connection_epoch() != 1)
    {
        return 7;
    }

    return 0;
}
