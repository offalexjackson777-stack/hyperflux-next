// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/generated/protocol_v4_json.hpp>

#include <cstdint>
#include <stdexcept>
#include <string_view>
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

hyperflux::v4::BridgeSnapshot snapshot()
{
    return {
        {
            text<hyperflux::StreamId>("stream-1"),
            number<hyperflux::StreamEpoch>(2),
            number<hyperflux::ProjectionRevision>(1),
            number<hyperflux::SequenceNumber>(7),
        },
        {
            {
                text<hyperflux::ReceiverId>("receiver-1"),
                number<hyperflux::GenerationId>(3),
                text<hyperflux::ProfileId>("receiver.profile"),
                text<hyperflux::ProfileDigest>(
                    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"),
                hyperflux::ReceiverLifecycleState::Active,
                {},
                {},
                false,
                hyperflux::RestoreState::Idle,
            },
        },
    };
}

bool rejects_unknown_field()
{
    auto encoded = hyperflux::json_codec::encode(snapshot());
    encoded["unexpected"] = true;
    try
    {
        static_cast<void>(
            hyperflux::json_codec::decode<hyperflux::v4::BridgeSnapshot>(encoded));
    }
    catch(const hyperflux::json_codec::CodecError&)
    {
        return true;
    }
    return false;
}

bool rejects_wrong_decimal_encoding()
{
    try
    {
        static_cast<void>(hyperflux::json_codec::decode<hyperflux::GenerationId>(1));
    }
    catch(const hyperflux::json_codec::CodecError&)
    {
        return true;
    }
    return false;
}

bool rejects_oversized_feature_offer()
{
    auto features = nlohmann::json::array();
    for(std::size_t index = 0; index < 65; ++index)
    {
        features.push_back("feature");
    }
    const auto value = nlohmann::json {
        {"client_id", "client"},
        {"client_name", "client name"},
        {"minimum_version", 4},
        {"maximum_version", 4},
        {"required_features", features},
        {"optional_features", nlohmann::json::array()},
    };
    try
    {
        static_cast<void>(
            hyperflux::json_codec::decode<hyperflux::v4::ClientHello>(value));
    }
    catch(const hyperflux::json_codec::CodecError&)
    {
        return true;
    }
    return false;
}

} // namespace

int main()
{
    using namespace hyperflux;
    const auto original = snapshot();
    const auto encoded = json_codec::encode(original);
    if(encoded.at("receivers").at(0).at("generation_id") != "3"
       || json_codec::decode<v4::BridgeSnapshot>(encoded) != original)
    {
        return 1;
    }

    const auto request_id = text<RequestId>("request-1");
    const v4::RpcRequest request = v4::RpcRequestNegotiate {
        {
            request_id,
            {
                text<ClientId>("client-1"),
                text<ClientName>("OpenRGB contract test"),
                number<ProtocolVersion>(4),
                number<ProtocolVersion>(4),
                {text<ProtocolFeatureId>("snapshot-profile-bindings")},
                {},
            },
        },
    };
    const auto request_wire = json_codec::encode(request);
    const auto decoded_request = json_codec::decode<v4::RpcRequest>(request_wire);
    if(request_wire.at("method") != "negotiate"
       || !std::holds_alternative<v4::RpcRequestNegotiate>(decoded_request))
    {
        return 2;
    }

    const v4::RpcResponse response = v4::RpcResponseSnapshotSuccess {
        {
            request_id,
            text<ServerInstanceId>("server-1"),
            original,
        },
    };
    const auto response_wire = json_codec::encode(response);
    const auto decoded_response = json_codec::decode<v4::RpcResponse>(response_wire);
    if(response_wire.at("type") != "snapshot-success"
       || !std::holds_alternative<v4::RpcResponseSnapshotSuccess>(decoded_response))
    {
        return 3;
    }

    if(!rejects_unknown_field() || !rejects_wrong_decimal_encoding()
       || !rejects_oversized_feature_offer())
    {
        return 4;
    }
    return 0;
}
