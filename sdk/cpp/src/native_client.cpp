// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/sdk/native_client.hpp>

#include <algorithm>
#include <memory>
#include <optional>
#include <string>
#include <type_traits>
#include <utility>
#include <variant>

namespace hyperflux::sdk
{
namespace
{

const RequestId* response_request_id(const v7::RpcResponse& response)
{
    return std::visit(
        [](const auto& alternative) -> const RequestId* {
            using Alternative = std::decay_t<decltype(alternative)>;
            if constexpr(std::is_same_v<Alternative, v7::ErrorEnvelope>)
            {
                return alternative.request_id.has_value() ? &*alternative.request_id : nullptr;
            }
            else
            {
                return &alternative.response.request_id;
            }
        },
        response);
}

const ServerInstanceId& response_server_instance(const v7::RpcResponse& response)
{
    return std::visit(
        [](const auto& alternative) -> const ServerInstanceId& {
            using Alternative = std::decay_t<decltype(alternative)>;
            if constexpr(std::is_same_v<Alternative, v7::ErrorEnvelope>)
            {
                return alternative.server_instance_id;
            }
            else
            {
                return alternative.response.server_instance_id;
            }
        },
        response);
}

Error server_error(const v7::ErrorEnvelope& envelope)
{
    return {
        ErrorCode::ServerRejected,
        std::string(envelope.error.message.value()),
        std::string(envelope.error.finding_id.value()),
    };
}

bool contains_feature(
    const std::vector<ProtocolFeatureId>& features,
    const ProtocolFeatureId& expected)
{
    return std::find(features.begin(), features.end(), expected) != features.end();
}

template<typename Wrapper, typename ResultType>
Result<ResultType> unwrap_success(Result<v7::RpcResponse> response, std::string expected)
{
    if(!response)
    {
        return Result<ResultType>::failure(response.error());
    }
    auto* success = std::get_if<Wrapper>(&response.value());
    if(success == nullptr)
    {
        return Result<ResultType>::failure({
            ErrorCode::UnexpectedResponse,
            "bridge returned an unexpected response to " + std::move(expected),
            std::nullopt,
        });
    }
    return Result<ResultType>::success(std::move(success->response.result));
}

} // namespace

NativeClient::NativeClient(
    std::unique_ptr<NativeRpcChannel> channel,
    std::unique_ptr<IdentitySource> identities,
    ClientId client_id,
    v7::ServerHello hello)
    : channel_(std::move(channel)),
      identities_(std::move(identities)),
      client_id_(std::move(client_id)),
      hello_(std::move(hello))
{
}

Result<NativeClient> NativeClient::connect(
    std::unique_ptr<NativeRpcChannel> channel,
    std::unique_ptr<IdentitySource> identities,
    NativeClientConfig config)
{
    if(channel == nullptr || identities == nullptr)
    {
        return Result<NativeClient>::failure({
            ErrorCode::InvalidArgument,
            "native SDK channel and identity source are required",
            std::nullopt,
        });
    }
    auto request_id = identities->next_request_id();
    if(!request_id)
    {
        return Result<NativeClient>::failure(request_id.error());
    }
    const auto expected_request_id = request_id.value();
    const auto version = ProtocolVersion::from(7);
    if(!version.has_value())
    {
        return Result<NativeClient>::failure({
            ErrorCode::InvalidProtocol,
            "protocol version 7 violates the generated domain contract",
            std::nullopt,
        });
    }
    const v7::RpcRequest request = v7::RpcRequestNegotiate {
        {
            std::move(request_id).value(),
            {
                config.client_id,
                std::move(config.client_name),
                *version,
                *version,
                config.required_features,
                std::move(config.optional_features),
            },
        },
    };
    auto response = channel->exchange(request);
    if(!response)
    {
        return Result<NativeClient>::failure(response.error());
    }
    if(response_request_id(response.value()) == nullptr
       || *response_request_id(response.value()) != expected_request_id)
    {
        return Result<NativeClient>::failure({
            ErrorCode::ResponseRequestMismatch,
            "bridge negotiation response does not match the request identity",
            std::nullopt,
        });
    }
    if(const auto* error = std::get_if<v7::ErrorEnvelope>(&response.value()))
    {
        return Result<NativeClient>::failure(server_error(*error));
    }
    const auto* success = std::get_if<v7::RpcResponseNegotiateSuccess>(&response.value());
    if(success == nullptr
       || success->response.server_instance_id != success->response.result.server_instance_id
       || success->response.result.selected_version != *version)
    {
        return Result<NativeClient>::failure({
            ErrorCode::NegotiationFailed,
            "bridge returned an invalid protocol-v7 negotiation",
            std::nullopt,
        });
    }
    for(const auto& required : config.required_features)
    {
        if(!contains_feature(success->response.result.enabled_features, required))
        {
            return Result<NativeClient>::failure({
                ErrorCode::RequiredFeatureMissing,
                "bridge negotiation omitted a required native SDK feature",
                std::nullopt,
            });
        }
    }
    return Result<NativeClient>::success(NativeClient(
        std::move(channel),
        std::move(identities),
        std::move(config.client_id),
        success->response.result));
}

Result<NativeClient> NativeClient::connect_unix(
    const UnixChannelConfig& channel_config,
    NativeClientConfig client_config)
{
    auto channel = NativeUnixRpcChannel::connect(channel_config);
    if(!channel)
    {
        return Result<NativeClient>::failure(channel.error());
    }
    auto identities = ProcessIdentitySource::create();
    if(!identities)
    {
        return Result<NativeClient>::failure(identities.error());
    }
    return connect(
        std::move(channel).value(),
        std::move(identities).value(),
        std::move(client_config));
}

const v7::ServerHello& NativeClient::server_hello() const noexcept
{
    return hello_;
}

const ClientId& NativeClient::client_id() const noexcept
{
    return client_id_;
}

Result<RequestId> NativeClient::next_request_id()
{
    return identities_->next_request_id();
}

Result<v7::RpcResponse> NativeClient::exchange_checked(
    const v7::RpcRequest& request,
    const RequestId& request_id)
{
    auto response = channel_->exchange(request);
    if(!response)
    {
        return response;
    }
    const auto* actual_request_id = response_request_id(response.value());
    if(actual_request_id == nullptr || *actual_request_id != request_id)
    {
        return Result<v7::RpcResponse>::failure({
            ErrorCode::ResponseRequestMismatch,
            "bridge response does not match the native SDK request identity",
            std::nullopt,
        });
    }
    if(response_server_instance(response.value()) != hello_.server_instance_id)
    {
        return Result<v7::RpcResponse>::failure({
            ErrorCode::ServerInstanceChanged,
            "bridge process identity changed; reconnect before issuing another request",
            std::nullopt,
        });
    }
    if(const auto* error = std::get_if<v7::ErrorEnvelope>(&response.value()))
    {
        return Result<v7::RpcResponse>::failure(server_error(*error));
    }
    return response;
}

Result<v7::LeaseResult> NativeClient::acquire_lease(
    std::vector<v7::ResourceKey> resources,
    LeaseDurationMs duration_ms)
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<v7::LeaseResult>::failure(request_id.error());
    }
    const auto expected = request_id.value();
    const auto params_id = request_id.value();
    const v7::RpcRequest request = v7::RpcRequestAcquireLease {
        envelope(
            std::move(request_id).value(),
            v7::LeaseRequest {
                params_id,
                client_id_,
                std::move(resources),
                duration_ms,
            }),
    };
    return unwrap_success<v7::RpcResponseAcquireLeaseSuccess, v7::LeaseResult>(
        exchange_checked(request, expected),
        "native lease acquisition");
}

Result<v7::LeaseResult> NativeClient::renew_lease(
    LeaseId lease_id,
    LeaseDurationMs duration_ms)
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<v7::LeaseResult>::failure(request_id.error());
    }
    const auto expected = request_id.value();
    const auto params_id = request_id.value();
    const v7::RpcRequest request = v7::RpcRequestRenewLease {
        envelope(
            std::move(request_id).value(),
            v7::RenewLeaseRequest {
                params_id,
                client_id_,
                std::move(lease_id),
                duration_ms,
            }),
    };
    return unwrap_success<v7::RpcResponseRenewLeaseSuccess, v7::LeaseResult>(
        exchange_checked(request, expected),
        "native lease renewal");
}

Result<v7::LeaseResult> NativeClient::release_lease(LeaseId lease_id)
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<v7::LeaseResult>::failure(request_id.error());
    }
    const auto expected = request_id.value();
    const auto params_id = request_id.value();
    const v7::RpcRequest request = v7::RpcRequestReleaseLease {
        envelope(
            std::move(request_id).value(),
            v7::ReleaseLeaseRequest {
                params_id,
                client_id_,
                std::move(lease_id),
            }),
    };
    return unwrap_success<v7::RpcResponseReleaseLeaseSuccess, v7::LeaseResult>(
        exchange_checked(request, expected),
        "native lease release");
}

Result<v7::NativeRouteInventory> NativeClient::native_enumerate()
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<v7::NativeRouteInventory>::failure(request_id.error());
    }
    const auto expected = request_id.value();
    const v7::RpcRequest request = v7::RpcRequestNativeEnumerate {
        envelope(std::move(request_id).value(), v7::EmptyRequest {}),
    };
    return unwrap_success<v7::RpcResponseNativeEnumerateSuccess, v7::NativeRouteInventory>(
        exchange_checked(request, expected),
        "native route enumeration");
}

Result<NativeExchangeSubmission> NativeClient::prepare_native_exchange(
    LeaseId lease_id,
    NativeRouteToken route_token,
    NativeReportHex report,
    NativeExchangeTimeoutMs timeout_ms)
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<NativeExchangeSubmission>::failure(request_id.error());
    }
    return Result<NativeExchangeSubmission>::success({
        std::move(request_id).value(),
        std::move(lease_id),
        std::move(route_token),
        std::move(report),
        timeout_ms,
    });
}

Result<v7::NativeExchangeOutcome> NativeClient::native_transact(
    const NativeExchangeSubmission& submission)
{
    const v7::RpcRequest request = v7::RpcRequestNativeTransact {
        envelope(
            submission.request_id,
            v7::NativeExchangeRequest {
                submission.request_id,
                client_id_,
                submission.lease_id,
                submission.route_token,
                submission.report,
                submission.timeout_ms,
            }),
    };
    return unwrap_success<v7::RpcResponseNativeTransactSuccess, v7::NativeExchangeOutcome>(
        exchange_checked(request, submission.request_id),
        "native receiver exchange");
}

Result<v7::NativeRouteEventBatch> NativeClient::native_watch(
    NativeEventSubscription subscription)
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<v7::NativeRouteEventBatch>::failure(request_id.error());
    }
    const auto expected = request_id.value();
    const v7::RpcRequest request = v7::RpcRequestNativeWatch {
        envelope(
            std::move(request_id).value(),
            v7::SubscriptionRequest {
                client_id_,
                std::move(subscription.subscription_id),
                std::move(subscription.expected_cursor),
                subscription.max_events,
            }),
    };
    return unwrap_success<v7::RpcResponseNativeWatchSuccess, v7::NativeRouteEventBatch>(
        exchange_checked(request, expected),
        "native route watch");
}

} // namespace hyperflux::sdk
