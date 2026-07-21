// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_native_fixture.hpp"

#include <hyperflux/openrgb/inventory_model.hpp>

#include <cstdlib>
#include <iostream>
#include <string>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-inventory-model-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb;
    using namespace hyperflux::test;

    auto source = native_integration_view(
        7,
        11,
        ControllerAvailability::Ready,
        ControllerAvailability::Sleeping);
    auto projected = project_inventory(source);
    if(!projected || projected.value().size() != 1
       || projected.value().front().generation_id.value() != 7
       || projected.value().front().devices.size() != 2
       || projected.value().front().devices[0].device_id.value() != "keyboard"
       || projected.value().front().devices[0].availability
           != InventoryAvailability::Sleeping
       || projected.value().front().devices[1].device_id.value() != "mouse"
       || projected.value().front().devices[1].availability
           != InventoryAvailability::Available)
    {
        return failure(__LINE__);
    }

    auto unqualified = copy_integration_view(source);
    auto unknown = unqualified.receivers.front().inventory.front();
    unknown.device_id = text<LogicalDeviceId>("unknown-child");
    unknown.product_id = number<ProductId>(0x0BAD);
    unknown.model_name.reset();
    unknown.profile.reset();
    unknown.support_level = SupportLevel::Identified;
    unqualified.receivers.front().inventory.push_back(std::move(unknown));
    auto retained = project_inventory(unqualified);
    if(!retained || retained.value().front().devices.size() != 3)
    {
        return failure(__LINE__);
    }

    auto duplicate_device = copy_integration_view(source);
    duplicate_device.receivers.front().inventory.push_back(
        duplicate_device.receivers.front().inventory.front());
    if(project_inventory(duplicate_device))
    {
        return failure(__LINE__);
    }

    auto duplicate_receiver = copy_integration_view(source);
    auto duplicate_receiver_source = copy_integration_view(source);
    duplicate_receiver.receivers.push_back(
        std::move(duplicate_receiver_source.receivers.front()));
    if(project_inventory(duplicate_receiver))
    {
        return failure(__LINE__);
    }

    return EXIT_SUCCESS;
}
