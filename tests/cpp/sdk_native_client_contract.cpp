// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/sdk.hpp>

#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <type_traits>
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

const hyperflux::RequestId& request_id(const hyperflux::v8::RpcRequest& request)
{
    return std::visit(
        [](const auto& alternative) -> const hyperflux::RequestId& {
            return alternative.request.request_id;
        },
        request);
}

hyperflux::v8::EventCursor cursor(std::uint64_t sequence)
{
    using namespace hyperflux;
    return {
        text<StreamId>("native-route-stream"),
        number<StreamEpoch>(1),
        number<ProjectionRevision>(1),
        number<SequenceNumber>(sequence),
    };
}

hyperflux::v8::ResourceKey resource()
{
    using namespace hyperflux;
    return {
        text<ReceiverId>("receiver-1"),
        number<GenerationId>(1),
        text<LogicalDeviceId>("mouse-00cd"),
        ResourceKind::NativeControl,
    };
}

hyperflux::v8::NativeRoute route()
{
    using namespace hyperflux;
    return {
        text<ReceiverId>("receiver-1"),
        number<GenerationId>(1),
        text<LogicalDeviceId>("mouse-00cd"),
        text<EndpointId>("endpoint-mouse"),
        DeviceKind::Mouse,
        number<VendorId>(0x1532),
        number<ProductId>(0x00cd),
        NativeRouteState::Available,
        NativeRouteAccess::Qualified,
        text<NativeProtocolFamily>("razer-feature-report-v1"),
        number<NativeReportByteCount>(90),
        text<NativeRouteToken>(std::string(64, 'a')),
        resource(),
    };
}

class DeterministicIdentities final : public hyperflux::sdk::IdentitySource
{
public:
    hyperflux::sdk::Result<hyperflux::RequestId> next_request_id() override
    {
        ++request_sequence_;
        return hyperflux::sdk::Result<hyperflux::RequestId>::success(
            text<hyperflux::RequestId>("native-request-" + std::to_string(request_sequence_)));
    }

    hyperflux::sdk::Result<hyperflux::TransactionId> next_transaction_id() override
    {
        return hyperflux::sdk::Result<hyperflux::TransactionId>::failure({
            hyperflux::sdk::ErrorCode::IdentityUnavailable,
            "native client does not request transaction identities",
            std::nullopt,
        });
    }

private:
    std::uint64_t request_sequence_ = 0;
};

enum class Fault
{
    None,
    MissingFeature,
    RequestMismatch,
    ServerChanged,
};

class FakeNativeChannel final : public hyperflux::sdk::NativeRpcChannel
{
public:
    explicit FakeNativeChannel(Fault fault) : fault_(fault) {}

    hyperflux::sdk::Result<hyperflux::v8::RpcResponse> exchange(
        const hyperflux::v8::RpcRequest& request) override
    {
        using namespace hyperflux;
        if(const auto* negotiation = std::get_if<v8::RpcRequestNegotiate>(&request))
        {
            auto features = negotiation->request.params.required_features;
            if(fault_ == Fault::MissingFeature)
            {
                features.clear();
            }
            const auto response_request = fault_ == Fault::RequestMismatch
                ? text<RequestId>("wrong-request")
                : negotiation->request.request_id;
            return sdk::Result<v8::RpcResponse>::success(v8::RpcResponseNegotiateSuccess {
                {
                    response_request,
                    text<ServerInstanceId>("server-1"),
                    {
                        number<ProtocolVersion>(8),
                        text<ServerInstanceId>("server-1"),
                        text<ProtocolSessionId>("session-1"),
                        text<NegotiationToken>("token-1"),
                        text<ComponentVersion>("0.0.0-dev.1"),
                        std::move(features),
                        number<QueueCapacity>(64),
                    },
                },
            });
        }

        const auto server = fault_ == Fault::ServerChanged ? "server-2" : "server-1";
        if(const auto* acquire = std::get_if<v8::RpcRequestAcquireLease>(&request))
        {
            return sdk::Result<v8::RpcResponse>::success(v8::RpcResponseAcquireLeaseSuccess {
                {
                    request_id(request),
                    text<ServerInstanceId>(server),
                    v8::LeaseResultGranted {
                        {
                            text<LeaseId>("lease-1"),
                            acquire->request.params.client_id,
                            acquire->request.params.resources,
                            number<MonotonicMs>(10'000),
                            LeaseState::Granted,
                        },
                    },
                },
            });
        }
        if(std::holds_alternative<v8::RpcRequestNativeEnumerate>(request))
        {
            return sdk::Result<v8::RpcResponse>::success(
                v8::RpcResponseNativeEnumerateSuccess {
                    {
                        request_id(request),
                        text<ServerInstanceId>(server),
                        {cursor(1), {route()}},
                    },
                });
        }
        if(const auto* transact = std::get_if<v8::RpcRequestNativeTransact>(&request))
        {
            return sdk::Result<v8::RpcResponse>::success(
                v8::RpcResponseNativeTransactSuccess {
                    {
                        request_id(request),
                        text<ServerInstanceId>(server),
                        {
                            transact->request.params.request_id,
                            transact->request.params.route_token,
                            v8::NativeExchangeResultMatchedResponse {
                                {text<NativeReportHex>(std::string(180, 'f'))},
                            },
                        },
                    },
                });
        }
        if(std::holds_alternative<v8::RpcRequestNativeWatch>(request))
        {
            return sdk::Result<v8::RpcResponse>::success(v8::RpcResponseNativeWatchSuccess {
                {
                    request_id(request),
                    text<ServerInstanceId>(server),
                    {
                        text<SubscriptionId>("native-watch-1"),
                        cursor(2),
                        {},
                        number<SequenceNumber>(1),
                        number<SequenceNumber>(2),
                        number<DroppedEventCount>(0),
                        false,
                        false,
                    },
                },
            });
        }
        return sdk::Result<v8::RpcResponse>::failure({
            sdk::ErrorCode::UnexpectedResponse,
            "test channel received an unexpected request",
            std::nullopt,
        });
    }

private:
    Fault fault_;
};

hyperflux::sdk::NativeClientConfig config()
{
    return {
        text<hyperflux::ClientId>("openrgb-native-test"),
        text<hyperflux::ClientName>("OpenRGB native SDK contract test"),
        {text<hyperflux::ProtocolFeatureId>("native-receiver-tunnel")},
        {},
    };
}

hyperflux::sdk::Result<hyperflux::sdk::NativeClient> connect(Fault fault)
{
    return hyperflux::sdk::NativeClient::connect(
        std::make_unique<FakeNativeChannel>(fault),
        std::make_unique<DeterministicIdentities>(),
        config());
}

} // namespace

int main()
{
    using namespace hyperflux;
    auto client = connect(Fault::None);
    if(!client || client.value().server_hello().selected_version != number<ProtocolVersion>(8))
    {
        return 1;
    }
    auto inventory = client.value().native_enumerate();
    if(!inventory || inventory.value().routes.size() != 1
       || inventory.value().routes.front().product_id != number<ProductId>(0x00cd))
    {
        return 2;
    }
    auto lease = client.value().acquire_lease(
        {resource()}, number<LeaseDurationMs>(5'000));
    const auto* granted = lease
        ? std::get_if<v8::LeaseResultGranted>(&lease.value())
        : nullptr;
    if(granted == nullptr || granted->detail.lease_id != text<LeaseId>("lease-1"))
    {
        return 3;
    }
    auto submission = client.value().prepare_native_exchange(
        granted->detail.lease_id,
        *inventory.value().routes.front().route_token,
        text<NativeReportHex>(std::string(180, '0')),
        number<NativeExchangeTimeoutMs>(1'000));
    if(!submission)
    {
        return 4;
    }
    const auto request_identity = submission.value().request_id;
    auto first = client.value().native_transact(submission.value());
    auto replay = client.value().native_transact(submission.value());
    const auto* matched = first
        ? std::get_if<v8::NativeExchangeResultMatchedResponse>(&first.value().result)
        : nullptr;
    if(matched == nullptr || !replay || first.value().request_id != request_identity
       || replay.value().request_id != request_identity
       || matched->detail.response.value() != std::string(180, 'f'))
    {
        return 5;
    }
    auto watched = client.value().native_watch({
        std::nullopt,
        inventory.value().cursor,
        number<EventBatchLimit>(32),
    });
    if(!watched || watched.value().next_cursor.sequence != number<SequenceNumber>(2))
    {
        return 6;
    }

    const auto missing = connect(Fault::MissingFeature);
    if(missing || missing.error().code != sdk::ErrorCode::RequiredFeatureMissing)
    {
        return 7;
    }
    const auto mismatched = connect(Fault::RequestMismatch);
    if(mismatched || mismatched.error().code != sdk::ErrorCode::ResponseRequestMismatch)
    {
        return 8;
    }
    auto restarted = connect(Fault::ServerChanged);
    if(!restarted)
    {
        return 9;
    }
    const auto stale = restarted.value().native_enumerate();
    if(stale || stale.error().code != sdk::ErrorCode::ServerInstanceChanged)
    {
        return 10;
    }
    return 0;
}
