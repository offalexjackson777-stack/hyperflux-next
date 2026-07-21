// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "plugin_application.hpp"

#include <optional>
#include <string>
#include <vector>

namespace hyperflux::openrgb::native
{

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

    friend bool operator==(
        const PluginInformationViewModel&,
        const PluginInformationViewModel&) = default;
};

/// Converts typed runtime state into stable user-facing presentation.
[[nodiscard]] PluginInformationViewModel make_plugin_information_view_model(
    const PluginApplicationStatus& status,
    const std::vector<InventoryReceiverModel>& inventory,
    const std::vector<ControllerModel>& controllers);

} // namespace hyperflux::openrgb::native
