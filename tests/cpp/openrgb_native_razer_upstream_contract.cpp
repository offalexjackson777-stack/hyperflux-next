// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/razer_feature_transport.hpp>

#include "RazerDevices.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <memory>
#include <string>
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

std::unique_ptr<RazerController> controller(
    ScriptedRazerExchange& exchange,
    unsigned short product_id,
    const char* name)
{
    auto transport =
        std::make_unique<hyperflux::openrgb::native::HyperFluxRazerFeatureTransport>(
            exchange);
    return std::make_unique<RazerController>(
        nullptr,
        nullptr,
        "hyperflux-next/native/v1/route",
        product_id,
        name,
        std::move(transport));
}

bool command_seen(const ScriptedRazerExchange& exchange, std::uint8_t command)
{
    return std::any_of(
        exchange.requests.begin(),
        exchange.requests.end(),
        [command](const auto& request) { return request[7] == command; });
}

} // namespace

int main()
{
    ScriptedRazerExchange mouse_exchange;
    auto mouse = controller(
        mouse_exchange,
        RAZER_BASILISK_V3_PRO_35K_WIRELESS_PID,
        "Razer Basilisk V3 Pro 35K (Wireless)");
    if(mouse->GetDeviceType() != DEVICE_TYPE_MOUSE
       || mouse->GetName() != "Razer Basilisk V3 Pro 35K (Wireless)"
       || mouse->GetDeviceLocation()
           != "HID: hyperflux-next/native/v1/route"
       || mouse->GetFirmwareString() != "v3.7"
       || mouse->GetSerialString() != "HFX-NATIVE-SERIAL-0001"
       || mouse->GetMaxBrightness() != 255
       || mouse->SupportsBreathing() || !mouse->SupportsWave()
       || mouse->SupportsReactive())
    {
        return 1;
    }
    mouse->SetBrightness(127);
    mouse->SetModeStatic(0x11, 0x22, 0x33);
    mouse->SetModeSpectrumCycle();
    mouse->SetModeWave(1);
    mouse->SetModeOff();
    if(mouse_exchange.requests.size() < 7
       || !command_seen(mouse_exchange, RAZER_COMMAND_ID_GET_FIRMWARE_VERSION)
       || !command_seen(mouse_exchange, RAZER_COMMAND_ID_GET_SERIAL_STRING))
    {
        return 2;
    }

    ScriptedRazerExchange keyboard_exchange;
    auto keyboard = controller(
        keyboard_exchange,
        RAZER_DEATHSTALKER_V2_PRO_TKL_WIRELESS_PID,
        "Razer DeathStalker V2 Pro TKL (Wireless)");
    if(keyboard->GetDeviceType() != DEVICE_TYPE_KEYBOARD
       || keyboard->GetKeyboardLayoutType() != RAZER_LAYOUT_TYPE_ANSI
       || keyboard->GetKeyboardLayoutString() != "US (ANSI)"
       || keyboard->GetVariantName() != "Black"
       || !keyboard->SupportsBreathing() || !keyboard->SupportsWave()
       || keyboard->SupportsReactive())
    {
        return 3;
    }
    if(!command_seen(keyboard_exchange, RAZER_COMMAND_ID_GET_KEYBOARD_INFO))
    {
        return 4;
    }
    return 0;
}
