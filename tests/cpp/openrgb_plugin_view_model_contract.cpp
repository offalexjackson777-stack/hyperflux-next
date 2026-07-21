// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_native_fixture.hpp"

#include <hyperflux/openrgb/plugin_view_model.hpp>

#include <cstdlib>
#include <iostream>
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

    auto projected = project_controllers(hyperflux::test::native_integration_view(1, 1));
    if(!projected || projected.value().size() != 2)
    {
        return failure(__LINE__);
    }
    auto controllers = std::move(projected).value();
    controllers[0].battery = {
        TelemetryAvailability::Reported,
        hyperflux::test::number<BatteryPercent>(76),
        FreshnessState::Fresh,
        EvidenceConfidence::Observed,
        hyperflux::test::number<MonotonicMs>(100),
    };
    controllers[1].availability = ControllerAvailability::Sleeping;
    controllers[1].battery = {
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
    const auto model = make_plugin_information_view_model(status, controllers);
    if(model.tone != PluginHealthTone::Positive || model.headline != "Ready"
       || model.summary != "2 controllers are available in OpenRGB."
       || model.controllers.size() != 2
       || model.effects_authority.find("official OpenRGB Effects plugin") == std::string::npos
       || model.build_identity.find("OpenRGB API 4") == std::string::npos)
    {
        return failure(__LINE__);
    }
    bool found_fresh = false;
    bool found_sleeping = false;
    for(const auto& row : model.controllers)
    {
        found_fresh = found_fresh || row.battery == "76%";
        found_sleeping = found_sleeping
            || (row.availability == "Sleeping"
                && row.battery == "42% - update overdue"
                && row.control == "Controlled by polychromatic");
    }
    if(!found_fresh || !found_sleeping)
    {
        return failure(__LINE__);
    }

    status.coordinator.worker_state = WorkerState::Recovering;
    status.coordinator.last_error = sdk::Error {
        sdk::ErrorCode::SocketConnect,
        "injected socket detail",
        std::nullopt,
    };
    const auto recovering = make_plugin_information_view_model(status, {});
    if(recovering.tone != PluginHealthTone::Warning
       || recovering.headline != "Connecting"
       || recovering.summary != "Waiting for the local HyperFlux bridge."
       || recovering.technical_detail != "injected socket detail")
    {
        return failure(__LINE__);
    }
    return EXIT_SUCCESS;
}
