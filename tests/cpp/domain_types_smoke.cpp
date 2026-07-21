// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/generated/domain_types.hpp>
#include <hyperflux/generated/error_catalog.hpp>
#include <hyperflux/generated/profile_catalog.hpp>
#include <hyperflux/generated/protocol_types.hpp>
#include <hyperflux/generated/protocol_v1_types.hpp>
#include <hyperflux/generated/protocol_v2_types.hpp>
#include <hyperflux/generated/protocol_v3_types.hpp>
#include <hyperflux/generated/protocol_v4_types.hpp>

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
    static_assert(minimum_protocol_version == 4);
    static_assert(maximum_protocol_version == 4);
    static_assert(v1::minimum_protocol_version == 1);
    static_assert(v1::maximum_protocol_version == 1);
    static_assert(v2::minimum_protocol_version == 2);
    static_assert(v2::maximum_protocol_version == 2);
    static_assert(v3::minimum_protocol_version == 3);
    static_assert(v3::maximum_protocol_version == 3);
    static_assert(v4::minimum_protocol_version == 4);
    static_assert(v4::maximum_protocol_version == 4);
    assert(methods[0].name == std::string_view{"negotiate"});
    assert(!methods[0].required_feature.has_value());
    const auto* generation_error = errors::error_by_code(errors::ErrorCode::HfxGeneration001);
    assert(generation_error != nullptr);
    assert(generation_error->retry_policy == errors::RetryPolicy::AfterRemediation);
    return 0;
}
