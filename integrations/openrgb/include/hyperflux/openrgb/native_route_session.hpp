// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "razer_feature_transport.hpp"

#include <hyperflux/sdk.hpp>

#include <memory>

namespace hyperflux::openrgb::native
{

struct NativeRouteIdentity
{
    ReceiverId receiver_id;
    GenerationId generation_id;
    LogicalDeviceId device_id;
    EndpointId endpoint_id;
    DeviceKind device_kind;
    VendorId vendor_id;
    ProductId product_id;

    friend bool operator==(const NativeRouteIdentity&, const NativeRouteIdentity&) = default;
};

[[nodiscard]] NativeRouteIdentity native_route_identity(const v8::NativeRoute& route);

[[nodiscard]] sdk::Result<sdk::NativeClientConfig> native_route_client_config(
    ClientId client_id);

class SdkNativeFeatureExchange final : public NativeFeatureExchange
{
public:
    SdkNativeFeatureExchange(const SdkNativeFeatureExchange&) = delete;
    SdkNativeFeatureExchange& operator=(const SdkNativeFeatureExchange&) = delete;
    ~SdkNativeFeatureExchange() override = default;

    [[nodiscard]] static sdk::Result<std::unique_ptr<SdkNativeFeatureExchange>> connect_unix(
        const sdk::UnixChannelConfig& channel_config,
        const NativeRouteIdentity& expected_route,
        ClientId client_id);

    [[nodiscard]] static sdk::Result<std::unique_ptr<SdkNativeFeatureExchange>> create(
        std::unique_ptr<sdk::NativeClientApi> client,
        const NativeRouteIdentity& expected_route);

    [[nodiscard]] NativeFeatureExchangeResult exchange(
        const std::array<std::uint8_t, razer_native_report_bytes>& request) override;

private:
    SdkNativeFeatureExchange(
        std::unique_ptr<sdk::NativeClientApi> client,
        v8::NativeRoute route);

    std::unique_ptr<sdk::NativeClientApi> client_;
    v8::NativeRoute route_;
};

} // namespace hyperflux::openrgb::native
