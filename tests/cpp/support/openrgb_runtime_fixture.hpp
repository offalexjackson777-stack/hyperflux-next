// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <hyperflux/openrgb/runtime_core.hpp>

#include <atomic>
#include <cstdint>
#include <deque>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <variant>
#include <vector>

namespace hyperflux::test
{

template <typename T> T text(std::string_view value)
{
    auto decoded = T::from(value);
    if(!decoded.has_value())
    {
        throw std::runtime_error("invalid test string domain value");
    }
    return *decoded;
}

template <typename T> T number(std::uint64_t value)
{
    auto decoded = T::from(static_cast<typename T::value_type>(value));
    if(!decoded.has_value())
    {
        throw std::runtime_error("invalid test numeric domain value");
    }
    return *decoded;
}

inline v5::RgbColor color(std::uint8_t red)
{
    return {
        number<ColorChannel>(red),
        number<ColorChannel>(0),
        number<ColorChannel>(0),
    };
}

inline v5::EventCursor cursor(std::uint64_t sequence)
{
    return {
        text<StreamId>("integration-stream"),
        number<StreamEpoch>(1),
        number<ProjectionRevision>(sequence),
        number<SequenceNumber>(sequence),
    };
}

inline v5::ControllerView controller(std::string_view device,
    DeviceKind kind,
    std::uint64_t generation,
    std::uint16_t product,
    std::uint16_t slots,
    ControllerAvailability availability)
{
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

inline v5::IntegrationView view(std::uint64_t generation,
    std::uint64_t sequence,
    ControllerAvailability mouse = ControllerAvailability::Ready,
    ControllerAvailability keyboard = ControllerAvailability::Ready)
{
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
                controller("keyboard", DeviceKind::Keyboard, generation, 662, 102, keyboard),
            },
        }},
    };
}

inline openrgb::QueuedLightingFrame frame(
    const openrgb::ControllerModel& controller_model, std::uint8_t red)
{
    return {
        controller_model.stable_id,
        controller_model.lighting.application_slot_count.value(),
        std::vector<v5::RgbColor>(
            controller_model.lighting.application_slot_count.value(), color(red)),
    };
}

inline const openrgb::ControllerModel& model(const openrgb::RuntimeCore& runtime, DeviceKind kind)
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

class FakeBridge final : public openrgb::RuntimeBridge
{
public:
    explicit FakeBridge(v5::IntegrationView initial)
        : current(std::move(initial))
    {
    }

    v5::IntegrationView current;
    std::deque<v5::EventBatch> event_batches;
    std::vector<sdk::TransactionSubmission> submissions;
    std::atomic_size_t submission_count {0};
    std::map<std::string, v5::TransactionResult> outcomes;
    std::vector<v5::ResourceKey> leased_resources;
    bool conflict = false;
    bool terminal_on_submit = false;
    bool unavailable_on_submit = false;
    std::size_t acquire_count = 0;
    std::size_t renew_count = 0;
    std::size_t release_count = 0;
    std::uint64_t lease_expiry_ms = 100'000;
    std::uint64_t connection_epoch_value = 1;
    std::optional<std::uint64_t> fail_integration_call;
    std::optional<std::uint64_t> fail_acquire_call;
    std::atomic_uint64_t integration_calls {0};

    std::uint64_t connection_epoch() const noexcept override
    {
        return connection_epoch_value;
    }

    sdk::Result<TransactionId> next_transaction_id() override
    {
        ++transaction_counter_;
        return sdk::Result<TransactionId>::success(
            text<TransactionId>("transaction-" + std::to_string(transaction_counter_)));
    }

    sdk::Result<v5::IntegrationView> integration_view() override
    {
        const auto call = integration_calls.fetch_add(1, std::memory_order_acq_rel) + 1;
        if(fail_integration_call == call)
        {
            return sdk::Result<v5::IntegrationView>::failure({
                sdk::ErrorCode::ReadFailed,
                "injected OpenRGB bridge interruption",
                "HFX-SERVICE-001",
            });
        }
        return sdk::Result<v5::IntegrationView>::success(current);
    }

    sdk::Result<v5::LeaseResult> acquire_lease(
        std::vector<v5::ResourceKey> resources, LeaseDurationMs) override
    {
        ++acquire_count;
        if(fail_acquire_call == acquire_count)
        {
            return sdk::Result<v5::LeaseResult>::failure({
                sdk::ErrorCode::SocketConnect,
                "injected lease transport interruption",
                "HFX-SERVICE-001",
            });
        }
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

    sdk::Result<v5::LeaseResult> renew_lease(LeaseId, LeaseDurationMs) override
    {
        ++renew_count;
        return sdk::Result<v5::LeaseResult>::success(
            v5::LeaseResultGranted {grant(LeaseState::Renewed)});
    }

    sdk::Result<v5::LeaseResult> release_lease(LeaseId) override
    {
        ++release_count;
        return sdk::Result<v5::LeaseResult>::success(
            v5::LeaseResultGranted {grant(LeaseState::Released)});
    }

    sdk::Result<v5::TransactionResult> submit_transaction(
        sdk::TransactionSubmission submission) override
    {
        submissions.push_back(submission);
        submission_count.fetch_add(1, std::memory_order_release);
        if(unavailable_on_submit)
        {
            return sdk::Result<v5::TransactionResult>::success(v5::TransactionResultUnavailable {{
                submission.transaction_id,
                ProtocolErrorKind::OutcomeUnknown,
                text<FindingId>("HFX-OUTCOME-001"),
            }});
        }
        if(terminal_on_submit)
        {
            return sdk::Result<v5::TransactionResult>::success(
                terminal(submission, TransactionState::Succeeded, submission.frames.size()));
        }
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

    sdk::Result<v5::TransactionResult> transaction_outcome(TransactionId transaction_id) override
    {
        return sdk::Result<v5::TransactionResult>::success(
            outcomes.at(std::string(transaction_id.value())));
    }

    sdk::Result<v5::EventBatch> subscribe(sdk::EventSubscription subscription) override
    {
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

    void complete_last(TransactionState state,
        std::uint16_t delivered,
        SideEffectCertainty certainty,
        bool live,
        std::optional<ProtocolErrorKind> error = std::nullopt)
    {
        const auto& submission = submissions.back();
        outcomes.insert_or_assign(std::string(submission.transaction_id.value()),
            terminal(submission, state, delivered, certainty, live, error));
    }

    void event(EventKind kind, bool gap = false)
    {
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
    v5::LeaseGrant grant(LeaseState state) const
    {
        return {
            text<LeaseId>("lease-1"),
            text<ClientId>("openrgb"),
            leased_resources,
            number<MonotonicMs>(lease_expiry_ms),
            state,
        };
    }

    v5::TransactionResult terminal(const sdk::TransactionSubmission& submission,
        TransactionState state,
        std::size_t delivered,
        SideEffectCertainty certainty = SideEffectCertainty::Committed,
        bool live = true,
        std::optional<ProtocolErrorKind> error = std::nullopt) const
    {
        return v5::TransactionResultTerminal {{
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
        }};
    }

    std::uint64_t transaction_counter_ = 0;
};

} // namespace hyperflux::test
