// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/runtime_core.hpp>

#include <cstdint>
#include <cstdlib>
#include <deque>
#include <map>
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

hyperflux::v5::RgbColor color(std::uint8_t red)
{
    return {
        number<hyperflux::ColorChannel>(red),
        number<hyperflux::ColorChannel>(0),
        number<hyperflux::ColorChannel>(0),
    };
}

hyperflux::v5::EventCursor cursor(std::uint64_t sequence)
{
    using namespace hyperflux;
    return {
        text<StreamId>("integration-stream"),
        number<StreamEpoch>(1),
        number<ProjectionRevision>(sequence),
        number<SequenceNumber>(sequence),
    };
}

hyperflux::v5::ControllerView controller(
    std::string_view device,
    hyperflux::DeviceKind kind,
    std::uint64_t generation,
    std::uint16_t product,
    std::uint16_t slots,
    hyperflux::ControllerAvailability availability)
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
        kind,
        number<ProductId>(product),
        receiver_profile,
        device_profile,
        text<ModelName>(std::string("Test ") + std::string(device)),
        {
            text<UpstreamId>("openrgb"),
            text<UpstreamOwner>("OpenRGB"),
            text<ComponentVersion>("1.0rc3"),
            text<SourceRevision>("6fbcf62d7694e7b92fd0a5884b40b92984fbd1b0"),
            text<PresentationKey>(std::string(device) + "_device"),
            kind == DeviceKind::Keyboard
                ? std::optional<PresentationKey>(text<PresentationKey>("keyboard_layout"))
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
            number<LedCount>(slots),
            number<LedCount>(slots),
            number<LedCount>(1),
            number<LedCount>(slots),
        },
        {receiver_id, generation_id, device_id, ResourceKind::Lighting},
        v5::ControllerOwnershipUnowned {v5::UnownedController {}},
        {availability == ControllerAvailability::Ready, false, false},
    };
}

hyperflux::v5::IntegrationView view(
    std::uint64_t generation,
    std::uint64_t sequence,
    hyperflux::ControllerAvailability mouse = hyperflux::ControllerAvailability::Ready,
    hyperflux::ControllerAvailability keyboard = hyperflux::ControllerAvailability::Ready)
{
    using namespace hyperflux;
    return {
        cursor(sequence),
        {{
            text<ReceiverId>("receiver-1"),
            number<GenerationId>(generation),
            std::nullopt,
            std::nullopt,
            ReceiverLifecycleState::Active,
            false,
            RestoreState::Idle,
            {},
            {
                controller("mouse", DeviceKind::Mouse, generation, 205, 13, mouse),
                controller(
                    "keyboard",
                    DeviceKind::Keyboard,
                    generation,
                    662,
                    102,
                    keyboard),
            },
        }},
    };
}

hyperflux::openrgb::QueuedLightingFrame frame(
    const hyperflux::openrgb::ControllerModel& controller_model,
    std::uint8_t red)
{
    return {
        controller_model.stable_id,
        controller_model.lighting.application_slot_count.value(),
        std::vector<hyperflux::v5::RgbColor>(
            controller_model.lighting.application_slot_count.value(),
            color(red)),
    };
}

class FakeBridge final : public hyperflux::openrgb::RuntimeBridge
{
public:
    explicit FakeBridge(hyperflux::v5::IntegrationView initial) : current(std::move(initial)) {}

    hyperflux::v5::IntegrationView current;
    std::deque<hyperflux::v5::EventBatch> event_batches;
    std::vector<hyperflux::sdk::TransactionSubmission> submissions;
    std::map<std::string, hyperflux::v5::TransactionResult> outcomes;
    std::vector<hyperflux::v5::ResourceKey> leased_resources;
    bool conflict = false;
    std::size_t acquire_count = 0;
    std::size_t renew_count = 0;
    std::size_t release_count = 0;
    std::uint64_t lease_expiry_ms = 100'000;

    hyperflux::sdk::Result<hyperflux::TransactionId> next_transaction_id() override
    {
        ++transaction_counter_;
        return hyperflux::sdk::Result<hyperflux::TransactionId>::success(
            text<hyperflux::TransactionId>(
                "transaction-" + std::to_string(transaction_counter_)));
    }

    hyperflux::sdk::Result<hyperflux::v5::IntegrationView> integration_view() override
    {
        return hyperflux::sdk::Result<hyperflux::v5::IntegrationView>::success(current);
    }

    hyperflux::sdk::Result<hyperflux::v5::LeaseResult> acquire_lease(
        std::vector<hyperflux::v5::ResourceKey> resources,
        hyperflux::LeaseDurationMs) override
    {
        using namespace hyperflux;
        ++acquire_count;
        if(conflict)
        {
            return sdk::Result<v5::LeaseResult>::success(v5::LeaseResultConflict {{
                text<ClientId>("another-client"),
                resources.front(),
            }});
        }
        leased_resources = std::move(resources);
        return sdk::Result<v5::LeaseResult>::success(
            v5::LeaseResultGranted {grant(LeaseState::Granted)});
    }

    hyperflux::sdk::Result<hyperflux::v5::LeaseResult> renew_lease(
        hyperflux::LeaseId,
        hyperflux::LeaseDurationMs) override
    {
        ++renew_count;
        return hyperflux::sdk::Result<hyperflux::v5::LeaseResult>::success(
            hyperflux::v5::LeaseResultGranted {grant(hyperflux::LeaseState::Renewed)});
    }

    hyperflux::sdk::Result<hyperflux::v5::LeaseResult> release_lease(
        hyperflux::LeaseId) override
    {
        ++release_count;
        return hyperflux::sdk::Result<hyperflux::v5::LeaseResult>::success(
            hyperflux::v5::LeaseResultGranted {grant(hyperflux::LeaseState::Released)});
    }

    hyperflux::sdk::Result<hyperflux::v5::TransactionResult> submit_transaction(
        hyperflux::sdk::TransactionSubmission submission) override
    {
        using namespace hyperflux;
        submissions.push_back(submission);
        const v5::TransactionResult result = v5::TransactionResultProgress {{
            text<RequestId>("request-submit"),
            text<RequestDigest>(std::string(64, 'd')),
            submission.transaction_id,
            submission.receiver_id,
            submission.generation_id,
            TransactionState::Queued,
            QueueAdmission::Enqueued,
            number<FrameCount>(submission.frames.size()),
            number<DeliveredFrameCount>(0),
            SideEffectCertainty::None,
            false,
        }};
        outcomes.insert_or_assign(std::string(submission.transaction_id.value()), result);
        return sdk::Result<v5::TransactionResult>::success(result);
    }

    hyperflux::sdk::Result<hyperflux::v5::TransactionResult> transaction_outcome(
        hyperflux::TransactionId transaction_id) override
    {
        return hyperflux::sdk::Result<hyperflux::v5::TransactionResult>::success(
            outcomes.at(std::string(transaction_id.value())));
    }

    hyperflux::sdk::Result<hyperflux::v5::EventBatch> subscribe(
        hyperflux::sdk::EventSubscription subscription) override
    {
        using namespace hyperflux;
        if(!event_batches.empty())
        {
            auto result = std::move(event_batches.front());
            event_batches.pop_front();
            return sdk::Result<v5::EventBatch>::success(std::move(result));
        }
        return sdk::Result<v5::EventBatch>::success({
            subscription.subscription_id.value_or(text<SubscriptionId>("subscription-1")),
            current.cursor,
            {},
            current.cursor.sequence,
            current.cursor.sequence,
            number<DroppedEventCount>(0),
            false,
            false,
        });
    }

    void complete_last(
        hyperflux::TransactionState state,
        std::uint16_t delivered,
        hyperflux::SideEffectCertainty certainty,
        bool live,
        std::optional<hyperflux::ProtocolErrorKind> error = std::nullopt)
    {
        using namespace hyperflux;
        const auto& submission = submissions.back();
        outcomes.insert_or_assign(
            std::string(submission.transaction_id.value()),
            v5::TransactionResultTerminal {{
                text<RequestId>("request-terminal"),
                text<RequestDigest>(std::string(64, 'e')),
                submission.transaction_id,
                submission.receiver_id,
                submission.generation_id,
                state,
                number<FrameCount>(submission.frames.size()),
                number<DeliveredFrameCount>(delivered),
                certainty,
                live,
                false,
                DeviceApplicationState::Unverified,
                number<SequenceNumber>(current.cursor.sequence.value() + 1),
                error,
                std::nullopt,
            }});
    }

    void event(hyperflux::EventKind kind, bool gap = false)
    {
        using namespace hyperflux;
        std::vector<v5::BridgeEvent> events;
        if(!gap)
        {
            events.push_back({
                current.cursor.sequence,
                kind,
                text<ReceiverId>("receiver-1"),
                current.receivers.front().generation_id,
                std::nullopt,
                std::nullopt,
                std::nullopt,
                std::nullopt,
            });
        }
        event_batches.push_back({
            text<SubscriptionId>("subscription-1"),
            current.cursor,
            std::move(events),
            current.cursor.sequence,
            current.cursor.sequence,
            number<DroppedEventCount>(gap ? 1 : 0),
            gap,
            false,
        });
    }

private:
    hyperflux::v5::LeaseGrant grant(hyperflux::LeaseState state) const
    {
        using namespace hyperflux;
        return {
            text<LeaseId>("lease-1"),
            text<ClientId>("openrgb"),
            leased_resources,
            number<MonotonicMs>(lease_expiry_ms),
            state,
        };
    }

    std::uint64_t transaction_counter_ = 0;
};

const hyperflux::openrgb::ControllerModel& model(
    const hyperflux::openrgb::RuntimeCore& runtime,
    hyperflux::DeviceKind kind)
{
    for(const auto& controller_model : runtime.controllers())
    {
        if(controller_model.device_kind == kind)
        {
            return controller_model;
        }
    }
    throw std::runtime_error("missing test controller");
}

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb;

    FakeBridge bridge(view(1, 1));
    auto invalid_config = RuntimeConfig {};
    invalid_config.lease_renew_margin_ms = invalid_config.lease_duration_ms;
    if(RuntimeCore::create(bridge, invalid_config))
    {
        return EXIT_FAILURE;
    }

    auto created = RuntimeCore::create(bridge);
    if(!created)
    {
        return EXIT_FAILURE;
    }
    auto runtime = std::move(created).value();
    const auto initialized = runtime.initialize();
    if(!initialized || !runtime.initialized() || runtime.controllers().size() != 2
       || initialized.value().controller_changes.size() != 2
       || initialized.value().controller_changes.front().kind != ReconcileKind::Added)
    {
        return EXIT_FAILURE;
    }
    const auto rescanned = runtime.rescan();
    if(!rescanned || rescanned.value().controller_changes.size() != 2
       || rescanned.value().controller_changes.front().kind != ReconcileKind::Retained)
    {
        return EXIT_FAILURE;
    }

    const auto mouse = frame(model(runtime, DeviceKind::Mouse), 1);
    const auto keyboard = frame(model(runtime, DeviceKind::Keyboard), 3);
    if(runtime.enqueue_effect(mouse, 100) != EnqueueDisposition::Accepted
       || runtime.enqueue_effect(frame(model(runtime, DeviceKind::Mouse), 2), 101)
           != EnqueueDisposition::Coalesced
       || runtime.enqueue_effect(keyboard, 102) != EnqueueDisposition::Accepted
       || !runtime.step(103) || bridge.submissions.size() != 0)
    {
        return EXIT_FAILURE;
    }
    const auto first_dispatch = runtime.step(104);
    if(!first_dispatch || bridge.acquire_count != 1 || bridge.leased_resources.size() != 2
       || bridge.submissions.size() != 1 || bridge.submissions.back().frames.size() != 2
       || runtime.pending_transaction_count() != 1)
    {
        return EXIT_FAILURE;
    }
    bool latest_mouse_frame = false;
    for(const auto& submitted_frame : bridge.submissions.back().frames)
    {
        if(submitted_frame.device_id.value() == "mouse")
        {
            latest_mouse_frame = submitted_frame.colors.front().red.value() == 2;
        }
    }
    if(!latest_mouse_frame)
    {
        return EXIT_FAILURE;
    }
    bridge.complete_last(
        TransactionState::Succeeded,
        2,
        SideEffectCertainty::Committed,
        true);
    const auto first_terminal = runtime.step(105);
    if(!first_terminal || first_terminal.value().dispatch_outcomes.size() != 1
       || first_terminal.value().dispatch_outcomes.front().state
           != DispatchOutcomeState::Succeeded
       || runtime.pending_transaction_count() != 0)
    {
        return EXIT_FAILURE;
    }

    if(runtime.enqueue_effect(frame(model(runtime, DeviceKind::Mouse), 10), 110)
           != EnqueueDisposition::Accepted
       || !runtime.step(114) || bridge.submissions.size() != 2
       || runtime.enqueue_effect(frame(model(runtime, DeviceKind::Mouse), 20), 114)
           != EnqueueDisposition::Accepted
       || runtime.enqueue_effect(frame(model(runtime, DeviceKind::Mouse), 21), 115)
           != EnqueueDisposition::Coalesced
       || runtime.enqueue_effect(frame(model(runtime, DeviceKind::Keyboard), 22), 115)
           != EnqueueDisposition::Accepted
       || !runtime.step(119) || bridge.submissions.size() != 2)
    {
        return EXIT_FAILURE;
    }
    bridge.complete_last(
        TransactionState::Succeeded,
        1,
        SideEffectCertainty::Committed,
        true);
    const auto coalesced_dispatch = runtime.step(120);
    if(!coalesced_dispatch || bridge.submissions.size() != 3
       || bridge.submissions.back().frames.size() != 2)
    {
        return EXIT_FAILURE;
    }
    latest_mouse_frame = false;
    for(const auto& submitted_frame : bridge.submissions.back().frames)
    {
        if(submitted_frame.device_id.value() == "mouse")
        {
            latest_mouse_frame = submitted_frame.colors.front().red.value() == 21;
        }
    }
    if(!latest_mouse_frame)
    {
        return EXIT_FAILURE;
    }
    bridge.complete_last(
        TransactionState::Succeeded,
        1,
        SideEffectCertainty::Committed,
        true);
    const auto incomplete_terminal = runtime.step(121);
    if(!incomplete_terminal || incomplete_terminal.value().dispatch_outcomes.size() != 1
       || incomplete_terminal.value().dispatch_outcomes.front().state
           != DispatchOutcomeState::Failed)
    {
        return EXIT_FAILURE;
    }

    bridge.current = view(2, 2);
    bridge.event(EventKind::GenerationReplaced);
    const auto generation = runtime.step(122);
    if(!generation || !generation.value().full_refresh
       || model(runtime, DeviceKind::Mouse).authority.generation_id.value() != 2)
    {
        return EXIT_FAILURE;
    }
    if(runtime.enqueue_stable(
           sdk::LightingIntent::Static,
           {frame(model(runtime, DeviceKind::Mouse), 30)})
           != EnqueueDisposition::Accepted)
    {
        return EXIT_FAILURE;
    }
    const auto new_generation_dispatch = runtime.step(123);
    if(!new_generation_dispatch || bridge.acquire_count != 2
       || bridge.submissions.back().generation_id.value() != 2)
    {
        return EXIT_FAILURE;
    }
    bridge.complete_last(
        TransactionState::Succeeded,
        1,
        SideEffectCertainty::Committed,
        true);
    if(!runtime.step(124))
    {
        return EXIT_FAILURE;
    }

    bridge.current = view(2, 3);
    bridge.event(EventKind::BatteryUpdated, true);
    const auto gap = runtime.step(125);
    if(!gap || !gap.value().cursor_gap_recovered || !gap.value().full_refresh)
    {
        return EXIT_FAILURE;
    }

    bridge.current = view(3, 4);
    bridge.event(EventKind::GenerationReplaced);
    if(!runtime.step(126))
    {
        return EXIT_FAILURE;
    }
    bridge.conflict = true;
    if(runtime.enqueue_stable(
           sdk::LightingIntent::Static,
           {frame(model(runtime, DeviceKind::Mouse), 40)})
           != EnqueueDisposition::Accepted)
    {
        return EXIT_FAILURE;
    }
    const auto conflicted = runtime.step(127);
    if(!conflicted || conflicted.value().dispatch_outcomes.size() != 1
       || conflicted.value().dispatch_outcomes.front().state
           != DispatchOutcomeState::Rejected
       || !conflicted.value().dispatch_outcomes.front().local_error.has_value()
       || conflicted.value().dispatch_outcomes.front().local_error->code
           != sdk::ErrorCode::OwnershipConflict)
    {
        return EXIT_FAILURE;
    }

    bridge.conflict = false;
    bridge.current = view(3, 5, ControllerAvailability::Sleeping);
    bridge.event(EventKind::DeviceSleeping);
    if(!runtime.step(128)
       || runtime.enqueue_effect(frame(model(runtime, DeviceKind::Mouse), 50), 128)
           != EnqueueDisposition::Accepted)
    {
        return EXIT_FAILURE;
    }
    const auto sleeping = runtime.step(132);
    if(!sleeping || sleeping.value().dispatch_outcomes.size() != 1
       || sleeping.value().dispatch_outcomes.front().state
           != DispatchOutcomeState::Rejected
       || !sleeping.value().dispatch_outcomes.front().local_error.has_value()
       || sleeping.value().dispatch_outcomes.front().local_error->finding_id
           != "HFX-LIFECYCLE-001")
    {
        return EXIT_FAILURE;
    }
    return EXIT_SUCCESS;
}
