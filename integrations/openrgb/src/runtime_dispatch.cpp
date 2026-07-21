// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/runtime_core.hpp>

#include "runtime_internal.hpp"

#include <algorithm>
#include <map>
#include <optional>
#include <utility>
#include <variant>
#include <vector>

namespace hyperflux::openrgb
{

sdk::Result<void> RuntimeCore::poll_outcomes(RuntimeStep& output)
{
    std::vector<std::string> receivers;
    receivers.reserve(pending_.size());
    for(const auto& [key, pending] : pending_)
    {
        (void)pending;
        receivers.push_back(key);
    }
    for(const auto& key : receivers)
    {
        const auto current = pending_.find(key);
        if(current == pending_.end())
        {
            continue;
        }
        const auto pending = current->second;
        auto result = bridge_->transaction_outcome(pending.transaction_id);
        if(!result)
        {
            return sdk::Result<void>::failure(result.error());
        }
        consume_transaction_result(
            pending.sequence,
            pending.intent,
            pending.expected_frames,
            pending.receiver_id,
            pending.generation_id,
            result.value(),
            output);
    }
    return sdk::Result<void>::success();
}

void RuntimeCore::consume_transaction_result(
    std::uint64_t sequence,
    sdk::LightingIntent intent,
    std::uint16_t expected_frames,
    const ReceiverId& receiver_id,
    const GenerationId& generation_id,
    const v5::TransactionResult& result,
    RuntimeStep& output)
{
    if(const auto* progress = std::get_if<v5::TransactionResultProgress>(&result))
    {
        pending_.insert_or_assign(
            runtime_detail::receiver_key(receiver_id),
            PendingTransaction {
                sequence,
                intent,
                expected_frames,
                receiver_id,
                generation_id,
                progress->detail.transaction_id,
            });
        return;
    }
    pending_.erase(runtime_detail::receiver_key(receiver_id));
    if(const auto* terminal = std::get_if<v5::TransactionResultTerminal>(&result))
    {
        output.dispatch_outcomes.push_back({
            sequence,
            intent,
            terminal->detail.receiver_id,
            terminal->detail.generation_id,
            terminal->detail.transaction_id,
            runtime_detail::terminal_state(terminal->detail, expected_frames),
            terminal->detail.declared_frames.value(),
            terminal->detail.delivered_frames.value(),
            terminal->detail.side_effect_certainty,
            terminal->detail.live_write_executed,
            terminal->detail.error_kind,
            std::nullopt,
        });
        return;
    }
    const auto& unavailable = std::get<v5::TransactionResultUnavailable>(result).detail;
    output.dispatch_outcomes.push_back({
        sequence,
        intent,
        receiver_id,
        generation_id,
        unavailable.transaction_id,
        DispatchOutcomeState::Unavailable,
        expected_frames,
        0,
        SideEffectCertainty::None,
        false,
        unavailable.error_kind,
        std::nullopt,
    });
}

void RuntimeCore::dispatch_ready(std::uint64_t now_ms, RuntimeStep& output)
{
    const auto preview = queue_.preview_ready(now_ms);
    if(!preview.has_value())
    {
        return;
    }

    struct FrameValidation
    {
        const ControllerModel* controller;
        std::optional<sdk::Error> error;
    };

    std::map<std::string, std::pair<ReceiverId, GenerationId>> authorities;
    std::vector<FrameValidation> validations;
    validations.reserve(preview->frames.size());
    for(const auto& frame : preview->frames)
    {
        const auto* controller = runtime_detail::find_controller(
            controllers_,
            frame.stable_id);
        if(controller == nullptr)
        {
            validations.push_back({
                nullptr,
                runtime_detail::error(
                    sdk::ErrorCode::InvalidController,
                    "queued OpenRGB frame names a controller outside the canonical view",
                    "HFX-INTEGRATION-001"),
            });
            continue;
        }
        if(controller->availability != ControllerAvailability::Ready)
        {
            validations.push_back({
                controller,
                runtime_detail::error(
                    sdk::ErrorCode::InvalidController,
                    "queued OpenRGB frame targets a sleeping controller",
                    "HFX-LIFECYCLE-001"),
            });
            continue;
        }
        if(frame.expected_slot_count
               != controller->lighting.application_slot_count.value()
           || frame.colors.size() != frame.expected_slot_count)
        {
            validations.push_back({
                controller,
                runtime_detail::error(
                    sdk::ErrorCode::InvalidLightingFrame,
                    "queued OpenRGB frame no longer matches the controller topology",
                    "HFX-REQUEST-001"),
            });
            continue;
        }
        validations.push_back({controller, std::nullopt});
        authorities.insert_or_assign(
            runtime_detail::receiver_key(controller->authority.receiver_id),
            std::make_pair(
                controller->authority.receiver_id,
                controller->authority.generation_id));
    }
    const auto blocked = std::any_of(
        authorities.begin(),
        authorities.end(),
        [this](const auto& entry) { return pending_.contains(entry.first); });
    if(blocked)
    {
        return;
    }

    auto batch = queue_.pop_ready(now_ms);
    if(!batch.has_value())
    {
        return;
    }

    std::map<std::string, std::vector<QueuedLightingFrame>> groups;
    for(std::size_t index = 0; index < batch->frames.size(); ++index)
    {
        auto& frame = batch->frames[index];
        auto& validation = validations[index];
        if(validation.error.has_value())
        {
            output.dispatch_outcomes.push_back({
                batch->sequence,
                batch->intent,
                validation.controller == nullptr
                    ? std::optional<ReceiverId> {}
                    : std::optional<ReceiverId> {
                          validation.controller->authority.receiver_id},
                validation.controller == nullptr
                    ? std::optional<GenerationId> {}
                    : std::optional<GenerationId> {
                          validation.controller->authority.generation_id},
                std::nullopt,
                DispatchOutcomeState::Rejected,
                1,
                0,
                SideEffectCertainty::None,
                false,
                std::nullopt,
                std::move(validation.error),
            });
            continue;
        }
        groups[runtime_detail::receiver_key(
                   validation.controller->authority.receiver_id)]
            .push_back(std::move(frame));
    }
    for(auto& [key, frames] : groups)
    {
        const auto authority = authorities.at(key);
        auto* session = ensure_session(authority.first, authority.second, output);
        if(session == nullptr)
        {
            output.dispatch_outcomes.push_back({
                batch->sequence,
                batch->intent,
                authority.first,
                authority.second,
                std::nullopt,
                DispatchOutcomeState::Rejected,
                static_cast<std::uint16_t>(frames.size()),
                0,
                SideEffectCertainty::None,
                false,
                std::nullopt,
                output.notices.empty()
                    ? std::optional<sdk::Error> {}
                    : std::optional<sdk::Error> {output.notices.back()},
            });
            continue;
        }
        std::vector<sdk::LightingUpdate> updates;
        updates.reserve(frames.size());
        for(auto& frame : frames)
        {
            const auto* controller = runtime_detail::find_controller(
                controllers_,
                frame.stable_id);
            updates.push_back({controller->lighting_target, std::move(frame.colors)});
        }
        const auto deadline = MonotonicMs::from(runtime_detail::saturating_add(
            now_ms,
            config_.transaction_timeout_ms)).value();
        auto submitted = session->lighting.submit(
            batch->intent,
            std::move(updates),
            deadline);
        if(!submitted)
        {
            output.dispatch_outcomes.push_back({
                batch->sequence,
                batch->intent,
                authority.first,
                authority.second,
                std::nullopt,
                DispatchOutcomeState::Rejected,
                static_cast<std::uint16_t>(frames.size()),
                0,
                SideEffectCertainty::None,
                false,
                std::nullopt,
                submitted.error(),
            });
            continue;
        }
        consume_transaction_result(
            batch->sequence,
            batch->intent,
            static_cast<std::uint16_t>(frames.size()),
            authority.first,
            authority.second,
            submitted.value(),
            output);
    }
}

} // namespace hyperflux::openrgb
