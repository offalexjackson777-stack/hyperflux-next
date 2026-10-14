// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/razer_feature_transport.hpp>

#include "RGBController_Razer.h"
#include "RazerDevices.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
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

std::optional<unsigned int> find_mode(RGBController_Razer& controller, std::string_view name)
{
    for(unsigned int index = 0; index < controller.GetModeCount(); ++index)
    {
        if(controller.GetModeName(index) == name)
        {
            return index;
        }
    }
    return std::nullopt;
}

bool has_led(RGBController_Razer& controller, std::string_view name)
{
    for(unsigned int index = 0; index < controller.GetLEDCount(); ++index)
    {
        if(controller.GetLEDName(index) == name)
        {
            return true;
        }
    }
    return false;
}

} // namespace

int main()
{
    ScriptedRazerExchange mouse_exchange;
    auto mouse = make_controller(
        mouse_exchange,
        RAZER_BASILISK_V3_PRO_35K_WIRELESS_PID,
        "Razer Basilisk V3 Pro 35K (Wireless)");

    if(mouse.GetName() != "Razer Basilisk V3 Pro 35K (Wireless)"
       || mouse.GetVendor() != "Razer" || mouse.GetDeviceType() != DEVICE_TYPE_MOUSE
       || mouse.GetLocation() != "HID: hyperflux-next/native/v1/route"
       || mouse.GetVersion() != "v3.7" || mouse.GetSerial() != "HFX-NATIVE-SERIAL-0001"
       || mouse.GetZoneCount() != 3 || mouse.GetLEDCount() != 13
       || mouse.GetZoneName(0) != "Scroll Wheel" || mouse.GetZoneLEDsCount(0) != 1
       || mouse.GetZoneName(1) != "Logo" || mouse.GetZoneLEDsCount(1) != 1
       || mouse.GetZoneName(2) != "LED Strip" || mouse.GetZoneLEDsCount(2) != 11)
    {
        return 1;
    }
    if(mouse.GetModeCount() != 5 || find_mode(mouse, "Direct") == std::nullopt
       || find_mode(mouse, "Off") == std::nullopt || find_mode(mouse, "Static") == std::nullopt
       || find_mode(mouse, "Spectrum Cycle") == std::nullopt
       || find_mode(mouse, "Wave") == std::nullopt
       || find_mode(mouse, "Breathing") != std::nullopt
       || find_mode(mouse, "Reactive") != std::nullopt)
    {
        return 2;
    }
    const auto mouse_direct = find_mode(mouse, "Direct");
    const auto mouse_wave = find_mode(mouse, "Wave");
    if(mouse_direct == std::nullopt || mouse_wave == std::nullopt
       || (mouse.GetModeFlags(*mouse_direct) & MODE_FLAG_HAS_PER_LED_COLOR) == 0
       || (mouse.GetModeFlags(*mouse_direct) & MODE_FLAG_HAS_BRIGHTNESS) == 0
       || mouse.GetModeBrightnessMin(*mouse_direct) != 0
       || mouse.GetModeBrightnessMax(*mouse_direct) != 255
       || (mouse.GetModeFlags(*mouse_wave) & MODE_FLAG_HAS_DIRECTION_LR) == 0)
    {
        return 3;
    }

    ScriptedRazerExchange keyboard_exchange;
    auto keyboard = make_controller(
        keyboard_exchange,
        RAZER_DEATHSTALKER_V2_PRO_TKL_WIRELESS_PID,
        "Razer DeathStalker V2 Pro TKL (Wireless)");

    if(keyboard.GetName() != "Razer DeathStalker V2 Pro TKL (Wireless)"
       || keyboard.GetDeviceType() != DEVICE_TYPE_KEYBOARD
       || keyboard.GetDescription() != "Razer Device, US (ANSI), Black"
       || keyboard.GetZoneCount() != 1 || keyboard.GetLEDCount() != 102
       || keyboard.GetZoneName(0) != ZONE_EN_KEYBOARD
       || keyboard.GetZoneType(0) != ZONE_TYPE_MATRIX
       || keyboard.GetZoneLEDsCount(0) != 102
       || keyboard.GetZoneMatrixMapHeight(0) != 6
       || keyboard.GetZoneMatrixMapWidth(0) != 17
       || !has_led(keyboard, "Key: 0"))
    {
        return 4;
    }
    if(keyboard.GetModeCount() != 6 || find_mode(keyboard, "Direct") == std::nullopt
       || find_mode(keyboard, "Off") == std::nullopt
       || find_mode(keyboard, "Static") == std::nullopt
       || find_mode(keyboard, "Breathing") == std::nullopt
       || find_mode(keyboard, "Spectrum Cycle") == std::nullopt
       || find_mode(keyboard, "Wave") == std::nullopt
       || find_mode(keyboard, "Reactive") != std::nullopt)
    {
        return 5;
    }

    return 0;
}
