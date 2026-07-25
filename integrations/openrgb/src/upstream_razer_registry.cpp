// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/upstream_razer_registry.hpp>

#include "RGBController.h"
#include "RGBController_Razer.h"
#include "RazerController.h"
#include "RazerDevices.h"

#include <algorithm>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <utility>

namespace hyperflux::openrgb::native
{
namespace
{

sdk::Error registry_error(std::string message)
{
    return {
        sdk::ErrorCode::InvalidController,
        std::move(message),
        "HFX-INTEGRATION-001",
    };
}

const razer_device* upstream_device(const v7::NativeRoute& route)
{
    const auto product_id = static_cast<unsigned short>(route.product_id.value());
    const auto found = std::find_if(
        device_list,
        device_list + RAZER_NUM_DEVICES,
        [product_id](const razer_device* device)
        {
            return device != nullptr && device->pid == product_id;
        });
    if(found == device_list + RAZER_NUM_DEVICES)
    {
        return nullptr;
    }
    const auto expected_type = route.device_kind == DeviceKind::Mouse
                                   ? DEVICE_TYPE_MOUSE
                                   : route.device_kind == DeviceKind::Keyboard
                                         ? DEVICE_TYPE_KEYBOARD
                                         : DEVICE_TYPE_UNKNOWN;
    return (*found)->type == expected_type ? *found : nullptr;
}

std::string route_location(const v7::NativeRoute& route)
{
    return "hyperflux-next/native/receiver/" + std::string(route.receiver_id.value())
        + "/generation/" + std::to_string(route.generation_id.value())
        + "/device/" + std::string(route.device_id.value())
        + "/endpoint/" + std::string(route.endpoint_id.value());
}

} // namespace

UpstreamRazerControllerInstance::UpstreamRazerControllerInstance(
    v7::NativeRoute route,
    std::unique_ptr<NativeFeatureExchange> exchange,
    std::unique_ptr<RGBController> controller)
    : route_(std::move(route)),
      exchange_(std::move(exchange)),
      controller_(std::move(controller))
{
}

UpstreamRazerControllerInstance::~UpstreamRazerControllerInstance() = default;

RGBController& UpstreamRazerControllerInstance::controller() noexcept
{
    return *controller_;
}

const RGBController& UpstreamRazerControllerInstance::controller() const noexcept
{
    return *controller_;
}

const v7::NativeRoute& UpstreamRazerControllerInstance::route() const noexcept
{
    return route_;
}

std::string_view UpstreamRazerControllerInstance::serial() const noexcept
{
    return controller_->serial;
}

NativeFeatureExchangeFactory make_unix_native_feature_exchange_factory(
    sdk::UnixChannelConfig channel_config)
{
    return [channel_config = std::move(channel_config)](
               const NativeRouteIdentity& route)
               -> sdk::Result<std::unique_ptr<NativeFeatureExchange>>
    {
        auto exchange = SdkNativeFeatureExchange::connect_unix(channel_config, route);
        if(!exchange)
        {
            return sdk::Result<std::unique_ptr<NativeFeatureExchange>>::failure(
                exchange.error());
        }
        std::unique_ptr<NativeFeatureExchange> erased = std::move(exchange).value();
        return sdk::Result<std::unique_ptr<NativeFeatureExchange>>::success(
            std::move(erased));
    };
}

UpstreamRazerControllerFactory::UpstreamRazerControllerFactory(
    NativeFeatureExchangeFactory exchange_factory)
    : exchange_factory_(std::move(exchange_factory))
{
}

sdk::Result<std::unique_ptr<UpstreamRazerControllerInstance>>
UpstreamRazerControllerFactory::create(const v7::NativeRoute& route) const
{
    if(!exchange_factory_ || !is_eligible_upstream_razer_route(route))
    {
        return sdk::Result<std::unique_ptr<UpstreamRazerControllerInstance>>::failure(
            registry_error("OpenRGB native route is not eligible for an upstream Razer controller"));
    }
    const auto* device = upstream_device(route);
    if(device == nullptr)
    {
        return sdk::Result<std::unique_ptr<UpstreamRazerControllerInstance>>::failure(
            registry_error("OpenRGB has no matching upstream Razer device record"));
    }

    auto exchange = exchange_factory_(native_route_identity(route));
    if(!exchange)
    {
        return sdk::Result<std::unique_ptr<UpstreamRazerControllerInstance>>::failure(
            exchange.error());
    }
    auto owned_exchange = std::move(exchange).value();
    auto transport = std::make_unique<HyperFluxRazerFeatureTransport>(*owned_exchange);
    auto upstream = std::make_unique<RazerController>(
        nullptr,
        nullptr,
        route_location(route).c_str(),
        static_cast<unsigned short>(route.product_id.value()),
        device->name.c_str(),
        std::move(transport));
    std::unique_ptr<RGBController> controller =
        std::make_unique<RGBController_Razer>(upstream.release());
    return sdk::Result<std::unique_ptr<UpstreamRazerControllerInstance>>::success(
        std::unique_ptr<UpstreamRazerControllerInstance>(new UpstreamRazerControllerInstance(
            route,
            std::move(owned_exchange),
            std::move(controller))));
}

UpstreamRazerControllerRegistry::UpstreamRazerControllerRegistry(
    UpstreamControllerHost& host,
    UpstreamRazerControllerFactory& factory) noexcept
    : host_(&host),
      factory_(&factory)
{
}

UpstreamRazerControllerRegistry::~UpstreamRazerControllerRegistry()
{
    shutdown();
}

std::string UpstreamRazerControllerRegistry::stable_id(const v7::NativeRoute& route)
{
    return std::string(route.receiver_id.value()) + "/" + std::string(route.device_id.value())
        + "/" + std::string(route.endpoint_id.value());
}

bool UpstreamRazerControllerRegistry::should_suppress(const Entry& entry) const
{
    const auto serial = canonical_queried_serial(entry.instance->serial());
    return !serial.empty() && direct_serials_.contains(serial);
}

void UpstreamRazerControllerRegistry::reconcile_registration(
    Entry& entry, UpstreamRegistryUpdate& update)
{
    const bool suppress = should_suppress(entry);
    if(suppress != entry.direct_suppressed)
    {
        suppress ? ++update.newly_suppressed : ++update.newly_restored;
        entry.direct_suppressed = suppress;
    }
    const bool should_register = !suspended_ && !entry.direct_suppressed;
    if(should_register && !entry.registered)
    {
        host_->register_controller(entry.instance->controller());
        entry.registered = true;
    }
    else if(!should_register && entry.registered)
    {
        host_->unregister_controller(entry.instance->controller());
        entry.registered = false;
    }
}

sdk::Result<UpstreamRegistryUpdate> UpstreamRazerControllerRegistry::apply_routes(
    std::vector<v7::NativeRoute> routes)
{
    if(stopped_)
    {
        return sdk::Result<UpstreamRegistryUpdate>::failure(
            registry_error("OpenRGB upstream Razer registry is stopped"));
    }

    std::map<std::string, v7::NativeRoute, std::less<>> eligible;
    for(auto& route : routes)
    {
        if(!is_eligible_upstream_razer_route(route) || upstream_device(route) == nullptr)
        {
            continue;
        }
        const auto id = stable_id(route);
        if(!eligible.emplace(id, std::move(route)).second)
        {
            return sdk::Result<UpstreamRegistryUpdate>::failure(
                registry_error("OpenRGB native route inventory contains a duplicate identity"));
        }
    }

    std::map<std::string, std::unique_ptr<UpstreamRazerControllerInstance>, std::less<>> prepared;
    for(const auto& [id, route] : eligible)
    {
        const auto existing = entries_.find(id);
        if(existing != entries_.end() && existing->second.instance->route() == route)
        {
            continue;
        }
        auto created = factory_->create(route);
        if(!created)
        {
            return sdk::Result<UpstreamRegistryUpdate>::failure(created.error());
        }
        prepared.emplace(id, std::move(created).value());
    }

    UpstreamRegistryUpdate update;
    for(auto existing = entries_.begin(); existing != entries_.end();)
    {
        if(eligible.contains(existing->first))
        {
            ++existing;
            continue;
        }
        if(existing->second.registered)
        {
            host_->unregister_controller(existing->second.instance->controller());
        }
        existing = entries_.erase(existing);
        ++update.removed;
    }

    for(const auto& [id, route] : eligible)
    {
        auto existing = entries_.find(id);
        if(existing == entries_.end())
        {
            Entry entry {std::move(prepared.at(id)), false, false};
            auto inserted = entries_.emplace(id, std::move(entry)).first;
            reconcile_registration(inserted->second, update);
            ++update.added;
            continue;
        }
        if(existing->second.instance->route() == route)
        {
            reconcile_registration(existing->second, update);
            ++update.retained;
            continue;
        }
        if(existing->second.registered)
        {
            host_->unregister_controller(existing->second.instance->controller());
        }
        existing->second = Entry {std::move(prepared.at(id)), false, false};
        reconcile_registration(existing->second, update);
        ++update.replaced;
    }
    update.registered = registered_count_internal();
    return sdk::Result<UpstreamRegistryUpdate>::success(update);
}

UpstreamRegistryUpdate UpstreamRazerControllerRegistry::apply_direct_serials(
    std::vector<std::string> physically_queried_serials)
{
    direct_serials_.clear();
    for(auto& serial : physically_queried_serials)
    {
        auto canonical = canonical_queried_serial(serial);
        if(!canonical.empty())
        {
            direct_serials_.insert(std::move(canonical));
        }
    }
    UpstreamRegistryUpdate update;
    if(!stopped_)
    {
        for(auto& [id, entry] : entries_)
        {
            (void)id;
            reconcile_registration(entry, update);
        }
    }
    update.registered = registered_count_internal();
    return update;
}

void UpstreamRazerControllerRegistry::suspend_for_detection() noexcept
{
    if(stopped_ || suspended_)
    {
        return;
    }
    suspended_ = true;
    for(auto& [id, entry] : entries_)
    {
        (void)id;
        if(entry.registered)
        {
            host_->unregister_controller(entry.instance->controller());
            entry.registered = false;
        }
    }
}

void UpstreamRazerControllerRegistry::resume_after_detection() noexcept
{
    if(stopped_ || !suspended_)
    {
        return;
    }
    suspended_ = false;
    UpstreamRegistryUpdate update;
    for(auto& [id, entry] : entries_)
    {
        (void)id;
        reconcile_registration(entry, update);
    }
}

void UpstreamRazerControllerRegistry::shutdown() noexcept
{
    if(stopped_)
    {
        return;
    }
    for(auto& [id, entry] : entries_)
    {
        (void)id;
        if(entry.registered)
        {
            host_->unregister_controller(entry.instance->controller());
        }
    }
    entries_.clear();
    direct_serials_.clear();
    suspended_ = false;
    stopped_ = true;
}

std::size_t UpstreamRazerControllerRegistry::size() const noexcept
{
    return entries_.size();
}

std::size_t UpstreamRazerControllerRegistry::registered_count() const noexcept
{
    return registered_count_internal();
}

std::size_t UpstreamRazerControllerRegistry::registered_count_internal() const noexcept
{
    return static_cast<std::size_t>(std::count_if(
        entries_.begin(),
        entries_.end(),
        [](const auto& item) { return item.second.registered; }));
}

std::size_t UpstreamRazerControllerRegistry::suppressed_count() const noexcept
{
    return static_cast<std::size_t>(std::count_if(
        entries_.begin(),
        entries_.end(),
        [](const auto& item) { return item.second.direct_suppressed; }));
}

bool UpstreamRazerControllerRegistry::suspended() const noexcept
{
    return suspended_;
}

std::vector<std::string> UpstreamRazerControllerRegistry::serials() const
{
    std::vector<std::string> result;
    result.reserve(entries_.size());
    for(const auto& [id, entry] : entries_)
    {
        (void)id;
        result.emplace_back(entry.instance->serial());
    }
    return result;
}

std::vector<UpstreamRazerControllerSummary>
UpstreamRazerControllerRegistry::summaries() const
{
    std::vector<UpstreamRazerControllerSummary> result;
    result.reserve(entries_.size());
    for(const auto& [id, entry] : entries_)
    {
        const auto& route = entry.instance->route();
        result.push_back({
            id,
            entry.instance->controller().name,
            route.device_kind,
            route.product_id,
            entry.registered,
            entry.direct_suppressed,
        });
    }
    return result;
}

std::uintptr_t UpstreamRazerControllerRegistry::controller_identity(
    std::string_view id) const noexcept
{
    const auto found = entries_.find(id);
    return found == entries_.end()
               ? 0
               : reinterpret_cast<std::uintptr_t>(&found->second.instance->controller());
}

bool is_eligible_upstream_razer_route(const v7::NativeRoute& route) noexcept
{
    return route.route_state == RouteState::Available
        && route.access == NativeRouteAccess::Qualified
        && route.protocol_family.value() == "razer-feature-report-v1"
        && route.report_bytes.value() == razer_native_report_bytes
        && route.vendor_id.value() == 0x1532 && route.route_token.has_value()
        && route.resource.has_value()
        && (route.device_kind == DeviceKind::Mouse
            || route.device_kind == DeviceKind::Keyboard);
}

std::string canonical_queried_serial(std::string_view serial)
{
    const auto first = serial.find_first_not_of(" \t\r\n");
    if(first == std::string_view::npos)
    {
        return {};
    }
    const auto last = serial.find_last_not_of(" \t\r\n");
    return std::string(serial.substr(first, last - first + 1));
}

std::vector<std::string> collect_direct_razer_serials(
    const std::vector<RGBController*>& controllers)
{
    std::set<std::string, std::less<>> unique;
    constexpr std::string_view hid_prefix = "HID: ";
    constexpr std::string_view native_prefix = "HID: hyperflux-next/native/";
    for(const auto* controller : controllers)
    {
        if(controller == nullptr || controller->vendor != "Razer"
           || !std::string_view(controller->location).starts_with(hid_prefix)
           || std::string_view(controller->location).starts_with(native_prefix)
           || controller->location.size() == hid_prefix.size())
        {
            continue;
        }
        auto serial = canonical_queried_serial(controller->serial);
        if(!serial.empty())
        {
            unique.insert(std::move(serial));
        }
    }
    return {unique.begin(), unique.end()};
}

} // namespace hyperflux::openrgb::native
