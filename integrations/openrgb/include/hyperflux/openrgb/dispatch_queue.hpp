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
    std::string stable_id;
    std::size_t expected_slot_count;
    std::vector<v5::RgbColor> colors;

    friend bool operator==(const QueuedLightingFrame&, const QueuedLightingFrame&) = default;
};

struct DispatchBatch
{
    std::uint64_t sequence;
    sdk::LightingIntent intent;
    std::vector<QueuedLightingFrame> frames;

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
/// Stable requests preserve exact insertion order. Effect frames are replaceable
/// only while unsent and only for the same logical controller. This mirrors the
/// bridge's semantic queue policy without guessing about transport completion.
class DispatchQueue
{
public:
    explicit DispatchQueue(DispatchQueueConfig config = {});

    [[nodiscard]] EnqueueDisposition enqueue_effect(
        QueuedLightingFrame frame,
        std::uint64_t now_ms);
    [[nodiscard]] EnqueueDisposition enqueue_stable(
        sdk::LightingIntent intent,
        std::vector<QueuedLightingFrame> frames);

    [[nodiscard]] std::optional<DispatchBatch> preview_ready(std::uint64_t now_ms) const;
    [[nodiscard]] std::optional<DispatchBatch> pop_ready(std::uint64_t now_ms);
    [[nodiscard]] std::optional<std::uint64_t> next_effect_due_ms() const noexcept;
    [[nodiscard]] std::size_t stable_size() const noexcept;
    [[nodiscard]] std::size_t effect_target_size() const noexcept;
    [[nodiscard]] std::set<std::string> effect_target_ids() const;
    [[nodiscard]] bool empty() const noexcept;

    void clear() noexcept;
    void discard_controller(const std::string& stable_id);

private:
    [[nodiscard]] bool valid_frame(const QueuedLightingFrame& frame) const noexcept;
    [[nodiscard]] std::uint64_t take_sequence() noexcept;

    DispatchQueueConfig config_;
    std::uint64_t next_sequence_ = 1;
    std::deque<DispatchBatch> stable_;
    std::map<std::string, QueuedLightingFrame> effects_;
    std::optional<std::uint64_t> effect_due_ms_;
};

} // namespace hyperflux::openrgb
