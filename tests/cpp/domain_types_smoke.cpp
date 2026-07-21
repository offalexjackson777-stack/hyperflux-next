// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/generated/domain_types.hpp>
#include <hyperflux/generated/error_catalog.hpp>
#include <hyperflux/generated/profile_catalog.hpp>
#include <hyperflux/generated/protocol_types.hpp>
#include <hyperflux/generated/protocol_v1_types.hpp>
#include <hyperflux/generated/protocol_v2_types.hpp>
#include <hyperflux/generated/protocol_v3_types.hpp>
#include <hyperflux/generated/protocol_v4_types.hpp>
#include <hyperflux/generated/protocol_v5_types.hpp>

#include <string_view>

int main()
{
    using namespace hyperflux;

    const auto battery = BatteryPercent::from(100);
    if(!battery.has_value() || battery->value() != 100
       || BatteryPercent::from(101).has_value() || GenerationId::from(0).has_value()
       || !ReceiverId::from("receiver-1").has_value() || ReceiverId::from("").has_value())
    {
        return 1;
    }
    if(to_string(DeviceKind::Keyboard) != std::string_view{"keyboard"}
       || to_string(ConnectionMode::Hyperflux24ghz)
           != std::string_view{"hyperflux-2.4ghz"})
    {
        return 2;
    }
    const auto* mouse = profiles::profile_by_id("child.razer.basilisk-v3-pro-35k.00cd");
    if(mouse == nullptr || mouse->device_kind != DeviceKind::Mouse || mouse->lighting == nullptr
       || mouse->lighting->application_index_to_carrier.size() != 13
       || mouse->lighting->application_index_to_carrier[0] != 1
       || profiles::profile_by_id("child.unknown") != nullptr)
    {
        return 3;
    }
    static_assert(minimum_protocol_version == 5);
    static_assert(maximum_protocol_version == 5);
    static_assert(v1::minimum_protocol_version == 1);
    static_assert(v1::maximum_protocol_version == 1);
    static_assert(v2::minimum_protocol_version == 2);
    static_assert(v2::maximum_protocol_version == 2);
    static_assert(v3::minimum_protocol_version == 3);
    static_assert(v3::maximum_protocol_version == 3);
    static_assert(v4::minimum_protocol_version == 4);
    static_assert(v4::maximum_protocol_version == 4);
    static_assert(v5::minimum_protocol_version == 5);
    static_assert(v5::maximum_protocol_version == 5);
    if(methods[0].name != std::string_view{"negotiate"}
       || methods[0].required_feature.has_value())
    {
        return 4;
    }
    const auto* generation_error = errors::error_by_code(errors::ErrorCode::HfxGeneration001);
    if(generation_error == nullptr
       || generation_error->retry_policy != errors::RetryPolicy::AfterRemediation)
    {
        return 5;
    }
    return 0;
}
