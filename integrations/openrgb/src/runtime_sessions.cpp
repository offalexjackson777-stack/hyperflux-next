// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/runtime_core.hpp>

#include "runtime_internal.hpp"

#include <algorithm>
#include <limits>
#include <set>
#include <utility>
#include <vector>

namespace hyperflux::openrgb
{
namespace
{

bool contains_target(
    const std::vector<sdk::LightingTarget>& targets,
    const sdk::LightingTarget& requested)
{
    return std::find(targets.begin(), targets.end(), requested) != targets.end();
}

bool covers(
    const std::vector<sdk::LightingTarget>& owned,
    const std::vector<sdk::LightingTarget>& requested)
{
    return std::all_of(requested.begin(), requested.end(), [&owned](const auto& target) {
        return contains_target(owned, target);
    });
}

bool overlaps(
    const std::vector<sdk::LightingTarget>& left,
    const std::vector<sdk::LightingTarget>& right)
{
    return std::any_of(left.begin(), left.end(), [&right](const auto& target) {
        return contains_target(right, target);
    });
}

sdk::Result<std::vector<sdk::LightingTarget>> resolve_targets(
    const std::vector<DispatchTarget>& requested,
    const std::vector<ControllerModel>& controllers)
{
    std::vector<sdk::LightingTarget> result;
    result.reserve(requested.size());
    std::set<std::string> identities;
    for(const auto& target : requested)
    {
        const auto* controller = runtime_detail::find_controller(
            controllers,
            target.stable_id);
        if(controller == nullptr
           || controller->authority.receiver_id != target.receiver_id
           || !identities.insert(target.stable_id).second)
        {
            return sdk::Result<std::vector<sdk::LightingTarget>>::failure(
                runtime_detail::error(
                    sdk::ErrorCode::InvalidController,
                    "logical OpenRGB request no longer matches the canonical controller view",
                    "HFX-INTEGRATION-001"));
        }
        if(controller->authority.generation_id != target.generation_id)
        {
            return sdk::Result<std::vector<sdk::LightingTarget>>::failure(
                runtime_detail::error(
                    sdk::ErrorCode::MixedReceiverGeneration,
                    "logical OpenRGB request belongs to an earlier receiver generation",
                    "HFX-GENERATION-001"));
        }
        if(controller->lighting.application_slot_count.value()
           != target.expected_slot_count)
        {
            return sdk::Result<std::vector<sdk::LightingTarget>>::failure(
                runtime_detail::error(
                    sdk::ErrorCode::InvalidLightingFrame,
                    "logical OpenRGB request no longer matches controller topology",
                    "HFX-REQUEST-001"));
        }
        result.push_back(controller->lighting_target);
    }
    return sdk::Result<std::vector<sdk::LightingTarget>>::success(std::move(result));
}

bool session_matches_view(
    const sdk::LightingSession& session,
    const std::vector<ControllerModel>& controllers)
{
    const auto* lease_id = session.lease_id();
    if(lease_id == nullptr)
    {
        return false;
    }
    return std::all_of(
        session.targets().begin(),
        session.targets().end(),
        [&controllers, lease_id](const sdk::LightingTarget& target) {
            return std::any_of(
                controllers.begin(),
                controllers.end(),
                [&target, lease_id](const ControllerModel& controller) {
                    return controller.lighting_target == target
                        && controller.control.ownership
                            == ControllerOwnerState::OwnedByOpenRgb
                        && controller.control.lease_id == *lease_id;
                });
        });
}

bool generation_still_exists(
    const sdk::LightingSession& session,
    const std::vector<ControllerModel>& controllers)
{
    return std::all_of(
        session.targets().begin(),
        session.targets().end(),
        [&controllers](const sdk::LightingTarget& target) {
            return std::any_of(
                controllers.begin(),
                controllers.end(),
                [&target](const ControllerModel& controller) {
                    return controller.authority.receiver_id == target.receiver_id
                        && controller.authority.generation_id == target.generation_id;
                });
        });
}

} // namespace

void RuntimeCore::renew_sessions(std::uint64_t now_ms, RuntimeStep& output)
{
    for(auto iterator = sessions_.begin(); iterator != sessions_.end();)
    {
        const auto idle_deadline = runtime_detail::saturating_add(
            iterator->second.last_used_ms,
            config_.ownership_idle_ms);
        if(iterator->second.active_requests == 0 && now_ms >= idle_deadline)
        {
            auto released = iterator->second.lighting.release();
            if(synchronize_connection(output))
            {
                return;
            }
            if(!released)
            {
                output.notices.push_back(released.error());
                ++iterator;
                continue;
            }
            iterator = sessions_.erase(iterator);
            continue;
        }

        const auto renew_before = runtime_detail::saturating_add(
            now_ms,
            config_.lease_renew_margin_ms);
        const auto* expires_at = iterator->second.lighting.expires_at_ms();
        if(expires_at == nullptr || expires_at->value() > renew_before)
        {
            ++iterator;
            continue;
        }
        auto renewed = iterator->second.lighting.renew(
            LeaseDurationMs::from(config_.lease_duration_ms).value());
        if(synchronize_connection(output))
        {
            return;
        }
        if(renewed)
        {
            ++iterator;
            continue;
        }
        output.notices.push_back(renewed.error());
        const auto failed_session = iterator->first;
        iterator->second.lighting.abandon();
        iterator = sessions_.erase(iterator);
        for(auto request = active_requests_.begin(); request != active_requests_.end();)
        {
            if(request->second.session_id != failed_session)
            {
                ++request;
                continue;
            }
            discard_queued_request(
                request->first,
                DispatchOutcomeState::Revoked,
                std::nullopt,
                renewed.error(),
                output);
            ++request;
        }
    }
}

void RuntimeCore::discard_queued_request(
    std::uint64_t sequence,
    DispatchOutcomeState state,
    std::optional<ProtocolErrorKind> protocol_error,
    const sdk::Error& error,
    RuntimeStep& output)
{
    const auto discarded = queue_.discard_request(sequence);
    const auto tracked = active_requests_.contains(sequence);
    std::vector<DispatchOutcome> outcomes;
    outcomes.reserve(discarded.size());
    std::uint16_t expected_frames = 0;
    for(const auto& batch : discarded)
    {
        std::optional<GenerationId> generation;
        if(!batch.frames.empty())
        {
            const auto candidate = batch.frames.front().generation_id;
            const auto one_generation = std::all_of(
                batch.frames.begin(),
                batch.frames.end(),
                [&candidate](const QueuedLightingFrame& frame) {
                    return frame.generation_id == candidate;
                });
            if(one_generation)
            {
                generation = candidate;
            }
        }
        const auto frame_count = static_cast<std::uint16_t>(batch.frames.size());
        expected_frames = static_cast<std::uint16_t>(expected_frames + frame_count);
        DispatchOutcome outcome {
            batch.sequence,
            batch.intent,
            batch.receiver_id,
            generation,
            std::nullopt,
            state,
            frame_count,
            0,
            SideEffectCertainty::None,
            false,
            protocol_error,
            error,
        };
        outcomes.push_back(outcome);
        record_dispatch_outcome(std::move(outcome), output);
    }
    if(!tracked && !outcomes.empty())
    {
        emit_logical_outcome(
            sequence,
            outcomes.front().intent,
            static_cast<std::uint16_t>(outcomes.size()),
            expected_frames,
            outcomes,
            output);
    }
}

void RuntimeCore::record_dispatch_outcome(DispatchOutcome outcome, RuntimeStep& output)
{
    const auto active = active_requests_.find(outcome.sequence);
    if(active != active_requests_.end())
    {
        active->second.outcomes.push_back(outcome);
    }
    output.dispatch_outcomes.push_back(std::move(outcome));
}

void RuntimeCore::emit_logical_outcome(
    std::uint64_t sequence,
    sdk::LightingIntent intent,
    std::uint16_t expected_receivers,
    std::uint16_t expected_frames,
    const std::vector<DispatchOutcome>& outcomes,
    RuntimeStep& output)
{
    std::set<std::string> terminal_receivers;
    std::uint32_t declared_frames = 0;
    std::uint32_t delivered_frames = 0;
    bool live_write_executed = false;
    bool possible = false;
    bool partial = false;
    bool committed = false;
    std::optional<ProtocolErrorKind> protocol_error;
    std::optional<sdk::Error> local_error;
    for(const auto& outcome : outcomes)
    {
        if(outcome.receiver_id.has_value())
        {
            terminal_receivers.insert(std::string(outcome.receiver_id->value()));
        }
        declared_frames += outcome.declared_frames;
        delivered_frames += outcome.delivered_frames;
        live_write_executed = live_write_executed || outcome.live_write_executed;
        possible = possible
            || outcome.side_effect_certainty == SideEffectCertainty::Possible;
        partial = partial
            || outcome.side_effect_certainty == SideEffectCertainty::Partial;
        committed = committed
            || outcome.side_effect_certainty == SideEffectCertainty::Committed;
        if(outcome.state != DispatchOutcomeState::Succeeded)
        {
            if(!protocol_error.has_value() && outcome.protocol_error.has_value())
            {
                protocol_error = outcome.protocol_error;
            }
            if(!local_error.has_value() && outcome.local_error.has_value())
            {
                local_error = outcome.local_error;
            }
        }
    }

    const auto every_fragment_succeeded = !outcomes.empty()
        && std::all_of(outcomes.begin(), outcomes.end(), [](const DispatchOutcome& outcome) {
               return outcome.state == DispatchOutcomeState::Succeeded;
           });
    const auto complete = terminal_receivers.size() == expected_receivers
        && declared_frames == expected_frames;
    DispatchOutcomeState state = DispatchOutcomeState::Unavailable;
    if(complete && every_fragment_succeeded && delivered_frames == expected_frames)
    {
        state = DispatchOutcomeState::Succeeded;
    }
    else
    {
        const auto has_state = [&outcomes](DispatchOutcomeState candidate) {
            return std::any_of(
                outcomes.begin(), outcomes.end(), [candidate](const DispatchOutcome& outcome) {
                    return outcome.state == candidate;
                });
        };
        if(has_state(DispatchOutcomeState::Unavailable))
        {
            state = DispatchOutcomeState::Unavailable;
        }
        else if(has_state(DispatchOutcomeState::Failed))
        {
            state = DispatchOutcomeState::Failed;
        }
        else if(has_state(DispatchOutcomeState::Revoked))
        {
            state = DispatchOutcomeState::Revoked;
        }
        else if(has_state(DispatchOutcomeState::Rejected))
        {
            state = DispatchOutcomeState::Rejected;
        }
        else if(has_state(DispatchOutcomeState::Superseded))
        {
            state = DispatchOutcomeState::Superseded;
        }
    }

    if(!complete && !local_error.has_value())
    {
        local_error = runtime_detail::error(
            sdk::ErrorCode::SessionInactive,
            "logical OpenRGB request ended without every receiver fragment",
            "HFX-OUTCOME-001");
    }
    SideEffectCertainty certainty = SideEffectCertainty::None;
    if(possible)
    {
        certainty = SideEffectCertainty::Possible;
    }
    else if(partial || (delivered_frames > 0 && state != DispatchOutcomeState::Succeeded))
    {
        certainty = SideEffectCertainty::Partial;
    }
    else if(committed && delivered_frames > 0)
    {
        certainty = SideEffectCertainty::Committed;
    }

    output.logical_outcomes.push_back({
        sequence,
        intent,
        state,
        expected_receivers,
        static_cast<std::uint16_t>(terminal_receivers.size()),
        expected_frames,
        static_cast<std::uint16_t>(
            std::min<std::uint32_t>(delivered_frames, expected_frames)),
        certainty,
        live_write_executed,
        protocol_error,
        local_error,
    });
}

RuntimeCore::RequestBindResult RuntimeCore::bind_request(
    const DispatchBatch& request,
    std::uint64_t now_ms,
    RuntimeStep& output)
{
    const auto active = active_requests_.find(request.sequence);
    if(active != active_requests_.end())
    {
        return sessions_.contains(active->second.session_id)
            ? RequestBindResult {RequestBindState::Ready, std::nullopt}
            : RequestBindResult {
                  RequestBindState::Rejected,
                  runtime_detail::error(
                      sdk::ErrorCode::SessionInactive,
                      "logical OpenRGB request lost its ownership session",
                      "HFX-OWNERSHIP-002"),
              };
    }

    auto resolved = resolve_targets(request.targets, controllers_);
    if(!resolved)
    {
        return {RequestBindState::Rejected, resolved.error()};
    }
    const auto& targets = resolved.value();

    if(request.intent != sdk::LightingIntent::EffectFrame)
    {
        const auto unavailable = std::find_if(
            request.targets.begin(),
            request.targets.end(),
            [this](const DispatchTarget& target) {
                const auto* controller = runtime_detail::find_controller(
                    controllers_, target.stable_id);
                return controller == nullptr
                    || controller->availability != ControllerAvailability::Ready;
            });
        if(unavailable != request.targets.end())
        {
            return {
                RequestBindState::Rejected,
                runtime_detail::error(
                    sdk::ErrorCode::InvalidController,
                    "stable OpenRGB request includes a controller that is not ready",
                    "HFX-LIFECYCLE-001"),
            };
        }
    }

    auto selected = sessions_.end();
    for(auto iterator = sessions_.begin(); iterator != sessions_.end(); ++iterator)
    {
        if(covers(iterator->second.lighting.targets(), targets)
           && (selected == sessions_.end()
               || iterator->second.lighting.targets().size()
                   < selected->second.lighting.targets().size()))
        {
            selected = iterator;
        }
    }

    if(selected == sessions_.end())
    {
        for(const auto& [session_id, session] : sessions_)
        {
            (void)session_id;
            if(session.active_requests != 0
               && overlaps(session.lighting.targets(), targets))
            {
                return {RequestBindState::Deferred, std::nullopt};
            }
        }

        for(auto iterator = sessions_.begin(); iterator != sessions_.end();)
        {
            if(iterator->second.active_requests != 0
               || !overlaps(iterator->second.lighting.targets(), targets))
            {
                ++iterator;
                continue;
            }
            auto released = iterator->second.lighting.release();
            if(synchronize_connection(output))
            {
                return {RequestBindState::Deferred, std::nullopt};
            }
            if(!released)
            {
                output.notices.push_back(released.error());
                return {RequestBindState::Deferred, std::nullopt};
            }
            iterator = sessions_.erase(iterator);
        }

        auto acquired = sdk::LightingSession::acquire(
            *bridge_,
            std::move(resolved).value(),
            LeaseDurationMs::from(config_.lease_duration_ms).value());
        if(synchronize_connection(output))
        {
            return {RequestBindState::Deferred, std::nullopt};
        }
        if(!acquired)
        {
            if(sdk::is_connection_error(acquired.error().code))
            {
                output.notices.push_back(acquired.error());
                return {RequestBindState::Deferred, std::nullopt};
            }
            return {RequestBindState::Rejected, acquired.error()};
        }
        if(next_ownership_session_id_ == std::numeric_limits<std::uint64_t>::max())
        {
            auto lease = std::move(acquired).value();
            (void)lease.release();
            return {
                RequestBindState::Rejected,
                runtime_detail::error(
                    sdk::ErrorCode::RuntimeConfiguration,
                    "OpenRGB ownership session identity space is exhausted"),
            };
        }
        const auto session_id = next_ownership_session_id_++;
        selected = sessions_
                       .emplace(
                           session_id,
                           OwnershipSession {
                               std::move(acquired).value(),
                               0,
                               now_ms,
                           })
                       .first;
    }

    std::set<std::string> receivers;
    for(const auto& target : request.targets)
    {
        receivers.insert(runtime_detail::receiver_key(target.receiver_id));
    }
    ++selected->second.active_requests;
    selected->second.last_used_ms = now_ms;
    active_requests_.emplace(
        request.sequence,
        ActiveRequest {
            selected->first,
            request.intent,
            static_cast<std::uint16_t>(receivers.size()),
            static_cast<std::uint16_t>(request.targets.size()),
            {},
        });
    return {RequestBindState::Ready, std::nullopt};
}

RuntimeCore::OwnershipSession* RuntimeCore::session_for_request(std::uint64_t sequence)
{
    const auto request = active_requests_.find(sequence);
    if(request == active_requests_.end())
    {
        return nullptr;
    }
    const auto session = sessions_.find(request->second.session_id);
    return session == sessions_.end() ? nullptr : &session->second;
}

void RuntimeCore::retire_completed_requests(std::uint64_t now_ms, RuntimeStep& output)
{
    for(auto request = active_requests_.begin(); request != active_requests_.end();)
    {
        const auto pending = std::any_of(
            pending_.begin(),
            pending_.end(),
            [&request](const auto& entry) {
                return entry.second.sequence == request->first;
            });
        if(queue_.contains_sequence(request->first) || pending)
        {
            ++request;
            continue;
        }
        const auto session = sessions_.find(request->second.session_id);
        if(session != sessions_.end())
        {
            if(session->second.active_requests == 0)
            {
                output.notices.push_back(runtime_detail::error(
                    sdk::ErrorCode::SessionInactive,
                    "OpenRGB ownership request accounting underflowed",
                    "HFX-OWNERSHIP-002"));
            }
            else
            {
                --session->second.active_requests;
                session->second.last_used_ms = now_ms;
            }
        }
        emit_logical_outcome(
            request->first,
            request->second.intent,
            request->second.expected_receivers,
            request->second.expected_frames,
            request->second.outcomes,
            output);
        request = active_requests_.erase(request);
    }
}

void RuntimeCore::invalidate_changed_sessions(RuntimeStep& output)
{
    for(auto iterator = sessions_.begin(); iterator != sessions_.end();)
    {
        if(session_matches_view(iterator->second.lighting, controllers_))
        {
            ++iterator;
            continue;
        }
        const auto invalid_session = iterator->first;
        if(generation_still_exists(iterator->second.lighting, controllers_))
        {
            auto released = iterator->second.lighting.release();
            if(!released)
            {
                output.notices.push_back(released.error());
                iterator->second.lighting.abandon();
            }
        }
        else
        {
            iterator->second.lighting.abandon();
        }
        iterator = sessions_.erase(iterator);

        for(auto request = active_requests_.begin(); request != active_requests_.end();)
        {
            if(request->second.session_id != invalid_session)
            {
                ++request;
                continue;
            }
            discard_queued_request(
                request->first,
                DispatchOutcomeState::Revoked,
                ProtocolErrorKind::StaleGeneration,
                runtime_detail::error(
                    sdk::ErrorCode::SessionInactive,
                    "OpenRGB request was revoked by a controller authority change",
                    "HFX-GENERATION-001"),
                output);
            ++request;
        }
    }
}

} // namespace hyperflux::openrgb
