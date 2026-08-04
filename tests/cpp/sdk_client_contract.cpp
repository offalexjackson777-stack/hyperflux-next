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

const hyperflux::RequestId& request_id(const hyperflux::v5::RpcRequest& request)
{
    return std::visit(
        [](const auto& alternative) -> const hyperflux::RequestId& {
            return alternative.request.request_id;
        },
        request);
}

class DeterministicIdentities final : public hyperflux::sdk::IdentitySource
{
public:
    hyperflux::sdk::Result<hyperflux::RequestId> next_request_id() override
    {
        ++request_sequence_;
        return hyperflux::sdk::Result<hyperflux::RequestId>::success(
            text<hyperflux::RequestId>("request-" + std::to_string(request_sequence_)));
    }

    hyperflux::sdk::Result<hyperflux::TransactionId> next_transaction_id() override
    {
        ++transaction_sequence_;
        return hyperflux::sdk::Result<hyperflux::TransactionId>::success(
            text<hyperflux::TransactionId>(
                "transaction-" + std::to_string(transaction_sequence_)));
    }

private:
    std::uint64_t request_sequence_ = 0;
    std::uint64_t transaction_sequence_ = 0;
};

enum class Fault
{
    None,
    MissingFeature,
    RequestMismatch,
    ServerChanged,
    ServerRejected,
};

class FakeChannel final : public hyperflux::sdk::RpcChannel
{
public:
    explicit FakeChannel(Fault fault) : fault_(fault) {}

    hyperflux::sdk::Result<hyperflux::v5::RpcResponse> exchange(
        const hyperflux::v5::RpcRequest& request) override
    {
        using namespace hyperflux;
        if(const auto* negotiation = std::get_if<v5::RpcRequestNegotiate>(&request))
        {
            auto features = negotiation->request.params.required_features;
            if(fault_ == Fault::MissingFeature)
            {
                features.clear();
            }
            const auto response_request = fault_ == Fault::RequestMismatch
                ? text<RequestId>("wrong-request")
                : negotiation->request.request_id;
            return sdk::Result<v5::RpcResponse>::success(v5::RpcResponseNegotiateSuccess {
                {
                    response_request,
                    text<ServerInstanceId>("server-1"),
                    {
                        number<ProtocolVersion>(5),
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
        if(fault_ == Fault::ServerRejected)
        {
            return sdk::Result<v5::RpcResponse>::success(v5::ErrorEnvelope {
                request_id(request),
                text<ServerInstanceId>("server-1"),
                {
                    request_id(request),
                    ProtocolErrorKind::InvalidRequest,
                    text<HumanMessage>("request rejected for contract test"),
                    text<FindingId>("HFX-TEST-001"),
                },
            });
        }
        const auto server = fault_ == Fault::ServerChanged ? "server-2" : "server-1";
        if(std::holds_alternative<v5::RpcRequestIntegrationView>(request))
        {
            return sdk::Result<v5::RpcResponse>::success(v5::RpcResponseIntegrationViewSuccess {
                {
                    request_id(request),
                    text<ServerInstanceId>(server),
                    {
                        {
                            text<StreamId>("stream-1"),
                            number<StreamEpoch>(1),
                            number<ProjectionRevision>(1),
                            number<SequenceNumber>(0),
                        },
                        {},
                    },
                },
            });
        }
        return sdk::Result<v5::RpcResponse>::success(v5::RpcResponseSnapshotSuccess {
            {
                request_id(request),
                text<ServerInstanceId>(server),
                {
                    {
                        text<StreamId>("stream-1"),
                        number<StreamEpoch>(1),
                        number<ProjectionRevision>(1),
                        number<SequenceNumber>(0),
                    },
                    {},
                },
            },
        });
    }

private:
    Fault fault_;
};

hyperflux::sdk::ClientConfig config()
{
    return {
        text<hyperflux::ClientId>("openrgb-test"),
        text<hyperflux::ClientName>("OpenRGB SDK contract test"),
        {text<hyperflux::ProtocolFeatureId>("integration-view-projection")},
        {},
    };
}

hyperflux::sdk::Result<hyperflux::sdk::Client> connect(Fault fault)
{
    return hyperflux::sdk::Client::connect(
        std::make_unique<FakeChannel>(fault),
        std::make_unique<DeterministicIdentities>(),
        config());
}

} // namespace

int main()
{
    using namespace hyperflux;
    auto client = connect(Fault::None);
    if(!client || client.value().server_hello().selected_version != number<ProtocolVersion>(5))
    {
        return 1;
    }
    auto snapshot = client.value().snapshot();
    if(!snapshot || snapshot.value().cursor.sequence != number<SequenceNumber>(0))
    {
        return 2;
    }
    auto transaction_id = client.value().next_transaction_id();
    if(!transaction_id || transaction_id.value().value() != "transaction-1")
    {
        return 3;
    }
    const auto integration_view = client.value().integration_view();
    if(!integration_view || !integration_view.value().receivers.empty())
    {
        return 4;
    }

    const auto missing = connect(Fault::MissingFeature);
    if(missing || missing.error().code != sdk::ErrorCode::RequiredFeatureMissing)
    {
        return 5;
    }
    const auto mismatched = connect(Fault::RequestMismatch);
    if(mismatched || mismatched.error().code != sdk::ErrorCode::ResponseRequestMismatch)
    {
        return 6;
    }

    auto restarted = connect(Fault::ServerChanged);
    if(!restarted)
    {
        return 7;
    }
    const auto stale = restarted.value().snapshot();
    if(stale || stale.error().code != sdk::ErrorCode::ServerInstanceChanged)
    {
        return 8;
    }

    auto rejected = connect(Fault::ServerRejected);
    if(!rejected)
    {
        return 9;
    }
    const auto rejection = rejected.value().snapshot();
    if(rejection || rejection.error().code != sdk::ErrorCode::ServerRejected
       || rejection.error().finding_id != "HFX-TEST-001")
    {
        return 10;
    }
    return 0;
}
