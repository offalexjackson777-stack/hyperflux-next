// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_native_fixture.hpp"

#include <hyperflux/openrgb/native_controller.hpp>

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <mutex>
#include <utility>
#include <vector>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-native-controller-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

class Sink final : public hyperflux::openrgb::native::LightingCommandSink
{
public:
    hyperflux::sdk::Result<hyperflux::openrgb::EnqueueDisposition> enqueue_effect(
        hyperflux::openrgb::QueuedLightingFrame frame) override
    {
        {
            std::lock_guard lock(mutex);
            effects.push_back(std::move(frame));
        }
        changed.notify_all();
        return hyperflux::sdk::Result<hyperflux::openrgb::EnqueueDisposition>::success(
            hyperflux::openrgb::EnqueueDisposition::Accepted);
    }

    hyperflux::sdk::Result<hyperflux::openrgb::EnqueueDisposition> enqueue_stable(
        hyperflux::sdk::LightingIntent intent,
        std::vector<hyperflux::openrgb::QueuedLightingFrame> frames) override
    {
        {
            std::lock_guard lock(mutex);
            stable_intents.push_back(intent);
            stable_frames.push_back(std::move(frames));
        }
        changed.notify_all();
        return hyperflux::sdk::Result<hyperflux::openrgb::EnqueueDisposition>::success(
            hyperflux::openrgb::EnqueueDisposition::Accepted);
    }

    /// OpenRGB 1.0 applies mode changes on the controller's device thread.
    [[nodiscard]] bool wait_for_stable(std::size_t count)
    {
        std::unique_lock lock(mutex);
        return changed.wait_for(lock, std::chrono::seconds(5),
            [&]
            {
                return stable_intents.size() >= count;
            });
    }

    std::mutex mutex;
    std::condition_variable changed;
    std::vector<hyperflux::openrgb::QueuedLightingFrame> effects;
    std::vector<hyperflux::sdk::LightingIntent> stable_intents;
    std::vector<std::vector<hyperflux::openrgb::QueuedLightingFrame>> stable_frames;
};

void set_all_colors(RGBController& controller, RGBColor color)
{
    for(unsigned int led = 0; led < controller.GetLEDCount(); ++led)
    {
        controller.SetColor(led, color);
    }
}

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb;
    using namespace hyperflux::openrgb::native;
    using namespace hyperflux::test;

    const auto mouse = native_controller_model(DeviceKind::Mouse);
    auto presentation = resolve_razer_presentation(mouse, KeyboardLayoutVariant::AnsiQwerty);
    if(!presentation)
    {
        return failure(__LINE__);
    }

    Sink sink;
    auto created =
        NativeController::create(mouse, std::move(presentation).value(), sink, "0.0.0-dev.1");
    if(!created)
    {
        return failure(__LINE__);
    }
    auto controller = std::move(created).value();
    if(controller->GetName() != "Razer Basilisk V3 Pro 35K (Wireless)"
        || controller->GetDeviceType() != DEVICE_TYPE_MOUSE || controller->GetModeCount() != 3
        || controller->GetModeName(0) != "Direct" || controller->GetModeName(1) != "Off"
        || controller->GetModeName(2) != "Static" || controller->GetZoneCount() != 3
        || controller->GetLEDCount() != 13
        || controller->GetActiveMode() != static_cast<int>(ControllerMode::Direct)
        || controller->GetZoneName(0) != "Scroll Wheel" || controller->GetZoneName(1) != "Logo"
        || controller->GetZoneName(2) != "LED Strip")
    {
        return failure(__LINE__);
    }

    set_all_colors(*controller, ToRGBColor(200, 100, 50));
    controller->SetModeBrightness(0, 50);
    controller->DeviceUpdateLEDs();
    if(sink.effects.size() != 1 || sink.effects.front().colors.size() != 13
        || sink.effects.front().generation_id.value() != 1
        || sink.effects.front().colors.front().red.value() != 100
        || sink.effects.front().colors.front().green.value() != 50
        || sink.effects.front().colors.front().blue.value() != 25)
    {
        return failure(__LINE__);
    }

    auto next_generation = mouse;
    next_generation.authority.generation_id = number<GenerationId>(2);
    controller->update_generation(next_generation.authority.generation_id);
    controller->DeviceUpdateLEDs();
    if(sink.effects.size() != 2 || sink.effects.back().generation_id.value() != 2)
    {
        return failure(__LINE__);
    }

    controller->SetModeColor(2, 0, ToRGBColor(120, 60, 30));
    controller->SetModeBrightness(2, 25);
    controller->SetActiveMode(static_cast<int>(ControllerMode::Off));
    if(!sink.wait_for_stable(1))
    {
        return failure(__LINE__);
    }
    controller->SetActiveMode(static_cast<int>(ControllerMode::Static));
    if(!sink.wait_for_stable(2))
    {
        return failure(__LINE__);
    }
    std::unique_lock sink_lock(sink.mutex);
    if(sink.stable_intents
            != std::vector<sdk::LightingIntent> {sdk::LightingIntent::Off,
                sdk::LightingIntent::Static}
        || sink.stable_frames.size() != 2
        || sink.stable_frames[0].front().colors.front().red.value() != 0
        || sink.stable_frames[1].front().colors.front().red.value() != 30
        || sink.stable_frames[1].front().colors.front().green.value() != 15
        || sink.stable_frames[1].front().colors.front().blue.value() != 8)
    {
        return failure(__LINE__);
    }
    sink_lock.unlock();

    const auto keyboard = native_controller_model(DeviceKind::Keyboard);
    auto keyboard_presentation =
        resolve_razer_presentation(keyboard, KeyboardLayoutVariant::AnsiQwerty);
    if(!keyboard_presentation)
    {
        return failure(__LINE__);
    }

    Sink keyboard_sink;
    auto keyboard_created = NativeController::create(
        keyboard, std::move(keyboard_presentation).value(), keyboard_sink, "0.0.0-dev.1");
    if(!keyboard_created)
    {
        return failure(__LINE__);
    }
    auto keyboard_controller = std::move(keyboard_created).value();
    if(keyboard_controller->GetName() != "Razer DeathStalker V2 Pro TKL (Wireless)"
        || keyboard_controller->GetDeviceType() != DEVICE_TYPE_KEYBOARD
        || keyboard_controller->GetZoneCount() != 1
        || keyboard_controller->GetZoneType(0) != ZONE_TYPE_MATRIX
        || keyboard_controller->GetZoneLEDsCount(0) != 102
        || keyboard_controller->GetZoneMatrixMapHeight(0) != 6
        || keyboard_controller->GetZoneMatrixMapWidth(0) != 17
        || keyboard_controller->GetZoneMatrixMap(0).map.size() != 102
        || keyboard_controller->GetLEDCount() != 102)
    {
        return failure(__LINE__);
    }

    const auto matrix = keyboard_controller->GetZoneMatrixMap(0).map;
    const auto missing_slots =
        std::count(matrix.begin(), matrix.end(), std::numeric_limits<unsigned int>::max());
    std::size_t zero_slot = keyboard_controller->GetLEDCount();
    for(unsigned int led = 0; led < keyboard_controller->GetLEDCount(); ++led)
    {
        if(keyboard_controller->GetLEDName(led) == "Key: 0")
        {
            zero_slot = led;
            break;
        }
    }
    if(missing_slots != 18 || zero_slot == keyboard_controller->GetLEDCount())
    {
        return failure(__LINE__);
    }

    set_all_colors(*keyboard_controller, ToRGBColor(0, 255, 0));
    keyboard_controller->SetColor(static_cast<unsigned int>(zero_slot), ToRGBColor(255, 0, 0));
    keyboard_controller->UpdateSingleLED(static_cast<int>(zero_slot));
    if(keyboard_sink.effects.size() != 1 || keyboard_sink.effects.front().colors.size() != 102
        || keyboard_sink.effects.front().colors[zero_slot].red.value() != 255
        || keyboard_sink.effects.front().colors[zero_slot].green.value() != 0
        || std::count_if(keyboard_sink.effects.front().colors.begin(),
               keyboard_sink.effects.front().colors.end(),
               [](const v5::RgbColor& entry)
               {
                   return entry.red.value() == 0 && entry.green.value() == 255
                          && entry.blue.value() == 0;
               })
               != 101)
    {
        return failure(__LINE__);
    }

    keyboard_controller->SetActiveMode(static_cast<int>(ControllerMode::Off));
    if(!keyboard_sink.wait_for_stable(1))
    {
        return failure(__LINE__);
    }
    std::lock_guard keyboard_lock(keyboard_sink.mutex);
    if(keyboard_sink.stable_intents != std::vector<sdk::LightingIntent> {sdk::LightingIntent::Off}
        || keyboard_sink.stable_frames.front().front().colors.size() != 102)
    {
        return failure(__LINE__);
    }
    return EXIT_SUCCESS;
}
