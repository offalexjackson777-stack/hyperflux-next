// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/dispatch_queue.hpp>

#include <cstdint>
#include <cstdlib>
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
    std::size_t count = 2)
{
    return {
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
    if(queue.enqueue_effect(frame("mouse", 1), 100) != EnqueueDisposition::Accepted
       || queue.enqueue_effect(frame("mouse", 2), 101) != EnqueueDisposition::Coalesced
       || queue.enqueue_effect(frame("keyboard", 3), 102) != EnqueueDisposition::Accepted
       || queue.pop_ready(103).has_value())
    {
        return EXIT_FAILURE;
    }
    const auto effect = queue.pop_ready(104);
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
    const auto first_stable = queue.pop_ready(200);
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
    const auto off = queue.pop_ready(201);
    const auto keyboard = queue.pop_ready(201);
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
    const auto remaining = queue.pop_ready(304);
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
    return EXIT_SUCCESS;
}
