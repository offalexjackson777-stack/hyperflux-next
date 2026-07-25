// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/upstream_razer_registry.hpp>

#include "RGBController.h"
#include "RazerDevices.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
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

hyperflux::v7::NativeRoute route(
    hyperflux::DeviceKind kind,
    std::string_view device_id,
    std::string_view endpoint_id,
    std::uint16_t product_id,
    std::uint64_t generation = 4,
    char token_fill = 'a')
{
    using namespace hyperflux;
    const v7::ResourceKey resource {
        text<ReceiverId>("receiver-1"),
        number<GenerationId>(generation),
        text<LogicalDeviceId>(device_id),
        ResourceKind::NativeControl,
    };
    return {
        resource.receiver_id,
        resource.generation_id,
        resource.device_id,
        text<EndpointId>(endpoint_id),
        kind,
        number<VendorId>(0x1532),
        number<ProductId>(product_id),
        NativeRouteState::Available,
        NativeRouteAccess::Qualified,
        text<NativeProtocolFamily>("razer-feature-report-v1"),
        number<NativeReportByteCount>(90),
        text<NativeRouteToken>(std::string(64, token_fill)),
        resource,
    };
}

class ScriptedExchange final
    : public hyperflux::openrgb::native::NativeFeatureExchange
{
public:
    explicit ScriptedExchange(std::string serial)
        : serial_(std::move(serial))
    {
    }

    hyperflux::openrgb::native::NativeFeatureExchangeResult exchange(
        const std::array<std::uint8_t,
            hyperflux::openrgb::native::razer_native_report_bytes>& request) override
    {
        auto response = request;
        response[0] = 0x02;
        response[5] = 0x00;
        response[8] = 0x00;
        if(request[6] == 0x00 && request[7] == RAZER_COMMAND_ID_GET_FIRMWARE_VERSION)
        {
            response[8] = 3;
            response[9] = 7;
        }
        else if(request[6] == 0x00 && request[7] == RAZER_COMMAND_ID_GET_SERIAL_STRING)
        {
            std::copy(serial_.begin(), serial_.end(), response.begin() + 8);
        }
        else if(request[6] == 0x00 && request[7] == RAZER_COMMAND_ID_GET_KEYBOARD_INFO)
        {
            response[8] = RAZER_KEYBOARD_LAYOUT_US;
            response[9] = RAZER_KEYBOARD_VARIANT_BLACK;
        }
        return {
            hyperflux::openrgb::native::NativeExchangeCertainty::MatchedResponse,
            response,
        };
    }

private:
    std::string serial_;
};

class RecordingHost final : public hyperflux::openrgb::native::UpstreamControllerHost
{
public:
    void register_controller(RGBController& controller) noexcept override
    {
        if(std::find(registered.begin(), registered.end(), &controller) == registered.end())
        {
            registered.push_back(&controller);
        }
    }

    void unregister_controller(RGBController& controller) noexcept override
    {
        registered.erase(
            std::remove(registered.begin(), registered.end(), &controller),
            registered.end());
    }

    std::vector<RGBController*> registered;
};

class TestController final : public RGBController
{
public:
    void SetupZones() override {}
    void ResizeZone(int, int) override {}
    void DeviceUpdateLEDs() override {}
    void UpdateZoneLEDs(int) override {}
    void UpdateSingleLED(int) override {}
    void DeviceUpdateMode() override {}
};

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb::native;

    auto exchange_factory = [](const NativeRouteIdentity& identity)
        -> sdk::Result<std::unique_ptr<NativeFeatureExchange>>
    {
        const auto serial = identity.device_kind == DeviceKind::Mouse
                                ? "MOUSE-SERIAL-0001"
                                : "KEYBOARD-SERIAL-0001";
        std::unique_ptr<NativeFeatureExchange> exchange =
            std::make_unique<ScriptedExchange>(serial);
        return sdk::Result<std::unique_ptr<NativeFeatureExchange>>::success(
            std::move(exchange));
    };
    UpstreamRazerControllerFactory factory(exchange_factory);
    RecordingHost host;
    UpstreamRazerControllerRegistry registry(host, factory);

    const auto mouse = route(
        DeviceKind::Mouse,
        "mouse-00cd",
        "endpoint-mouse",
        RAZER_BASILISK_V3_PRO_35K_WIRELESS_PID);
    const auto keyboard = route(
        DeviceKind::Keyboard,
        "keyboard-0296",
        "endpoint-keyboard",
        RAZER_DEATHSTALKER_V2_PRO_TKL_WIRELESS_PID,
        4,
        'b');
    auto initial = registry.apply_routes({keyboard, mouse});
    if(!initial || initial.value().added != 2 || initial.value().registered != 2
       || registry.size() != 2 || host.registered.size() != 2)
    {
        return 1;
    }

    const auto mouse_identity = registry.controller_identity(
        "receiver-1/mouse-00cd/endpoint-mouse");
    if(mouse_identity == 0
       || registry.serials()
              != std::vector<std::string> {
                  "KEYBOARD-SERIAL-0001  ",
                  "MOUSE-SERIAL-0001     ",
              })
    {
        return 2;
    }

    const auto unrelated_direct = registry.apply_direct_serials({
        "",
        "                      ",
        "DIFFERENT-SERIAL-SAME-PID-NAME-KIND",
    });
    if(unrelated_direct.newly_suppressed != 0 || registry.registered_count() != 2)
    {
        return 3;
    }

    TestController direct_mouse_controller;
    direct_mouse_controller.vendor = "Razer";
    direct_mouse_controller.location = "HID: /dev/hidraw-direct-mouse";
    direct_mouse_controller.serial = "MOUSE-SERIAL-0001     ";
    TestController network_controller;
    network_controller.vendor = "Razer";
    network_controller.location = "Network: 127.0.0.1";
    network_controller.serial = "MOUSE-SERIAL-0001";
    TestController unrelated_controller;
    unrelated_controller.vendor = "Other";
    unrelated_controller.location = "HID: /dev/hidraw-other";
    unrelated_controller.serial = "MOUSE-SERIAL-0001";
    TestController blank_serial_controller;
    blank_serial_controller.vendor = "Razer";
    blank_serial_controller.location = "HID: /dev/hidraw-blank";
    blank_serial_controller.serial = "                      ";
    const auto direct_serials = collect_direct_razer_serials({
        &direct_mouse_controller,
        &network_controller,
        &unrelated_controller,
        &blank_serial_controller,
        nullptr,
    });
    if(direct_serials != std::vector<std::string> {"MOUSE-SERIAL-0001"})
    {
        return 12;
    }

    const auto direct_mouse = registry.apply_direct_serials(direct_serials);
    if(direct_mouse.newly_suppressed != 1 || registry.suppressed_count() != 1
       || registry.registered_count() != 1 || host.registered.size() != 1
       || canonical_queried_serial(host.registered.front()->serial)
              != "KEYBOARD-SERIAL-0001")
    {
        return 4;
    }

    const auto repeat_direct = registry.apply_direct_serials({"MOUSE-SERIAL-0001"});
    if(repeat_direct.newly_suppressed != 0 || repeat_direct.newly_restored != 0
       || registry.registered_count() != 1)
    {
        return 5;
    }

    const auto direct_removed = registry.apply_direct_serials({});
    if(direct_removed.newly_restored != 1 || registry.registered_count() != 2
       || registry.controller_identity("receiver-1/mouse-00cd/endpoint-mouse")
              != mouse_identity)
    {
        return 6;
    }

    registry.suspend_for_detection();
    if(registry.registered_count() != 0 || !host.registered.empty())
    {
        return 7;
    }
    registry.resume_after_detection();
    if(registry.registered_count() != 2 || host.registered.size() != 2)
    {
        return 8;
    }

    auto next_mouse = mouse;
    next_mouse.generation_id = number<GenerationId>(5);
    next_mouse.route_token = text<NativeRouteToken>(std::string(64, 'c'));
    next_mouse.resource->generation_id = next_mouse.generation_id;
    auto replacement = registry.apply_routes({keyboard, next_mouse});
    if(!replacement || replacement.value().replaced != 1
       || replacement.value().retained != 1
       || registry.controller_identity("receiver-1/mouse-00cd/endpoint-mouse")
              == mouse_identity)
    {
        return 9;
    }

    auto candidate = keyboard;
    candidate.access = NativeRouteAccess::Candidate;
    auto unavailable = mouse;
    unavailable.route_state = NativeRouteState::TemporarilyUnavailable;
    auto filtered = registry.apply_routes({candidate, unavailable});
    if(!filtered || filtered.value().removed != 2 || registry.size() != 0
       || !host.registered.empty())
    {
        return 10;
    }

    auto duplicate = registry.apply_routes({mouse, mouse});
    if(duplicate || registry.size() != 0)
    {
        return 11;
    }

    return 0;
}
