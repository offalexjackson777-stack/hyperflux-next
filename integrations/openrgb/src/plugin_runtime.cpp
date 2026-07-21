// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/build_config.hpp>
#include <hyperflux/openrgb/plugin_runtime.hpp>

#include <hyperflux/sdk/recovery.hpp>

#include <algorithm>
#include <array>
#include <cctype>
#include <memory>
#include <string>
#include <sys/un.h>
#include <utility>
#include <vector>

namespace hyperflux::openrgb::native
{
namespace
{

constexpr std::array<std::string_view, 6> required_features = {
    "integration-view-projection",
    "event-subscriptions",
    "ownership-leases",
    "atomic-transactions",
    "profile-bound-transactions",
    "semantic-stable-lighting",
};

constexpr std::array<std::string_view, 1> optional_features = {
    "structured-diagnostics",
};

sdk::Error configuration_error(std::string message)
{
    return {
        sdk::ErrorCode::RuntimeConfiguration,
        std::move(message),
        "HFX-RUNTIME-001",
    };
}

bool valid_account_name(std::string_view value) noexcept
{
    if(value.empty() || value.size() > 32)
    {
        return false;
    }
    const auto first = static_cast<unsigned char>(value.front());
    if(first != '_' && std::islower(first) == 0)
    {
        return false;
    }
    return std::all_of(value.begin() + 1, value.end(), [](char character)
    {
        const auto value = static_cast<unsigned char>(character);
        return std::islower(value) != 0 || std::isdigit(value) != 0
            || character == '_' || character == '-';
    });
}

sdk::Result<std::vector<ProtocolFeatureId>> decode_features(
    std::span<const std::string_view> values)
{
    std::vector<ProtocolFeatureId> decoded;
    decoded.reserve(values.size());
    for(const auto value : values)
    {
        auto feature = ProtocolFeatureId::from(value);
        if(!feature.has_value())
        {
            return sdk::Result<std::vector<ProtocolFeatureId>>::failure(
                configuration_error("OpenRGB runtime contains an invalid protocol feature"));
        }
        decoded.push_back(std::move(*feature));
    }
    return sdk::Result<std::vector<ProtocolFeatureId>>::success(std::move(decoded));
}

} // namespace

std::span<const std::string_view> required_runtime_features() noexcept
{
    return required_features;
}

std::span<const std::string_view> optional_runtime_features() noexcept
{
    return optional_features;
}

ProductionRuntimeConfig default_production_runtime_config()
{
    return {
        std::string(build_config::bridge_runtime_directory) + "/"
            + std::string(build_config::bridge_socket_name),
        5'000,
        std::string(build_config::bridge_service_account),
        "openrgb",
        "HyperFlux Next OpenRGB integration",
    };
}

sdk::Result<std::unique_ptr<RuntimeBridge>> create_production_runtime(
    ProductionRuntimeConfig config)
{
    if(config.socket_path.empty() || config.socket_path.front() != '/'
       || config.socket_path.find('\0') != std::string::npos
       || config.socket_path.size() >= sizeof(sockaddr_un {}.sun_path))
    {
        return sdk::Result<std::unique_ptr<RuntimeBridge>>::failure(
            configuration_error("OpenRGB runtime requires a bounded absolute Unix socket path"));
    }
    if(config.timeout_ms == 0)
    {
        return sdk::Result<std::unique_ptr<RuntimeBridge>>::failure(
            configuration_error("OpenRGB runtime timeout must be nonzero"));
    }
    if(!valid_account_name(config.expected_peer_user))
    {
        return sdk::Result<std::unique_ptr<RuntimeBridge>>::failure(
            configuration_error("OpenRGB runtime requires a valid bridge service account"));
    }
    auto client_id = ClientId::from(config.client_id);
    auto client_name = ClientName::from(config.client_name);
    if(!client_id.has_value() || !client_name.has_value())
    {
        return sdk::Result<std::unique_ptr<RuntimeBridge>>::failure(
            configuration_error("OpenRGB runtime client identity violates the domain contract"));
    }
    auto required = decode_features(required_runtime_features());
    auto optional = decode_features(optional_runtime_features());
    if(!required || !optional)
    {
        return sdk::Result<std::unique_ptr<RuntimeBridge>>::failure(
            !required ? required.error() : optional.error());
    }

    sdk::UnixChannelConfig channel {
        std::move(config.socket_path),
        config.timeout_ms,
        std::nullopt,
        std::move(config.expected_peer_user),
    };
    sdk::ClientConfig client {
        std::move(*client_id),
        std::move(*client_name),
        std::move(required).value(),
        std::move(optional).value(),
    };
    auto recovering = sdk::RecoveringClient::create(
        std::make_unique<sdk::UnixClientFactory>(std::move(channel), std::move(client)));
    if(!recovering)
    {
        return sdk::Result<std::unique_ptr<RuntimeBridge>>::failure(recovering.error());
    }
    auto runtime = ClientRuntimeBridge::create(std::move(recovering).value());
    if(!runtime)
    {
        return sdk::Result<std::unique_ptr<RuntimeBridge>>::failure(runtime.error());
    }
    return sdk::Result<std::unique_ptr<RuntimeBridge>>::success(
        std::move(runtime).value());
}

} // namespace hyperflux::openrgb::native
