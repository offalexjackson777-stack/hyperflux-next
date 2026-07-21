// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/runtime_core.hpp>

#include "runtime_internal.hpp"

#include <utility>

namespace hyperflux::openrgb
{

RuntimeCore::RuntimeCore(RuntimeBridge& bridge, RuntimeConfig config)
    : bridge_(&bridge),
      config_(config),
      queue_(config.dispatch_queue),
      connection_epoch_(bridge.connection_epoch())
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
        return sdk::Result<RuntimeCore>::failure(runtime_detail::error(
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
        return sdk::Result<RuntimeStep>::failure(runtime_detail::error(
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
        return sdk::Result<RuntimeStep>::failure(runtime_detail::error(
            sdk::ErrorCode::RuntimeNotInitialized,
            "OpenRGB runtime must initialize before it can process work"));
    }
    RuntimeStep output;
    auto events = poll_events(output);
    if(!events)
    {
        return sdk::Result<RuntimeStep>::failure(events.error());
    }
    auto refreshed = refresh_if_required(output);
    if(!refreshed)
    {
        return sdk::Result<RuntimeStep>::failure(refreshed.error());
    }
    auto outcomes = poll_outcomes(output);
    if(!outcomes)
    {
        return sdk::Result<RuntimeStep>::failure(outcomes.error());
    }
    refreshed = refresh_if_required(output);
    if(!refreshed)
    {
        return sdk::Result<RuntimeStep>::failure(refreshed.error());
    }
    renew_sessions(now_ms, output);
    refreshed = refresh_if_required(output);
    if(!refreshed)
    {
        return sdk::Result<RuntimeStep>::failure(refreshed.error());
    }
    dispatch_ready(now_ms, output);
    refreshed = refresh_if_required(output);
    if(!refreshed)
    {
        return sdk::Result<RuntimeStep>::failure(refreshed.error());
    }
    return sdk::Result<RuntimeStep>::success(std::move(output));
}

RuntimeStep RuntimeCore::shutdown()
{
    RuntimeStep output;
    for(auto& [key, session] : sessions_)
    {
        (void)key;
        auto released = session.lighting.release();
        if(!released)
        {
            output.notices.push_back(released.error());
            session.lighting.abandon();
        }
    }
    sessions_.clear();
    for(const auto& [key, pending] : pending_)
    {
        (void)key;
        output.dispatch_outcomes.push_back({
            pending.sequence,
            pending.intent,
            pending.receiver_id,
            pending.generation_id,
            pending.transaction_id,
            DispatchOutcomeState::Unavailable,
            pending.expected_frames,
            0,
            SideEffectCertainty::Possible,
            false,
            ProtocolErrorKind::OutcomeUnknown,
            runtime_detail::error(
                sdk::ErrorCode::SessionInactive,
                "OpenRGB runtime stopped before the transaction reached a terminal outcome",
                "HFX-OUTCOME-001"),
        });
    }
    pending_.clear();
    queue_.clear();
    subscription_id_.reset();
    cursor_.reset();
    refresh_required_ = false;
    initialized_ = false;
    return output;
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

std::size_t RuntimeCore::queued_stable_count() const noexcept
{
    return queue_.stable_size();
}

std::set<std::string> RuntimeCore::queued_effect_targets() const
{
    return queue_.effect_target_ids();
}

} // namespace hyperflux::openrgb
