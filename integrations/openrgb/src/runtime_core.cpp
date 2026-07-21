// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/runtime_core.hpp>

#include <algorithm>
#include <iterator>
#include <limits>
#include <string_view>
#include <utility>
#include <variant>

namespace hyperflux::openrgb
{
namespace
{

sdk::Error runtime_error(
    sdk::ErrorCode code,
    std::string message,
    std::string finding = "HFX-RUNTIME-001")
{
    return {code, std::move(message), std::move(finding)};
}

std::string receiver_key(const ReceiverId& receiver_id)
{
    return std::string(receiver_id.value());
}

std::uint64_t saturating_add(std::uint64_t left, std::uint64_t right) noexcept
{
    return left > std::numeric_limits<std::uint64_t>::max() - right
        ? std::numeric_limits<std::uint64_t>::max()
        : left + right;
}

const ControllerModel* find_controller(
    const std::vector<ControllerModel>& controllers,
    std::string_view stable_id)
{
    const auto found = std::find_if(
        controllers.begin(),
        controllers.end(),
        [stable_id](const ControllerModel& controller) {
            return controller.stable_id == stable_id;
        });
    return found == controllers.end() ? nullptr : &*found;
}

std::vector<sdk::LightingTarget> ready_targets(
    const std::vector<ControllerModel>& controllers,
    const ReceiverId& receiver_id,
    const GenerationId& generation_id)
{
    std::vector<sdk::LightingTarget> result;
    for(const auto& controller : controllers)
    {
        if(controller.authority.receiver_id == receiver_id
           && controller.authority.generation_id == generation_id
           && controller.availability == ControllerAvailability::Ready)
        {
            result.push_back(controller.lighting_target);
        }
    }
    return result;
}

bool refresh_event(EventKind kind) noexcept
{
    return kind != EventKind::TransactionCompleted
        && kind != EventKind::DiagnosticRaised;
}

DispatchOutcomeState terminal_state(
    const v5::TransactionTerminal& terminal,
    std::uint16_t expected_frames) noexcept
{
    const auto complete = terminal.state == TransactionState::Succeeded
        && terminal.declared_frames.value() == expected_frames
        && terminal.delivered_frames.value() == expected_frames
        && terminal.side_effect_certainty == SideEffectCertainty::Committed
        && terminal.live_write_executed;
    if(complete)
    {
        return DispatchOutcomeState::Succeeded;
    }
    if(terminal.state == TransactionState::Revoked)
    {
        return DispatchOutcomeState::Revoked;
    }
    if(terminal.state == TransactionState::Superseded)
    {
        return DispatchOutcomeState::Superseded;
    }
    return DispatchOutcomeState::Failed;
}

bool same_targets(
    const std::vector<sdk::LightingTarget>& left,
    const std::vector<sdk::LightingTarget>& right)
{
    return left == right;
}

} // namespace

RuntimeCore::RuntimeCore(RuntimeBridge& bridge, RuntimeConfig config)
    : bridge_(&bridge), config_(config), queue_(config.dispatch_queue)
{
}

sdk::Result<RuntimeCore> RuntimeCore::create(RuntimeBridge& bridge, RuntimeConfig config)
{
    const auto lease_duration = LeaseDurationMs::from(config.lease_duration_ms);
    const auto event_limit = EventBatchLimit::from(config.event_batch_limit);
    if(!lease_duration.has_value() || !event_limit.has_value()
       || config.lease_renew_margin_ms >= config.lease_duration_ms
       || config.transaction_timeout_ms == 0
       || config.max_event_batches_per_step == 0
       || config.dispatch_queue.stable_capacity == 0
       || config.dispatch_queue.effect_target_capacity == 0
       || config.dispatch_queue.effect_window_ms == 0)
    {
        return sdk::Result<RuntimeCore>::failure(runtime_error(
            sdk::ErrorCode::RuntimeConfiguration,
            "OpenRGB runtime configuration violates a bounded protocol limit"));
    }
    return sdk::Result<RuntimeCore>::success(RuntimeCore(bridge, config));
}

sdk::Result<RuntimeStep> RuntimeCore::initialize()
{
    RuntimeStep output;
    auto refreshed = refresh_controllers(output, false);
    if(!refreshed)
    {
        return sdk::Result<RuntimeStep>::failure(refreshed.error());
    }
    initialized_ = true;
    return sdk::Result<RuntimeStep>::success(std::move(output));
}

sdk::Result<RuntimeStep> RuntimeCore::rescan()
{
    if(!initialized_)
    {
        return sdk::Result<RuntimeStep>::failure(runtime_error(
            sdk::ErrorCode::RuntimeNotInitialized,
            "OpenRGB runtime must initialize before a rescan"));
    }
    RuntimeStep output;
    auto refreshed = refresh_controllers(output, false);
    if(!refreshed)
    {
        return sdk::Result<RuntimeStep>::failure(refreshed.error());
    }
    return sdk::Result<RuntimeStep>::success(std::move(output));
}

sdk::Result<RuntimeStep> RuntimeCore::step(std::uint64_t now_ms)
{
    if(!initialized_)
    {
        return sdk::Result<RuntimeStep>::failure(runtime_error(
            sdk::ErrorCode::RuntimeNotInitialized,
            "OpenRGB runtime must initialize before it can process work"));
    }
    RuntimeStep output;
    auto events = poll_events(output);
    if(!events)
    {
        return sdk::Result<RuntimeStep>::failure(events.error());
    }
    auto outcomes = poll_outcomes(output);
    if(!outcomes)
    {
        return sdk::Result<RuntimeStep>::failure(outcomes.error());
    }
    renew_sessions(now_ms, output);
    dispatch_ready(now_ms, output);
    return sdk::Result<RuntimeStep>::success(std::move(output));
}

EnqueueDisposition RuntimeCore::enqueue_effect(
    QueuedLightingFrame frame,
    std::uint64_t now_ms)
{
    return queue_.enqueue_effect(std::move(frame), now_ms);
}

EnqueueDisposition RuntimeCore::enqueue_stable(
    sdk::LightingIntent intent,
    std::vector<QueuedLightingFrame> frames)
{
    return queue_.enqueue_stable(intent, std::move(frames));
}

bool RuntimeCore::initialized() const noexcept
{
    return initialized_;
}

const std::vector<ControllerModel>& RuntimeCore::controllers() const noexcept
{
    return controllers_;
}

std::size_t RuntimeCore::pending_transaction_count() const noexcept
{
    return pending_.size();
}

sdk::Result<void> RuntimeCore::refresh_controllers(
    RuntimeStep& output,
    bool cursor_gap)
{
    auto view = bridge_->integration_view();
    if(!view)
    {
        return sdk::Result<void>::failure(view.error());
    }
    auto projected = project_controllers(view.value());
    if(!projected)
    {
        return sdk::Result<void>::failure(projected.error());
    }
    auto changes = reconcile_controllers(controllers_, projected.value());
    controllers_ = std::move(projected).value();
    cursor_ = view.value().cursor;
    if(cursor_gap)
    {
        subscription_id_.reset();
        output.cursor_gap_recovered = true;
    }
    output.full_refresh = true;
    output.controller_changes.insert(
        output.controller_changes.end(),
        std::make_move_iterator(changes.begin()),
        std::make_move_iterator(changes.end()));
    invalidate_changed_sessions();
    return sdk::Result<void>::success();
}

sdk::Result<void> RuntimeCore::poll_events(RuntimeStep& output)
{
    bool requires_refresh = false;
    for(std::size_t batch_index = 0;
        batch_index < config_.max_event_batches_per_step;
        ++batch_index)
    {
        auto batch = bridge_->subscribe({
            subscription_id_,
            cursor_,
            EventBatchLimit::from(config_.event_batch_limit).value(),
        });
        if(!batch)
        {
            return sdk::Result<void>::failure(batch.error());
        }
        subscription_id_ = batch.value().subscription_id;
        cursor_ = batch.value().next_cursor;
        if(batch.value().cursor_gap)
        {
            return refresh_controllers(output, true);
        }
        requires_refresh = requires_refresh
            || std::any_of(
                batch.value().events.begin(),
                batch.value().events.end(),
                [](const v5::BridgeEvent& event) { return refresh_event(event.kind); });
        if(!batch.value().has_more)
        {
            break;
        }
    }
    return requires_refresh ? refresh_controllers(output, false)
                            : sdk::Result<void>::success();
}

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

void RuntimeCore::renew_sessions(std::uint64_t now_ms, RuntimeStep& output)
{
    const auto renew_before = saturating_add(now_ms, config_.lease_renew_margin_ms);
    for(auto iterator = sessions_.begin(); iterator != sessions_.end();)
    {
        const auto* expires_at = iterator->second.lighting.expires_at_ms();
        if(expires_at == nullptr || expires_at->value() > renew_before)
        {
            ++iterator;
            continue;
        }
        auto renewed = iterator->second.lighting.renew(
            LeaseDurationMs::from(config_.lease_duration_ms).value());
        if(renewed)
        {
            ++iterator;
            continue;
        }
        output.notices.push_back(renewed.error());
        iterator->second.lighting.abandon();
        iterator = sessions_.erase(iterator);
    }
}

RuntimeCore::ReceiverSession* RuntimeCore::ensure_session(
    const ReceiverId& receiver_id,
    const GenerationId& generation_id,
    RuntimeStep& output)
{
    const auto key = receiver_key(receiver_id);
    const auto targets = ready_targets(controllers_, receiver_id, generation_id);
    if(targets.empty())
    {
        output.notices.push_back(runtime_error(
            sdk::ErrorCode::InvalidController,
            "receiver generation has no ready OpenRGB lighting controllers",
            "HFX-INTEGRATION-001"));
        return nullptr;
    }
    auto existing = sessions_.find(key);
    if(existing != sessions_.end()
       && existing->second.generation_id == generation_id
       && existing->second.lighting.active()
       && same_targets(existing->second.lighting.targets(), targets))
    {
        return &existing->second;
    }
    if(existing != sessions_.end())
    {
        if(existing->second.generation_id == generation_id)
        {
            auto released = existing->second.lighting.release();
            if(!released)
            {
                output.notices.push_back(released.error());
                existing->second.lighting.abandon();
            }
        }
        else
        {
            existing->second.lighting.abandon();
        }
        sessions_.erase(existing);
    }
    auto acquired = sdk::LightingSession::acquire(
        *bridge_,
        targets,
        LeaseDurationMs::from(config_.lease_duration_ms).value());
    if(!acquired)
    {
        output.notices.push_back(acquired.error());
        return nullptr;
    }
    auto inserted = sessions_.emplace(
        key,
        ReceiverSession {receiver_id, generation_id, std::move(acquired).value()});
    return &inserted.first->second;
}

void RuntimeCore::invalidate_changed_sessions()
{
    for(auto iterator = sessions_.begin(); iterator != sessions_.end();)
    {
        const auto targets = ready_targets(
            controllers_,
            iterator->second.receiver_id,
            iterator->second.generation_id);
        if(!targets.empty()
           && same_targets(iterator->second.lighting.targets(), targets))
        {
            ++iterator;
            continue;
        }
        const auto generation_still_exists = std::any_of(
            controllers_.begin(),
            controllers_.end(),
            [&iterator](const ControllerModel& controller) {
                return controller.authority.receiver_id == iterator->second.receiver_id
                    && controller.authority.generation_id
                        == iterator->second.generation_id;
            });
        if(generation_still_exists)
        {
            auto released = iterator->second.lighting.release();
            if(!released)
            {
                iterator->second.lighting.abandon();
            }
        }
        else
        {
            iterator->second.lighting.abandon();
        }
        iterator = sessions_.erase(iterator);
    }
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
            receiver_key(receiver_id),
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
    pending_.erase(receiver_key(receiver_id));
    if(const auto* terminal = std::get_if<v5::TransactionResultTerminal>(&result))
    {
        output.dispatch_outcomes.push_back({
            sequence,
            intent,
            terminal->detail.receiver_id,
            terminal->detail.generation_id,
            terminal->detail.transaction_id,
            terminal_state(terminal->detail, expected_frames),
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

    std::map<std::string, std::pair<ReceiverId, GenerationId>> authorities;
    std::optional<sdk::Error> invalid;
    std::optional<ReceiverId> invalid_receiver;
    std::optional<GenerationId> invalid_generation;
    for(const auto& frame : preview->frames)
    {
        const auto* controller = find_controller(controllers_, frame.stable_id);
        if(controller == nullptr)
        {
            invalid = runtime_error(
                sdk::ErrorCode::InvalidController,
                "queued OpenRGB frame names a controller outside the canonical view",
                "HFX-INTEGRATION-001");
            break;
        }
        invalid_receiver = controller->authority.receiver_id;
        invalid_generation = controller->authority.generation_id;
        if(controller->availability != ControllerAvailability::Ready)
        {
            invalid = runtime_error(
                sdk::ErrorCode::InvalidController,
                "queued OpenRGB frame targets a sleeping controller",
                "HFX-LIFECYCLE-001");
            break;
        }
        if(frame.expected_slot_count
               != controller->lighting.application_slot_count.value()
           || frame.colors.size() != frame.expected_slot_count)
        {
            invalid = runtime_error(
                sdk::ErrorCode::InvalidLightingFrame,
                "queued OpenRGB frame no longer matches the controller topology",
                "HFX-REQUEST-001");
            break;
        }
        authorities.insert_or_assign(
            receiver_key(controller->authority.receiver_id),
            std::make_pair(
                controller->authority.receiver_id,
                controller->authority.generation_id));
    }
    if(!invalid.has_value())
    {
        const auto blocked = std::any_of(
            authorities.begin(),
            authorities.end(),
            [this](const auto& entry) { return pending_.contains(entry.first); });
        if(blocked)
        {
            return;
        }
    }

    auto batch = queue_.pop_ready(now_ms);
    if(!batch.has_value())
    {
        return;
    }
    if(invalid.has_value())
    {
        output.dispatch_outcomes.push_back({
            batch->sequence,
            batch->intent,
            invalid_receiver,
            invalid_generation,
            std::nullopt,
            DispatchOutcomeState::Rejected,
            static_cast<std::uint16_t>(batch->frames.size()),
            0,
            SideEffectCertainty::None,
            false,
            std::nullopt,
            std::move(invalid),
        });
        return;
    }

    std::map<std::string, std::vector<QueuedLightingFrame>> groups;
    for(auto& frame : batch->frames)
    {
        const auto* controller = find_controller(controllers_, frame.stable_id);
        groups[receiver_key(controller->authority.receiver_id)].push_back(std::move(frame));
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
            const auto* controller = find_controller(controllers_, frame.stable_id);
            updates.push_back({controller->lighting_target, std::move(frame.colors)});
        }
        const auto deadline = MonotonicMs::from(
            saturating_add(now_ms, config_.transaction_timeout_ms)).value();
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
