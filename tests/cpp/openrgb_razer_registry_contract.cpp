// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/razer_registry.hpp>

#include <hyperflux/generated/profile_catalog.hpp>

#include <algorithm>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace
{

template<typename T>
T text(std::string_view value)
{
    auto decoded = T::from(value);
    if(!decoded.has_value())
    {
        throw std::runtime_error("invalid test string domain value");
    }
    return *decoded;
}

template<typename T>
T number(std::uint64_t value)
{
    auto decoded = T::from(static_cast<typename T::value_type>(value));
    if(!decoded.has_value())
    {
        throw std::runtime_error("invalid test numeric domain value");
    }
    return *decoded;
}

hyperflux::openrgb::ControllerModel model(std::string_view profile_id)
{
    using namespace hyperflux;
    const auto* profile = profiles::profile_by_id(profile_id);
    const auto* receiver = profiles::profile_by_id("receiver.razer.hyperflux-v2.1532-00cf");
    if(profile == nullptr || receiver == nullptr || profile->lighting == nullptr
       || profile->presentation == nullptr)
    {
        throw std::runtime_error("generated test profile is incomplete");
    }
    const auto receiver_id = text<ReceiverId>("receiver-1");
    const auto generation_id = number<GenerationId>(1);
    const auto device_id = text<LogicalDeviceId>(
        profile->device_kind == DeviceKind::Keyboard ? "keyboard" : "mouse");
    const auto endpoint_id = text<EndpointId>(
        profile->device_kind == DeviceKind::Keyboard ? "keyboard-wireless" : "mouse-wireless");
    const v5::ProfileBindingView receiver_binding {
        text<ProfileId>(receiver->id),
        text<ProfileDigest>(receiver->runtime_sha256),
    };
    const v5::ProfileBindingView device_binding {
        text<ProfileId>(profile->id),
        text<ProfileDigest>(profile->runtime_sha256),
    };
    const auto product_id_value = number<ProductId>(profile->product_id);
    const v5::PresentationView presentation {
        text<UpstreamId>(profile->presentation->upstream_id),
        text<UpstreamOwner>(profile->presentation->owner),
        text<ComponentVersion>(profile->presentation->project_version),
        text<SourceRevision>(profile->presentation->source_commit),
        text<PresentationKey>(profile->presentation->model_key),
        profile->presentation->layout_key.has_value()
            ? std::optional<PresentationKey>(text<PresentationKey>(*profile->presentation->layout_key))
            : std::nullopt,
        text<TransportVariant>(profile->presentation->transport_variant),
    };
    std::vector<CapabilityId> capabilities;
    capabilities.reserve(profile->capabilities.size());
    for(const auto& capability : profile->capabilities)
    {
        capabilities.push_back(text<CapabilityId>(capability.id));
    }
    const v5::LightingTopologyView topology {
        number<LedCount>(profile->lighting->physical_led_count),
        number<LedCount>(profile->lighting->application_slot_count),
        number<LedCount>(profile->lighting->rows),
        number<LedCount>(profile->lighting->columns),
    };
    const v5::ResourceKey resource {
        receiver_id,
        generation_id,
        device_id,
        ResourceKind::Lighting,
    };
    const sdk::LightingTarget target {
        receiver_id,
        generation_id,
        device_id,
        endpoint_id,
        receiver_binding,
        device_binding,
        topology.application_slot_count,
        resource,
    };
    return {
        std::string(receiver_id.value()) + "/" + std::string(device_id.value()) + "/"
            + std::string(device_binding.profile_id.value()),
        {receiver_id, generation_id, device_id, endpoint_id},
        profile->device_kind,
        product_id_value,
        text<ModelName>(profile->model_name),
        device_binding,
        presentation,
        ControllerAvailability::Ready,
        {
            TelemetryAvailability::Unavailable,
            std::nullopt,
            FreshnessState::Unknown,
            EvidenceConfidence::Unknown,
            std::nullopt,
        },
        std::move(capabilities),
        topology,
        {
            openrgb::ControllerOwnerState::Unowned,
            std::nullopt,
            std::nullopt,
            std::nullopt,
            {true, false, false},
        },
        target,
    };
}

} // namespace

int main()
{
    using namespace hyperflux;
    const auto mouse = openrgb::resolve_razer_presentation(
        model("child.razer.basilisk-v3-pro-35k.00cd"),
        openrgb::KeyboardLayoutVariant::AnsiQwerty);
    if(!mouse || mouse.value().model_name != "Razer Basilisk V3 Pro 35K (Wireless)"
       || mouse.value().zones.size() != 3 || mouse.value().leds.size() != 13
       || mouse.value().zones[0].name != "Scroll Wheel"
       || mouse.value().zones[1].name != "Logo"
       || mouse.value().zones[2].name != "LED Strip"
       || mouse.value().zones[2].slot_count != 11)
    {
        return 1;
    }

    const auto keyboard = openrgb::resolve_razer_presentation(
        model("child.razer.deathstalker-v2-pro-tkl.0296"),
        openrgb::KeyboardLayoutVariant::AnsiQwerty);
    if(!keyboard || keyboard.value().model_name != "Razer DeathStalker V2 Pro TKL (Wireless)"
       || keyboard.value().zones.size() != 1 || keyboard.value().leds.size() != 102
       || keyboard.value().zones.front().kind != openrgb::LayoutZoneKind::Matrix
       || keyboard.value().zones.front().rows != 6
       || keyboard.value().zones.front().columns != 17
       || keyboard.value().zones.front().matrix_map.size() != 102
       || std::count_if(
              keyboard.value().leds.begin(),
              keyboard.value().leds.end(),
              [](const openrgb::LayoutLed& led) { return led.physically_present; })
              != 84
       || std::none_of(
              keyboard.value().leds.begin(),
              keyboard.value().leds.end(),
              [](const openrgb::LayoutLed& led) { return led.name == "Key: 0"; }))
    {
        return 2;
    }

    auto drift = model("child.razer.basilisk-v3-pro-35k.00cd");
    drift.lighting.application_slot_count = number<LedCount>(12);
    const auto rejected_drift = openrgb::resolve_razer_presentation(
        drift,
        openrgb::KeyboardLayoutVariant::AnsiQwerty);
    if(rejected_drift || rejected_drift.error().finding_id != "HFX-INTEGRATION-001")
    {
        return 3;
    }

    auto unsupported = model("child.razer.basilisk-v3-pro-35k.00cd");
    unsupported.product_id = number<ProductId>(999);
    const auto rejected_unknown = openrgb::resolve_razer_presentation(
        unsupported,
        openrgb::KeyboardLayoutVariant::AnsiQwerty);
    if(rejected_unknown || rejected_unknown.error().code != sdk::ErrorCode::InvalidController)
    {
        return 4;
    }
    return 0;
}
