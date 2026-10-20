// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/plugin_view_model.hpp>

#include <algorithm>
#include <ctime>
#include <iomanip>
#include <sstream>
#include <string>
#include <string_view>

namespace hyperflux::openrgb::native
{
namespace
{

std::string controller_type(DeviceKind kind)
{
    switch(kind)
    {
        case DeviceKind::Mouse: return "Mouse";
        case DeviceKind::Keyboard: return "Keyboard";
        default: return "Device";
    }
}

constexpr std::string_view doctor_command = "hyperfluxctl doctor --explain";

std::string presence(InventoryAvailability value)
{
    switch(value)
    {
        case InventoryAvailability::Sleeping: return "Asleep";
        case InventoryAvailability::Unavailable:
        case InventoryAvailability::ReceiverUnavailable: return "Not reachable";
        case InventoryAvailability::Available:
        case InventoryAvailability::Unknown:
        case InventoryAvailability::Unpaired:
        case InventoryAvailability::PairingUnknown: return {};
    }
    return {};
}

std::string presence(ControllerAvailability value)
{
    return value == ControllerAvailability::Ready ? std::string() : std::string("Asleep");
}

#if HFX_OPENRGB_NATIVE_ROUTE_ENABLED
constexpr std::string_view wired_note = "OpenRGB controls it over the cable until it is unplugged.";
constexpr std::string_view direct_note = "OpenRGB controls it directly, as its own entry in the device list.";

std::string presence(const std::optional<PresenceState>& value)
{
    if(!value.has_value())
    {
        return {};
    }
    switch(*value)
    {
        case PresenceState::Sleeping: return "Asleep";
        case PresenceState::Unavailable: return "Not reachable";
        case PresenceState::Available:
        case PresenceState::Unknown: return {};
    }
    return {};
}
#endif

/// Age of a reading, or nothing when it has no time or one from another boot.
template<typename Observation>
std::optional<std::uint64_t> age(const Observation& observation, std::uint64_t now_ms)
{
    if(!observation.observed_at_ms.has_value() || observation.observed_at_ms->value() > now_ms)
    {
        return std::nullopt;
    }
    return now_ms - observation.observed_at_ms->value();
}

template<typename Observation>
void apply_battery(PluginDeviceRow& row, const Observation& observation, std::uint64_t now_ms)
{
    if(observation.availability != TelemetryAvailability::Reported
       || !observation.percentage.has_value())
    {
        return;
    }
    const auto percent = observation.percentage->value();
    row.battery_percent = static_cast<std::uint8_t>(percent);
    if(observation.freshness == FreshnessState::Stale)
    {
        const auto elapsed = age(observation, now_ms);
        row.stale = elapsed.has_value() ? "No update for " + elapsed_phrase(*elapsed) : "Update overdue";
    }
    if constexpr(requires { observation.charge; })
    {
        if(observation.freshness == FreshnessState::Fresh && observation.charge.has_value()
           && *observation.charge == BatteryCharge::Charging)
        {
            row.charge = percent >= 100 ? "Full" : "Charging";
        }
    }
}

/// The newest reading across devices, for the banner that says how old they are.
template<typename Observation>
void track_newest(
    std::optional<std::uint64_t>& newest,
    const Observation& observation,
    std::uint64_t now_ms)
{
    if(observation.availability != TelemetryAvailability::Reported)
    {
        return;
    }
    const auto elapsed = age(observation, now_ms);
    if(elapsed.has_value() && (!newest.has_value() || *elapsed < *newest))
    {
        newest = elapsed;
    }
}

/// OpenRGB names receiver devices "<model> (Wireless)" or "<model> (Wired)";
/// the card shows the model and moves the suffix into the connection chip.
void split_connection(PluginDeviceRow& row)
{
    for(const std::string_view suffix : {std::string_view(" (Wireless)"), std::string_view(" (Wired)")})
    {
        if(row.device.size() > suffix.size()
           && std::string_view(row.device).substr(row.device.size() - suffix.size()) == suffix)
        {
            row.connection = std::string(suffix.substr(2, suffix.size() - 3));
            row.device.resize(row.device.size() - suffix.size());
            return;
        }
    }
}

std::string fallback_device_name(const InventoryDeviceModel& device)
{
    if(device.model_name.has_value())
    {
        return std::string(device.model_name->value());
    }
    std::ostringstream output;
    output << "Unknown " << controller_type(device.device_kind) << " (PID 0x"
           << std::hex << std::uppercase << std::setw(4) << std::setfill('0')
           << device.product_id.value() << ')';
    return output.str();
}

PluginDeviceRow row(const InventoryDeviceModel& device, std::uint64_t now_ms)
{
    PluginDeviceRow result {
        device.stable_id,
        fallback_device_name(device),
        controller_type(device.device_kind),
    };
    result.presence = presence(device.availability);
    apply_battery(result, device.battery, now_ms);
    split_connection(result);
    return result;
}

PluginDeviceRow fallback_row(const ControllerModel& controller, std::uint64_t now_ms)
{
    PluginDeviceRow result {
        controller.stable_id,
        std::string(controller.model_name.value()),
        controller_type(controller.device_kind),
    };
    result.presence = presence(controller.availability);
    apply_battery(result, controller.battery, now_ms);
    split_connection(result);
    return result;
}

void sort_devices(std::vector<PluginDeviceRow>& devices)
{
    std::sort(
        devices.begin(),
        devices.end(),
        [](const PluginDeviceRow& left, const PluginDeviceRow& right)
        {
            if(left.type != right.type)
            {
                return left.type < right.type;
            }
            return left.device < right.device;
        });
}

enum class Runtime
{
    NotLoaded,
    Running,
    Connecting,
    Recovering,
    Failed,
    Stopped,
    VersionMismatch,
};

/// The bridge turned this plugin away for its protocol version or features.
/// After an update the old bridge keeps running until the computer restarts.
bool version_mismatch(const std::optional<sdk::Error>& error)
{
    if(!error.has_value())
    {
        return false;
    }
    switch(error->code)
    {
        case sdk::ErrorCode::NegotiationFailed:
        case sdk::ErrorCode::RequiredFeatureMissing: return true;
        case sdk::ErrorCode::ServerRejected:
            return error->finding_id == std::optional<std::string>("HFX-PROTOCOL-001")
                || error->finding_id == std::optional<std::string>("HFX-PROTOCOL-002");
        default: return false;
    }
}

/// A version mismatch outranks whatever retry state the worker is in, since
/// retrying cannot fix it; a plugin that is running or stopped says so instead.
Runtime with_version_check(Runtime value, const std::optional<sdk::Error>& error)
{
    const bool retrying = value == Runtime::Connecting || value == Runtime::Recovering || value == Runtime::Failed;
    return retrying && version_mismatch(error) ? Runtime::VersionMismatch : value;
}

/// Header chip, banner and empty state. Normal states stay quiet: the banner
/// appears only when the readings on screen are old or the bridge has failed.
void summarise(
    PluginInformationViewModel& result,
    Runtime runtime,
    std::optional<std::uint64_t> newest_reading_age)
{
    const bool has_devices = !result.devices.empty();
    const std::string age_clause = newest_reading_age.has_value()
        ? "from " + elapsed_phrase(*newest_reading_age) + " ago"
        : std::string();
    switch(runtime)
    {
        case Runtime::NotLoaded:
            result.tone = PluginHealthTone::Neutral;
            result.headline = "Not loaded";
            return;
        case Runtime::Stopped:
            result.tone = PluginHealthTone::Neutral;
            result.headline = "Stopped";
            return;
        case Runtime::Running:
            result.tone = PluginHealthTone::Positive;
            result.headline = "Ready";
            if(!has_devices)
            {
                result.empty_state = "No devices found on a supported receiver";
            }
            return;
        case Runtime::Connecting:
        case Runtime::Recovering:
            result.tone = PluginHealthTone::Warning;
            result.headline = "Connecting";
            // Without old readings on screen the chip already says everything.
            if(has_devices && !age_clause.empty())
            {
                result.attention = PluginAttention {
                    PluginHealthTone::Warning,
                    "Connecting to the HyperFlux bridge. These readings are " + age_clause + ".",
                };
            }
            return;
        case Runtime::VersionMismatch:
            result.tone = PluginHealthTone::Negative;
            result.headline = "Needs attention";
            result.attention = PluginAttention {
                PluginHealthTone::Negative,
                "This plugin and the running HyperFlux bridge are different versions. "
                "Restart the computer to finish the update.",
            };
            return;
        case Runtime::Failed:
            result.tone = PluginHealthTone::Negative;
            result.headline = "Needs attention";
            result.attention = PluginAttention {
                PluginHealthTone::Negative,
                std::string("The HyperFlux bridge is not answering")
                    + (has_devices && !age_clause.empty()
                           ? ", so these readings are " + age_clause + ". "
                           : ". ")
                    + "Run " + std::string(doctor_command) + " in a terminal to see the fix.",
                std::string(doctor_command),
            };
            return;
    }
}

Runtime runtime(const PluginApplicationStatus& status)
{
    if(!status.loaded)
    {
        return Runtime::NotLoaded;
    }
    switch(status.coordinator.worker_state)
    {
        case WorkerState::Running: return Runtime::Running;
        case WorkerState::Created:
        case WorkerState::Starting: return Runtime::Connecting;
        case WorkerState::Recovering: return Runtime::Recovering;
        case WorkerState::Failed: return Runtime::Failed;
        case WorkerState::Stopping:
        case WorkerState::Stopped: return Runtime::Stopped;
    }
    return Runtime::Stopped;
}

#if HFX_OPENRGB_NATIVE_ROUTE_ENABLED
Runtime runtime(const NativePluginApplicationStatus& status)
{
    if(!status.loaded)
    {
        return Runtime::NotLoaded;
    }
    switch(status.coordinator.worker_state)
    {
        case NativeRouteWorkerState::Running: return Runtime::Running;
        case NativeRouteWorkerState::Created:
        case NativeRouteWorkerState::Starting: return Runtime::Connecting;
        case NativeRouteWorkerState::Recovering: return Runtime::Recovering;
        case NativeRouteWorkerState::Failed: return Runtime::Failed;
        case NativeRouteWorkerState::Stopping:
        case NativeRouteWorkerState::Stopped: return Runtime::Stopped;
    }
    return Runtime::Stopped;
}
#endif

} // namespace

std::uint64_t monotonic_now_ms() noexcept
{
    timespec now {};
    if(clock_gettime(CLOCK_MONOTONIC, &now) != 0)
    {
        return 0;
    }
    return static_cast<std::uint64_t>(now.tv_sec) * 1000U
        + static_cast<std::uint64_t>(now.tv_nsec) / 1000000U;
}

std::string elapsed_phrase(std::uint64_t milliseconds)
{
    const auto minutes = std::max<std::uint64_t>(1, milliseconds / 60000U);
    if(minutes < 60)
    {
        return std::to_string(minutes) + (minutes == 1 ? " minute" : " minutes");
    }
    const auto hours = minutes / 60;
    if(hours < 48)
    {
        return std::to_string(hours) + (hours == 1 ? " hour" : " hours");
    }
    return std::to_string(hours / 24) + " days";
}

PluginInformationViewModel make_plugin_information_view_model(
    const PluginApplicationStatus& status,
    const std::vector<InventoryReceiverModel>& inventory,
    const std::vector<ControllerModel>& controllers,
    std::uint64_t now_ms)
{
    PluginInformationViewModel result;
    std::optional<std::uint64_t> newest;
    for(const auto& receiver : inventory)
    {
        for(const auto& device : receiver.devices)
        {
            result.devices.push_back(row(device, now_ms));
            track_newest(newest, device.battery, now_ms);
        }
    }
    if(result.devices.empty())
    {
        for(const auto& controller : controllers)
        {
            result.devices.push_back(fallback_row(controller, now_ms));
            track_newest(newest, controller.battery, now_ms);
        }
    }
    sort_devices(result.devices);
    if(status.loaded && status.coordinator.last_error.has_value())
    {
        result.technical_detail = status.coordinator.last_error->message;
    }
    summarise(
        result,
        with_version_check(runtime(status), status.loaded ? status.coordinator.last_error : std::nullopt),
        newest);
    return result;
}

#if HFX_OPENRGB_NATIVE_ROUTE_ENABLED
PluginInformationViewModel make_plugin_information_view_model(
    const NativePluginApplicationStatus& status,
    const std::vector<UpstreamRazerControllerSummary>& controllers,
    std::uint64_t now_ms)
{
    PluginInformationViewModel result;
    std::optional<std::uint64_t> newest;
    for(const auto& controller : controllers)
    {
        PluginDeviceRow row {
            controller.stable_id,
            controller.name,
            controller_type(controller.device_kind),
        };
        if(controller.battery.has_value())
        {
            apply_battery(row, *controller.battery, now_ms);
            track_newest(newest, *controller.battery, now_ms);
        }
        split_connection(row);
        if(controller.direct_suppressed)
        {
            // OpenRGB drives the same serial itself, over the cable or through
            // its own receiver; only its controller name says which. The
            // bridge's presence says nothing about a device it does not drive.
            row.connection = controller.direct_connection;
            row.note = std::string(row.connection == "Wired" ? wired_note : direct_note);
        }
        else
        {
            if(row.connection.empty())
            {
                row.connection = "Wireless";
            }
            row.presence = presence(controller.presence);
        }
        result.devices.push_back(std::move(row));
    }
    sort_devices(result.devices);
    if(status.loaded && status.coordinator.last_error.has_value())
    {
        result.technical_detail = status.coordinator.last_error->message;
    }
    summarise(
        result,
        with_version_check(runtime(status), status.loaded ? status.coordinator.last_error : std::nullopt),
        newest);
    return result;
}
#endif

} // namespace hyperflux::openrgb::native
