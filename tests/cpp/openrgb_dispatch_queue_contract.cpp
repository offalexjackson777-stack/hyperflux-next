// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/dispatch_queue.hpp>

#include <cstdint>
#include <cstdlib>
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
    std::string receiver = "receiver-1")
{
    return {
        hyperflux::ReceiverId::from(receiver).value(),
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
       || first_scope->frames.size() != 1)
    {
        return EXIT_FAILURE;
    }
    const auto first_scope_popped = scoped.pop_ready_for(receiver_one, 500);
    const auto second_scope = scoped.preview_ready(500);
    if(!first_scope_popped || !second_scope || second_scope->receiver_id != receiver_two
       || second_scope->sequence != first_scope_popped->sequence
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
           != 30
       || scoped.preview_ready(605).has_value())
    {
        return EXIT_FAILURE;
    }
    const auto second_due = scoped.preview_ready(606);
    if(!second_due || second_due->receiver_id != receiver_two)
    {
        return EXIT_FAILURE;
    }
    if(!scoped.pop_ready_for(receiver_two, 606))
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
       || !pressure.empty())
    {
        return EXIT_FAILURE;
    }
    return EXIT_SUCCESS;
}
