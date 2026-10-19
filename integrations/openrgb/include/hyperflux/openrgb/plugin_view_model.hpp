// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "plugin_application.hpp"

#if HFX_OPENRGB_NATIVE_ROUTE_ENABLED
#include "native_plugin_application.hpp"
#endif

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace hyperflux::openrgb::native
{

inline constexpr std::string_view repository_url =
    "https://github.com/offalexjackson777-stack/hyperflux-next";

enum class PluginHealthTone
{
    Neutral,
    Positive,
    Warning,
    Negative,
};

struct PluginDeviceRow
{
    std::string stable_id;
    std::string device;
    std::string type;
    std::string pairing;
    std::string availability;
    std::string battery;
    std::string support;
    std::string openrgb;
    /// Card presentation: "Wired" or "Wireless", empty when unknown.
    std::string connection {};
    std::optional<std::uint8_t> battery_percent {};
    /// "Charging", "Full" or "On battery"; empty when the bridge did not report it.
    std::string charge {};
    /// Short qualifier shown beside the battery, empty when the reading is current.
    std::string battery_note {};

    friend bool operator==(const PluginDeviceRow&, const PluginDeviceRow&) = default;
};

struct PluginInformationViewModel
{
    PluginHealthTone tone = PluginHealthTone::Neutral;
    std::string headline;
    std::string summary;
    std::optional<std::string> technical_detail;
    std::vector<PluginDeviceRow> devices;
    std::string lighting_transport;
    std::string effects_authority;
    std::string build_identity;
    /// Plain next step shown as a banner when devices cannot be reached.
    std::optional<std::string> attention {};

    friend bool operator==(
        const PluginInformationViewModel&,
        const PluginInformationViewModel&) = default;
};

/// Converts typed runtime state into stable user-facing presentation.
[[nodiscard]] PluginInformationViewModel make_plugin_information_view_model(
    const PluginApplicationStatus& status,
    const std::vector<InventoryReceiverModel>& inventory,
    const std::vector<ControllerModel>& controllers);

#if HFX_OPENRGB_NATIVE_ROUTE_ENABLED
[[nodiscard]] PluginInformationViewModel make_plugin_information_view_model(
    const NativePluginApplicationStatus& status,
    const std::vector<UpstreamRazerControllerSummary>& controllers);
#endif

} // namespace hyperflux::openrgb::native
