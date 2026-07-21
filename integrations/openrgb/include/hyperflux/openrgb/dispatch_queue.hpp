// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <hyperflux/sdk/lighting.hpp>

#include <cstddef>
#include <cstdint>
#include <deque>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <vector>

namespace hyperflux::openrgb
{

struct QueuedLightingFrame
{
    ReceiverId receiver_id;
    GenerationId generation_id;
    std::string stable_id;
    std::size_t expected_slot_count;
    std::vector<v5::RgbColor> colors;

    friend bool operator==(const QueuedLightingFrame&, const QueuedLightingFrame&) = default;
};

struct DispatchTarget
{
    ReceiverId receiver_id;
    GenerationId generation_id;
    std::string stable_id;
    std::size_t expected_slot_count;

    friend bool operator==(const DispatchTarget&, const DispatchTarget&) = default;
};

struct DispatchBatch
{
    std::uint64_t sequence;
    ReceiverId receiver_id;
    sdk::LightingIntent intent;
    std::vector<QueuedLightingFrame> frames;
    std::vector<DispatchTarget> targets;

    friend bool operator==(const DispatchBatch&, const DispatchBatch&) = default;
};

struct DispatchQueueConfig
{
    std::size_t stable_capacity = 64;
    std::size_t effect_target_capacity = 32;
    std::uint64_t effect_window_ms = 4;
};

enum class EnqueueDisposition
{
    Accepted,
    Coalesced,
    RejectedInvalid,
    RejectedCapacity,
};

/// Bounded application-side queue policy for one serialized bridge client.
///
/// Stable requests preserve exact insertion order within every receiver. A
/// logical request spanning receivers remains one capacity reservation, while
/// each receiver is extracted as its own atomic bridge transaction. Effect
/// frames are replaceable only while unsent and only for the same logical
/// controller.
class DispatchQueue
{
public:
    explicit DispatchQueue(
        DispatchQueueConfig config = {},
        std::uint64_t initial_sequence = 1);

    [[nodiscard]] EnqueueDisposition enqueue_effect(
        QueuedLightingFrame frame,
        std::uint64_t now_ms);
    [[nodiscard]] EnqueueDisposition enqueue_stable(
        sdk::LightingIntent intent,
        std::vector<QueuedLightingFrame> frames);

    [[nodiscard]] std::optional<DispatchBatch> preview_ready(
        std::uint64_t now_ms,
        const std::set<std::string>& blocked_receiver_keys = {}) const;
    [[nodiscard]] std::optional<DispatchBatch> pop_ready_for(
        const ReceiverId& receiver_id,
        std::uint64_t now_ms);
    [[nodiscard]] std::optional<std::uint64_t> next_effect_due_ms() const noexcept;
    [[nodiscard]] std::size_t stable_size() const noexcept;
    [[nodiscard]] std::size_t effect_target_size() const noexcept;
    [[nodiscard]] std::set<std::string> effect_target_ids() const;
    [[nodiscard]] bool empty() const noexcept;
    [[nodiscard]] bool contains_sequence(std::uint64_t sequence) const noexcept;
    [[nodiscard]] std::vector<std::uint64_t> request_sequences() const;
    [[nodiscard]] std::vector<DispatchBatch> discard_request(std::uint64_t sequence);

    void clear() noexcept;
    void discard_controller(const std::string& stable_id);

private:
    struct StableRequest
    {
        std::uint64_t sequence;
        sdk::LightingIntent intent;
        std::map<std::string, std::vector<QueuedLightingFrame>> receiver_frames;
        std::vector<DispatchTarget> targets;
    };

    struct EffectRequest
    {
        std::uint64_t sequence;
        std::map<std::string, std::map<std::string, QueuedLightingFrame>> receiver_frames;
        std::vector<DispatchTarget> targets;
        std::uint64_t due_ms;
        bool started = false;
    };

    [[nodiscard]] bool valid_frame(const QueuedLightingFrame& frame) const noexcept;
    [[nodiscard]] std::optional<std::uint64_t> take_sequence() noexcept;
    [[nodiscard]] std::optional<std::string> select_ready_receiver(
        std::uint64_t now_ms,
        const std::set<std::string>& blocked_receiver_keys) const;
    void erase_effect_target(const std::string& stable_id);

    DispatchQueueConfig config_;
    std::uint64_t next_sequence_ = 1;
    bool sequence_exhausted_ = false;
    std::deque<StableRequest> stable_;
    std::deque<EffectRequest> effects_;
    std::size_t effect_target_size_ = 0;
    std::optional<std::string> last_receiver_key_;
};

} // namespace hyperflux::openrgb
