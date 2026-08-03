// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <hyperflux/openrgb/controller_model.hpp>

#include <cstdint>
#include <string>
#include <vector>

namespace hyperflux::openrgb
{

enum class KeyboardLayoutVariant
{
    AnsiQwerty,
    IsoQwerty,
    IsoQwertz,
    IsoAzerty,
    Jis,
    Abnt2,
};

enum class LayoutZoneKind
{
    Single,
    Linear,
    Matrix,
};

struct LayoutZone
{
    std::string name;
    LayoutZoneKind kind;
    std::uint16_t rows;
    std::uint16_t columns;
    std::uint32_t first_slot;
    std::uint32_t slot_count;
    std::vector<unsigned int> matrix_map;

    friend bool operator==(const LayoutZone&, const LayoutZone&) = default;
};

struct LayoutLed
{
    std::string name;
    std::uint32_t application_slot;
    bool physically_present;

    friend bool operator==(const LayoutLed&, const LayoutLed&) = default;
};

struct RazerPresentation
{
    std::string provider;
    std::string model_name;
    DeviceKind device_kind;
    ProductId product_id;
    std::vector<LayoutZone> zones;
    std::vector<LayoutLed> leds;

    friend bool operator==(const RazerPresentation&, const RazerPresentation&) = default;
};

/// Resolves presentation from the exact pinned OpenRGB Razer registry linked
/// into the plugin build. It never opens a USB or HID transport.
[[nodiscard]] sdk::Result<RazerPresentation> resolve_razer_presentation(
    const ControllerModel& controller,
    KeyboardLayoutVariant keyboard_layout);

} // namespace hyperflux::openrgb
