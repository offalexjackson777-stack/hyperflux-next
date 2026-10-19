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
    /// "Mouse", "Keyboard" or "Device": picks the card's icon and names it for screen readers.
    std::string type;
    /// "Wired" or "Wireless", empty when unknown.
    std::string connection {};
    std::optional<std::uint8_t> battery_percent {};
    /// "Charging" or "Full" while plugged in; empty otherwise, because running
    /// on battery is the normal case.
    std::string charge {};
    /// "No update for 14 minutes" when the reading is overdue, empty while current.
    std::string stale {};
    /// "Asleep" or "Not reachable"; empty while the device answers.
    std::string presence {};
    /// One plain sentence for an unusual state, such as a wired device.
    std::string note {};

    friend bool operator==(const PluginDeviceRow&, const PluginDeviceRow&) = default;
};

/// A banner above the cards. `command` is part of `message` and is shown as code.
struct PluginAttention
{
    PluginHealthTone tone = PluginHealthTone::Warning;
    std::string message;
    std::string command {};

    friend bool operator==(const PluginAttention&, const PluginAttention&) = default;
};

struct PluginInformationViewModel
{
    PluginHealthTone tone = PluginHealthTone::Neutral;
    /// The header chip: "Ready", "Connecting", "Needs attention", "Stopped" or "Not loaded".
    std::string headline;
    /// The bridge's own error text, shown as the header chip's tooltip.
    std::optional<std::string> technical_detail;
    std::vector<PluginDeviceRow> devices;
    std::optional<PluginAttention> attention {};
    /// Shown instead of cards when the bridge answers but reports no device.
    std::optional<std::string> empty_state {};

    friend bool operator==(
        const PluginInformationViewModel&,
        const PluginInformationViewModel&) = default;
};

/// Milliseconds on CLOCK_MONOTONIC, the clock the bridge stamps readings with.
[[nodiscard]] std::uint64_t monotonic_now_ms() noexcept;

/// "14 minutes" or "2 hours"; whole units, rounded down, at least one minute.
[[nodiscard]] std::string elapsed_phrase(std::uint64_t milliseconds);

/// Converts typed runtime state into stable user-facing presentation.
/// `now_ms` is monotonic_now_ms(), passed in so tests can fix the time.
[[nodiscard]] PluginInformationViewModel make_plugin_information_view_model(
    const PluginApplicationStatus& status,
    const std::vector<InventoryReceiverModel>& inventory,
    const std::vector<ControllerModel>& controllers,
    std::uint64_t now_ms);

#if HFX_OPENRGB_NATIVE_ROUTE_ENABLED
[[nodiscard]] PluginInformationViewModel make_plugin_information_view_model(
    const NativePluginApplicationStatus& status,
    const std::vector<UpstreamRazerControllerSummary>& controllers,
    std::uint64_t now_ms);
#endif

} // namespace hyperflux::openrgb::native
