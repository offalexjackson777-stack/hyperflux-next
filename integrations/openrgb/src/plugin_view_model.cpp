// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/build_config.hpp>
#include <hyperflux/openrgb/plugin_view_model.hpp>

#include <algorithm>
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

std::string availability(ControllerAvailability value)
{
    switch(value)
    {
        case ControllerAvailability::Ready: return "Ready";
        case ControllerAvailability::Sleeping: return "Sleeping";
    }
    return "Unknown";
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

PluginControllerRow row(const ControllerModel& controller)
{
    return {
        controller.stable_id,
        std::string(controller.model_name.value()),
        controller_type(controller.device_kind),
        availability(controller.availability),
        battery(controller.battery),
        lighting(controller),
        control(controller.control),
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
            result.headline = result.controllers.empty() ? "No controllable devices" : "Ready";
            result.summary = result.controllers.empty()
                ? "The bridge is connected; no qualified controller is currently available."
                : std::to_string(result.controllers.size())
                    + (result.controllers.size() == 1
                           ? " controller is available in OpenRGB."
                           : " controllers are available in OpenRGB.");
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
    const std::vector<ControllerModel>& controllers)
{
    PluginInformationViewModel result;
    result.controllers.reserve(controllers.size());
    std::transform(
        controllers.begin(),
        controllers.end(),
        std::back_inserter(result.controllers),
        row);
    std::sort(
        result.controllers.begin(),
        result.controllers.end(),
        [](const PluginControllerRow& left, const PluginControllerRow& right)
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
