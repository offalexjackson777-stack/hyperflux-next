// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_native_fixture.hpp"

#include <hyperflux/openrgb/native_controller.hpp>

#include <algorithm>
#include <cstdlib>
#include <iostream>
#include <iterator>
#include <limits>
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
        effects.push_back(std::move(frame));
        return hyperflux::sdk::Result<hyperflux::openrgb::EnqueueDisposition>::success(
            hyperflux::openrgb::EnqueueDisposition::Accepted);
    }

    hyperflux::sdk::Result<hyperflux::openrgb::EnqueueDisposition> enqueue_stable(
        hyperflux::sdk::LightingIntent intent,
        std::vector<hyperflux::openrgb::QueuedLightingFrame> frames) override
    {
        stable_intents.push_back(intent);
        stable_frames.push_back(std::move(frames));
        return hyperflux::sdk::Result<hyperflux::openrgb::EnqueueDisposition>::success(
            hyperflux::openrgb::EnqueueDisposition::Accepted);
    }

    std::vector<hyperflux::openrgb::QueuedLightingFrame> effects;
    std::vector<hyperflux::sdk::LightingIntent> stable_intents;
    std::vector<std::vector<hyperflux::openrgb::QueuedLightingFrame>> stable_frames;
};

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
    if(controller->name != "Razer Basilisk V3 Pro 35K (Wireless)"
        || controller->type != DEVICE_TYPE_MOUSE || controller->modes.size() != 3
        || controller->modes[0].name != "Direct" || controller->modes[1].name != "Off"
        || controller->modes[2].name != "Static" || controller->zones.size() != 3
        || controller->leds.size() != 13 || controller->colors.size() != 13
        || controller->zones[0].name != "Scroll Wheel" || controller->zones[1].name != "Logo"
        || controller->zones[2].name != "LED Strip")
    {
        return failure(__LINE__);
    }

    controller->colors.assign(13, ToRGBColor(200, 100, 50));
    controller->modes[0].brightness = 50;
    controller->active_mode = static_cast<int>(ControllerMode::Direct);
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

    controller->active_mode = static_cast<int>(ControllerMode::Off);
    controller->DeviceUpdateMode();
    controller->active_mode = static_cast<int>(ControllerMode::Static);
    controller->modes[2].colors.front() = ToRGBColor(120, 60, 30);
    controller->modes[2].brightness = 25;
    controller->DeviceUpdateMode();
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
    if(keyboard_controller->name != "Razer DeathStalker V2 Pro TKL (Wireless)"
        || keyboard_controller->type != DEVICE_TYPE_KEYBOARD
        || keyboard_controller->zones.size() != 1
        || keyboard_controller->zones.front().type != ZONE_TYPE_MATRIX
        || keyboard_controller->zones.front().leds_count != 102
        || keyboard_controller->zones.front().matrix_map == nullptr
        || keyboard_controller->zones.front().matrix_map->height != 6
        || keyboard_controller->zones.front().matrix_map->width != 17
        || keyboard_controller->leds.size() != 102 || keyboard_controller->colors.size() != 102)
    {
        return failure(__LINE__);
    }

    const auto* matrix = keyboard_controller->zones.front().matrix_map->map;
    const auto missing_slots =
        std::count(matrix, matrix + 102, std::numeric_limits<unsigned int>::max());
    const auto zero = std::find_if(keyboard_controller->leds.begin(),
        keyboard_controller->leds.end(),
        [](const led& entry)
        {
            return entry.name == "Key: 0";
        });
    if(missing_slots != 18 || zero == keyboard_controller->leds.end())
    {
        return failure(__LINE__);
    }

    keyboard_controller->colors.assign(102, ToRGBColor(0, 255, 0));
    const auto zero_slot =
        static_cast<std::size_t>(std::distance(keyboard_controller->leds.begin(), zero));
    keyboard_controller->colors[zero_slot] = ToRGBColor(255, 0, 0);
    keyboard_controller->active_mode = static_cast<int>(ControllerMode::Direct);
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

    keyboard_controller->active_mode = static_cast<int>(ControllerMode::Off);
    keyboard_controller->DeviceUpdateLEDs();
    if(keyboard_sink.stable_intents != std::vector<sdk::LightingIntent> {sdk::LightingIntent::Off}
        || keyboard_sink.stable_frames.front().front().colors.size() != 102)
    {
        return failure(__LINE__);
    }
    return EXIT_SUCCESS;
}
