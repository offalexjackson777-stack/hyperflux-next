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

DispatchTarget target(const QueuedLightingFrame& frame)
{
    return {
        frame.receiver_id,
        frame.generation_id,
        frame.stable_id,
        frame.expected_slot_count,
    };
}

void sort_targets(std::vector<DispatchTarget>& targets)
{
    std::sort(targets.begin(), targets.end(), [](const auto& left, const auto& right) {
        return left.stable_id < right.stable_id;
    });
}

std::vector<QueuedLightingFrame> copied_frames(
    const std::map<std::string, QueuedLightingFrame>& source)
{
    std::vector<QueuedLightingFrame> result;
    result.reserve(source.size());
    for(const auto& [stable_id, frame] : source)
    {
        (void)stable_id;
        result.push_back(frame);
    }
    return result;
}

std::vector<QueuedLightingFrame> moved_frames(
    std::map<std::string, QueuedLightingFrame>& source)
{
    std::vector<QueuedLightingFrame> result;
    result.reserve(source.size());
    for(auto& [stable_id, frame] : source)
    {
        (void)stable_id;
        result.push_back(std::move(frame));
    }
    return result;
}

} // namespace

DispatchQueue::DispatchQueue(
    DispatchQueueConfig config,
    std::uint64_t initial_sequence)
    : config_(config), next_sequence_(initial_sequence)
{
}

bool DispatchQueue::valid_frame(const QueuedLightingFrame& frame) const noexcept
{
    return !frame.stable_id.empty() && frame.expected_slot_count > 0
        && frame.expected_slot_count <= LedCount::maximum
        && frame.colors.size() == frame.expected_slot_count;
}

std::optional<std::uint64_t> DispatchQueue::take_sequence() noexcept
{
    if(sequence_exhausted_)
    {
        return std::nullopt;
    }
    const auto current = next_sequence_;
    if(next_sequence_ == std::numeric_limits<std::uint64_t>::max())
    {
        sequence_exhausted_ = true;
    }
    else
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
       || config_.effect_target_capacity > QueueCapacity::maximum
       || config_.effect_window_ms == 0)
    {
        return EnqueueDisposition::RejectedInvalid;
    }
    const auto key = receiver_key(frame.receiver_id);
    for(auto& request : effects_)
    {
        for(auto& [existing_key, group] : request.receiver_frames)
        {
            const auto existing = group.find(frame.stable_id);
            if(existing == group.end())
            {
                continue;
            }
            if(existing_key != key)
            {
                return EnqueueDisposition::RejectedInvalid;
            }
            existing->second = std::move(frame);
            const auto updated = target(existing->second);
            const auto target_entry = std::find_if(
                request.targets.begin(),
                request.targets.end(),
                [&updated](const DispatchTarget& value) {
                    return value.stable_id == updated.stable_id;
                });
            if(target_entry != request.targets.end())
            {
                *target_entry = updated;
                sort_targets(request.targets);
            }
            return EnqueueDisposition::Coalesced;
        }
    }
    if(effect_target_size_ >= config_.effect_target_capacity)
    {
        return EnqueueDisposition::RejectedCapacity;
    }
    if(effects_.empty() || effects_.back().started)
    {
        const auto sequence = take_sequence();
        if(!sequence.has_value())
        {
            return EnqueueDisposition::RejectedCapacity;
        }
        effects_.push_back({
            *sequence,
            {},
            {},
            due_at(now_ms, config_.effect_window_ms),
            false,
        });
    }
    auto& request = effects_.back();
    request.targets.push_back(target(frame));
    sort_targets(request.targets);
    auto stable_id = frame.stable_id;
    request.receiver_frames[key].emplace(std::move(stable_id), std::move(frame));
    ++effect_target_size_;
    return EnqueueDisposition::Accepted;
}

EnqueueDisposition DispatchQueue::enqueue_stable(
    sdk::LightingIntent intent,
    std::vector<QueuedLightingFrame> frames)
{
    if((intent != sdk::LightingIntent::Static && intent != sdk::LightingIntent::Off)
       || frames.empty() || frames.size() > FrameCount::maximum
       || config_.stable_capacity == 0
       || config_.stable_capacity > QueueCapacity::maximum
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
    const auto sequence = take_sequence();
    if(!sequence.has_value())
    {
        return EnqueueDisposition::RejectedCapacity;
    }
    for(auto& frame : frames)
    {
        erase_effect_target(frame.stable_id);
        receiver_frames[receiver_key(frame.receiver_id)].push_back(std::move(frame));
    }
    std::vector<DispatchTarget> targets;
    targets.reserve(frames.size());
    for(const auto& [key, group] : receiver_frames)
    {
        (void)key;
        for(const auto& frame : group)
        {
            targets.push_back(target(frame));
        }
    }
    sort_targets(targets);
    stable_.push_back(
        {*sequence, intent, std::move(receiver_frames), std::move(targets)});
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
    for(const auto& request : effects_)
    {
        if(now_ms < request.due_ms)
        {
            continue;
        }
        for(const auto& [key, frames] : request.receiver_frames)
        {
            (void)frames;
            if(!stable_receivers.contains(key) && !blocked_receiver_keys.contains(key))
            {
                ready.insert(key);
            }
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
                request.targets,
            };
        }
    }
    for(const auto& request : effects_)
    {
        const auto group = request.receiver_frames.find(*selected);
        if(group == request.receiver_frames.end() || now_ms < request.due_ms)
        {
            continue;
        }
        return DispatchBatch {
            request.sequence,
            group->second.begin()->second.receiver_id,
            sdk::LightingIntent::EffectFrame,
            copied_frames(group->second),
            request.targets,
        };
    }
    return std::nullopt;
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
            request->targets,
        };
        request->receiver_frames.erase(group);
        if(request->receiver_frames.empty())
        {
            stable_.erase(request);
        }
        last_receiver_key_ = key;
        return batch;
    }

    for(auto request = effects_.begin(); request != effects_.end(); ++request)
    {
        const auto group = request->receiver_frames.find(key);
        if(group == request->receiver_frames.end() || now_ms < request->due_ms)
        {
            continue;
        }
        request->started = true;
        auto frames = moved_frames(group->second);
        effect_target_size_ -= frames.size();
        DispatchBatch batch {
            request->sequence,
            frames.front().receiver_id,
            sdk::LightingIntent::EffectFrame,
            std::move(frames),
            request->targets,
        };
        request->receiver_frames.erase(group);
        if(request->receiver_frames.empty())
        {
            effects_.erase(request);
        }
        last_receiver_key_ = key;
        return batch;
    }
    return std::nullopt;
}

std::optional<std::uint64_t> DispatchQueue::next_effect_due_ms() const noexcept
{
    std::optional<std::uint64_t> result;
    for(const auto& request : effects_)
    {
        result = !result.has_value() ? std::optional<std::uint64_t> {request.due_ms}
                                     : std::optional<std::uint64_t> {
                                           std::min(*result, request.due_ms)};
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
    for(const auto& request : effects_)
    {
        for(const auto& [key, group] : request.receiver_frames)
        {
            (void)key;
            for(const auto& [stable_id, frame] : group)
            {
                (void)frame;
                result.insert(stable_id);
            }
        }
    }
    return result;
}

bool DispatchQueue::empty() const noexcept
{
    return stable_.empty() && effects_.empty();
}

bool DispatchQueue::contains_sequence(std::uint64_t sequence) const noexcept
{
    return std::any_of(stable_.begin(), stable_.end(), [sequence](const auto& request) {
               return request.sequence == sequence;
           })
        || std::any_of(effects_.begin(), effects_.end(), [sequence](const auto& request) {
               return request.sequence == sequence;
        });
}

std::vector<std::uint64_t> DispatchQueue::request_sequences() const
{
    std::vector<std::uint64_t> result;
    result.reserve(stable_.size() + effects_.size());
    for(const auto& request : stable_)
    {
        result.push_back(request.sequence);
    }
    for(const auto& request : effects_)
    {
        result.push_back(request.sequence);
    }
    std::sort(result.begin(), result.end());
    result.erase(std::unique(result.begin(), result.end()), result.end());
    return result;
}

std::vector<DispatchBatch> DispatchQueue::discard_request(std::uint64_t sequence)
{
    std::vector<DispatchBatch> result;
    const auto stable = std::find_if(
        stable_.begin(),
        stable_.end(),
        [sequence](const auto& request) { return request.sequence == sequence; });
    if(stable != stable_.end())
    {
        result.reserve(stable->receiver_frames.size());
        for(auto& [key, frames] : stable->receiver_frames)
        {
            (void)key;
            result.push_back({
                stable->sequence,
                frames.front().receiver_id,
                stable->intent,
                std::move(frames),
                stable->targets,
            });
        }
        stable_.erase(stable);
        return result;
    }
    const auto effect = std::find_if(
        effects_.begin(),
        effects_.end(),
        [sequence](const auto& request) { return request.sequence == sequence; });
    if(effect == effects_.end())
    {
        return result;
    }
    result.reserve(effect->receiver_frames.size());
    for(auto& [key, frames] : effect->receiver_frames)
    {
        (void)key;
        auto moved = moved_frames(frames);
        effect_target_size_ -= moved.size();
        result.push_back({
            effect->sequence,
            moved.front().receiver_id,
            sdk::LightingIntent::EffectFrame,
            std::move(moved),
            effect->targets,
        });
    }
    effects_.erase(effect);
    return result;
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
    for(auto request = effects_.begin(); request != effects_.end(); ++request)
    {
        for(auto group = request->receiver_frames.begin();
            group != request->receiver_frames.end();
            ++group)
        {
            const auto frame = group->second.find(stable_id);
            if(frame == group->second.end())
            {
                continue;
            }
            group->second.erase(frame);
            request->targets.erase(
                std::remove_if(
                    request->targets.begin(),
                    request->targets.end(),
                    [&stable_id](const DispatchTarget& value) {
                        return value.stable_id == stable_id;
                    }),
                request->targets.end());
            --effect_target_size_;
            if(group->second.empty())
            {
                request->receiver_frames.erase(group);
            }
            if(request->receiver_frames.empty())
            {
                effects_.erase(request);
            }
            return;
        }
    }
}

void DispatchQueue::discard_controller(const std::string& stable_id)
{
    erase_effect_target(stable_id);
    for(auto request = stable_.begin(); request != stable_.end();)
    {
        request->targets.erase(
            std::remove_if(
                request->targets.begin(),
                request->targets.end(),
                [&stable_id](const DispatchTarget& value) {
                    return value.stable_id == stable_id;
                }),
            request->targets.end());
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
