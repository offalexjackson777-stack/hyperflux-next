// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/sdk.hpp>

#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <variant>
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

hyperflux::v5::RgbColor color(std::uint8_t red, std::uint8_t green, std::uint8_t blue)
{
    return {
        number<hyperflux::ColorChannel>(red),
        number<hyperflux::ColorChannel>(green),
        number<hyperflux::ColorChannel>(blue),
    };
}

hyperflux::v5::ControllerView controller(
    std::string_view device,
    std::uint64_t generation,
    std::uint16_t slots)
{
    using namespace hyperflux;
    const auto receiver_id = text<ReceiverId>("receiver-1");
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
        DeviceKind::Mouse,
        number<ProductId>(205),
        receiver_profile,
        device_profile,
        text<ModelName>("Test controller"),
        {
            text<UpstreamId>("openrgb"),
            text<UpstreamOwner>("OpenRGB"),
            text<ComponentVersion>("1.0rc3"),
            text<SourceRevision>(std::string(40, 'c')),
            text<PresentationKey>("test_device"),
            std::nullopt,
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

class FakeBridge final : public hyperflux::sdk::LightingBridge
{
public:
    bool conflict = false;
    std::optional<hyperflux::sdk::TransactionSubmission> submission;

    hyperflux::sdk::Result<hyperflux::TransactionId> next_transaction_id() override
    {
        return hyperflux::sdk::Result<hyperflux::TransactionId>::success(
            text<hyperflux::TransactionId>("transaction-1"));
    }

    hyperflux::sdk::Result<hyperflux::v5::LeaseResult> acquire_lease(
        std::vector<hyperflux::v5::ResourceKey> resources,
        hyperflux::LeaseDurationMs) override
    {
        using namespace hyperflux;
        if(conflict)
        {
            return sdk::Result<v5::LeaseResult>::success(v5::LeaseResultConflict {
                {
                    text<ClientId>("another-client"),
                    resources.front(),
                },
            });
        }
        resources_ = resources;
        return sdk::Result<v5::LeaseResult>::success(v5::LeaseResultGranted {
            grant(LeaseState::Granted),
        });
    }

    hyperflux::sdk::Result<hyperflux::v5::LeaseResult> renew_lease(
        hyperflux::LeaseId,
        hyperflux::LeaseDurationMs) override
    {
        return hyperflux::sdk::Result<hyperflux::v5::LeaseResult>::success(
            hyperflux::v5::LeaseResultGranted {grant(hyperflux::LeaseState::Renewed)});
    }

    hyperflux::sdk::Result<hyperflux::v5::LeaseResult> release_lease(
        hyperflux::LeaseId) override
    {
        return hyperflux::sdk::Result<hyperflux::v5::LeaseResult>::success(
            hyperflux::v5::LeaseResultGranted {grant(hyperflux::LeaseState::Released)});
    }

    hyperflux::sdk::Result<hyperflux::v5::TransactionResult> submit_transaction(
        hyperflux::sdk::TransactionSubmission value) override
    {
        using namespace hyperflux;
        submission = value;
        return sdk::Result<v5::TransactionResult>::success(v5::TransactionResultProgress {
            {
                text<RequestId>("request-1"),
                text<RequestDigest>(std::string(64, 'd')),
                value.transaction_id,
                value.receiver_id,
                value.generation_id,
                TransactionState::Queued,
                QueueAdmission::Enqueued,
                number<FrameCount>(value.frames.size()),
                number<DeliveredFrameCount>(0),
                SideEffectCertainty::None,
                false,
            },
        });
    }

    hyperflux::sdk::Result<hyperflux::v5::TransactionResult> transaction_outcome(
        hyperflux::TransactionId transaction_id) override
    {
        using namespace hyperflux;
        return sdk::Result<v5::TransactionResult>::success(v5::TransactionResultUnavailable {
            {
                std::move(transaction_id),
                ProtocolErrorKind::OutcomeUnknown,
                text<FindingId>("HFX-OUTCOME-001"),
            },
        });
    }

private:
    hyperflux::v5::LeaseGrant grant(hyperflux::LeaseState state) const
    {
        using namespace hyperflux;
        return {
            text<LeaseId>("lease-1"),
            text<ClientId>("openrgb"),
            resources_,
            number<MonotonicMs>(10'000),
            state,
        };
    }

    std::vector<hyperflux::v5::ResourceKey> resources_;
};

} // namespace

int main()
{
    using namespace hyperflux;
    const auto mouse = sdk::lighting_target(controller("mouse", 1, 2));
    if(!mouse || mouse.value().application_slot_count != number<LedCount>(2))
    {
        return 1;
    }

    FakeBridge bridge;
    auto session = sdk::LightingSession::acquire(
        bridge,
        {mouse.value()},
        number<LeaseDurationMs>(30'000));
    if(!session || !session.value().active() || session.value().lease_id() == nullptr)
    {
        return 2;
    }

    auto submitted = session.value().submit(
        sdk::LightingIntent::Static,
        {{mouse.value(), {color(255, 0, 0), color(0, 0, 255)}}},
        number<MonotonicMs>(5'000));
    if(!submitted || !bridge.submission.has_value()
       || bridge.submission->transaction_class != TransactionClass::StaticLighting
       || bridge.submission->stable_intents.size() != 1
       || bridge.submission->stable_intents.front().mode != StableLightingMode::Static
       || bridge.submission->frames.size() != 1
       || bridge.submission->frames.front().colors.size() != 2)
    {
        return 3;
    }

    const auto non_black_off = session.value().submit(
        sdk::LightingIntent::Off,
        {{mouse.value(), {color(0, 0, 0), color(1, 0, 0)}}},
        number<MonotonicMs>(5'000));
    if(non_black_off || non_black_off.error().code != sdk::ErrorCode::InvalidLightingFrame)
    {
        return 4;
    }

    const auto renewed = session.value().renew(number<LeaseDurationMs>(30'000));
    if(!renewed)
    {
        return 5;
    }
    const auto released = session.value().release();
    if(!released || session.value().active())
    {
        return 6;
    }
    const auto after_release = session.value().submit(
        sdk::LightingIntent::EffectFrame,
        {{mouse.value(), {color(0, 0, 0), color(0, 0, 0)}}},
        number<MonotonicMs>(5'000));
    if(after_release || after_release.error().code != sdk::ErrorCode::SessionInactive)
    {
        return 7;
    }

    const auto next_generation = sdk::lighting_target(controller("keyboard", 2, 2));
    auto mixed = sdk::LightingSession::acquire(
        bridge,
        {mouse.value(), next_generation.value()},
        number<LeaseDurationMs>(30'000));
    if(mixed || mixed.error().code != sdk::ErrorCode::MixedReceiverGeneration)
    {
        return 8;
    }

    FakeBridge contended;
    contended.conflict = true;
    auto conflict = sdk::LightingSession::acquire(
        contended,
        {mouse.value()},
        number<LeaseDurationMs>(30'000));
    if(conflict || conflict.error().code != sdk::ErrorCode::OwnershipConflict
       || conflict.error().finding_id != "HFX-OWNERSHIP-001")
    {
        return 9;
    }
    return 0;
}
