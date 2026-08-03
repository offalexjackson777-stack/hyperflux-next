// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/dispatch_queue.hpp>

#include <cstdint>
#include <cstdlib>
#include <limits>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace
{

hyperflux::ColorChannel channel(std::uint8_t value)
{
    return hyperflux::ColorChannel::from(value).value();
}

hyperflux::openrgb::QueuedLightingFrame frame(
    std::string stable_id,
    std::uint8_t red,
    std::size_t count = 2,
    std::string receiver = "receiver-1",
    std::uint64_t generation = 1)
{
    return {
        hyperflux::ReceiverId::from(receiver).value(),
        hyperflux::GenerationId::from(generation).value(),
        std::move(stable_id),
        count,
        std::vector<hyperflux::v5::RgbColor>(
            count,
            {channel(red), channel(0), channel(0)}),
    };
}

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb;

    DispatchQueue queue({2, 2, 4});
    const auto receiver_one = ReceiverId::from("receiver-1").value();
    const auto receiver_two = ReceiverId::from("receiver-2").value();
    if(queue.enqueue_effect(frame("mouse", 1), 100) != EnqueueDisposition::Accepted
       || queue.enqueue_effect(frame("mouse", 2), 101) != EnqueueDisposition::Coalesced
       || queue.enqueue_effect(frame("keyboard", 3), 102) != EnqueueDisposition::Accepted
       || queue.pop_ready_for(receiver_one, 103).has_value())
    {
        return EXIT_FAILURE;
    }
    const auto effect = queue.pop_ready_for(receiver_one, 104);
    if(!effect || effect->intent != sdk::LightingIntent::EffectFrame
       || effect->frames.size() != 2 || effect->frames[0].stable_id != "keyboard"
       || effect->frames[1].colors[0].red.value() != 2)
    {
        return EXIT_FAILURE;
    }

    if(queue.enqueue_effect(frame("mouse", 4), 200) != EnqueueDisposition::Accepted
       || queue.enqueue_stable(sdk::LightingIntent::Static, {frame("mouse", 5)})
           != EnqueueDisposition::Accepted
       || queue.effect_target_size() != 0)
    {
        return EXIT_FAILURE;
    }
    const auto first_stable = queue.pop_ready_for(receiver_one, 200);
    if(!first_stable || first_stable->intent != sdk::LightingIntent::Static
       || first_stable->frames.front().colors.front().red.value() != 5)
    {
        return EXIT_FAILURE;
    }

    if(queue.enqueue_stable(sdk::LightingIntent::Off, {frame("mouse", 0)})
           != EnqueueDisposition::Accepted
       || queue.enqueue_stable(sdk::LightingIntent::Static, {frame("keyboard", 6)})
           != EnqueueDisposition::Accepted
       || queue.enqueue_stable(sdk::LightingIntent::Static, {frame("mouse", 7)})
           != EnqueueDisposition::RejectedCapacity)
    {
        return EXIT_FAILURE;
    }
    const auto off = queue.pop_ready_for(receiver_one, 201);
    const auto keyboard = queue.pop_ready_for(receiver_one, 201);
    if(!off || !keyboard || off->intent != sdk::LightingIntent::Off
       || keyboard->frames.front().stable_id != "keyboard"
       || off->sequence >= keyboard->sequence)
    {
        return EXIT_FAILURE;
    }

    if(queue.enqueue_effect(frame("mouse", 8), 300) != EnqueueDisposition::Accepted
       || queue.enqueue_effect(frame("keyboard", 9), 300) != EnqueueDisposition::Accepted
       || queue.enqueue_effect(frame("third", 10), 300)
           != EnqueueDisposition::RejectedCapacity)
    {
        return EXIT_FAILURE;
    }
    queue.discard_controller("mouse");
    const auto remaining = queue.pop_ready_for(receiver_one, 304);
    if(!remaining || remaining->frames.size() != 1
       || remaining->frames.front().stable_id != "keyboard")
    {
        return EXIT_FAILURE;
    }

    if(queue.enqueue_stable(sdk::LightingIntent::EffectFrame, {frame("mouse", 1)})
           != EnqueueDisposition::RejectedInvalid
       || queue.enqueue_effect(frame("", 1), 400) != EnqueueDisposition::RejectedInvalid)
    {
        return EXIT_FAILURE;
    }

    DispatchQueue scoped({4, 4, 4});
    if(scoped.enqueue_stable(
           sdk::LightingIntent::Static,
           {frame("receiver-1/mouse", 11),
            frame("receiver-2/mouse", 22, 2, "receiver-2")})
           != EnqueueDisposition::Accepted
       || scoped.stable_size() != 1)
    {
        return EXIT_FAILURE;
    }
    const auto first_scope = scoped.preview_ready(500);
    if(!first_scope || first_scope->receiver_id != receiver_one
       || first_scope->frames.size() != 1 || first_scope->targets.size() != 2)
    {
        return EXIT_FAILURE;
    }
    const auto first_scope_popped = scoped.pop_ready_for(receiver_one, 500);
    const auto second_scope = scoped.preview_ready(500);
    if(!first_scope_popped || !second_scope || second_scope->receiver_id != receiver_two
       || second_scope->sequence != first_scope_popped->sequence
       || second_scope->targets != first_scope_popped->targets
       || scoped.stable_size() != 1)
    {
        return EXIT_FAILURE;
    }
    if(!scoped.pop_ready_for(receiver_two, 500) || scoped.stable_size() != 0)
    {
        return EXIT_FAILURE;
    }

    if(scoped.enqueue_effect(frame("receiver-1/mouse", 30), 600)
           != EnqueueDisposition::Accepted
       || scoped.enqueue_effect(frame("receiver-2/mouse", 40, 2, "receiver-2"), 602)
           != EnqueueDisposition::Accepted)
    {
        return EXIT_FAILURE;
    }
    const auto first_due = scoped.preview_ready(604);
    if(!first_due || first_due->receiver_id != receiver_one
       || scoped.pop_ready_for(receiver_one, 604)->frames.front().colors.front().red.value()
           != 30)
    {
        return EXIT_FAILURE;
    }
    const auto second_due = scoped.preview_ready(605);
    if(!second_due || second_due->receiver_id != receiver_two
       || second_due->sequence != first_due->sequence
       || second_due->targets.size() != 2)
    {
        return EXIT_FAILURE;
    }
    if(!scoped.pop_ready_for(receiver_two, 605))
    {
        return EXIT_FAILURE;
    }

    DispatchQueue pressure({4, 2, 4});
    std::uint8_t latest_one = 0;
    std::uint8_t latest_two = 0;
    for(std::size_t index = 0; index < 4'096; ++index)
    {
        latest_one = static_cast<std::uint8_t>(index % 251);
        latest_two = static_cast<std::uint8_t>((index + 17) % 251);
        const auto one = pressure.enqueue_effect(
            frame("receiver-1/mouse", latest_one),
            700 + index);
        const auto two = pressure.enqueue_effect(
            frame("receiver-2/mouse", latest_two, 2, "receiver-2"),
            700 + index);
        if((index == 0
                && (one != EnqueueDisposition::Accepted
                    || two != EnqueueDisposition::Accepted))
           || (index != 0
               && (one != EnqueueDisposition::Coalesced
                   || two != EnqueueDisposition::Coalesced)))
        {
            return EXIT_FAILURE;
        }
    }
    if(pressure.effect_target_size() != 2
       || pressure.next_effect_due_ms() != std::optional<std::uint64_t> {704}
       || pressure.preview_ready(703).has_value())
    {
        return EXIT_FAILURE;
    }
    const auto pressure_one = pressure.pop_ready_for(receiver_one, 704);
    const auto pressure_two = pressure.pop_ready_for(receiver_two, 704);
    if(!pressure_one || !pressure_two
       || pressure_one->frames.front().colors.front().red.value() != latest_one
       || pressure_two->frames.front().colors.front().red.value() != latest_two
       || pressure_one->sequence != pressure_two->sequence
       || !pressure.empty())
    {
        return EXIT_FAILURE;
    }

    DispatchQueue lifecycle({4, 4, 4});
    if(lifecycle.enqueue_effect(frame("receiver-1/mouse", 1), 800)
           != EnqueueDisposition::Accepted
       || lifecycle.enqueue_effect(frame("receiver-2/mouse", 2, 2, "receiver-2"), 800)
           != EnqueueDisposition::Accepted)
    {
        return EXIT_FAILURE;
    }
    const auto first_wave = lifecycle.pop_ready_for(receiver_one, 804);
    if(!first_wave || !lifecycle.contains_sequence(first_wave->sequence)
       || lifecycle.enqueue_effect(frame("receiver-1/mouse", 3), 805)
           != EnqueueDisposition::Accepted)
    {
        return EXIT_FAILURE;
    }
    const auto sibling_wave = lifecycle.pop_ready_for(receiver_two, 805);
    const auto next_wave = lifecycle.preview_ready(809);
    if(!sibling_wave || sibling_wave->sequence != first_wave->sequence
       || lifecycle.contains_sequence(first_wave->sequence) || !next_wave
       || next_wave->sequence == first_wave->sequence
       || next_wave->frames.front().colors.front().red.value() != 3)
    {
        return EXIT_FAILURE;
    }
    if(lifecycle.request_sequences()
       != std::vector<std::uint64_t> {next_wave->sequence})
    {
        return EXIT_FAILURE;
    }
    const auto discarded = lifecycle.discard_request(next_wave->sequence);
    if(discarded.size() != 1 || lifecycle.contains_sequence(next_wave->sequence)
       || !lifecycle.empty())
    {
        return EXIT_FAILURE;
    }

    DispatchQueue generation_queue({2, 2, 4});
    if(generation_queue.enqueue_effect(
           frame("receiver-1/mouse", 4, 2, "receiver-1", 1), 900)
           != EnqueueDisposition::Accepted
       || generation_queue.enqueue_effect(
              frame("receiver-1/mouse", 5, 2, "receiver-1", 2), 901)
           != EnqueueDisposition::Coalesced)
    {
        return EXIT_FAILURE;
    }
    const auto current_generation = generation_queue.preview_ready(904);
    if(!current_generation
       || current_generation->frames.front().generation_id.value() != 2
       || current_generation->targets.front().generation_id.value() != 2)
    {
        return EXIT_FAILURE;
    }

    DispatchQueue sequence_boundary(
        {2, 2, 4},
        std::numeric_limits<std::uint64_t>::max());
    if(sequence_boundary.enqueue_stable(
           sdk::LightingIntent::Static,
           {frame("receiver-1/mouse", 6)})
           != EnqueueDisposition::Accepted)
    {
        return EXIT_FAILURE;
    }
    const auto final_sequence = sequence_boundary.pop_ready_for(receiver_one, 1'000);
    if(!final_sequence
       || final_sequence->sequence != std::numeric_limits<std::uint64_t>::max()
       || sequence_boundary.enqueue_stable(
              sdk::LightingIntent::Static,
              {frame("receiver-1/mouse", 7)})
           != EnqueueDisposition::RejectedCapacity
       || !sequence_boundary.empty())
    {
        return EXIT_FAILURE;
    }

    DispatchQueue protocol_bounds({2, 2, 4});
    std::vector<QueuedLightingFrame> oversized_request;
    oversized_request.reserve(static_cast<std::size_t>(FrameCount::maximum) + 1U);
    for(std::size_t index = 0;
        index < static_cast<std::size_t>(FrameCount::maximum) + 1U;
        ++index)
    {
        oversized_request.push_back(frame("controller-" + std::to_string(index), 1, 1));
    }
    if(protocol_bounds.enqueue_stable(
           sdk::LightingIntent::Static,
           std::move(oversized_request))
           != EnqueueDisposition::RejectedInvalid
       || protocol_bounds.enqueue_effect(
              frame(
                  "oversized-controller",
                  1,
                  static_cast<std::size_t>(LedCount::maximum) + 1U),
              1'100)
           != EnqueueDisposition::RejectedInvalid
       || !protocol_bounds.empty())
    {
        return EXIT_FAILURE;
    }
    return EXIT_SUCCESS;
}
