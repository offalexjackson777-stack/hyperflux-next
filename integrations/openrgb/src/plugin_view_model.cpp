// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/build_config.hpp>
#include <hyperflux/openrgb/plugin_view_model.hpp>

#include <algorithm>
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

std::string pairing(PairingState value)
{
    switch(value)
    {
        case PairingState::Paired: return "Paired";
        case PairingState::Unpaired: return "Not paired";
        case PairingState::Unknown: return "Unknown";
    }
    return "Unknown";
}

std::string availability(InventoryAvailability value)
{
    switch(value)
    {
        case InventoryAvailability::Available: return "Available";
        case InventoryAvailability::Sleeping: return "Sleeping";
        case InventoryAvailability::Unavailable: return "Unavailable";
        case InventoryAvailability::Unknown: return "Status unknown";
        case InventoryAvailability::Unpaired: return "Not paired";
        case InventoryAvailability::PairingUnknown: return "Pairing unknown";
        case InventoryAvailability::ReceiverUnavailable: return "Receiver unavailable";
    }
    return "Status unknown";
}

std::string availability(ControllerAvailability value)
{
    return value == ControllerAvailability::Ready ? "Available" : "Sleeping";
}

template<typename Observation>
std::string battery(const Observation& observation)
{
    if(observation.availability == TelemetryAvailability::Unavailable)
    {
        return "Not reported";
    }
    if(observation.availability != TelemetryAvailability::Reported
       || !observation.percentage.has_value())
    {
        return "Waiting for report";
    }
    std::string result = std::to_string(observation.percentage->value()) + "%";
    if(observation.freshness == FreshnessState::Stale)
    {
        result += " - update overdue";
    }
    else if(observation.freshness == FreshnessState::Unknown)
    {
        result += " - freshness unknown";
    }
    return result;
}

template<typename Observation>
void apply_battery(PluginDeviceRow& row, const Observation& observation)
{
    row.battery = battery(observation);
    const bool reported = observation.availability == TelemetryAvailability::Reported
        && observation.percentage.has_value();
    if(!reported)
    {
        row.battery_note = row.battery;
        return;
    }
    const auto percent = observation.percentage->value();
    row.battery_percent = static_cast<std::uint8_t>(percent);
    if(observation.freshness == FreshnessState::Stale)
    {
        row.battery_note = "Update overdue";
    }
    else if(observation.freshness == FreshnessState::Unknown)
    {
        row.battery_note = "Freshness unknown";
    }
    if constexpr(requires { observation.charge; })
    {
        if(observation.freshness == FreshnessState::Fresh && observation.charge.has_value())
        {
            row.charge = *observation.charge == BatteryCharge::Charging
                ? (percent >= 100 ? "Full" : "Charging")
                : "On battery";
        }
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

std::string lighting(const ControllerModel& controller)
{
    const auto slots = controller.lighting.application_slot_count.value();
    const auto rows = controller.lighting.rows.value();
    const auto columns = controller.lighting.columns.value();
    if(controller.device_kind == DeviceKind::Keyboard && rows > 1 && columns > 1)
    {
        return std::to_string(slots) + " keys, " + std::to_string(rows) + " x "
            + std::to_string(columns) + " layout";
    }
    return std::to_string(slots) + (slots == 1 ? " LED" : " LEDs");
}

std::string control(const ControllerControlState& state)
{
    switch(state.ownership)
    {
        case ControllerOwnerState::OwnedByOpenRgb:
            return "Controlled by OpenRGB";
        case ControllerOwnerState::OwnedByAnotherClient:
            return state.owner_client_id.has_value()
                ? "Controlled by " + std::string(state.owner_client_id->value())
                : "Controlled by another application";
        case ControllerOwnerState::Unowned:
            return state.actions.can_acquire ? "Available" : "Read only";
    }
    return "Unknown";
}

std::string support(SupportLevel value)
{
    switch(value)
    {
        case SupportLevel::Candidate: return "Candidate";
        case SupportLevel::Identified: return "Identified";
        case SupportLevel::ReadOnly: return "Read only";
        case SupportLevel::TelemetryQualified: return "Telemetry qualified";
        case SupportLevel::LightingQualified: return "Lighting qualified";
        case SupportLevel::SettingsQualified: return "Settings qualified";
        case SupportLevel::PairingQualified: return "Pairing qualified";
        case SupportLevel::ProductionQualified: return "Production qualified";
    }
    return "Unknown";
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

const ControllerModel* controller_for(
    const InventoryDeviceModel& device,
    const std::vector<ControllerModel>& controllers)
{
    const auto found = std::find_if(
        controllers.begin(),
        controllers.end(),
        [&device](const ControllerModel& controller) {
            return controller.authority.receiver_id == device.receiver_id
                && controller.authority.generation_id == device.generation_id
                && controller.authority.device_id == device.device_id;
        });
    return found == controllers.end() ? nullptr : &*found;
}

PluginDeviceRow row(
    const InventoryDeviceModel& device,
    const std::vector<ControllerModel>& controllers)
{
    const auto* controller = controller_for(device, controllers);
    PluginDeviceRow result {
        device.stable_id,
        fallback_device_name(device),
        controller_type(device.device_kind),
        pairing(device.pairing),
        availability(device.availability),
        battery(device.battery),
        support(device.support_level),
        controller == nullptr
            ? "Not exposed"
            : lighting(*controller) + " | " + control(controller->control),
    };
    apply_battery(result, device.battery);
    split_connection(result);
    return result;
}

PluginDeviceRow fallback_row(const ControllerModel& controller)
{
    PluginDeviceRow result {
        controller.stable_id,
        std::string(controller.model_name.value()),
        controller_type(controller.device_kind),
        "Unknown",
        availability(controller.availability),
        battery(controller.battery),
        "Qualified controller",
        lighting(controller) + " | " + control(controller.control),
    };
    apply_battery(result, controller.battery);
    split_connection(result);
    return result;
}

void runtime_summary(
    PluginInformationViewModel& result,
    const PluginApplicationStatus& status)
{
    if(!status.loaded)
    {
        result.tone = PluginHealthTone::Neutral;
        result.headline = "Not loaded";
        result.summary = "The OpenRGB integration is not active.";
        return;
    }
    if(status.coordinator.last_error.has_value())
    {
        result.technical_detail = status.coordinator.last_error->message;
    }
    switch(status.coordinator.worker_state)
    {
        case WorkerState::Running:
            result.tone = PluginHealthTone::Positive;
            result.headline = status.coordinator.controllers == 0
                ? "No controllable devices"
                : "Ready";
            if(result.devices.empty())
            {
                result.summary = "The bridge is connected; no paired device is reported.";
            }
            else if(status.coordinator.controllers == 0)
            {
                result.summary = std::to_string(result.devices.size())
                    + (result.devices.size() == 1
                           ? " paired device is known, but it is not currently exposed for lighting."
                           : " paired devices are known, but none is currently exposed for lighting.");
            }
            else
            {
                result.summary = std::to_string(result.devices.size())
                    + (result.devices.size() == 1 ? " paired device; " : " paired devices; ")
                    + std::to_string(status.coordinator.controllers)
                    + (status.coordinator.controllers == 1
                           ? " controller is exposed in OpenRGB."
                           : " controllers are exposed in OpenRGB.");
            }
            return;
        case WorkerState::Created:
        case WorkerState::Starting:
        case WorkerState::Recovering:
            result.tone = PluginHealthTone::Warning;
            result.headline = "Connecting";
            result.summary = "Waiting for the local HyperFlux bridge.";
            return;
        case WorkerState::Failed:
            result.tone = PluginHealthTone::Negative;
            result.headline = "Needs attention";
            result.summary = "The integration could not continue.";
            return;
        case WorkerState::Stopping:
        case WorkerState::Stopped:
            result.tone = PluginHealthTone::Neutral;
            result.headline = "Stopped";
            result.summary = "The OpenRGB integration is stopped.";
            return;
    }
}

} // namespace

PluginInformationViewModel make_plugin_information_view_model(
    const PluginApplicationStatus& status,
    const std::vector<InventoryReceiverModel>& inventory,
    const std::vector<ControllerModel>& controllers)
{
    PluginInformationViewModel result;
    for(const auto& receiver : inventory)
    {
        for(const auto& device : receiver.devices)
        {
            result.devices.push_back(row(device, controllers));
        }
    }
    if(result.devices.empty() && !controllers.empty())
    {
        std::transform(
            controllers.begin(),
            controllers.end(),
            std::back_inserter(result.devices),
            fallback_row);
    }
    std::sort(
        result.devices.begin(),
        result.devices.end(),
        [](const PluginDeviceRow& left, const PluginDeviceRow& right)
        {
            if(left.type != right.type)
            {
                return left.type < right.type;
            }
            return left.device < right.device;
        });
    runtime_summary(result, status);
    result.lighting_transport =
        "Direct, Static, Off, brightness, and per-LED frames use the local bridge.";
    result.effects_authority =
        "Animations are generated by the official OpenRGB Effects plugin and transported as Direct frames.";
    result.build_identity = std::string("Version ")
        + std::string(build_config::component_version) + " | OpenRGB API 5 | Source "
        + std::string(build_config::source_revision);
    return result;
}

#if HFX_OPENRGB_NATIVE_ROUTE_ENABLED
PluginInformationViewModel make_plugin_information_view_model(
    const NativePluginApplicationStatus& status,
    const std::vector<UpstreamRazerControllerSummary>& controllers)
{
    PluginInformationViewModel result;
    for(const auto& controller : controllers)
    {
        PluginDeviceRow row {
            controller.stable_id,
            controller.name,
            controller_type(controller.device_kind),
            "Receiver route",
            controller.direct_suppressed
                ? "Direct USB active"
                : (controller.registered ? "Available" : "Waiting"),
            "Waiting for report",
            "Full support",
            controller.direct_suppressed
                ? "OpenRGB is using the USB cable"
                : (controller.registered ? "Controlled by OpenRGB" : "Waiting for OpenRGB"),
        };
        if(controller.battery.has_value())
        {
            apply_battery(row, *controller.battery);
        }
        else
        {
            row.battery_note = row.battery;
        }
        split_connection(row);
        if(row.connection.empty())
        {
            row.connection = controller.direct_suppressed ? "Wired" : "Wireless";
        }
        result.devices.push_back(std::move(row));
    }
    std::sort(
        result.devices.begin(),
        result.devices.end(),
        [](const PluginDeviceRow& left, const PluginDeviceRow& right)
        {
            if(left.type != right.type)
            {
                return left.type < right.type;
            }
            return left.device < right.device;
        });

    if(!status.loaded)
    {
        result.tone = PluginHealthTone::Neutral;
        result.headline = "Not loaded";
        result.summary = "The native OpenRGB transport is not active.";
    }
    else
    {
        if(status.coordinator.last_error.has_value())
        {
            result.technical_detail = status.coordinator.last_error->message;
        }
        switch(status.coordinator.worker_state)
        {
            case NativeRouteWorkerState::Running:
                result.tone = PluginHealthTone::Positive;
                result.headline = status.coordinator.registered_controllers == 0
                    ? "No receiver-routed devices"
                    : "Native transport ready";
                result.summary = std::to_string(status.coordinator.registered_controllers)
                    + (status.coordinator.registered_controllers == 1
                           ? " upstream Razer controller is active."
                           : " upstream Razer controllers are active.");
                if(status.coordinator.direct_suppressed_controllers > 0)
                {
                    result.summary += " "
                        + std::to_string(status.coordinator.direct_suppressed_controllers)
                        + (status.coordinator.direct_suppressed_controllers == 1
                               ? " matching receiver route is paused for direct USB."
                               : " matching receiver routes are paused for direct USB.");
                }
                break;
            case NativeRouteWorkerState::Created:
            case NativeRouteWorkerState::Starting:
            case NativeRouteWorkerState::Recovering:
                result.tone = PluginHealthTone::Warning;
                result.headline = "Connecting";
                result.summary = "Waiting for the local HyperFlux native tunnel.";
                if(status.coordinator.worker_state == NativeRouteWorkerState::Recovering)
                {
                    result.attention =
                        "HyperFlux can't reach your devices right now. Run "
                        "hyperfluxctl doctor --explain in a terminal for the fix.";
                }
                break;
            case NativeRouteWorkerState::Failed:
                result.tone = PluginHealthTone::Negative;
                result.headline = "Needs attention";
                result.summary = "The native OpenRGB transport could not continue.";
                result.attention =
                    "HyperFlux can't reach your devices right now. Run "
                    "hyperfluxctl doctor --explain in a terminal for the fix.";
                break;
            case NativeRouteWorkerState::Stopping:
            case NativeRouteWorkerState::Stopped:
                result.tone = PluginHealthTone::Neutral;
                result.headline = "Stopped";
                result.summary = "The native OpenRGB transport is stopped.";
                break;
        }
    }
    result.lighting_transport =
        "OpenRGB's upstream Razer controller sends standard Feature reports through the local HyperFlux tunnel.";
    result.effects_authority =
        "OpenRGB owns hardware modes, layouts, profiles and software effects; HyperFlux transports their reports unchanged.";
    result.build_identity = std::string("Version ")
        + std::string(build_config::component_version) + " | OpenRGB API 5 | Source "
        + std::string(build_config::source_revision);
    return result;
}
#endif

} // namespace hyperflux::openrgb::native
