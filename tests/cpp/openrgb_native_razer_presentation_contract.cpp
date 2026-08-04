// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/razer_feature_transport.hpp>

#include "RGBController_Razer.h"
#include "RazerDevices.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace
{

class ScriptedRazerExchange final
    : public hyperflux::openrgb::native::NativeFeatureExchange
{
public:
    hyperflux::openrgb::native::NativeFeatureExchangeResult exchange(
        const std::array<std::uint8_t,
            hyperflux::openrgb::native::razer_native_report_bytes>& request) override
    {
        requests.push_back(request);
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
            constexpr char serial[] = "HFX-NATIVE-SERIAL-0001";
            std::copy_n(
                reinterpret_cast<const std::uint8_t*>(serial),
                sizeof(serial) - 1,
                response.begin() + 8);
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

    std::vector<std::array<std::uint8_t,
        hyperflux::openrgb::native::razer_native_report_bytes>> requests;
};

RGBController_Razer make_controller(
    ScriptedRazerExchange& exchange,
    unsigned short product_id,
    const char* name)
{
    auto transport =
        std::make_unique<hyperflux::openrgb::native::HyperFluxRazerFeatureTransport>(
            exchange);
    return RGBController_Razer(new RazerController(
        nullptr,
        nullptr,
        "hyperflux-next/native/v1/route",
        product_id,
        name,
        std::move(transport)));
}

const mode* find_mode(const RGBController_Razer& controller, std::string_view name)
{
    const auto found = std::find_if(
        controller.modes.begin(),
        controller.modes.end(),
        [name](const mode& candidate) { return candidate.name == name; });
    return found == controller.modes.end() ? nullptr : &*found;
}

} // namespace

int main()
{
    ScriptedRazerExchange mouse_exchange;
    auto mouse = make_controller(
        mouse_exchange,
        RAZER_BASILISK_V3_PRO_35K_WIRELESS_PID,
        "Razer Basilisk V3 Pro 35K (Wireless)");

    if(mouse.name != "Razer Basilisk V3 Pro 35K (Wireless)"
       || mouse.vendor != "Razer" || mouse.type != DEVICE_TYPE_MOUSE
       || mouse.location != "HID: hyperflux-next/native/v1/route"
       || mouse.version != "v3.7" || mouse.serial != "HFX-NATIVE-SERIAL-0001"
       || mouse.zones.size() != 3 || mouse.leds.size() != 13
       || mouse.zones[0].name != "Scroll Wheel" || mouse.zones[0].leds_count != 1
       || mouse.zones[1].name != "Logo" || mouse.zones[1].leds_count != 1
       || mouse.zones[2].name != "LED Strip" || mouse.zones[2].leds_count != 11)
    {
        return 1;
    }
    if(mouse.modes.size() != 5 || find_mode(mouse, "Direct") == nullptr
       || find_mode(mouse, "Off") == nullptr || find_mode(mouse, "Static") == nullptr
       || find_mode(mouse, "Spectrum Cycle") == nullptr
       || find_mode(mouse, "Wave") == nullptr
       || find_mode(mouse, "Breathing") != nullptr
       || find_mode(mouse, "Reactive") != nullptr)
    {
        return 2;
    }
    const auto* mouse_direct = find_mode(mouse, "Direct");
    const auto* mouse_wave = find_mode(mouse, "Wave");
    if(mouse_direct == nullptr || mouse_wave == nullptr
       || (mouse_direct->flags & MODE_FLAG_HAS_PER_LED_COLOR) == 0
       || (mouse_direct->flags & MODE_FLAG_HAS_BRIGHTNESS) == 0
       || mouse_direct->brightness_min != 0 || mouse_direct->brightness_max != 255
       || (mouse_wave->flags & MODE_FLAG_HAS_DIRECTION_LR) == 0)
    {
        return 3;
    }

    ScriptedRazerExchange keyboard_exchange;
    auto keyboard = make_controller(
        keyboard_exchange,
        RAZER_DEATHSTALKER_V2_PRO_TKL_WIRELESS_PID,
        "Razer DeathStalker V2 Pro TKL (Wireless)");

    if(keyboard.name != "Razer DeathStalker V2 Pro TKL (Wireless)"
       || keyboard.type != DEVICE_TYPE_KEYBOARD
       || keyboard.description != "Razer Device, US (ANSI), Black"
       || keyboard.zones.size() != 1 || keyboard.leds.size() != 102
       || keyboard.zones.front().name != ZONE_EN_KEYBOARD
       || keyboard.zones.front().type != ZONE_TYPE_MATRIX
       || keyboard.zones.front().leds_count != 102
       || keyboard.zones.front().matrix_map == nullptr
       || keyboard.zones.front().matrix_map->height != 6
       || keyboard.zones.front().matrix_map->width != 17
       || std::none_of(
              keyboard.leds.begin(),
              keyboard.leds.end(),
              [](const led& candidate) { return candidate.name == "Key: 0"; }))
    {
        return 4;
    }
    if(keyboard.modes.size() != 6 || find_mode(keyboard, "Direct") == nullptr
       || find_mode(keyboard, "Off") == nullptr
       || find_mode(keyboard, "Static") == nullptr
       || find_mode(keyboard, "Breathing") == nullptr
       || find_mode(keyboard, "Spectrum Cycle") == nullptr
       || find_mode(keyboard, "Wave") == nullptr
       || find_mode(keyboard, "Reactive") != nullptr)
    {
        return 5;
    }

    return 0;
}
