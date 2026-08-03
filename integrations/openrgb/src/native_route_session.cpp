// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/native_route_session.hpp>

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <variant>

namespace hyperflux::openrgb::native
{
namespace
{

constexpr std::uint32_t lease_duration_ms = 30'000;
constexpr std::uint16_t exchange_timeout_ms = 1'000;

sdk::Error route_error(sdk::ErrorCode code, std::string message)
{
    return {code, std::move(message), "HFX-NATIVE-OPENRGB-001"};
}

template<typename T, typename Value>
sdk::Result<T> checked_domain(Value value, std::string_view label)
{
    auto decoded = T::from(value);
    if(!decoded.has_value())
    {
        return sdk::Result<T>::failure(route_error(
            sdk::ErrorCode::InvalidArgument,
            "invalid generated domain value for " + std::string(label)));
    }
    return sdk::Result<T>::success(std::move(*decoded));
}

bool route_is_usable(const v7::NativeRoute& route)
{
    return route.route_state == NativeRouteState::Available
        && route.access == NativeRouteAccess::Qualified
        && route.protocol_family.value() == "razer-feature-report-v1"
        && route.report_bytes.value() == razer_native_report_bytes
        && route.route_token.has_value()
        && route.resource.has_value()
        && route.resource->receiver_id == route.receiver_id
        && route.resource->generation_id == route.generation_id
        && route.resource->device_id == route.device_id
        && route.resource->kind == ResourceKind::NativeControl;
}

std::string encode_hex(
    const std::array<std::uint8_t, razer_native_report_bytes>& report)
{
    constexpr std::array<char, 16> digits {
        '0', '1', '2', '3', '4', '5', '6', '7',
        '8', '9', 'a', 'b', 'c', 'd', 'e', 'f',
    };
    std::string encoded;
    encoded.reserve(report.size() * 2);
    for(const auto byte : report)
    {
        encoded.push_back(digits[byte >> 4]);
        encoded.push_back(digits[byte & 0x0f]);
    }
    return encoded;
}

std::optional<std::array<std::uint8_t, razer_native_report_bytes>> decode_hex(
    std::string_view encoded)
{
    if(encoded.size() != razer_native_report_bytes * 2)
    {
        return std::nullopt;
    }
    const auto nibble = [](char value) -> std::optional<std::uint8_t> {
        if(value >= '0' && value <= '9')
        {
            return static_cast<std::uint8_t>(value - '0');
        }
        if(value >= 'a' && value <= 'f')
        {
            return static_cast<std::uint8_t>(10 + value - 'a');
        }
        if(value >= 'A' && value <= 'F')
        {
            return static_cast<std::uint8_t>(10 + value - 'A');
        }
        return std::nullopt;
    };
    std::array<std::uint8_t, razer_native_report_bytes> report {};
    for(std::size_t index = 0; index < report.size(); ++index)
    {
        const auto high = nibble(encoded[index * 2]);
        const auto low = nibble(encoded[index * 2 + 1]);
        if(!high.has_value() || !low.has_value())
        {
            return std::nullopt;
        }
        report[index] = static_cast<std::uint8_t>((*high << 4) | *low);
    }
    return report;
}

} // namespace

NativeRouteIdentity native_route_identity(const v7::NativeRoute& route)
{
    return {
        route.receiver_id,
        route.generation_id,
        route.device_id,
        route.endpoint_id,
        route.device_kind,
        route.vendor_id,
        route.product_id,
    };
}

sdk::Result<sdk::NativeClientConfig> native_route_client_config(ClientId client_id)
{
    auto client_name = checked_domain<ClientName>(
        "OpenRGB HyperFlux native transport", "client name");
    auto tunnel = checked_domain<ProtocolFeatureId>(
        "native-receiver-tunnel", "required tunnel feature");
    auto ownership = checked_domain<ProtocolFeatureId>(
        "ownership-leases", "required ownership feature");
    if(!client_name || !tunnel || !ownership)
    {
        return sdk::Result<sdk::NativeClientConfig>::failure(
            !client_name ? client_name.error()
                         : (!tunnel ? tunnel.error() : ownership.error()));
    }
    return sdk::Result<sdk::NativeClientConfig>::success({
        std::move(client_id),
        std::move(client_name).value(),
        {
            std::move(tunnel).value(),
            std::move(ownership).value(),
        },
        {},
    });
}

SdkNativeFeatureExchange::SdkNativeFeatureExchange(
    std::unique_ptr<sdk::NativeClientApi> client,
    v7::NativeRoute route)
    : client_(std::move(client)),
      route_(std::move(route))
{
}

sdk::Result<std::unique_ptr<SdkNativeFeatureExchange>>
SdkNativeFeatureExchange::connect_unix(
    const sdk::UnixChannelConfig& channel_config,
    const NativeRouteIdentity& expected_route,
    ClientId client_id)
{
    auto client_config = native_route_client_config(std::move(client_id));
    if(!client_config)
    {
        return sdk::Result<std::unique_ptr<SdkNativeFeatureExchange>>::failure(
            client_config.error());
    }
    auto connected = sdk::NativeClient::connect_unix(
        channel_config,
        std::move(client_config).value());
    if(!connected)
    {
        return sdk::Result<std::unique_ptr<SdkNativeFeatureExchange>>::failure(
            connected.error());
    }
    return create(
        std::make_unique<sdk::NativeClient>(std::move(connected).value()),
        expected_route);
}

sdk::Result<std::unique_ptr<SdkNativeFeatureExchange>> SdkNativeFeatureExchange::create(
    std::unique_ptr<sdk::NativeClientApi> client,
    const NativeRouteIdentity& expected_route)
{
    if(client == nullptr)
    {
        return sdk::Result<std::unique_ptr<SdkNativeFeatureExchange>>::failure(
            route_error(sdk::ErrorCode::InvalidArgument, "native route session inputs are incomplete"));
    }
    auto inventory = client->native_enumerate();
    if(!inventory)
    {
        return sdk::Result<std::unique_ptr<SdkNativeFeatureExchange>>::failure(
            inventory.error());
    }
    const auto found = std::find_if(
        inventory.value().routes.begin(),
        inventory.value().routes.end(),
        [&expected_route](const v7::NativeRoute& route) {
            return native_route_identity(route) == expected_route;
        });
    if(found == inventory.value().routes.end() || !route_is_usable(*found))
    {
        return sdk::Result<std::unique_ptr<SdkNativeFeatureExchange>>::failure(route_error(
            sdk::ErrorCode::InvalidController,
            "native OpenRGB route is absent, stale, or not qualified"));
    }
    return sdk::Result<std::unique_ptr<SdkNativeFeatureExchange>>::success(
        std::unique_ptr<SdkNativeFeatureExchange>(new SdkNativeFeatureExchange(
            std::move(client),
            *found)));
}

NativeFeatureExchangeResult SdkNativeFeatureExchange::exchange(
    const std::array<std::uint8_t, razer_native_report_bytes>& request)
{
    if(!route_.route_token.has_value() || !route_.resource.has_value())
    {
        return {NativeExchangeCertainty::NotSent, {}};
    }
    auto duration = LeaseDurationMs::from(lease_duration_ms);
    if(!duration.has_value())
    {
        return {NativeExchangeCertainty::NotSent, {}};
    }
    auto lease = client_->acquire_lease({*route_.resource}, *duration);
    const auto* granted = lease
        ? std::get_if<v7::LeaseResultGranted>(&lease.value())
        : nullptr;
    if(granted == nullptr)
    {
        return {NativeExchangeCertainty::NotSent, {}};
    }
    const auto lease_id = granted->detail.lease_id;
    auto report = NativeReportHex::from(encode_hex(request));
    auto timeout = NativeExchangeTimeoutMs::from(exchange_timeout_ms);
    if(!report.has_value() || !timeout.has_value())
    {
        static_cast<void>(client_->release_lease(lease_id));
        return {NativeExchangeCertainty::NotSent, {}};
    }
    auto submission = client_->prepare_native_exchange(
        lease_id, *route_.route_token, *report, *timeout);
    if(!submission)
    {
        static_cast<void>(client_->release_lease(lease_id));
        return {NativeExchangeCertainty::NotSent, {}};
    }
    auto outcome = client_->native_transact(submission.value());
    static_cast<void>(client_->release_lease(lease_id));
    if(!outcome)
    {
        return {NativeExchangeCertainty::MayHaveBeenSent, {}};
    }
    if(outcome.value().request_id != submission.value().request_id
       || outcome.value().route_token != submission.value().route_token)
    {
        return {NativeExchangeCertainty::MayHaveBeenSent, {}};
    }
    if(std::holds_alternative<v7::NativeExchangeResultNotSent>(outcome.value().result))
    {
        return {NativeExchangeCertainty::NotSent, {}};
    }
    if(std::holds_alternative<v7::NativeExchangeResultMayHaveBeenSent>(
           outcome.value().result))
    {
        return {NativeExchangeCertainty::MayHaveBeenSent, {}};
    }
    const auto* matched = std::get_if<v7::NativeExchangeResultMatchedResponse>(
        &outcome.value().result);
    if(matched == nullptr)
    {
        return {NativeExchangeCertainty::MayHaveBeenSent, {}};
    }
    const auto response = decode_hex(matched->detail.response.value());
    if(!response.has_value())
    {
        return {NativeExchangeCertainty::MayHaveBeenSent, {}};
    }
    return {NativeExchangeCertainty::MatchedResponse, *response};
}

} // namespace hyperflux::openrgb::native
