// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/generated/protocol_v5_json.hpp>
#include <hyperflux/sdk/channel.hpp>

#include <arpa/inet.h>
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <optional>
#include <pwd.h>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/socket.h>
#include <sys/un.h>
#include <thread>
#include <unistd.h>
#include <utility>
#include <variant>

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

bool read_exact(int file_descriptor, void* buffer, std::size_t size)
{
    auto* bytes = static_cast<std::uint8_t*>(buffer);
    std::size_t received = 0;
    while(received < size)
    {
        const auto result = ::recv(file_descriptor, bytes + received, size - received, 0);
        if(result <= 0)
        {
            return false;
        }
        received += static_cast<std::size_t>(result);
    }
    return true;
}

bool write_fragmented(int file_descriptor, std::string_view payload)
{
    const auto network_length = htonl(static_cast<std::uint32_t>(payload.size()));
    const auto* prefix = reinterpret_cast<const std::uint8_t*>(&network_length);
    for(std::size_t index = 0; index < sizeof(network_length); ++index)
    {
        if(::send(file_descriptor, prefix + index, 1, MSG_NOSIGNAL) != 1)
        {
            return false;
        }
    }
    for(const auto byte : payload)
    {
        if(::send(file_descriptor, &byte, 1, MSG_NOSIGNAL) != 1)
        {
            return false;
        }
    }
    return true;
}

std::string read_request_payload(int file_descriptor)
{
    std::uint32_t network_length = 0;
    if(!read_exact(file_descriptor, &network_length, sizeof(network_length)))
    {
        return {};
    }
    const auto length = static_cast<std::size_t>(ntohl(network_length));
    std::string payload(length, '\0');
    if(!read_exact(file_descriptor, payload.data(), payload.size()))
    {
        return {};
    }
    return payload;
}

hyperflux::v5::RpcRequest request()
{
    using namespace hyperflux;
    return v5::RpcRequestNegotiate {
        {
            text<RequestId>("request-1"),
            {
                text<ClientId>("client-1"),
                text<ClientName>("socket contract"),
                number<ProtocolVersion>(5),
                number<ProtocolVersion>(5),
                {text<ProtocolFeatureId>("snapshot-profile-bindings")},
                {},
            },
        },
    };
}

bool valid_exchange()
{
    int sockets[2] {-1, -1};
    if(::socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sockets) != 0)
    {
        return false;
    }
    std::atomic<bool> server_ok {false};
    std::thread server([file_descriptor = sockets[1], &server_ok]() {
        const auto payload = read_request_payload(file_descriptor);
        try
        {
            const auto decoded = hyperflux::json_codec::decode<hyperflux::v5::RpcRequest>(
                nlohmann::json::parse(payload));
            const auto* negotiation = std::get_if<hyperflux::v5::RpcRequestNegotiate>(&decoded);
            if(negotiation != nullptr)
            {
                const hyperflux::v5::RpcResponse response =
                    hyperflux::v5::RpcResponseNegotiateSuccess {
                        {
                            negotiation->request.request_id,
                            text<hyperflux::ServerInstanceId>("server-1"),
                            {
                                number<hyperflux::ProtocolVersion>(5),
                                text<hyperflux::ServerInstanceId>("server-1"),
                                text<hyperflux::ProtocolSessionId>("session-1"),
                                text<hyperflux::NegotiationToken>("token-1"),
                                text<hyperflux::ComponentVersion>("0.0.0-dev.1"),
                                negotiation->request.params.required_features,
                                number<hyperflux::QueueCapacity>(64),
                            },
                        },
                    };
                const auto response_payload = hyperflux::json_codec::encode(response).dump();
                server_ok = write_fragmented(file_descriptor, response_payload);
            }
        }
        catch(const std::exception&)
        {
            server_ok = false;
        }
        static_cast<void>(::close(file_descriptor));
    });
    auto channel = hyperflux::sdk::UnixRpcChannel::adopt_connected_socket(sockets[0], 2'000);
    if(!channel)
    {
        server.join();
        return false;
    }
    const auto response = channel.value()->exchange(request());
    server.join();
    return response && server_ok
        && std::holds_alternative<hyperflux::v5::RpcResponseNegotiateSuccess>(response.value());
}

hyperflux::sdk::ErrorCode invalid_response(
    std::uint32_t declared_length,
    std::string payload)
{
    int sockets[2] {-1, -1};
    if(::socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sockets) != 0)
    {
        return hyperflux::sdk::ErrorCode::SocketCreate;
    }
    std::thread server(
        [file_descriptor = sockets[1], declared_length, payload = std::move(payload)]() {
            static_cast<void>(read_request_payload(file_descriptor));
            const auto network_length = htonl(declared_length);
            static_cast<void>(::send(
                file_descriptor,
                &network_length,
                sizeof(network_length),
                MSG_NOSIGNAL));
            if(!payload.empty())
            {
                static_cast<void>(::send(
                    file_descriptor,
                    payload.data(),
                    payload.size(),
                    MSG_NOSIGNAL));
            }
            static_cast<void>(::close(file_descriptor));
        });
    auto channel = hyperflux::sdk::UnixRpcChannel::adopt_connected_socket(sockets[0], 2'000);
    if(!channel)
    {
        server.join();
        return channel.error().code;
    }
    const auto response = channel.value()->exchange(request());
    server.join();
    return response ? hyperflux::sdk::ErrorCode::UnexpectedResponse : response.error().code;
}

std::string current_account_name()
{
    passwd record {};
    passwd* resolved = nullptr;
    std::array<char, 16'384> buffer {};
    if(::getpwuid_r(::geteuid(), &record, buffer.data(), buffer.size(), &resolved) != 0
       || resolved == nullptr)
    {
        return {};
    }
    return record.pw_name;
}

hyperflux::sdk::ErrorCode connect_to_local_peer(
    std::optional<std::uint32_t> expected_uid,
    std::optional<std::string> expected_user,
    std::string_view suffix)
{
    const auto path = "/tmp/hfx-sdk-channel-" + std::to_string(::getpid()) + "-"
        + std::string(suffix) + ".sock";
    static_cast<void>(::unlink(path.c_str()));
    const auto listener = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if(listener < 0)
    {
        return hyperflux::sdk::ErrorCode::SocketCreate;
    }
    sockaddr_un address {};
    address.sun_family = AF_UNIX;
    std::memcpy(address.sun_path, path.c_str(), path.size() + 1);
    if(::bind(listener, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0
       || ::listen(listener, 1) != 0)
    {
        static_cast<void>(::close(listener));
        static_cast<void>(::unlink(path.c_str()));
        return hyperflux::sdk::ErrorCode::SocketConfigure;
    }

    std::thread server([listener] {
        const auto accepted = ::accept4(listener, nullptr, nullptr, SOCK_CLOEXEC);
        if(accepted >= 0)
        {
            static_cast<void>(::close(accepted));
        }
        static_cast<void>(::close(listener));
    });
    auto connected = hyperflux::sdk::UnixRpcChannel::connect({
        path,
        2'000,
        expected_uid,
        std::move(expected_user),
    });
    server.join();
    static_cast<void>(::unlink(path.c_str()));
    return connected ? hyperflux::sdk::ErrorCode::UnexpectedResponse : connected.error().code;
}

} // namespace

int main()
{
    if(!valid_exchange())
    {
        return 1;
    }
    if(invalid_response(
           static_cast<std::uint32_t>(hyperflux::v5::max_wire_message_bytes + 1),
           {})
       != hyperflux::sdk::ErrorCode::PayloadTooLarge)
    {
        return 2;
    }
    if(invalid_response(10, "{}") != hyperflux::sdk::ErrorCode::TruncatedFrame)
    {
        return 3;
    }
    const std::string too_deep = std::string(129, '[') + "0" + std::string(129, ']');
    if(invalid_response(static_cast<std::uint32_t>(too_deep.size()), too_deep)
       != hyperflux::sdk::ErrorCode::InvalidJson)
    {
        return 4;
    }
    const auto account = current_account_name();
    if(account.empty()
       || connect_to_local_peer(std::nullopt, account, "named")
           != hyperflux::sdk::ErrorCode::UnexpectedResponse)
    {
        return 5;
    }
    const auto current_uid = static_cast<std::uint32_t>(::geteuid());
    const auto other_uid = current_uid == std::numeric_limits<std::uint32_t>::max()
        ? current_uid - 1
        : current_uid + 1;
    if(connect_to_local_peer(other_uid, std::nullopt, "mismatch")
       != hyperflux::sdk::ErrorCode::PeerCredentialMismatch)
    {
        return 6;
    }
    const auto conflicting = hyperflux::sdk::UnixRpcChannel::connect({
        "/tmp/hfx-sdk-channel-unused.sock",
        2'000,
        current_uid,
        account,
    });
    if(conflicting || conflicting.error().code != hyperflux::sdk::ErrorCode::InvalidArgument)
    {
        return 7;
    }
    const auto unavailable = hyperflux::sdk::UnixRpcChannel::connect({
        "/tmp/hfx-sdk-channel-unused.sock",
        2'000,
        std::nullopt,
        "hyperflux-account-must-not-exist-7f6df7d3",
    });
    if(unavailable || unavailable.error().code != hyperflux::sdk::ErrorCode::SocketConfigure)
    {
        return 8;
    }
    return 0;
}
