// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/generated/domain_types.hpp>

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
    return 0;
}
