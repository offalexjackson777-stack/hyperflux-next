// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/controller_model.hpp>

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

hyperflux::v5::ControllerView controller(
    std::string_view receiver,
    std::string_view device,
    hyperflux::DeviceKind kind,
    std::uint64_t generation,
    std::uint16_t product,
    std::uint16_t slots,
    std::string_view upstream = "openrgb")
{
    using namespace hyperflux;
    const auto receiver_id = text<ReceiverId>(receiver);
    const auto generation_id = number<GenerationId>(generation);
    const auto device_id = text<LogicalDeviceId>(device);
    const v5::ProfileBindingView receiver_profile {
        text<ProfileId>("receiver.razer.hyperflux-v2.00cf"),
        text<ProfileDigest>(std::string(64, 'a')),
    };
    const v5::ProfileBindingView device_profile {
        text<ProfileId>(std::string("child.test.") + std::string(device)),
        text<ProfileDigest>(std::string(64, 'b')),
    };
    return {
        receiver_id,
        generation_id,
        device_id,
        text<EndpointId>(std::string("endpoint-") + std::string(device)),
        kind,
        number<ProductId>(product),
        receiver_profile,
        device_profile,
        text<ModelName>(std::string("Test ") + std::string(device)),
        {
            text<UpstreamId>(upstream),
            text<UpstreamOwner>(upstream == "openrgb" ? "OpenRGB" : "OpenRazer"),
            text<ComponentVersion>(upstream == "openrgb" ? "1.0rc3" : "3.12.1"),
            text<SourceRevision>(
                upstream == "openrgb"
                    ? "6fbcf62d7694e7b92fd0a5884b40b92984fbd1b0"
                    : "6820f9da169d354bc7e6e93a0aa8683a6bb75792"),
            text<PresentationKey>(std::string(device) + "_device"),
            kind == DeviceKind::Keyboard
                ? std::optional<PresentationKey>(text<PresentationKey>("keyboard_layout"))
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
            number<LedCount>(slots),
            number<LedCount>(slots),
            number<LedCount>(1),
            number<LedCount>(slots),
        },
        {receiver_id, generation_id, device_id, ResourceKind::Lighting},
        v5::ControllerOwnershipUnowned {v5::UnownedController {}},
        {true, false, false},
    };
}

hyperflux::v5::IntegrationView view(std::vector<hyperflux::v5::ControllerView> controllers)
{
    using namespace hyperflux;
    return {
        {
            text<StreamId>("integration-stream"),
            number<StreamEpoch>(1),
            number<ProjectionRevision>(1),
            number<SequenceNumber>(1),
        },
        {{
            text<ReceiverId>("receiver-1"),
            number<GenerationId>(1),
            std::nullopt,
            std::nullopt,
            ReceiverLifecycleState::Active,
            false,
            RestoreState::Idle,
            {},
            std::move(controllers),
        }},
    };
}

} // namespace

int main()
{
    using namespace hyperflux;
    auto projected = openrgb::project_controllers(view({
        controller("receiver-1", "mouse", DeviceKind::Mouse, 1, 205, 13),
        controller("receiver-1", "keyboard", DeviceKind::Keyboard, 1, 662, 102),
        controller("receiver-1", "ignored", DeviceKind::Mouse, 1, 1, 1, "openrazer"),
    }));
    if(!projected || projected.value().size() != 2
       || projected.value()[0].authority.device_id.value() != "keyboard"
       || projected.value()[1].authority.device_id.value() != "mouse")
    {
        return 1;
    }

    const auto unchanged = openrgb::reconcile_controllers(projected.value(), projected.value());
    if(unchanged.size() != 2
       || unchanged[0].kind != openrgb::ReconcileKind::Retained
       || unchanged[1].kind != openrgb::ReconcileKind::Retained)
    {
        return 2;
    }

    auto reconnected_view = view({
        controller("receiver-1", "mouse", DeviceKind::Mouse, 2, 205, 13),
        controller("receiver-1", "keyboard", DeviceKind::Keyboard, 2, 662, 102),
    });
    reconnected_view.receivers.front().generation_id = number<GenerationId>(2);
    auto reconnected = openrgb::project_controllers(reconnected_view);
    const auto generation_delta =
        openrgb::reconcile_controllers(projected.value(), reconnected.value());
    if(generation_delta.size() != 2
       || generation_delta[0].kind != openrgb::ReconcileKind::StateUpdated
       || generation_delta[1].kind != openrgb::ReconcileKind::StateUpdated
       || generation_delta[0].before->authority == generation_delta[0].after->authority)
    {
        return 3;
    }

    auto changed_layout = reconnected.value();
    changed_layout.front().lighting.application_slot_count = number<LedCount>(84);
    changed_layout.front().lighting_target.application_slot_count = number<LedCount>(84);
    const auto presentation_delta =
        openrgb::reconcile_controllers(reconnected.value(), changed_layout);
    if(presentation_delta.front().kind != openrgb::ReconcileKind::PresentationReplaced)
    {
        return 4;
    }

    auto drift = view({controller("receiver-1", "mouse", DeviceKind::Mouse, 1, 205, 13)});
    drift.receivers.front().controllers.front().presentation.source_revision =
        text<SourceRevision>(std::string(40, 'f'));
    const auto rejected = openrgb::project_controllers(drift);
    if(rejected || rejected.error().code != sdk::ErrorCode::InvalidController
       || rejected.error().finding_id != "HFX-INTEGRATION-001")
    {
        return 5;
    }
    return 0;
}
