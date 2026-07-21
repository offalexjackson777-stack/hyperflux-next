// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/runtime_core.hpp>

#include "runtime_internal.hpp"

#include <algorithm>
#include <utility>

namespace hyperflux::openrgb
{

void RuntimeCore::renew_sessions(std::uint64_t now_ms, RuntimeStep& output)
{
    const auto renew_before = runtime_detail::saturating_add(
        now_ms,
        config_.lease_renew_margin_ms);
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
    const auto key = runtime_detail::receiver_key(receiver_id);
    const auto targets = runtime_detail::ready_targets(
        controllers_,
        receiver_id,
        generation_id);
    if(targets.empty())
    {
        output.notices.push_back(runtime_detail::error(
            sdk::ErrorCode::InvalidController,
            "receiver generation has no ready OpenRGB lighting controllers",
            "HFX-INTEGRATION-001"));
        return nullptr;
    }
    auto existing = sessions_.find(key);
    if(existing != sessions_.end()
       && existing->second.generation_id == generation_id
       && existing->second.lighting.active()
       && runtime_detail::same_targets(existing->second.lighting.targets(), targets))
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
        const auto targets = runtime_detail::ready_targets(
            controllers_,
            iterator->second.receiver_id,
            iterator->second.generation_id);
        if(!targets.empty()
           && runtime_detail::same_targets(iterator->second.lighting.targets(), targets))
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

} // namespace hyperflux::openrgb
