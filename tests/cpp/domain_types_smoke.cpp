// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/generated/domain_types.hpp>
#include <hyperflux/generated/profile_catalog.hpp>

#include <cassert>
#include <string_view>

int main()
{
    using namespace hyperflux;

    const auto battery = BatteryPercent::from(100);
    assert(battery.has_value());
    assert(battery->value() == 100);
    assert(!BatteryPercent::from(101).has_value());
    assert(!GenerationId::from(0).has_value());
    assert(ReceiverId::from("receiver-1").has_value());
    assert(!ReceiverId::from("").has_value());
    assert(to_string(DeviceKind::Keyboard) == std::string_view{"keyboard"});
    assert(to_string(ConnectionMode::Hyperflux24ghz) == std::string_view{"hyperflux-2.4ghz"});
    const auto* mouse = profiles::profile_by_id("child.razer.basilisk-v3-pro-35k.00cd");
    assert(mouse != nullptr);
    assert(mouse->device_kind == DeviceKind::Mouse);
    assert(mouse->lighting != nullptr);
    assert(mouse->lighting->application_index_to_carrier.size() == 13);
    assert(mouse->lighting->application_index_to_carrier[0] == 1);
    assert(profiles::profile_by_id("child.unknown") == nullptr);
    return 0;
}
