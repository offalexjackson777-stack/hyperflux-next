// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/razer_registry.hpp>

#include "Controllers/RazerController/RazerDevices.h"
#include "LogManager.h"

#include <algorithm>
#include <cstdarg>
#include <cstdint>
#include <limits>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace hyperflux::openrgb
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

device_type native_device_kind(DeviceKind kind)
{
    return kind == DeviceKind::Keyboard ? DEVICE_TYPE_KEYBOARD : DEVICE_TYPE_MOUSE;
}

KEYBOARD_LAYOUT native_keyboard_layout(KeyboardLayoutVariant layout)
{
    switch(layout)
    {
        case KeyboardLayoutVariant::AnsiQwerty: return KEYBOARD_LAYOUT_ANSI_QWERTY;
        case KeyboardLayoutVariant::IsoQwerty: return KEYBOARD_LAYOUT_ISO_QWERTY;
        case KeyboardLayoutVariant::IsoQwertz: return KEYBOARD_LAYOUT_ISO_QWERTZ;
        case KeyboardLayoutVariant::IsoAzerty: return KEYBOARD_LAYOUT_ISO_AZERTY;
        case KeyboardLayoutVariant::Jis: return KEYBOARD_LAYOUT_JIS;
        case KeyboardLayoutVariant::Abnt2: return KEYBOARD_LAYOUT_ABNT2;
    }
    return KEYBOARD_LAYOUT_ANSI_QWERTY;
}

std::optional<LayoutZoneKind> stable_zone_kind(unsigned int kind)
{
    switch(kind)
    {
        case ZONE_TYPE_SINGLE: return LayoutZoneKind::Single;
        case ZONE_TYPE_LINEAR: return LayoutZoneKind::Linear;
        case ZONE_TYPE_MATRIX: return LayoutZoneKind::Matrix;
        default: return std::nullopt;
    }
}

const razer_device* find_device(const ControllerModel& controller)
{
    const auto expected_type = native_device_kind(controller.device_kind);
    const razer_device* match = nullptr;
    for(unsigned int index = 0; index < RAZER_NUM_DEVICES; ++index)
    {
        const auto* candidate = device_list[index];
        if(candidate == nullptr || candidate->pid != controller.product_id.value()
           || candidate->type != expected_type)
        {
            continue;
        }
        if(match != nullptr)
        {
            return nullptr;
        }
        match = candidate;
    }
    return match;
}

std::string indexed_led_name(const std::string& zone, std::uint32_t index, std::uint32_t count)
{
    return count == 1 ? zone : zone + " LED " + std::to_string(index + 1);
}

sdk::Result<RazerPresentation> build_presentation(
    const ControllerModel& controller,
    const razer_device& source,
    KeyboardLayoutVariant keyboard_layout)
{
    RazerPresentation result {
        "OpenRGB RazerDevices registry at " + std::string(controller.presentation.source_revision.value()),
        source.name,
        controller.device_kind,
        controller.product_id,
        {},
        {},
    };
    std::uint32_t first_slot = 0;
    std::uint32_t physical_leds = 0;
    for(unsigned int zone_index = 0; zone_index < RAZER_MAX_ZONES; ++zone_index)
    {
        const auto* source_zone = source.zones[zone_index];
        if(source_zone == nullptr)
        {
            continue;
        }
        if(source_zone->rows == 0 || source_zone->cols == 0
           || source_zone->rows > std::numeric_limits<std::uint16_t>::max()
           || source_zone->cols > std::numeric_limits<std::uint16_t>::max())
        {
            return sdk::Result<RazerPresentation>::failure(
                registry_error("OpenRGB Razer registry contains invalid zone dimensions"));
        }
        const auto zone_kind = stable_zone_kind(source_zone->type);
        if(!zone_kind.has_value())
        {
            return sdk::Result<RazerPresentation>::failure(
                registry_error("OpenRGB Razer registry contains an unsupported zone type"));
        }
        const auto slot_count = static_cast<std::uint32_t>(source_zone->rows)
            * static_cast<std::uint32_t>(source_zone->cols);
        result.zones.push_back({
            source_zone->name,
            *zone_kind,
            static_cast<std::uint16_t>(source_zone->rows),
            static_cast<std::uint16_t>(source_zone->cols),
            first_slot,
            slot_count,
            {},
        });
        auto& zone = result.zones.back();
        const bool managed_keyboard = controller.device_kind == DeviceKind::Keyboard
            && *zone_kind == LayoutZoneKind::Matrix && source.layout != nullptr;
        if(managed_keyboard)
        {
            KeyboardLayoutManager manager(
                native_keyboard_layout(keyboard_layout),
                source.layout->base_size,
                source.layout->key_values);
            manager.ChangeKeys(*source.layout);
            std::vector<unsigned int> native_map(slot_count);
            manager.GetKeyMap(
                native_map.data(),
                KEYBOARD_MAP_FILL_TYPE_INDEX,
                static_cast<std::uint8_t>(source_zone->rows),
                static_cast<std::uint8_t>(source_zone->cols));
            zone.matrix_map.reserve(slot_count);
            for(std::uint32_t row = 0; row < source_zone->rows; ++row)
            {
                for(std::uint32_t column = 0; column < source_zone->cols; ++column)
                {
                    const auto offset = row * source_zone->cols + column;
                    const bool present = native_map[offset]
                        != std::numeric_limits<unsigned int>::max();
                    zone.matrix_map.push_back(
                        present ? first_slot + offset : std::numeric_limits<unsigned int>::max());
                    result.leds.push_back({
                        manager.GetKeyNameAt(row, column),
                        first_slot + offset,
                        present,
                    });
                    physical_leds += present ? 1U : 0U;
                }
            }
        }
        else
        {
            if(*zone_kind == LayoutZoneKind::Matrix)
            {
                zone.matrix_map.reserve(slot_count);
                for(std::uint32_t offset = 0; offset < slot_count; ++offset)
                {
                    zone.matrix_map.push_back(first_slot + offset);
                }
            }
            for(std::uint32_t offset = 0; offset < slot_count; ++offset)
            {
                result.leds.push_back({
                    indexed_led_name(source_zone->name, offset, slot_count),
                    first_slot + offset,
                    true,
                });
            }
            physical_leds += slot_count;
        }
        first_slot += slot_count;
    }

    if(result.zones.empty() || result.leds.empty()
       || first_slot != controller.lighting.application_slot_count.value()
       || physical_leds != controller.lighting.physical_led_count.value()
       || source.rows != controller.lighting.rows.value()
       || source.cols != controller.lighting.columns.value())
    {
        return sdk::Result<RazerPresentation>::failure(registry_error(
            "OpenRGB Razer presentation dimensions drift from the qualified HyperFlux profile"));
    }
    return sdk::Result<RazerPresentation>::success(std::move(result));
}

} // namespace

sdk::Result<RazerPresentation> resolve_razer_presentation(
    const ControllerModel& controller,
    KeyboardLayoutVariant keyboard_layout)
{
    if(controller.presentation.upstream_id.value() != "openrgb")
    {
        return sdk::Result<RazerPresentation>::failure(
            registry_error("controller does not delegate presentation to OpenRGB"));
    }
    const auto* source = find_device(controller);
    if(source == nullptr)
    {
        return sdk::Result<RazerPresentation>::failure(registry_error(
            "pinned OpenRGB Razer registry has no unique PID and device-kind match"));
    }
    return build_presentation(controller, *source, keyboard_layout);
}

} // namespace hyperflux::openrgb

// RazerDevices.cpp uses OpenRGB logging for registry initialization only. The
// adapter links the registry without the direct HID controller, so logging is
// intentionally local and side-effect free.
const char* LogManager::log_codes[] = {
    "FATAL:", "ERROR:", "Warning:", "Info:",
    "Verbose:", "Debug:", "Trace:", "Dialog:",
};

LogManager::LogManager() : log_console_enabled(false), log_file_enabled(false) {}
LogManager::~LogManager() = default;

LogManager* LogManager::get()
{
    static LogManager instance;
    return &instance;
}

void LogManager::append(const char*, int, unsigned int, const char*, ...) {}
