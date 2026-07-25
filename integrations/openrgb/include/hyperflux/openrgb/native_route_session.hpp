// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "razer_feature_transport.hpp"

#include <hyperflux/sdk.hpp>

#include <chrono>
#include <functional>
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

[[nodiscard]] NativeRouteIdentity native_route_identity(const v7::NativeRoute& route);

class SdkNativeFeatureExchange final : public NativeFeatureExchange
{
public:
    using Clock = std::function<std::chrono::steady_clock::time_point()>;

    SdkNativeFeatureExchange(const SdkNativeFeatureExchange&) = delete;
    SdkNativeFeatureExchange& operator=(const SdkNativeFeatureExchange&) = delete;
    ~SdkNativeFeatureExchange() override;

    [[nodiscard]] static sdk::Result<std::unique_ptr<SdkNativeFeatureExchange>> connect_unix(
        const sdk::UnixChannelConfig& channel_config,
        const NativeRouteIdentity& expected_route);

    [[nodiscard]] static sdk::Result<std::unique_ptr<SdkNativeFeatureExchange>> create(
        std::unique_ptr<sdk::NativeClientApi> client,
        const NativeRouteIdentity& expected_route,
        Clock clock = [] { return std::chrono::steady_clock::now(); });

    [[nodiscard]] NativeFeatureExchangeResult exchange(
        const std::array<std::uint8_t, razer_native_report_bytes>& request) override;

private:
    SdkNativeFeatureExchange(
        std::unique_ptr<sdk::NativeClientApi> client,
        v7::NativeRoute route,
        LeaseId lease_id,
        Clock clock);

    [[nodiscard]] bool renew_if_due();

    std::unique_ptr<sdk::NativeClientApi> client_;
    v7::NativeRoute route_;
    LeaseId lease_id_;
    Clock clock_;
    std::chrono::steady_clock::time_point renew_due_;
};

} // namespace hyperflux::openrgb::native
