// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_native_fixture.hpp"

#include <hyperflux/openrgb/plugin_view_model.hpp>

#include <cstdlib>
#include <cstdint>
#include <iostream>
#include <optional>
#include <string>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-plugin-view-model-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb;
    using namespace hyperflux::openrgb::native;

    const auto source = hyperflux::test::native_integration_view(1, 1);
    auto projected = project_controllers(source);
    auto inventory_result = project_inventory(source);
    if(!projected || !inventory_result || projected.value().size() != 2
       || inventory_result.value().size() != 1
       || inventory_result.value().front().devices.size() != 2)
    {
        return failure(__LINE__);
    }
    auto controllers = std::move(projected).value();
    auto inventory = std::move(inventory_result).value();
    inventory[0].devices[0].battery = {
        TelemetryAvailability::Reported,
        hyperflux::test::number<BatteryPercent>(76),
        FreshnessState::Fresh,
        EvidenceConfidence::Observed,
        hyperflux::test::number<MonotonicMs>(100),
    };
    inventory[0].devices[1].availability = InventoryAvailability::Sleeping;
    inventory[0].devices[1].presence = PresenceState::Sleeping;
    inventory[0].devices[1].battery = {
        TelemetryAvailability::Reported,
        hyperflux::test::number<BatteryPercent>(42),
        FreshnessState::Stale,
        EvidenceConfidence::Derived,
        hyperflux::test::number<MonotonicMs>(80),
    };
    controllers[1].control.ownership = ControllerOwnerState::OwnedByAnotherClient;
    controllers[1].control.owner_client_id = hyperflux::test::text<ClientId>("polychromatic");

    PluginApplicationStatus status;
    status.loaded = true;
    status.coordinator.worker_state = WorkerState::Running;
    status.coordinator.started = true;
    status.coordinator.controllers = controllers.size();
    const auto model = make_plugin_information_view_model(status, inventory, controllers);
    if(model.tone != PluginHealthTone::Positive || model.headline != "Ready"
       || model.summary != "2 paired devices; 2 controllers are exposed in OpenRGB."
       || model.devices.size() != 2
       || model.effects_authority.find("official OpenRGB Effects plugin") == std::string::npos
       || model.build_identity.find("OpenRGB API 5") == std::string::npos)
    {
        return failure(__LINE__);
    }
    bool found_fresh = false;
    bool found_sleeping = false;
    for(const auto& row : model.devices)
    {
        found_fresh = found_fresh || row.battery == "76%";
        found_sleeping = found_sleeping
            || (row.availability == "Sleeping"
                && row.battery == "42% - update overdue"
                && row.openrgb.find("Controlled by polychromatic") != std::string::npos);
    }
    if(!found_fresh || !found_sleeping)
    {
        return failure(__LINE__);
    }
    if(model.attention.has_value())
    {
        return failure(__LINE__);
    }

    status.coordinator.worker_state = WorkerState::Recovering;
    status.coordinator.last_error = sdk::Error {
        sdk::ErrorCode::SocketConnect,
        "injected socket detail",
        std::nullopt,
    };
    const auto recovering = make_plugin_information_view_model(status, {}, {});
    if(recovering.tone != PluginHealthTone::Warning
       || recovering.headline != "Connecting"
       || recovering.summary != "Waiting for the local HyperFlux bridge."
       || recovering.technical_detail != "injected socket detail")
    {
        return failure(__LINE__);
    }

#if HFX_OPENRGB_NATIVE_ROUTE_ENABLED
    NativePluginApplicationStatus native_status;
    native_status.loaded = true;
    native_status.coordinator.worker_state = NativeRouteWorkerState::Running;
    native_status.coordinator.started = true;
    native_status.coordinator.controllers = 2;
    native_status.coordinator.registered_controllers = 1;
    native_status.coordinator.direct_suppressed_controllers = 1;
    const std::vector<UpstreamRazerControllerSummary> native_controllers {
        {
            "receiver-1/mouse-00cd/endpoint-mouse",
            "Razer Basilisk V3 Pro 35K (Wireless)",
            DeviceKind::Mouse,
            hyperflux::test::number<ProductId>(0x00cd),
            hyperflux::test::text<ReceiverId>("receiver-1"),
            hyperflux::test::number<GenerationId>(4),
            hyperflux::test::text<LogicalDeviceId>("mouse-00cd"),
            true,
            false,
            v8::BatteryObservation {
                TelemetryAvailability::Reported,
                hyperflux::test::number<BatteryPercent>(81),
                BatteryCharge::Charging,
                FreshnessState::Fresh,
                EvidenceConfidence::Observed,
                hyperflux::test::number<MonotonicMs>(100),
            },
        },
        {
            "receiver-1/keyboard-0296/endpoint-keyboard",
            "Razer DeathStalker V2 Pro TKL (Wireless)",
            DeviceKind::Keyboard,
            hyperflux::test::number<ProductId>(0x0296),
            hyperflux::test::text<ReceiverId>("receiver-1"),
            hyperflux::test::number<GenerationId>(4),
            hyperflux::test::text<LogicalDeviceId>("keyboard-0296"),
            false,
            true,
        },
    };
    const auto native_model = make_plugin_information_view_model(
        native_status, native_controllers);
    if(native_model.tone != PluginHealthTone::Positive
       || native_model.headline != "Native transport ready"
       || native_model.summary
              != "1 upstream Razer controller is active. 1 matching receiver route is paused for direct USB."
       || native_model.devices.size() != 2
       || native_model.lighting_transport.find("standard Feature reports")
              == std::string::npos
       || native_model.effects_authority.find("OpenRGB owns hardware modes")
              == std::string::npos)
    {
        return failure(__LINE__);
    }
    if(native_model.devices[0].availability != "Direct USB active"
       || native_model.devices[1].availability != "Available")
    {
        return failure(__LINE__);
    }
    // Cards show the model name with the connection as a chip, never "TenKeyless".
    const auto& keyboard = native_model.devices[0];
    const auto& mouse = native_model.devices[1];
    if(keyboard.device != "Razer DeathStalker V2 Pro TKL" || keyboard.connection != "Wireless"
       || keyboard.battery_percent.has_value() || keyboard.battery_note != "Waiting for report"
       || !keyboard.charge.empty() || keyboard.support != "Full support"
       || mouse.device != "Razer Basilisk V3 Pro 35K" || mouse.connection != "Wireless"
       || mouse.battery_percent != std::optional<std::uint8_t>(81) || mouse.battery != "81%"
       || mouse.charge != "Charging" || !mouse.battery_note.empty())
    {
        return failure(__LINE__);
    }
    auto stale_controllers = native_controllers;
    stale_controllers[0].battery->freshness = FreshnessState::Stale;
    const auto stale = make_plugin_information_view_model(native_status, stale_controllers);
    if(stale.devices[1].charge != "" || stale.devices[1].battery_note != "Update overdue")
    {
        return failure(__LINE__);
    }
    native_status.coordinator.worker_state = NativeRouteWorkerState::Recovering;
    const auto unreachable = make_plugin_information_view_model(native_status, {});
    if(!unreachable.attention.has_value()
       || unreachable.attention->find("hyperfluxctl doctor --explain") == std::string::npos)
    {
        return failure(__LINE__);
    }
#endif
    return EXIT_SUCCESS;
}
