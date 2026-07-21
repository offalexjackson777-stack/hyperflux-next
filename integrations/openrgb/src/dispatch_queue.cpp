// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/dispatch_queue.hpp>

#include <algorithm>
#include <limits>
#include <set>
#include <utility>

namespace hyperflux::openrgb
{

DispatchQueue::DispatchQueue(DispatchQueueConfig config) : config_(config) {}

bool DispatchQueue::valid_frame(const QueuedLightingFrame& frame) const noexcept
{
    return !frame.stable_id.empty() && frame.expected_slot_count > 0
        && frame.colors.size() == frame.expected_slot_count;
}

std::uint64_t DispatchQueue::take_sequence() noexcept
{
    const auto current = next_sequence_;
    if(next_sequence_ != std::numeric_limits<std::uint64_t>::max())
    {
        ++next_sequence_;
    }
    return current;
}

EnqueueDisposition DispatchQueue::enqueue_effect(
    QueuedLightingFrame frame,
    std::uint64_t now_ms)
{
    if(!valid_frame(frame) || config_.effect_target_capacity == 0
       || config_.effect_window_ms == 0)
    {
        return EnqueueDisposition::RejectedInvalid;
    }
    const auto existing = effects_.find(frame.stable_id);
    if(existing != effects_.end())
    {
        existing->second = std::move(frame);
        return EnqueueDisposition::Coalesced;
    }
    if(effects_.size() >= config_.effect_target_capacity)
    {
        return EnqueueDisposition::RejectedCapacity;
    }
    auto stable_id = frame.stable_id;
    effects_.emplace(std::move(stable_id), std::move(frame));
    if(!effect_due_ms_.has_value())
    {
        effect_due_ms_ = now_ms > std::numeric_limits<std::uint64_t>::max()
                - config_.effect_window_ms
            ? std::numeric_limits<std::uint64_t>::max()
            : now_ms + config_.effect_window_ms;
    }
    return EnqueueDisposition::Accepted;
}

EnqueueDisposition DispatchQueue::enqueue_stable(
    sdk::LightingIntent intent,
    std::vector<QueuedLightingFrame> frames)
{
    if((intent != sdk::LightingIntent::Static && intent != sdk::LightingIntent::Off)
       || frames.empty() || config_.stable_capacity == 0
       || stable_.size() >= config_.stable_capacity)
    {
        return stable_.size() >= config_.stable_capacity
            ? EnqueueDisposition::RejectedCapacity
            : EnqueueDisposition::RejectedInvalid;
    }
    std::set<std::string> identities;
    for(const auto& frame : frames)
    {
        if(!valid_frame(frame) || !identities.insert(frame.stable_id).second)
        {
            return EnqueueDisposition::RejectedInvalid;
        }
    }
    for(const auto& frame : frames)
    {
        effects_.erase(frame.stable_id);
    }
    if(effects_.empty())
    {
        effect_due_ms_.reset();
    }
    stable_.push_back({take_sequence(), intent, std::move(frames)});
    return EnqueueDisposition::Accepted;
}

std::optional<DispatchBatch> DispatchQueue::pop_ready(std::uint64_t now_ms)
{
    if(!stable_.empty())
    {
        auto batch = std::move(stable_.front());
        stable_.pop_front();
        return batch;
    }
    if(effects_.empty() || !effect_due_ms_.has_value() || now_ms < *effect_due_ms_)
    {
        return std::nullopt;
    }
    std::vector<QueuedLightingFrame> frames;
    frames.reserve(effects_.size());
    for(auto& [stable_id, frame] : effects_)
    {
        (void)stable_id;
        frames.push_back(std::move(frame));
    }
    effects_.clear();
    effect_due_ms_.reset();
    return DispatchBatch {
        take_sequence(),
        sdk::LightingIntent::EffectFrame,
        std::move(frames),
    };
}

std::optional<DispatchBatch> DispatchQueue::preview_ready(std::uint64_t now_ms) const
{
    if(!stable_.empty())
    {
        return stable_.front();
    }
    if(effects_.empty() || !effect_due_ms_.has_value() || now_ms < *effect_due_ms_)
    {
        return std::nullopt;
    }
    std::vector<QueuedLightingFrame> frames;
    frames.reserve(effects_.size());
    for(const auto& [stable_id, frame] : effects_)
    {
        (void)stable_id;
        frames.push_back(frame);
    }
    return DispatchBatch {
        next_sequence_,
        sdk::LightingIntent::EffectFrame,
        std::move(frames),
    };
}

std::optional<std::uint64_t> DispatchQueue::next_effect_due_ms() const noexcept
{
    return effect_due_ms_;
}

std::size_t DispatchQueue::stable_size() const noexcept
{
    return stable_.size();
}

std::size_t DispatchQueue::effect_target_size() const noexcept
{
    return effects_.size();
}

bool DispatchQueue::empty() const noexcept
{
    return stable_.empty() && effects_.empty();
}

void DispatchQueue::clear() noexcept
{
    stable_.clear();
    effects_.clear();
    effect_due_ms_.reset();
}

void DispatchQueue::discard_controller(const std::string& stable_id)
{
    effects_.erase(stable_id);
    if(effects_.empty())
    {
        effect_due_ms_.reset();
    }
    for(auto batch = stable_.begin(); batch != stable_.end();)
    {
        auto& frames = batch->frames;
        frames.erase(
            std::remove_if(
                frames.begin(),
                frames.end(),
                [&stable_id](const QueuedLightingFrame& frame) {
                    return frame.stable_id == stable_id;
                }),
            frames.end());
        batch = frames.empty() ? stable_.erase(batch) : std::next(batch);
    }
}

} // namespace hyperflux::openrgb
