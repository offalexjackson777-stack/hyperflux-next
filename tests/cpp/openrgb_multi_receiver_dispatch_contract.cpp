// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_runtime_fixture.hpp"

#include <cstdlib>
#include <iostream>
#include <string_view>
#include <utility>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-multi-receiver-dispatch-contract failed at line " << line
              << '\n';
    return EXIT_FAILURE;
}

bool submitted_to(
    const hyperflux::sdk::TransactionSubmission& submission,
    std::string_view receiver)
{
    return submission.receiver_id.value() == receiver;
}

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb;
    using namespace hyperflux::test;

    FakeBridge bridge(multi_receiver_view(1, 1));
    RuntimeConfig config;
    config.max_outcomes_per_step = 1;
    config.max_dispatches_per_step = 4;
    auto created = RuntimeCore::create(bridge, config);
    if(!created)
    {
        return failure(__LINE__);
    }
    auto runtime = std::move(created).value();
    if(!runtime.initialize() || runtime.controllers().size() != 2)
    {
        return failure(__LINE__);
    }

    const auto receiver_one = frame(model(runtime, "receiver-1", DeviceKind::Mouse), 10);
    const auto receiver_two = frame(model(runtime, "receiver-2", DeviceKind::Mouse), 20);
    if(runtime.enqueue_stable(
           sdk::LightingIntent::Static,
           {receiver_one, receiver_two})
           != EnqueueDisposition::Accepted)
    {
        return failure(__LINE__);
    }
    const auto split_apply_all = runtime.step(100);
    if(!split_apply_all || bridge.submissions.size() != 2
       || !submitted_to(bridge.submissions[0], "receiver-1")
       || !submitted_to(bridge.submissions[1], "receiver-2")
       || bridge.submissions[0].frames.size() != 1
       || bridge.submissions[1].frames.size() != 1
       || runtime.pending_transaction_count() != 2
       || runtime.queued_stable_count() != 0)
    {
        return failure(__LINE__);
    }

    bridge.complete_submission(
        1,
        TransactionState::Succeeded,
        1,
        SideEffectCertainty::Committed,
        true);
    const auto first_poll = runtime.step(101);
    if(!first_poll || !first_poll.value().dispatch_outcomes.empty()
       || bridge.outcome_queries.size() != 1
       || bridge.outcome_queries.back() != bridge.submissions[0].transaction_id)
    {
        return failure(__LINE__);
    }
    const auto second_poll = runtime.step(102);
    if(!second_poll || second_poll.value().dispatch_outcomes.size() != 1
       || second_poll.value().dispatch_outcomes.front().receiver_id->value()
           != "receiver-2"
       || bridge.outcome_queries.size() != 2
       || bridge.outcome_queries.back() != bridge.submissions[1].transaction_id
       || runtime.pending_transaction_count() != 1)
    {
        return failure(__LINE__);
    }

    bridge.terminal_on_submit = true;
    if(runtime.enqueue_stable(
           sdk::LightingIntent::Static,
           {frame(model(runtime, "receiver-1", DeviceKind::Mouse), 31),
            frame(model(runtime, "receiver-2", DeviceKind::Mouse), 32)})
           != EnqueueDisposition::Accepted)
    {
        return failure(__LINE__);
    }
    const auto unblocked_sibling = runtime.step(103);
    if(!unblocked_sibling || bridge.submissions.size() != 3
       || !submitted_to(bridge.submissions.back(), "receiver-2")
       || unblocked_sibling.value().dispatch_outcomes.size() != 1
       || unblocked_sibling.value().dispatch_outcomes.front().receiver_id->value()
           != "receiver-2"
       || runtime.queued_stable_count() != 1
       || runtime.pending_transaction_count() != 1)
    {
        return failure(__LINE__);
    }
    const auto logical_request_sequence =
        unblocked_sibling.value().dispatch_outcomes.front().sequence;

    bridge.complete_submission(
        0,
        TransactionState::Succeeded,
        1,
        SideEffectCertainty::Committed,
        true);
    const auto released_receiver = runtime.step(104);
    if(!released_receiver || bridge.submissions.size() != 4
       || !submitted_to(bridge.submissions.back(), "receiver-1")
       || released_receiver.value().dispatch_outcomes.size() != 2
       || released_receiver.value().dispatch_outcomes.back().sequence
           != logical_request_sequence
       || released_receiver.value().dispatch_outcomes.back().receiver_id->value()
           != "receiver-1"
       || runtime.pending_transaction_count() != 0
       || runtime.queued_stable_count() != 0)
    {
        return failure(__LINE__);
    }

    FakeBridge ordered_bridge(multi_receiver_view(1, 1));
    ordered_bridge.terminal_on_submit = true;
    RuntimeConfig ordered_config;
    ordered_config.max_dispatches_per_step = 1;
    auto ordered_created = RuntimeCore::create(ordered_bridge, ordered_config);
    if(!ordered_created)
    {
        return failure(__LINE__);
    }
    auto ordered = std::move(ordered_created).value();
    if(!ordered.initialize())
    {
        return failure(__LINE__);
    }
    const auto& ordered_one = model(ordered, "receiver-1", DeviceKind::Mouse);
    const auto& ordered_two = model(ordered, "receiver-2", DeviceKind::Mouse);
    if(ordered.enqueue_stable(sdk::LightingIntent::Static, {frame(ordered_one, 1)})
           != EnqueueDisposition::Accepted
       || ordered.enqueue_stable(sdk::LightingIntent::Static, {frame(ordered_one, 2)})
           != EnqueueDisposition::Accepted
       || ordered.enqueue_stable(sdk::LightingIntent::Static, {frame(ordered_two, 3)})
           != EnqueueDisposition::Accepted
       || !ordered.step(200) || !ordered.step(201) || !ordered.step(202))
    {
        return failure(__LINE__);
    }
    if(ordered_bridge.submissions.size() != 3
       || !submitted_to(ordered_bridge.submissions[0], "receiver-1")
       || !submitted_to(ordered_bridge.submissions[1], "receiver-2")
       || !submitted_to(ordered_bridge.submissions[2], "receiver-1")
       || ordered_bridge.submissions[0].frames.front().colors.front().red.value() != 1
       || ordered_bridge.submissions[1].frames.front().colors.front().red.value() != 3
       || ordered_bridge.submissions[2].frames.front().colors.front().red.value() != 2)
    {
        return failure(__LINE__);
    }

    return EXIT_SUCCESS;
}
