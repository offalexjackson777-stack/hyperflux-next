// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_native_fixture.hpp"

#include <hyperflux/openrgb/plugin_view_model.hpp>

#include <cstdlib>
#include <cstdint>
#include <iostream>
#include <optional>
#include <string>
#include <utility>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-plugin-view-model-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

constexpr std::uint64_t minute = 60'000;

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb;
    using namespace hyperflux::openrgb::native;

    // Ages read in whole minutes, then hours, then days, never "0 minutes".
    if(elapsed_phrase(0) != "1 minute" || elapsed_phrase(2 * minute) != "2 minutes"
       || elapsed_phrase(59 * minute) != "59 minutes" || elapsed_phrase(60 * minute) != "1 hour"
       || elapsed_phrase(47 * 60 * minute) != "47 hours"
       || elapsed_phrase(48 * 60 * minute) != "2 days")
    {
        return failure(__LINE__);
    }

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
    const std::uint64_t now = 80 + 14 * minute;
    const auto fresh_id = inventory[0].devices[0].stable_id;
    const auto sleeping_id = inventory[0].devices[1].stable_id;

    PluginApplicationStatus status;
    status.loaded = true;
    status.coordinator.worker_state = WorkerState::Running;
    status.coordinator.started = true;
    status.coordinator.controllers = controllers.size();
    const auto model = make_plugin_information_view_model(status, inventory, controllers, now);
    // A normal state is quiet: one chip, no banner, no empty state.
    if(model.tone != PluginHealthTone::Positive || model.headline != "Ready"
       || model.devices.size() != 2 || model.attention.has_value() || model.empty_state.has_value()
       || model.technical_detail.has_value() || model.devices[0].type > model.devices[1].type)
    {
        return failure(__LINE__);
    }
    const PluginDeviceRow* fresh = nullptr;
    const PluginDeviceRow* sleeping = nullptr;
    for(const auto& row : model.devices)
    {
        fresh = row.stable_id == fresh_id ? &row : fresh;
        sleeping = row.stable_id == sleeping_id ? &row : sleeping;
    }
    if(fresh == nullptr || sleeping == nullptr
       || fresh->battery_percent != std::optional<std::uint8_t>(76) || !fresh->stale.empty()
       || !fresh->presence.empty() || !fresh->charge.empty()
       || sleeping->battery_percent != std::optional<std::uint8_t>(42)
       || sleeping->presence != "Asleep" || sleeping->stale != "No update for 14 minutes")
    {
        return failure(__LINE__);
    }

    // A reading from another boot has no age, so it can only say it is overdue.
    const auto future = make_plugin_information_view_model(status, inventory, controllers, 50);
    for(const auto& row : future.devices)
    {
        if(row.stable_id == sleeping_id && row.stale != "Update overdue")
        {
            return failure(__LINE__);
        }
    }

    const auto no_devices = make_plugin_information_view_model(status, {}, {}, now);
    if(no_devices.empty_state != std::optional<std::string>("No devices found on a supported receiver")
       || no_devices.attention.has_value())
    {
        return failure(__LINE__);
    }

    status.coordinator.worker_state = WorkerState::Recovering;
    status.coordinator.last_error = sdk::Error {
        sdk::ErrorCode::SocketConnect,
        "injected socket detail",
        std::nullopt,
    };
    // Nothing old on screen: the chip alone says it is connecting.
    const auto recovering = make_plugin_information_view_model(status, {}, {}, now);
    if(recovering.tone != PluginHealthTone::Warning || recovering.headline != "Connecting"
       || recovering.technical_detail != std::optional<std::string>("injected socket detail")
       || recovering.attention.has_value() || recovering.empty_state.has_value())
    {
        return failure(__LINE__);
    }
    // Old readings on screen: the banner says how old the newest one is.
    const auto recovering_with_readings
        = make_plugin_information_view_model(status, inventory, controllers, now);
    if(!recovering_with_readings.attention.has_value()
       || recovering_with_readings.attention->tone != PluginHealthTone::Warning
       || recovering_with_readings.attention->message
              != "Connecting to the HyperFlux bridge. These readings are from 13 minutes ago."
       || !recovering_with_readings.attention->command.empty())
    {
        return failure(__LINE__);
    }

    status.coordinator.worker_state = WorkerState::Failed;
    const auto failed = make_plugin_information_view_model(status, inventory, controllers, now);
    if(failed.tone != PluginHealthTone::Negative || failed.headline != "Needs attention"
       || !failed.attention.has_value() || failed.attention->tone != PluginHealthTone::Negative
       || failed.attention->message
              != "The HyperFlux bridge is not answering, so these readings are from 13 minutes ago. "
                 "Run hyperfluxctl doctor --explain in a terminal to see the fix."
       || failed.attention->command != "hyperfluxctl doctor --explain")
    {
        return failure(__LINE__);
    }
    const auto failed_empty = make_plugin_information_view_model(status, {}, {}, now);
    if(!failed_empty.attention.has_value()
       || failed_empty.attention->message
              != "The HyperFlux bridge is not answering. "
                 "Run hyperfluxctl doctor --explain in a terminal to see the fix.")
    {
        return failure(__LINE__);
    }

    // A bridge from another version: retrying cannot help, a restart can.
    const std::string mismatch_message = "This plugin and the running HyperFlux bridge are different versions. "
                                         "Restart the computer to finish the update.";
    for(const auto& [state, error] : {
            std::pair {WorkerState::Recovering, sdk::Error {sdk::ErrorCode::NegotiationFailed, "x", std::nullopt}},
            std::pair {WorkerState::Failed, sdk::Error {sdk::ErrorCode::RequiredFeatureMissing, "x", std::nullopt}},
            std::pair {WorkerState::Starting, sdk::Error {sdk::ErrorCode::ServerRejected, "x", "HFX-PROTOCOL-001"}},
            std::pair {WorkerState::Recovering, sdk::Error {sdk::ErrorCode::ServerRejected, "x", "HFX-PROTOCOL-002"}},
        })
    {
        status.coordinator.worker_state = state;
        status.coordinator.last_error = error;
        const auto mismatch = make_plugin_information_view_model(status, inventory, controllers, now);
        if(mismatch.tone != PluginHealthTone::Negative || mismatch.headline != "Needs attention"
           || !mismatch.attention.has_value() || mismatch.attention->message != mismatch_message
           || !mismatch.attention->command.empty()
           || mismatch.technical_detail != std::optional<std::string>("x"))
        {
            return failure(__LINE__);
        }
    }
    // Any other refusal is not a version problem, and a running plugin is fine.
    status.coordinator.worker_state = WorkerState::Recovering;
    status.coordinator.last_error = sdk::Error {sdk::ErrorCode::ServerRejected, "x", "HFX-QUEUE-001"};
    const auto other_refusal = make_plugin_information_view_model(status, {}, {}, now);
    status.coordinator.worker_state = WorkerState::Running;
    status.coordinator.last_error = sdk::Error {sdk::ErrorCode::NegotiationFailed, "x", std::nullopt};
    const auto recovered = make_plugin_information_view_model(status, inventory, controllers, now);
    if(other_refusal.headline != "Connecting" || recovered.headline != "Ready" || recovered.attention.has_value())
    {
        return failure(__LINE__);
    }

    status.coordinator.worker_state = WorkerState::Stopped;
    const auto stopped = make_plugin_information_view_model(status, inventory, controllers, now);
    status.loaded = false;
    const auto not_loaded = make_plugin_information_view_model(status, inventory, controllers, now);
    if(stopped.tone != PluginHealthTone::Neutral || stopped.headline != "Stopped"
       || stopped.attention.has_value() || not_loaded.tone != PluginHealthTone::Neutral
       || not_loaded.headline != "Not loaded" || not_loaded.technical_detail.has_value())
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
    std::vector<UpstreamRazerControllerSummary> native_controllers {
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
            PresenceState::Available,
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
            std::nullopt,
            PresenceState::Sleeping,
            "Wired",
        },
    };
    const auto native_model = make_plugin_information_view_model(
        native_status, native_controllers, 100 + minute);
    if(native_model.tone != PluginHealthTone::Positive || native_model.headline != "Ready"
       || native_model.devices.size() != 2 || native_model.attention.has_value())
    {
        return failure(__LINE__);
    }
    // Cards show the model name with the connection as a chip, never "TenKeyless".
    // A device OpenRGB drives itself takes the connection from OpenRGB's own
    // controller, whatever its receiver name says, and its presence is not shown.
    const auto& keyboard = native_model.devices[0];
    const auto& mouse = native_model.devices[1];
    if(keyboard.device != "Razer DeathStalker V2 Pro TKL" || keyboard.connection != "Wired"
       || keyboard.note != "OpenRGB controls it over the cable until it is unplugged."
       || !keyboard.presence.empty() || keyboard.battery_percent.has_value()
       || !keyboard.charge.empty() || mouse.device != "Razer Basilisk V3 Pro 35K"
       || mouse.connection != "Wireless" || !mouse.note.empty() || !mouse.presence.empty()
       || mouse.battery_percent != std::optional<std::uint8_t>(81) || mouse.charge != "Charging"
       || !mouse.stale.empty())
    {
        return failure(__LINE__);
    }

    // OpenRGB drives it through its own receiver, or its name does not say how.
    auto direct = native_controllers;
    for(const auto& [connection, chip] :
        {std::pair<std::string, std::string> {"Wireless", "Wireless"}, {"", ""}})
    {
        direct[1].direct_connection = connection;
        const auto model = make_plugin_information_view_model(native_status, direct, 100 + minute);
        if(model.devices[0].connection != chip
           || model.devices[0].note != "OpenRGB controls it directly, as its own entry in the device list."
           || !model.devices[0].presence.empty())
        {
            return failure(__LINE__);
        }
    }

    auto variant = native_controllers;
    variant[0].battery->percentage = hyperflux::test::number<BatteryPercent>(100);
    variant[0].presence = PresenceState::Sleeping;
    const auto full = make_plugin_information_view_model(native_status, variant, 100 + minute);
    if(full.devices[1].charge != "Full" || full.devices[1].presence != "Asleep")
    {
        return failure(__LINE__);
    }
    variant[0].battery->freshness = FreshnessState::Stale;
    variant[0].presence = PresenceState::Unavailable;
    const auto stale = make_plugin_information_view_model(native_status, variant, 100 + 3 * 60 * minute);
    if(!stale.devices[1].charge.empty() || stale.devices[1].stale != "No update for 3 hours"
       || stale.devices[1].presence != "Not reachable")
    {
        return failure(__LINE__);
    }

    native_status.coordinator.worker_state = NativeRouteWorkerState::Recovering;
    const auto reconnecting = make_plugin_information_view_model(native_status, {}, 100 + minute);
    if(reconnecting.headline != "Connecting" || reconnecting.attention.has_value())
    {
        return failure(__LINE__);
    }
    native_status.coordinator.last_error = sdk::Error {sdk::ErrorCode::ServerRejected, "x", "HFX-PROTOCOL-001"};
    const auto native_mismatch = make_plugin_information_view_model(native_status, native_controllers, 100 + minute);
    native_status.coordinator.last_error.reset();
    if(native_mismatch.headline != "Needs attention" || !native_mismatch.attention.has_value()
       || native_mismatch.attention->message != mismatch_message)
    {
        return failure(__LINE__);
    }
    native_status.coordinator.worker_state = NativeRouteWorkerState::Failed;
    const auto unreachable = make_plugin_information_view_model(
        native_status, native_controllers, 100 + 5 * minute);
    if(!unreachable.attention.has_value()
       || unreachable.attention->message.find("these readings are from 5 minutes ago")
              == std::string::npos
       || unreachable.attention->command != "hyperfluxctl doctor --explain")
    {
        return failure(__LINE__);
    }
#endif
    return EXIT_SUCCESS;
}
