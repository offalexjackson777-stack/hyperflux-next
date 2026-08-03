// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/runtime_core.hpp>

#include "runtime_internal.hpp"

#include <algorithm>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <variant>
#include <vector>

namespace hyperflux::openrgb
{

sdk::Result<void> RuntimeCore::poll_outcomes(RuntimeStep& output)
{
    if(pending_.empty())
    {
        return sdk::Result<void>::success();
    }

    std::vector<std::string> keys;
    keys.reserve(pending_.size());
    for(const auto& [key, pending] : pending_)
    {
        (void)pending;
        keys.push_back(key);
    }
    if(outcome_poll_cursor_.has_value())
    {
        const auto start = std::upper_bound(
            keys.begin(),
            keys.end(),
            *outcome_poll_cursor_);
        std::rotate(keys.begin(), start, keys.end());
    }
    if(keys.size() > config_.max_outcomes_per_step)
    {
        keys.resize(config_.max_outcomes_per_step);
    }

    for(const auto& key : keys)
    {
        const auto found = pending_.find(key);
        if(found == pending_.end())
        {
            continue;
        }
        const auto pending = found->second;
        auto result = bridge_->transaction_outcome(pending.transaction_id);
        (void)synchronize_connection(output);
        if(!result)
        {
            return sdk::Result<void>::failure(result.error());
        }
        outcome_poll_cursor_ = key;
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
        record_dispatch_outcome({
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
        }, output);
        return;
    }
    const auto& unavailable = std::get<v5::TransactionResultUnavailable>(result).detail;
    record_dispatch_outcome({
        sequence,
        intent,
        receiver_id,
        generation_id,
        unavailable.transaction_id,
        DispatchOutcomeState::Unavailable,
        expected_frames,
        0,
        SideEffectCertainty::Possible,
        false,
        unavailable.error_kind,
        std::nullopt,
    }, output);
}

void RuntimeCore::dispatch_ready(std::uint64_t now_ms, RuntimeStep& output)
{
    std::set<std::string> deferred_receivers;
    for(std::size_t dispatch_index = 0;
        dispatch_index < config_.max_dispatches_per_step;
        ++dispatch_index)
    {
        std::set<std::string> blocked_receivers = deferred_receivers;
        for(const auto& [key, pending] : pending_)
        {
            (void)pending;
            blocked_receivers.insert(key);
        }
        const auto preview = queue_.preview_ready(now_ms, blocked_receivers);
        if(!preview.has_value())
        {
            return;
        }

        const auto binding = bind_request(*preview, now_ms, output);
        if(refresh_required_)
        {
            return;
        }
        if(binding.state == RequestBindState::Deferred)
        {
            for(const auto& target : preview->targets)
            {
                deferred_receivers.insert(runtime_detail::receiver_key(target.receiver_id));
            }
            continue;
        }
        if(binding.state == RequestBindState::Rejected)
        {
            const auto error = binding.error.value_or(runtime_detail::error(
                sdk::ErrorCode::SessionInactive,
                "OpenRGB request binding was rejected without a diagnostic",
                "HFX-OWNERSHIP-002"));
            discard_queued_request(
                preview->sequence,
                DispatchOutcomeState::Rejected,
                std::nullopt,
                error,
                output);
            continue;
        }

        struct FrameValidation
        {
            const ControllerModel* controller;
            std::optional<sdk::Error> error;
        };

        std::optional<std::pair<ReceiverId, GenerationId>> authority;
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
            if(controller->authority.receiver_id != frame.receiver_id
               || controller->authority.generation_id != frame.generation_id
               || frame.receiver_id != preview->receiver_id)
            {
                validations.push_back({
                    controller,
                    runtime_detail::error(
                        sdk::ErrorCode::InvalidController,
                        "queued OpenRGB frame no longer matches its receiver authority",
                        "HFX-GENERATION-001"),
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
            const auto candidate = std::make_pair(
                controller->authority.receiver_id,
                controller->authority.generation_id);
            if(authority.has_value() && *authority != candidate)
            {
                validations.push_back({
                    controller,
                    runtime_detail::error(
                        sdk::ErrorCode::InvalidController,
                        "one receiver-scoped OpenRGB batch crossed generation authority",
                        "HFX-GENERATION-001"),
                });
                continue;
            }
            authority = candidate;
            validations.push_back({controller, std::nullopt});
        }

        auto batch = queue_.pop_ready_for(preview->receiver_id, now_ms);
        if(!batch.has_value())
        {
            continue;
        }

        std::vector<sdk::LightingUpdate> updates;
        updates.reserve(batch->frames.size());
        for(std::size_t index = 0; index < batch->frames.size(); ++index)
        {
            auto& frame = batch->frames[index];
            auto& validation = validations[index];
            if(validation.error.has_value())
            {
                record_dispatch_outcome({
                    batch->sequence,
                    batch->intent,
                    batch->receiver_id,
                    frame.generation_id,
                    std::nullopt,
                    DispatchOutcomeState::Rejected,
                    1,
                    0,
                    SideEffectCertainty::None,
                    false,
                    std::nullopt,
                    std::move(validation.error),
                }, output);
                continue;
            }
            updates.push_back(
                {validation.controller->lighting_target, std::move(frame.colors)});
        }

        if(updates.empty() || !authority.has_value())
        {
            retire_completed_requests(now_ms, output);
            continue;
        }

        auto* session = session_for_request(batch->sequence);
        if(session == nullptr)
        {
            record_dispatch_outcome({
                batch->sequence,
                batch->intent,
                authority->first,
                authority->second,
                std::nullopt,
                DispatchOutcomeState::Rejected,
                static_cast<std::uint16_t>(updates.size()),
                0,
                SideEffectCertainty::None,
                false,
                std::nullopt,
                runtime_detail::error(
                    sdk::ErrorCode::SessionInactive,
                    "OpenRGB request has no active ownership session",
                    "HFX-OWNERSHIP-002"),
            }, output);
            retire_completed_requests(now_ms, output);
            continue;
        }
        const auto deadline = MonotonicMs::from(runtime_detail::saturating_add(
            now_ms,
            config_.transaction_timeout_ms)).value();
        const auto expected_frames = static_cast<std::uint16_t>(updates.size());
        auto submitted = session->lighting.submit(
            batch->intent,
            std::move(updates),
            deadline);
        (void)synchronize_connection(output);
        if(!submitted)
        {
            record_dispatch_outcome({
                batch->sequence,
                batch->intent,
                authority->first,
                authority->second,
                std::nullopt,
                DispatchOutcomeState::Rejected,
                expected_frames,
                0,
                SideEffectCertainty::None,
                false,
                std::nullopt,
                submitted.error(),
            }, output);
            retire_completed_requests(now_ms, output);
            continue;
        }
        consume_transaction_result(
            batch->sequence,
            batch->intent,
            expected_frames,
            authority->first,
            authority->second,
            submitted.value(),
            output);
        retire_completed_requests(now_ms, output);
    }
}

} // namespace hyperflux::openrgb
