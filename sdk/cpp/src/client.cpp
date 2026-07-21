// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/sdk/client.hpp>

#include <algorithm>
#include <functional>
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

const RequestId* response_request_id(const v5::RpcResponse& response)
{
    return std::visit(
        [](const auto& alternative) -> const RequestId* {
            using Alternative = std::decay_t<decltype(alternative)>;
            if constexpr(std::is_same_v<Alternative, v5::ErrorEnvelope>)
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

const ServerInstanceId& response_server_instance(const v5::RpcResponse& response)
{
    return std::visit(
        [](const auto& alternative) -> const ServerInstanceId& {
            using Alternative = std::decay_t<decltype(alternative)>;
            if constexpr(std::is_same_v<Alternative, v5::ErrorEnvelope>)
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

Error server_error(const v5::ErrorEnvelope& envelope)
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
Result<ResultType> unwrap_success(Result<v5::RpcResponse> response, std::string expected)
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

Client::Client(
    std::unique_ptr<RpcChannel> channel,
    std::unique_ptr<IdentitySource> identities,
    ClientId client_id,
    v5::ServerHello hello)
    : channel_(std::move(channel)),
      identities_(std::move(identities)),
      client_id_(std::move(client_id)),
      hello_(std::move(hello))
{
}

Result<Client> Client::connect(
    std::unique_ptr<RpcChannel> channel,
    std::unique_ptr<IdentitySource> identities,
    ClientConfig config)
{
    if(channel == nullptr || identities == nullptr)
    {
        return Result<Client>::failure({
            ErrorCode::InvalidArgument,
            "SDK channel and identity source are required",
            std::nullopt,
        });
    }
    auto request_id = identities->next_request_id();
    if(!request_id)
    {
        return Result<Client>::failure(request_id.error());
    }
    const auto expected_request_id = request_id.value();
    const auto version = ProtocolVersion::from(5);
    if(!version.has_value())
    {
        return Result<Client>::failure({
            ErrorCode::InvalidProtocol,
            "protocol version 5 violates the generated domain contract",
            std::nullopt,
        });
    }
    const v5::RpcRequest request = v5::RpcRequestNegotiate {
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
        return Result<Client>::failure(response.error());
    }
    if(response_request_id(response.value()) == nullptr
       || *response_request_id(response.value()) != expected_request_id)
    {
        return Result<Client>::failure({
            ErrorCode::ResponseRequestMismatch,
            "bridge negotiation response does not match the request identity",
            std::nullopt,
        });
    }
    if(const auto* error = std::get_if<v5::ErrorEnvelope>(&response.value()))
    {
        return Result<Client>::failure(server_error(*error));
    }
    const auto* success = std::get_if<v5::RpcResponseNegotiateSuccess>(&response.value());
    if(success == nullptr
       || success->response.server_instance_id != success->response.result.server_instance_id
       || success->response.result.selected_version != *version)
    {
        return Result<Client>::failure({
            ErrorCode::NegotiationFailed,
            "bridge returned an invalid protocol-v5 negotiation",
            std::nullopt,
        });
    }
    for(const auto& required : config.required_features)
    {
        if(!contains_feature(success->response.result.enabled_features, required))
        {
            return Result<Client>::failure({
                ErrorCode::RequiredFeatureMissing,
                "bridge negotiation omitted a required SDK feature",
                std::nullopt,
            });
        }
    }
    return Result<Client>::success(Client(
        std::move(channel),
        std::move(identities),
        std::move(config.client_id),
        success->response.result));
}

Result<Client> Client::connect_unix(
    const UnixChannelConfig& channel_config,
    ClientConfig client_config)
{
    auto channel = UnixRpcChannel::connect(channel_config);
    if(!channel)
    {
        return Result<Client>::failure(channel.error());
    }
    auto identities = ProcessIdentitySource::create();
    if(!identities)
    {
        return Result<Client>::failure(identities.error());
    }
    return connect(
        std::move(channel).value(),
        std::move(identities).value(),
        std::move(client_config));
}

const v5::ServerHello& Client::server_hello() const noexcept
{
    return hello_;
}

const ClientId& Client::client_id() const noexcept
{
    return client_id_;
}

Result<RequestId> Client::next_request_id()
{
    return identities_->next_request_id();
}

Result<TransactionId> Client::next_transaction_id()
{
    return identities_->next_transaction_id();
}

Result<v5::RpcResponse> Client::exchange_checked(
    const v5::RpcRequest& request,
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
        return Result<v5::RpcResponse>::failure({
            ErrorCode::ResponseRequestMismatch,
            "bridge response does not match the SDK request identity",
            std::nullopt,
        });
    }
    if(response_server_instance(response.value()) != hello_.server_instance_id)
    {
        return Result<v5::RpcResponse>::failure({
            ErrorCode::ServerInstanceChanged,
            "bridge process identity changed; reconnect before issuing another request",
            std::nullopt,
        });
    }
    if(const auto* error = std::get_if<v5::ErrorEnvelope>(&response.value()))
    {
        return Result<v5::RpcResponse>::failure(server_error(*error));
    }
    return response;
}

Result<v5::BridgeSnapshot> Client::snapshot()
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<v5::BridgeSnapshot>::failure(request_id.error());
    }
    const auto expected = request_id.value();
    const v5::RpcRequest request = v5::RpcRequestSnapshot {
        envelope(std::move(request_id).value(), v5::EmptyRequest {}),
    };
    return unwrap_success<v5::RpcResponseSnapshotSuccess, v5::BridgeSnapshot>(
        exchange_checked(request, expected),
        "snapshot");
}

Result<v5::IntegrationView> Client::integration_view()
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<v5::IntegrationView>::failure(request_id.error());
    }
    const auto expected = request_id.value();
    const v5::RpcRequest request = v5::RpcRequestIntegrationView {
        envelope(std::move(request_id).value(), v5::EmptyRequest {}),
    };
    return unwrap_success<v5::RpcResponseIntegrationViewSuccess, v5::IntegrationView>(
        exchange_checked(request, expected),
        "integration view");
}

Result<v5::LeaseResult> Client::acquire_lease(
    std::vector<v5::ResourceKey> resources,
    LeaseDurationMs duration_ms)
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<v5::LeaseResult>::failure(request_id.error());
    }
    const auto expected = request_id.value();
    const auto params_id = request_id.value();
    const v5::RpcRequest request = v5::RpcRequestAcquireLease {
        envelope(
            std::move(request_id).value(),
            v5::LeaseRequest {
                params_id,
                client_id_,
                std::move(resources),
                duration_ms,
            }),
    };
    return unwrap_success<v5::RpcResponseAcquireLeaseSuccess, v5::LeaseResult>(
        exchange_checked(request, expected),
        "lease acquisition");
}

Result<v5::LeaseResult> Client::renew_lease(LeaseId lease_id, LeaseDurationMs duration_ms)
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<v5::LeaseResult>::failure(request_id.error());
    }
    const auto expected = request_id.value();
    const auto params_id = request_id.value();
    const v5::RpcRequest request = v5::RpcRequestRenewLease {
        envelope(
            std::move(request_id).value(),
            v5::RenewLeaseRequest {
                params_id,
                client_id_,
                std::move(lease_id),
                duration_ms,
            }),
    };
    return unwrap_success<v5::RpcResponseRenewLeaseSuccess, v5::LeaseResult>(
        exchange_checked(request, expected),
        "lease renewal");
}

Result<v5::LeaseResult> Client::release_lease(LeaseId lease_id)
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<v5::LeaseResult>::failure(request_id.error());
    }
    const auto expected = request_id.value();
    const auto params_id = request_id.value();
    const v5::RpcRequest request = v5::RpcRequestReleaseLease {
        envelope(
            std::move(request_id).value(),
            v5::ReleaseLeaseRequest {
                params_id,
                client_id_,
                std::move(lease_id),
            }),
    };
    return unwrap_success<v5::RpcResponseReleaseLeaseSuccess, v5::LeaseResult>(
        exchange_checked(request, expected),
        "lease release");
}

Result<v5::TransactionResult> Client::submit_transaction(TransactionSubmission submission)
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<v5::TransactionResult>::failure(request_id.error());
    }
    const auto expected = request_id.value();
    const auto params_id = request_id.value();
    const v5::RpcRequest request = v5::RpcRequestSubmitTransaction {
        envelope(
            std::move(request_id).value(),
            v5::TransactionRequest {
                params_id,
                std::move(submission.transaction_id),
                client_id_,
                std::move(submission.lease_id),
                std::move(submission.receiver_id),
                submission.generation_id,
                std::move(submission.receiver_profile_id),
                std::move(submission.receiver_profile_digest),
                std::move(submission.device_profiles),
                submission.transaction_class,
                std::move(submission.stable_intents),
                submission.deadline_ms,
                std::move(submission.resources),
                std::move(submission.frames),
            }),
    };
    return unwrap_success<v5::RpcResponseSubmitTransactionSuccess, v5::TransactionResult>(
        exchange_checked(request, expected),
        "transaction submission");
}

Result<v5::TransactionResult> Client::transaction_outcome(TransactionId transaction_id)
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<v5::TransactionResult>::failure(request_id.error());
    }
    const auto expected = request_id.value();
    const auto params_id = request_id.value();
    const v5::RpcRequest request = v5::RpcRequestTransactionOutcome {
        envelope(
            std::move(request_id).value(),
            v5::TransactionLookup {
                params_id,
                client_id_,
                std::move(transaction_id),
            }),
    };
    return unwrap_success<v5::RpcResponseTransactionOutcomeSuccess, v5::TransactionResult>(
        exchange_checked(request, expected),
        "transaction lookup");
}

Result<v5::EventBatch> Client::subscribe(EventSubscription subscription)
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<v5::EventBatch>::failure(request_id.error());
    }
    const auto expected = request_id.value();
    const v5::RpcRequest request = v5::RpcRequestSubscribe {
        envelope(
            std::move(request_id).value(),
            v5::SubscriptionRequest {
                client_id_,
                std::move(subscription.subscription_id),
                std::move(subscription.expected_cursor),
                subscription.max_events,
            }),
    };
    return unwrap_success<v5::RpcResponseSubscribeSuccess, v5::EventBatch>(
        exchange_checked(request, expected),
        "event subscription");
}

Result<v5::DiagnosticSnapshot> Client::diagnostics()
{
    auto request_id = next_request_id();
    if(!request_id)
    {
        return Result<v5::DiagnosticSnapshot>::failure(request_id.error());
    }
    const auto expected = request_id.value();
    const v5::RpcRequest request = v5::RpcRequestDiagnostics {
        envelope(std::move(request_id).value(), v5::EmptyRequest {}),
    };
    return unwrap_success<v5::RpcResponseDiagnosticsSuccess, v5::DiagnosticSnapshot>(
        exchange_checked(request, expected),
        "diagnostics");
}

} // namespace hyperflux::sdk
