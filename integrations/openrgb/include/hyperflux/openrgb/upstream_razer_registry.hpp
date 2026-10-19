// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "client_identity.hpp"
#include "native_route_session.hpp"

#include <hyperflux/sdk.hpp>

#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <vector>

class RGBController;
class RGBControllerInterface;

namespace hyperflux::openrgb::native
{

class UpstreamRazerControllerInstance
{
public:
    UpstreamRazerControllerInstance(const UpstreamRazerControllerInstance&) = delete;
    UpstreamRazerControllerInstance& operator=(const UpstreamRazerControllerInstance&) = delete;
    ~UpstreamRazerControllerInstance();

    [[nodiscard]] RGBController& controller() noexcept;
    [[nodiscard]] const RGBController& controller() const noexcept;
    [[nodiscard]] const v8::NativeRoute& route() const noexcept;
    [[nodiscard]] std::string_view name() const noexcept;
    [[nodiscard]] std::string_view serial() const noexcept;

private:
    friend class UpstreamRazerControllerFactory;

    UpstreamRazerControllerInstance(
        v8::NativeRoute route,
        std::unique_ptr<NativeFeatureExchange> exchange,
        std::unique_ptr<RGBController> controller);

    v8::NativeRoute route_;
    std::unique_ptr<NativeFeatureExchange> exchange_;
    std::unique_ptr<RGBController> controller_;
    std::string name_;
    std::string serial_;
};

using NativeFeatureExchangeFactory = std::function<
    sdk::Result<std::unique_ptr<NativeFeatureExchange>>(
        const NativeRouteIdentity&,
        ClientId)>;

[[nodiscard]] NativeFeatureExchangeFactory make_unix_native_feature_exchange_factory(
    sdk::UnixChannelConfig channel_config);

class UpstreamRazerControllerFactory
{
public:
    explicit UpstreamRazerControllerFactory(
        NativeFeatureExchangeFactory exchange_factory,
        OpenRgbClientIdFactory client_id_factory =
            process_openrgb_client_id_factory(OpenRgbClientRole::Feature));

    [[nodiscard]] sdk::Result<std::unique_ptr<UpstreamRazerControllerInstance>> create(
        const v8::NativeRoute& route);

private:
    NativeFeatureExchangeFactory exchange_factory_;
    OpenRgbClientIdFactory client_id_factory_;
};

class UpstreamControllerHost
{
public:
    virtual ~UpstreamControllerHost() = default;
    virtual void register_controller(RGBController& controller) noexcept = 0;
    virtual void unregister_controller(RGBController& controller) noexcept = 0;
};

struct UpstreamRegistryUpdate
{
    std::size_t added = 0;
    std::size_t removed = 0;
    std::size_t replaced = 0;
    std::size_t retained = 0;
    std::size_t newly_suppressed = 0;
    std::size_t newly_restored = 0;
    std::size_t registered = 0;

    friend bool operator==(const UpstreamRegistryUpdate&, const UpstreamRegistryUpdate&) = default;
};

struct UpstreamRazerControllerSummary
{
    std::string stable_id;
    std::string name;
    DeviceKind device_kind;
    ProductId product_id;
    ReceiverId receiver_id;
    GenerationId generation_id;
    LogicalDeviceId device_id;
    bool registered;
    bool direct_suppressed;
    /// Bridge battery observation for this device, when the bridge reported one.
    std::optional<v8::BatteryObservation> battery {};
    /// Whether the bridge last saw the device awake, asleep or out of reach.
    std::optional<PresenceState> presence {};

    friend bool operator==(
        const UpstreamRazerControllerSummary&,
        const UpstreamRazerControllerSummary&) = default;
};

/// Owns only receiver-routed upstream controllers created by HyperFlux.
///
/// Direct-route precedence is deliberately serial-only. Callers provide
/// nonempty serials that OpenRGB physically queried from direct local HID
/// controllers; PID, name and device kind never participate in suppression.
class UpstreamRazerControllerRegistry
{
public:
    UpstreamRazerControllerRegistry(
        UpstreamControllerHost& host,
        UpstreamRazerControllerFactory& factory) noexcept;
    UpstreamRazerControllerRegistry(const UpstreamRazerControllerRegistry&) = delete;
    UpstreamRazerControllerRegistry& operator=(const UpstreamRazerControllerRegistry&) = delete;
    ~UpstreamRazerControllerRegistry();

    [[nodiscard]] sdk::Result<UpstreamRegistryUpdate> apply_routes(
        std::vector<v8::NativeRoute> routes);
    [[nodiscard]] UpstreamRegistryUpdate apply_direct_serials(
        std::vector<std::string> physically_queried_serials);
    void shutdown() noexcept;

    [[nodiscard]] std::size_t size() const noexcept;
    [[nodiscard]] std::size_t registered_count() const noexcept;
    [[nodiscard]] std::size_t suppressed_count() const noexcept;
    [[nodiscard]] std::vector<std::string> serials() const;
    [[nodiscard]] std::vector<UpstreamRazerControllerSummary> summaries() const;
    [[nodiscard]] std::uintptr_t controller_identity(std::string_view stable_id) const noexcept;

private:
    struct Entry
    {
        std::unique_ptr<UpstreamRazerControllerInstance> instance;
        bool registered = false;
        bool direct_suppressed = false;
    };

    [[nodiscard]] static std::string stable_id(const v8::NativeRoute& route);
    [[nodiscard]] bool should_suppress(const Entry& entry) const;
    void reconcile_registration(Entry& entry, UpstreamRegistryUpdate& update);
    [[nodiscard]] std::size_t registered_count_internal() const noexcept;

    UpstreamControllerHost* host_;
    UpstreamRazerControllerFactory* factory_;
    std::map<std::string, Entry, std::less<>> entries_;
    std::set<std::string, std::less<>> direct_serials_;
    bool stopped_ = false;
};

[[nodiscard]] bool is_eligible_upstream_razer_route(const v8::NativeRoute& route) noexcept;
[[nodiscard]] std::string canonical_queried_serial(std::string_view serial);
[[nodiscard]] std::vector<std::string> collect_direct_razer_serials(
    const std::vector<RGBControllerInterface*>& controllers);

} // namespace hyperflux::openrgb::native
