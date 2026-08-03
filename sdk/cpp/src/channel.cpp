// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/sdk/channel.hpp>

#include <hyperflux/generated/protocol_v5_json.hpp>

#include <arpa/inet.h>
#include <cerrno>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <memory>
#include <optional>
#include <pwd.h>
#include <string>
#include <string_view>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>
#include <vector>

namespace hyperflux::sdk
{
namespace
{

Result<void> configure_timeout(int file_descriptor, std::uint32_t timeout_ms)
{
    if(timeout_ms == 0)
    {
        return Result<void>::failure({
            ErrorCode::InvalidArgument,
            "socket timeout must be greater than zero",
            std::nullopt,
        });
    }
    const timeval timeout {
        static_cast<time_t>(timeout_ms / 1'000),
        static_cast<suseconds_t>((timeout_ms % 1'000) * 1'000),
    };
    if(::setsockopt(file_descriptor, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) != 0
       || ::setsockopt(file_descriptor, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) != 0)
    {
        return Result<void>::failure({
            ErrorCode::SocketConfigure,
            "failed to configure bounded socket I/O timeouts",
            std::nullopt,
        });
    }
    return Result<void>::success();
}

Result<std::optional<std::uint32_t>> resolve_expected_peer(
    const UnixChannelConfig& config)
{
    if(config.expected_peer_uid.has_value() && config.expected_peer_user.has_value())
    {
        return Result<std::optional<std::uint32_t>>::failure({
            ErrorCode::InvalidArgument,
            "bridge peer authority must use either an account name or a numeric UID",
            std::nullopt,
        });
    }
    if(config.expected_peer_uid.has_value())
    {
        return Result<std::optional<std::uint32_t>>::success(config.expected_peer_uid);
    }
    if(!config.expected_peer_user.has_value())
    {
        return Result<std::optional<std::uint32_t>>::success(std::nullopt);
    }
    if(config.expected_peer_user->empty() || config.expected_peer_user->size() > 64)
    {
        return Result<std::optional<std::uint32_t>>::failure({
            ErrorCode::InvalidArgument,
            "bridge peer account name is empty or exceeds the local bound",
            std::nullopt,
        });
    }

    passwd record {};
    passwd* resolved = nullptr;
    std::vector<char> buffer(16'384);
    const auto status = ::getpwnam_r(
        config.expected_peer_user->c_str(),
        &record,
        buffer.data(),
        buffer.size(),
        &resolved);
    if(status != 0 || resolved == nullptr
       || static_cast<std::uintmax_t>(record.pw_uid)
           > std::numeric_limits<std::uint32_t>::max())
    {
        return Result<std::optional<std::uint32_t>>::failure({
            ErrorCode::SocketConfigure,
            "configured bridge service account is unavailable",
            std::nullopt,
        });
    }
    return Result<std::optional<std::uint32_t>>::success(
        static_cast<std::uint32_t>(record.pw_uid));
}

Result<void> verify_peer(int file_descriptor, std::optional<std::uint32_t> expected_uid)
{
    if(!expected_uid.has_value())
    {
        return Result<void>::success();
    }
    ucred credentials {};
    socklen_t length = sizeof(credentials);
    if(::getsockopt(file_descriptor, SOL_SOCKET, SO_PEERCRED, &credentials, &length) != 0
       || length != sizeof(credentials))
    {
        return Result<void>::failure({
            ErrorCode::SocketConfigure,
            "failed to read local bridge peer credentials",
            std::nullopt,
        });
    }
    if(credentials.uid != *expected_uid)
    {
        return Result<void>::failure({
            ErrorCode::PeerCredentialMismatch,
            "local bridge peer UID does not match the configured authority",
            std::nullopt,
        });
    }
    return Result<void>::success();
}

Result<void> write_all(int file_descriptor, const std::uint8_t* bytes, std::size_t size)
{
    std::size_t written = 0;
    while(written < size)
    {
        const auto result = ::send(
            file_descriptor,
            bytes + written,
            size - written,
            MSG_NOSIGNAL);
        if(result < 0)
        {
            if(errno == EINTR)
            {
                continue;
            }
            return Result<void>::failure({
                ErrorCode::WriteFailed,
                "failed to write a complete bridge RPC frame",
                std::nullopt,
            });
        }
        if(result == 0)
        {
            return Result<void>::failure({
                ErrorCode::WriteFailed,
                "bridge socket accepted zero bytes while writing",
                std::nullopt,
            });
        }
        written += static_cast<std::size_t>(result);
    }
    return Result<void>::success();
}

Result<void> read_all(int file_descriptor, std::uint8_t* bytes, std::size_t size)
{
    std::size_t received = 0;
    while(received < size)
    {
        const auto result = ::recv(file_descriptor, bytes + received, size - received, 0);
        if(result < 0)
        {
            if(errno == EINTR)
            {
                continue;
            }
            return Result<void>::failure({
                ErrorCode::ReadFailed,
                "failed to read a complete bridge RPC frame",
                std::nullopt,
            });
        }
        if(result == 0)
        {
            return Result<void>::failure({
                ErrorCode::TruncatedFrame,
                "bridge RPC frame ended before its declared length",
                std::nullopt,
            });
        }
        received += static_cast<std::size_t>(result);
    }
    return Result<void>::success();
}

bool nesting_is_bounded(std::string_view payload)
{
    std::size_t depth = 0;
    bool in_string = false;
    bool escaped = false;
    for(const auto character : payload)
    {
        if(in_string)
        {
            if(escaped)
            {
                escaped = false;
            }
            else if(character == '\\')
            {
                escaped = true;
            }
            else if(character == '"')
            {
                in_string = false;
            }
            continue;
        }
        if(character == '"')
        {
            in_string = true;
        }
        else if(character == '{' || character == '[')
        {
            ++depth;
            if(depth > v5::max_json_depth)
            {
                return false;
            }
        }
        else if(character == '}' || character == ']')
        {
            if(depth == 0)
            {
                return false;
            }
            --depth;
        }
    }
    return !in_string && depth == 0;
}

Result<v5::RpcResponse> decode_response(const std::string& payload)
{
    if(!nesting_is_bounded(payload))
    {
        return Result<v5::RpcResponse>::failure({
            ErrorCode::InvalidJson,
            "bridge response exceeds the JSON nesting bound or is structurally incomplete",
            std::nullopt,
        });
    }
    try
    {
        const auto document = nlohmann::json::parse(payload);
        return Result<v5::RpcResponse>::success(
            json_codec::decode<v5::RpcResponse>(document));
    }
    catch(const std::exception&)
    {
        return Result<v5::RpcResponse>::failure({
            ErrorCode::InvalidProtocol,
            "bridge response is not a valid protocol-v5 message",
            std::nullopt,
        });
    }
}

} // namespace

UnixRpcChannel::UnixRpcChannel(int file_descriptor) : file_descriptor_(file_descriptor) {}

UnixRpcChannel::~UnixRpcChannel()
{
    if(file_descriptor_ >= 0)
    {
        static_cast<void>(::close(file_descriptor_));
    }
}

UnixRpcChannel::UnixRpcChannel(UnixRpcChannel&& other) noexcept
    : file_descriptor_(other.file_descriptor_)
{
    other.file_descriptor_ = -1;
}

UnixRpcChannel& UnixRpcChannel::operator=(UnixRpcChannel&& other) noexcept
{
    if(this != &other)
    {
        if(file_descriptor_ >= 0)
        {
            static_cast<void>(::close(file_descriptor_));
        }
        file_descriptor_ = other.file_descriptor_;
        other.file_descriptor_ = -1;
    }
    return *this;
}

Result<std::unique_ptr<UnixRpcChannel>> UnixRpcChannel::connect(
    const UnixChannelConfig& config)
{
    if(config.socket_path.empty() || config.socket_path.size() >= sizeof(sockaddr_un::sun_path))
    {
        return Result<std::unique_ptr<UnixRpcChannel>>::failure({
            ErrorCode::SocketPath,
            "bridge socket path is empty or exceeds the local socket limit",
            std::nullopt,
        });
    }
    auto expected_peer = resolve_expected_peer(config);
    if(!expected_peer)
    {
        return Result<std::unique_ptr<UnixRpcChannel>>::failure(expected_peer.error());
    }
    const auto file_descriptor = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if(file_descriptor < 0)
    {
        return Result<std::unique_ptr<UnixRpcChannel>>::failure({
            ErrorCode::SocketCreate,
            "failed to create the local bridge socket",
            std::nullopt,
        });
    }
    sockaddr_un address {};
    address.sun_family = AF_UNIX;
    std::memcpy(address.sun_path, config.socket_path.c_str(), config.socket_path.size() + 1);
    if(::connect(file_descriptor, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0)
    {
        static_cast<void>(::close(file_descriptor));
        return Result<std::unique_ptr<UnixRpcChannel>>::failure({
            ErrorCode::SocketConnect,
            "failed to connect to the local HyperFlux bridge",
            std::nullopt,
        });
    }
    auto timeout = configure_timeout(file_descriptor, config.timeout_ms);
    if(!timeout)
    {
        static_cast<void>(::close(file_descriptor));
        return Result<std::unique_ptr<UnixRpcChannel>>::failure(timeout.error());
    }
    auto peer = verify_peer(file_descriptor, expected_peer.value());
    if(!peer)
    {
        static_cast<void>(::close(file_descriptor));
        return Result<std::unique_ptr<UnixRpcChannel>>::failure(peer.error());
    }
    return Result<std::unique_ptr<UnixRpcChannel>>::success(
        std::unique_ptr<UnixRpcChannel>(new UnixRpcChannel(file_descriptor)));
}

Result<std::unique_ptr<UnixRpcChannel>> UnixRpcChannel::adopt_connected_socket(
    int file_descriptor,
    std::uint32_t timeout_ms)
{
    if(file_descriptor < 0)
    {
        return Result<std::unique_ptr<UnixRpcChannel>>::failure({
            ErrorCode::InvalidArgument,
            "connected socket file descriptor is invalid",
            std::nullopt,
        });
    }
    auto configured = configure_timeout(file_descriptor, timeout_ms);
    if(!configured)
    {
        static_cast<void>(::close(file_descriptor));
        return Result<std::unique_ptr<UnixRpcChannel>>::failure(configured.error());
    }
    return Result<std::unique_ptr<UnixRpcChannel>>::success(
        std::unique_ptr<UnixRpcChannel>(new UnixRpcChannel(file_descriptor)));
}

Result<v5::RpcResponse> UnixRpcChannel::exchange(const v5::RpcRequest& request)
{
    std::string payload;
    try
    {
        payload = json_codec::encode(request).dump();
    }
    catch(const std::exception&)
    {
        return Result<v5::RpcResponse>::failure({
            ErrorCode::InvalidProtocol,
            "SDK request cannot be encoded as a protocol-v5 message",
            std::nullopt,
        });
    }
    if(payload.empty() || payload.size() > v5::max_wire_message_bytes
       || payload.size() > std::numeric_limits<std::uint32_t>::max())
    {
        return Result<v5::RpcResponse>::failure({
            ErrorCode::PayloadTooLarge,
            "SDK request is empty or exceeds the bridge message bound",
            std::nullopt,
        });
    }
    const auto length = htonl(static_cast<std::uint32_t>(payload.size()));
    auto written = write_all(
        file_descriptor_,
        reinterpret_cast<const std::uint8_t*>(&length),
        sizeof(length));
    if(!written)
    {
        return Result<v5::RpcResponse>::failure(written.error());
    }
    written = write_all(
        file_descriptor_,
        reinterpret_cast<const std::uint8_t*>(payload.data()),
        payload.size());
    if(!written)
    {
        return Result<v5::RpcResponse>::failure(written.error());
    }

    std::uint32_t network_length = 0;
    auto read = read_all(
        file_descriptor_,
        reinterpret_cast<std::uint8_t*>(&network_length),
        sizeof(network_length));
    if(!read)
    {
        return Result<v5::RpcResponse>::failure(read.error());
    }
    const auto response_size = static_cast<std::size_t>(ntohl(network_length));
    if(response_size == 0)
    {
        return Result<v5::RpcResponse>::failure({
            ErrorCode::InvalidFrame,
            "bridge response declares an empty payload",
            std::nullopt,
        });
    }
    if(response_size > v5::max_wire_message_bytes)
    {
        return Result<v5::RpcResponse>::failure({
            ErrorCode::PayloadTooLarge,
            "bridge response exceeds the negotiated message bound",
            std::nullopt,
        });
    }
    std::string response(response_size, '\0');
    read = read_all(
        file_descriptor_,
        reinterpret_cast<std::uint8_t*>(response.data()),
        response.size());
    if(!read)
    {
        return Result<v5::RpcResponse>::failure(read.error());
    }
    return decode_response(response);
}

} // namespace hyperflux::sdk
