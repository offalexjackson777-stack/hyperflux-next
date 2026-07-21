// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "openrgb_runtime_fixture.hpp"

#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>

namespace hyperflux::test
{

struct NativeDeviceFixture
{
    DeviceKind kind;
    std::string_view device_id;
    std::uint16_t product_id;
    std::uint16_t physical_leds;
    std::uint16_t application_slots;
    std::uint16_t rows;
    std::uint16_t columns;
    std::string_view model_name;
    std::string_view model_key;
    std::optional<std::string_view> layout_key;
};

inline NativeDeviceFixture native_device_fixture(DeviceKind kind)
{
    if(kind == DeviceKind::Mouse)
    {
        return {
            DeviceKind::Mouse,
            "mouse",
            0x00CD,
            13,
            13,
            1,
            13,
            "Razer Basilisk V3 Pro 35K",
            "basilisk_v3_pro_35k_wireless_device",
            std::nullopt,
        };
    }
    if(kind == DeviceKind::Keyboard)
    {
        return {
            DeviceKind::Keyboard,
            "keyboard",
            0x0296,
            84,
            102,
            6,
            17,
            "Razer DeathStalker V2 Pro Tenkeyless",
            "deathstalker_v2_pro_tkl_wireless_device",
            "razer_deathstalker_v2_pro_tkl_layout",
        };
    }
    throw std::runtime_error("unsupported native OpenRGB fixture device kind");
}

inline v5::ControllerView native_controller_view(DeviceKind kind,
    std::uint64_t generation,
    ControllerAvailability availability = ControllerAvailability::Ready)
{
    const auto fixture = native_device_fixture(kind);
    const auto receiver_id = text<ReceiverId>("receiver-1");
    const auto generation_id = number<GenerationId>(generation);
    const auto device_id = text<LogicalDeviceId>(fixture.device_id);
    const v5::ProfileBindingView receiver_profile {
        text<ProfileId>("receiver.razer.hyperflux-v2.00cf"),
        text<ProfileDigest>(std::string(64, 'a')),
    };
    const v5::ProfileBindingView device_profile {
        text<ProfileId>(std::string("child.test.") + std::string(fixture.device_id)),
        text<ProfileDigest>(std::string(64, 'b')),
    };
    return {
        receiver_id,
        generation_id,
        device_id,
        text<EndpointId>(std::string("endpoint-") + std::string(fixture.device_id)),
        fixture.kind,
        number<ProductId>(fixture.product_id),
        receiver_profile,
        device_profile,
        text<ModelName>(fixture.model_name),
        {
            text<UpstreamId>("openrgb"),
            text<UpstreamOwner>("OpenRGB"),
            text<ComponentVersion>("1.0rc3"),
            text<SourceRevision>("6fbcf62d7694e7b92fd0a5884b40b92984fbd1b0"),
            text<PresentationKey>(fixture.model_key),
            fixture.layout_key.has_value()
                ? std::optional<PresentationKey>(text<PresentationKey>(*fixture.layout_key))
                : std::nullopt,
            text<TransportVariant>("wireless"),
        },
        availability,
        {
            TelemetryAvailability::Unavailable,
            std::nullopt,
            FreshnessState::Unknown,
            EvidenceConfidence::Unknown,
            std::nullopt,
        },
        {text<CapabilityId>("lighting.direct-frame")},
        {
            number<LedCount>(fixture.physical_leds),
            number<LedCount>(fixture.application_slots),
            number<LedCount>(fixture.rows),
            number<LedCount>(fixture.columns),
        },
        {
            receiver_id,
            generation_id,
            device_id,
            ResourceKind::Lighting,
        },
        v5::ControllerOwnershipUnowned {v5::UnownedController {}},
        {availability == ControllerAvailability::Ready, false, false},
    };
}

inline v5::IntegrationView native_integration_view(std::uint64_t generation,
    std::uint64_t sequence,
    ControllerAvailability mouse = ControllerAvailability::Ready,
    ControllerAvailability keyboard = ControllerAvailability::Ready)
{
    v5::IntegrationReceiverView receiver {
        text<ReceiverId>("receiver-1"),
        number<GenerationId>(generation),
        std::nullopt,
        std::nullopt,
        ReceiverLifecycleState::Active,
        false,
        RestoreState::Idle,
        {},
        {},
    };
    receiver.controllers.reserve(2);
    receiver.controllers.push_back(native_controller_view(DeviceKind::Mouse, generation, mouse));
    receiver.controllers.push_back(
        native_controller_view(DeviceKind::Keyboard, generation, keyboard));

    v5::IntegrationView result {cursor(sequence), {}};
    result.receivers.push_back(std::move(receiver));
    return result;
}

inline openrgb::ControllerModel native_controller_model(DeviceKind kind)
{
    auto projected = openrgb::project_controllers(native_integration_view(1, 1));
    if(!projected)
    {
        throw std::runtime_error("native OpenRGB fixture projection failed");
    }
    for(auto& model : projected.value())
    {
        if(model.device_kind == kind)
        {
            return std::move(model);
        }
    }
    throw std::runtime_error("native OpenRGB fixture projection omitted a controller");
}

} // namespace hyperflux::test
