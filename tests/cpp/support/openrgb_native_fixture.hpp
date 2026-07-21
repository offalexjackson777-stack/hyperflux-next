// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "openrgb_runtime_fixture.hpp"

#include <optional>
#include <string>
#include <string_view>

namespace hyperflux::test
{

inline openrgb::ControllerModel native_controller_model(DeviceKind kind)
{
    const bool keyboard = kind == DeviceKind::Keyboard;
    const std::string_view device = keyboard ? "keyboard" : "mouse";
    const auto product_id = keyboard ? 0x0296U : 0x00CDU;
    const auto physical_leds = keyboard ? 84U : 13U;
    const auto application_slots = keyboard ? 102U : 13U;
    const auto receiver_id = text<ReceiverId>("receiver-1");
    const auto generation_id = number<GenerationId>(1);
    const auto device_id = text<LogicalDeviceId>(device);
    const auto endpoint_id = text<EndpointId>(std::string("endpoint-") + std::string(device));
    const v5::ProfileBindingView receiver_profile {
        text<ProfileId>("receiver.razer.hyperflux-v2.00cf"),
        text<ProfileDigest>(std::string(64, 'a')),
    };
    const v5::ProfileBindingView device_profile {
        text<ProfileId>(std::string("child.test.") + std::string(device)),
        text<ProfileDigest>(std::string(64, 'b')),
    };
    const v5::ResourceKey resource {
        receiver_id,
        generation_id,
        device_id,
        ResourceKind::Lighting,
    };
    return {
        std::string("receiver-1/") + std::string(device) + "/child.test." + std::string(device),
        {receiver_id, generation_id, device_id, endpoint_id},
        kind,
        number<ProductId>(product_id),
        text<ModelName>(
            keyboard ? "Razer DeathStalker V2 Pro Tenkeyless" : "Razer Basilisk V3 Pro 35K"),
        device_profile,
        {
            text<UpstreamId>("openrgb"),
            text<UpstreamOwner>("OpenRGB"),
            text<ComponentVersion>("1.0rc3"),
            text<SourceRevision>("6fbcf62d7694e7b92fd0a5884b40b92984fbd1b0"),
            text<PresentationKey>(keyboard ? "deathstalker_v2_pro_tkl_wireless_device"
                                           : "basilisk_v3_pro_35k_wireless_device"),
            keyboard ? std::optional<PresentationKey>(
                           text<PresentationKey>("razer_deathstalker_v2_pro_tkl_layout"))
                     : std::nullopt,
            text<TransportVariant>("wireless"),
        },
        ControllerAvailability::Ready,
        {
            TelemetryAvailability::Unavailable,
            std::nullopt,
            FreshnessState::Unknown,
            EvidenceConfidence::Unknown,
            std::nullopt,
        },
        {text<CapabilityId>("lighting.direct-frame")},
        {
            number<LedCount>(physical_leds),
            number<LedCount>(application_slots),
            number<LedCount>(keyboard ? 6U : 1U),
            number<LedCount>(keyboard ? 17U : 13U),
        },
        {
            openrgb::ControllerOwnerState::Unowned,
            std::nullopt,
            std::nullopt,
            std::nullopt,
            {true, false, false},
        },
        {
            receiver_id,
            generation_id,
            device_id,
            endpoint_id,
            receiver_profile,
            device_profile,
            number<LedCount>(application_slots),
            resource,
        },
    };
}

} // namespace hyperflux::test
