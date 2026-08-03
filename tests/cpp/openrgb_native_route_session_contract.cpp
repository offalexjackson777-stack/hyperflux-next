// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/native_route_session.hpp>

#include <chrono>
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

hyperflux::v7::EventCursor cursor()
{
    using namespace hyperflux;
    return {
        text<StreamId>("native-routes"),
        number<StreamEpoch>(1),
        number<ProjectionRevision>(1),
        number<SequenceNumber>(1),
    };
}

hyperflux::v7::NativeRoute route()
{
    using namespace hyperflux;
    const v7::ResourceKey resource {
        text<ReceiverId>("receiver-1"),
        number<GenerationId>(4),
        text<LogicalDeviceId>("mouse-00cd"),
        ResourceKind::NativeControl,
    };
    return {
        resource.receiver_id,
        resource.generation_id,
        resource.device_id,
        text<EndpointId>("endpoint-mouse"),
        DeviceKind::Mouse,
        number<VendorId>(0x1532),
        number<ProductId>(0x00cd),
        NativeRouteState::Available,
        NativeRouteAccess::Qualified,
        text<NativeProtocolFamily>("razer-feature-report-v1"),
        number<NativeReportByteCount>(90),
        text<NativeRouteToken>(std::string(64, 'a')),
        resource,
    };
}

enum class Outcome
{
    Matched,
    NotSent,
    MayHaveBeenSent,
    TransportError,
    MismatchedIdentity,
    MalformedResponse,
};

struct Counters
{
    std::size_t enumerated = 0;
    std::size_t acquired = 0;
    std::size_t renewed = 0;
    std::size_t released = 0;
    std::size_t prepared = 0;
    std::size_t transacted = 0;
};

class FakeClient final : public hyperflux::sdk::NativeClientApi
{
public:
    FakeClient(
        std::shared_ptr<Counters> counters,
        hyperflux::v7::NativeRoute route_value,
        Outcome outcome,
        bool grant = true)
        : counters_(std::move(counters)),
          route_(std::move(route_value)),
          outcome_(outcome),
          grant_(grant)
    {
    }

    hyperflux::sdk::Result<hyperflux::v7::LeaseResult> acquire_lease(
        std::vector<hyperflux::v7::ResourceKey> resources,
        hyperflux::LeaseDurationMs) override
    {
        ++counters_->acquired;
        if(!grant_)
        {
            return hyperflux::sdk::Result<hyperflux::v7::LeaseResult>::success(
                hyperflux::v7::LeaseResultRejected {
                    {
                        hyperflux::ProtocolErrorKind::OwnershipConflict,
                        text<hyperflux::FindingId>("HFX-TEST-LEASE"),
                    },
                });
        }
        return hyperflux::sdk::Result<hyperflux::v7::LeaseResult>::success(
            hyperflux::v7::LeaseResultGranted {
                {
                    text<hyperflux::LeaseId>("lease-1"),
                    text<hyperflux::ClientId>("openrgb-native-route"),
                    std::move(resources),
                    number<hyperflux::MonotonicMs>(30'000),
                    hyperflux::LeaseState::Granted,
                },
            });
    }

    hyperflux::sdk::Result<hyperflux::v7::LeaseResult> renew_lease(
        hyperflux::LeaseId,
        hyperflux::LeaseDurationMs) override
    {
        ++counters_->renewed;
        return hyperflux::sdk::Result<hyperflux::v7::LeaseResult>::success(
            hyperflux::v7::LeaseResultGranted {
                {
                    text<hyperflux::LeaseId>("lease-1"),
                    text<hyperflux::ClientId>("openrgb-native-route"),
                    {*route_.resource},
                    number<hyperflux::MonotonicMs>(60'000),
                    hyperflux::LeaseState::Renewed,
                },
            });
    }

    hyperflux::sdk::Result<hyperflux::v7::LeaseResult> release_lease(
        hyperflux::LeaseId) override
    {
        ++counters_->released;
        return hyperflux::sdk::Result<hyperflux::v7::LeaseResult>::success(
            hyperflux::v7::LeaseResultGranted {
                {
                    text<hyperflux::LeaseId>("lease-1"),
                    text<hyperflux::ClientId>("openrgb-native-route"),
                    {*route_.resource},
                    number<hyperflux::MonotonicMs>(60'000),
                    hyperflux::LeaseState::Released,
                },
            });
    }

    hyperflux::sdk::Result<hyperflux::v7::NativeRouteInventory> native_enumerate() override
    {
        ++counters_->enumerated;
        return hyperflux::sdk::Result<hyperflux::v7::NativeRouteInventory>::success(
            {cursor(), {route_}});
    }

    hyperflux::sdk::Result<hyperflux::sdk::NativeExchangeSubmission>
    prepare_native_exchange(
        hyperflux::LeaseId lease_id,
        hyperflux::NativeRouteToken route_token,
        hyperflux::NativeReportHex report,
        hyperflux::NativeExchangeTimeoutMs timeout_ms) override
    {
        ++counters_->prepared;
        return hyperflux::sdk::Result<hyperflux::sdk::NativeExchangeSubmission>::success({
            text<hyperflux::RequestId>("request-1"),
            std::move(lease_id),
            std::move(route_token),
            std::move(report),
            timeout_ms,
        });
    }

    hyperflux::sdk::Result<hyperflux::v7::NativeExchangeOutcome> native_transact(
        const hyperflux::sdk::NativeExchangeSubmission& submission) override
    {
        ++counters_->transacted;
        if(outcome_ == Outcome::TransportError)
        {
            return hyperflux::sdk::Result<hyperflux::v7::NativeExchangeOutcome>::failure({
                hyperflux::sdk::ErrorCode::ReadFailed,
                "test transport failed after submission",
                std::nullopt,
            });
        }
        auto request_id = submission.request_id;
        auto token = submission.route_token;
        if(outcome_ == Outcome::MismatchedIdentity)
        {
            request_id = text<hyperflux::RequestId>("wrong-request");
        }
        if(outcome_ == Outcome::NotSent)
        {
            return hyperflux::sdk::Result<hyperflux::v7::NativeExchangeOutcome>::success({
                request_id,
                token,
                hyperflux::v7::NativeExchangeResultNotSent {
                    {
                        hyperflux::ProtocolErrorKind::TransportFailure,
                        text<hyperflux::FindingId>("HFX-TEST-NOT-SENT"),
                    },
                },
            });
        }
        if(outcome_ == Outcome::MayHaveBeenSent)
        {
            return hyperflux::sdk::Result<hyperflux::v7::NativeExchangeOutcome>::success({
                request_id,
                token,
                hyperflux::v7::NativeExchangeResultMayHaveBeenSent {
                    {
                        hyperflux::ProtocolErrorKind::OutcomeUnknown,
                        text<hyperflux::FindingId>("HFX-TEST-UNCERTAIN"),
                    },
                },
            });
        }
        const auto response = outcome_ == Outcome::MalformedResponse
            ? std::string(180, 'z')
            : std::string(180, 'f');
        return hyperflux::sdk::Result<hyperflux::v7::NativeExchangeOutcome>::success({
            request_id,
            token,
            hyperflux::v7::NativeExchangeResultMatchedResponse {
                {text<hyperflux::NativeReportHex>(response)},
            },
        });
    }

    hyperflux::sdk::Result<hyperflux::v7::NativeRouteEventBatch> native_watch(
        hyperflux::sdk::NativeEventSubscription) override
    {
        return hyperflux::sdk::Result<hyperflux::v7::NativeRouteEventBatch>::failure({
            hyperflux::sdk::ErrorCode::UnexpectedResponse,
            "watch is outside this contract",
            std::nullopt,
        });
    }

private:
    std::shared_ptr<Counters> counters_;
    hyperflux::v7::NativeRoute route_;
    Outcome outcome_;
    bool grant_;
};

hyperflux::openrgb::native::NativeExchangeCertainty run_outcome(Outcome outcome)
{
    auto counters = std::make_shared<Counters>();
    auto route_value = route();
    auto session = hyperflux::openrgb::native::SdkNativeFeatureExchange::create(
        std::make_unique<FakeClient>(counters, route_value, outcome),
        hyperflux::openrgb::native::native_route_identity(route_value));
    if(!session)
    {
        throw std::runtime_error("test session did not initialize");
    }
    std::array<std::uint8_t, hyperflux::openrgb::native::razer_native_report_bytes> report {};
    return session.value()->exchange(report).certainty;
}

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb::native;
    const auto expected_client_id = text<ClientId>("openrgb-native-route-42");
    auto client_config = native_route_client_config(expected_client_id);
    if(!client_config
       || client_config.value().client_id != expected_client_id
       || client_config.value().client_name.value()
              != "OpenRGB HyperFlux native transport"
       || client_config.value().required_features.size() != 2
       || client_config.value().required_features[0].value()
              != "native-receiver-tunnel"
       || client_config.value().required_features[1].value()
              != "ownership-leases"
       || !client_config.value().optional_features.empty())
    {
        return 9;
    }
    auto counters = std::make_shared<Counters>();
    auto route_value = route();
    {
        auto session = SdkNativeFeatureExchange::create(
            std::make_unique<FakeClient>(counters, route_value, Outcome::Matched),
            native_route_identity(route_value));
        if(!session || counters->enumerated != 1 || counters->acquired != 0)
        {
            return 1;
        }
        std::array<std::uint8_t, razer_native_report_bytes> report {};
        report[0] = 0x02;
        const auto first = session.value()->exchange(report);
        if(first.certainty != NativeExchangeCertainty::MatchedResponse
           || first.response.front() != 0xff || first.response.back() != 0xff
           || counters->prepared != 1 || counters->transacted != 1
           || counters->acquired != 1 || counters->released != 1
           || counters->renewed != 0)
        {
            return 2;
        }
        const auto second = session.value()->exchange(report);
        if(second.certainty != NativeExchangeCertainty::MatchedResponse
           || counters->acquired != 2 || counters->released != 2
           || counters->renewed != 0 || counters->transacted != 2)
        {
            return 3;
        }
    }
    if(counters->released != 2)
    {
        return 4;
    }
    if(run_outcome(Outcome::NotSent) != NativeExchangeCertainty::NotSent
       || run_outcome(Outcome::MayHaveBeenSent)
           != NativeExchangeCertainty::MayHaveBeenSent
       || run_outcome(Outcome::TransportError)
           != NativeExchangeCertainty::MayHaveBeenSent
       || run_outcome(Outcome::MismatchedIdentity)
           != NativeExchangeCertainty::MayHaveBeenSent
       || run_outcome(Outcome::MalformedResponse)
           != NativeExchangeCertainty::MayHaveBeenSent)
    {
        return 5;
    }

    auto stale = route();
    auto expected = native_route_identity(stale);
    expected.generation_id = number<GenerationId>(99);
    auto missing = SdkNativeFeatureExchange::create(
        std::make_unique<FakeClient>(
            std::make_shared<Counters>(), stale, Outcome::Matched),
        expected);
    if(missing || missing.error().code != sdk::ErrorCode::InvalidController)
    {
        return 6;
    }
    auto denied_route = route();
    auto denied_counters = std::make_shared<Counters>();
    auto denied = SdkNativeFeatureExchange::create(
        std::make_unique<FakeClient>(
            denied_counters, denied_route, Outcome::Matched, false),
        native_route_identity(denied_route));
    if(!denied || denied_counters->acquired != 0)
    {
        return 7;
    }
    std::array<std::uint8_t, razer_native_report_bytes> report {};
    if(denied.value()->exchange(report).certainty != NativeExchangeCertainty::NotSent
       || denied_counters->acquired != 1 || denied_counters->released != 0
       || denied_counters->prepared != 0 || denied_counters->transacted != 0)
    {
        return 8;
    }
    return 0;
}
