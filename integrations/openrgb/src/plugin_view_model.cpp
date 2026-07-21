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

std::string battery(const v5::BatteryObservation& observation)
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
    return {
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
}

PluginDeviceRow fallback_row(const ControllerModel& controller)
{
    return {
        controller.stable_id,
        std::string(controller.model_name.value()),
        controller_type(controller.device_kind),
        "Unknown",
        availability(controller.availability),
        battery(controller.battery),
        "Qualified controller",
        lighting(controller) + " | " + control(controller.control),
    };
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
        + std::string(build_config::component_version) + " | OpenRGB API 4 | Source "
        + std::string(build_config::source_revision);
    return result;
}

} // namespace hyperflux::openrgb::native
