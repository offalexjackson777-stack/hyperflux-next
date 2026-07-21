// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/dispatch_queue.hpp>

#include <algorithm>
#include <limits>
#include <set>
#include <string>
#include <utility>

namespace hyperflux::openrgb
{
namespace
{

std::string receiver_key(const ReceiverId& receiver_id)
{
    return std::string(receiver_id.value());
}

std::uint64_t due_at(std::uint64_t now_ms, std::uint64_t window_ms) noexcept
{
    return now_ms > std::numeric_limits<std::uint64_t>::max() - window_ms
        ? std::numeric_limits<std::uint64_t>::max()
        : now_ms + window_ms;
}

} // namespace

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
    const auto key = receiver_key(frame.receiver_id);
    for(auto& [existing_key, group] : effects_)
    {
        const auto existing = group.frames.find(frame.stable_id);
        if(existing == group.frames.end())
        {
            continue;
        }
        if(existing_key != key)
        {
            return EnqueueDisposition::RejectedInvalid;
        }
        existing->second = std::move(frame);
        return EnqueueDisposition::Coalesced;
    }
    if(effect_target_size_ >= config_.effect_target_capacity)
    {
        return EnqueueDisposition::RejectedCapacity;
    }
    auto stable_id = frame.stable_id;
    auto group = effects_.find(key);
    if(group == effects_.end())
    {
        group = effects_
                    .emplace(
                        key,
                        EffectGroup {
                            frame.receiver_id,
                            {},
                            due_at(now_ms, config_.effect_window_ms),
                        })
                    .first;
    }
    group->second.frames.emplace(std::move(stable_id), std::move(frame));
    ++effect_target_size_;
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
    std::map<std::string, std::vector<QueuedLightingFrame>> receiver_frames;
    for(const auto& frame : frames)
    {
        if(!valid_frame(frame) || !identities.insert(frame.stable_id).second)
        {
            return EnqueueDisposition::RejectedInvalid;
        }
    }
    for(auto& frame : frames)
    {
        erase_effect_target(frame.stable_id);
        receiver_frames[receiver_key(frame.receiver_id)].push_back(std::move(frame));
    }
    stable_.push_back({take_sequence(), intent, std::move(receiver_frames)});
    return EnqueueDisposition::Accepted;
}

std::optional<std::string> DispatchQueue::select_ready_receiver(
    std::uint64_t now_ms,
    const std::set<std::string>& blocked_receiver_keys) const
{
    std::set<std::string> stable_receivers;
    for(const auto& request : stable_)
    {
        for(const auto& [key, frames] : request.receiver_frames)
        {
            (void)frames;
            stable_receivers.insert(key);
        }
    }

    std::set<std::string> ready;
    for(const auto& key : stable_receivers)
    {
        if(!blocked_receiver_keys.contains(key))
        {
            ready.insert(key);
        }
    }
    for(const auto& [key, group] : effects_)
    {
        if(!stable_receivers.contains(key) && !blocked_receiver_keys.contains(key)
           && now_ms >= group.due_ms)
        {
            ready.insert(key);
        }
    }
    if(ready.empty())
    {
        return std::nullopt;
    }
    if(!last_receiver_key_.has_value())
    {
        return *ready.begin();
    }
    const auto next = ready.upper_bound(*last_receiver_key_);
    return next == ready.end() ? std::optional<std::string> {*ready.begin()}
                               : std::optional<std::string> {*next};
}

std::optional<DispatchBatch> DispatchQueue::preview_ready(
    std::uint64_t now_ms,
    const std::set<std::string>& blocked_receiver_keys) const
{
    const auto selected = select_ready_receiver(now_ms, blocked_receiver_keys);
    if(!selected.has_value())
    {
        return std::nullopt;
    }
    for(const auto& request : stable_)
    {
        const auto frames = request.receiver_frames.find(*selected);
        if(frames != request.receiver_frames.end())
        {
            return DispatchBatch {
                request.sequence,
                frames->second.front().receiver_id,
                request.intent,
                frames->second,
            };
        }
    }
    const auto group = effects_.find(*selected);
    if(group == effects_.end() || now_ms < group->second.due_ms)
    {
        return std::nullopt;
    }
    std::vector<QueuedLightingFrame> frames;
    frames.reserve(group->second.frames.size());
    for(const auto& [stable_id, frame] : group->second.frames)
    {
        (void)stable_id;
        frames.push_back(frame);
    }
    return DispatchBatch {
        next_sequence_,
        group->second.receiver_id,
        sdk::LightingIntent::EffectFrame,
        std::move(frames),
    };
}

std::optional<DispatchBatch> DispatchQueue::pop_ready_for(
    const ReceiverId& receiver_id,
    std::uint64_t now_ms)
{
    const auto key = receiver_key(receiver_id);
    for(auto request = stable_.begin(); request != stable_.end(); ++request)
    {
        const auto group = request->receiver_frames.find(key);
        if(group == request->receiver_frames.end())
        {
            continue;
        }
        DispatchBatch batch {
            request->sequence,
            group->second.front().receiver_id,
            request->intent,
            std::move(group->second),
        };
        request->receiver_frames.erase(group);
        if(request->receiver_frames.empty())
        {
            stable_.erase(request);
        }
        last_receiver_key_ = key;
        return batch;
    }

    const auto group = effects_.find(key);
    if(group == effects_.end() || now_ms < group->second.due_ms)
    {
        return std::nullopt;
    }
    std::vector<QueuedLightingFrame> frames;
    frames.reserve(group->second.frames.size());
    for(auto& [stable_id, frame] : group->second.frames)
    {
        (void)stable_id;
        frames.push_back(std::move(frame));
    }
    effect_target_size_ -= frames.size();
    const auto selected_receiver = group->second.receiver_id;
    effects_.erase(group);
    last_receiver_key_ = key;
    return DispatchBatch {
        take_sequence(),
        selected_receiver,
        sdk::LightingIntent::EffectFrame,
        std::move(frames),
    };
}

std::optional<std::uint64_t> DispatchQueue::next_effect_due_ms() const noexcept
{
    std::optional<std::uint64_t> result;
    for(const auto& [key, group] : effects_)
    {
        (void)key;
        result = !result.has_value() ? std::optional<std::uint64_t> {group.due_ms}
                                     : std::optional<std::uint64_t> {
                                           std::min(*result, group.due_ms)};
    }
    return result;
}

std::size_t DispatchQueue::stable_size() const noexcept
{
    return stable_.size();
}

std::size_t DispatchQueue::effect_target_size() const noexcept
{
    return effect_target_size_;
}

std::set<std::string> DispatchQueue::effect_target_ids() const
{
    std::set<std::string> result;
    for(const auto& [key, group] : effects_)
    {
        (void)key;
        for(const auto& [stable_id, frame] : group.frames)
        {
            (void)frame;
            result.insert(stable_id);
        }
    }
    return result;
}

bool DispatchQueue::empty() const noexcept
{
    return stable_.empty() && effects_.empty();
}

void DispatchQueue::clear() noexcept
{
    stable_.clear();
    effects_.clear();
    effect_target_size_ = 0;
    last_receiver_key_.reset();
}

void DispatchQueue::erase_effect_target(const std::string& stable_id)
{
    for(auto group = effects_.begin(); group != effects_.end(); ++group)
    {
        const auto frame = group->second.frames.find(stable_id);
        if(frame == group->second.frames.end())
        {
            continue;
        }
        group->second.frames.erase(frame);
        --effect_target_size_;
        if(group->second.frames.empty())
        {
            effects_.erase(group);
        }
        return;
    }
}

void DispatchQueue::discard_controller(const std::string& stable_id)
{
    erase_effect_target(stable_id);
    for(auto request = stable_.begin(); request != stable_.end();)
    {
        for(auto group = request->receiver_frames.begin();
            group != request->receiver_frames.end();)
        {
            auto& frames = group->second;
            frames.erase(
                std::remove_if(
                    frames.begin(),
                    frames.end(),
                    [&stable_id](const QueuedLightingFrame& frame) {
                        return frame.stable_id == stable_id;
                    }),
                frames.end());
            group = frames.empty() ? request->receiver_frames.erase(group)
                                   : std::next(group);
        }
        request = request->receiver_frames.empty() ? stable_.erase(request)
                                                    : std::next(request);
    }
}

} // namespace hyperflux::openrgb
