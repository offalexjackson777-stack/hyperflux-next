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
    if(!split_apply_all || bridge.acquire_count != 1
       || bridge.lease_acquisitions.size() != 1
       || bridge.lease_acquisitions.front().size() != 2
       || bridge.submissions.size() != 2
       || !submitted_to(bridge.submissions[0], "receiver-1")
       || !submitted_to(bridge.submissions[1], "receiver-2")
       || bridge.submissions[0].lease_id != bridge.submissions[1].lease_id
       || bridge.submissions[0].frames.size() != 1
       || bridge.submissions[1].frames.size() != 1
       || runtime.pending_transaction_count() != 2
       || runtime.queued_stable_count() != 0
       || !split_apply_all.value().logical_outcomes.empty())
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
       || !first_poll.value().logical_outcomes.empty()
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
       || runtime.pending_transaction_count() != 1
       || !second_poll.value().logical_outcomes.empty())
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
       || !unblocked_sibling.value().logical_outcomes.empty()
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
       || released_receiver.value().logical_outcomes.size() != 2
       || released_receiver.value().logical_outcomes[0].state
           != DispatchOutcomeState::Succeeded
       || released_receiver.value().logical_outcomes[1].state
           != DispatchOutcomeState::Succeeded
       || released_receiver.value().logical_outcomes[0].expected_receivers != 2
       || released_receiver.value().logical_outcomes[0].terminal_receivers != 2
       || released_receiver.value().logical_outcomes[0].declared_frames != 2
       || released_receiver.value().logical_outcomes[0].delivered_frames != 2
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

    FakeBridge conflict_bridge(multi_receiver_view(1, 1));
    conflict_bridge.conflict = true;
    auto conflict_created = RuntimeCore::create(conflict_bridge, config);
    if(!conflict_created)
    {
        return failure(__LINE__);
    }
    auto conflicted = std::move(conflict_created).value();
    if(!conflicted.initialize())
    {
        return failure(__LINE__);
    }
    const auto& conflict_one = model(conflicted, "receiver-1", DeviceKind::Mouse);
    const auto& conflict_two = model(conflicted, "receiver-2", DeviceKind::Mouse);
    if(conflicted.enqueue_stable(
           sdk::LightingIntent::Static,
           {frame(conflict_one, 4), frame(conflict_two, 5)})
           != EnqueueDisposition::Accepted)
    {
        return failure(__LINE__);
    }
    const auto rejected = conflicted.step(300);
    if(!rejected || conflict_bridge.acquire_count != 1
       || !conflict_bridge.submissions.empty()
       || rejected.value().dispatch_outcomes.size() != 2
       || rejected.value().dispatch_outcomes[0].state != DispatchOutcomeState::Rejected
       || rejected.value().dispatch_outcomes[1].state != DispatchOutcomeState::Rejected
       || rejected.value().logical_outcomes.size() != 1
       || rejected.value().logical_outcomes.front().state
           != DispatchOutcomeState::Rejected
       || rejected.value().logical_outcomes.front().expected_receivers != 2
       || rejected.value().logical_outcomes.front().delivered_frames != 0
       || conflicted.queued_stable_count() != 0)
    {
        return failure(__LINE__);
    }

    FakeBridge effect_bridge(multi_receiver_view(1, 1));
    effect_bridge.terminal_on_submit = true;
    auto effect_created = RuntimeCore::create(effect_bridge, config);
    if(!effect_created)
    {
        return failure(__LINE__);
    }
    auto effects = std::move(effect_created).value();
    if(!effects.initialize()
       || effects.enqueue_effect(
              frame(model(effects, "receiver-1", DeviceKind::Mouse), 6),
              400)
           != EnqueueDisposition::Accepted
       || effects.enqueue_effect(
              frame(model(effects, "receiver-2", DeviceKind::Mouse), 7),
              400)
           != EnqueueDisposition::Accepted)
    {
        return failure(__LINE__);
    }
    const auto effect_wave = effects.step(404);
    if(!effect_wave || effect_bridge.acquire_count != 1
       || effect_bridge.submissions.size() != 2
       || effect_bridge.submissions[0].lease_id != effect_bridge.submissions[1].lease_id
       || effect_wave.value().dispatch_outcomes.size() != 2
       || effect_wave.value().logical_outcomes.size() != 1
       || effect_wave.value().logical_outcomes.front().state
           != DispatchOutcomeState::Succeeded)
    {
        return failure(__LINE__);
    }

    FakeBridge consolidation_bridge(multi_receiver_view(1, 1));
    consolidation_bridge.terminal_on_submit = true;
    auto consolidation_created = RuntimeCore::create(consolidation_bridge, config);
    if(!consolidation_created)
    {
        return failure(__LINE__);
    }
    auto consolidation = std::move(consolidation_created).value();
    if(!consolidation.initialize())
    {
        return failure(__LINE__);
    }
    const auto& consolidation_one = model(
        consolidation, "receiver-1", DeviceKind::Mouse);
    const auto& consolidation_two = model(
        consolidation, "receiver-2", DeviceKind::Mouse);
    if(consolidation.enqueue_stable(
           sdk::LightingIntent::Static,
           {frame(consolidation_one, 8)})
           != EnqueueDisposition::Accepted
       || !consolidation.step(500)
       || consolidation.enqueue_stable(
              sdk::LightingIntent::Static,
              {frame(consolidation_two, 9)})
           != EnqueueDisposition::Accepted
       || !consolidation.step(501)
       || consolidation_bridge.acquire_count != 2
       || consolidation_bridge.release_count != 0
       || consolidation.enqueue_stable(
              sdk::LightingIntent::Static,
              {frame(consolidation_one, 10), frame(consolidation_two, 11)})
           != EnqueueDisposition::Accepted)
    {
        return failure(__LINE__);
    }
    const auto consolidated = consolidation.step(502);
    if(!consolidated || consolidation_bridge.release_count != 2
       || consolidation_bridge.acquire_count != 3
       || consolidation_bridge.submissions.size() != 4
       || consolidation_bridge.submissions[2].lease_id
           != consolidation_bridge.submissions[3].lease_id)
    {
        return failure(__LINE__);
    }

    FakeBridge renewal_bridge(multi_receiver_view(1, 1));
    renewal_bridge.lease_expiry_ms = 5;
    auto renewal_config = config;
    renewal_config.max_dispatches_per_step = 1;
    auto renewal_created = RuntimeCore::create(renewal_bridge, renewal_config);
    if(!renewal_created)
    {
        return failure(__LINE__);
    }
    auto renewal = std::move(renewal_created).value();
    if(!renewal.initialize())
    {
        return failure(__LINE__);
    }
    const auto& renewal_one = model(renewal, "receiver-1", DeviceKind::Mouse);
    const auto& renewal_two = model(renewal, "receiver-2", DeviceKind::Mouse);
    if(renewal.enqueue_stable(
           sdk::LightingIntent::Static,
           {frame(renewal_one, 12), frame(renewal_two, 13)})
           != EnqueueDisposition::Accepted
       || !renewal.step(600) || renewal_bridge.submissions.size() != 1
       || renewal.pending_transaction_count() != 1)
    {
        return failure(__LINE__);
    }
    renewal_bridge.fail_renew_call = 1;
    const auto renewal_failed = renewal.step(601);
    if(!renewal_failed || renewal_bridge.renew_count != 1
       || renewal_failed.value().dispatch_outcomes.size() != 1
       || renewal_failed.value().dispatch_outcomes.front().state
           != DispatchOutcomeState::Revoked
       || !renewal_failed.value().dispatch_outcomes.front().local_error.has_value()
       || renewal_failed.value().dispatch_outcomes.front().local_error->code
           != sdk::ErrorCode::LeaseRejected
       || renewal.queued_stable_count() != 0
       || renewal.pending_transaction_count() != 1
       || renewal_bridge.submissions.size() != 1
       || !renewal_failed.value().logical_outcomes.empty())
    {
        return failure(__LINE__);
    }
    renewal_bridge.complete_submission(
        0,
        TransactionState::Succeeded,
        1,
        SideEffectCertainty::Committed,
        true);
    const auto first_fragment_terminal = renewal.step(602);
    if(!first_fragment_terminal
       || first_fragment_terminal.value().dispatch_outcomes.size() != 1
       || first_fragment_terminal.value().dispatch_outcomes.front().state
           != DispatchOutcomeState::Succeeded
       || first_fragment_terminal.value().logical_outcomes.size() != 1
       || first_fragment_terminal.value().logical_outcomes.front().state
           != DispatchOutcomeState::Revoked
       || first_fragment_terminal.value().logical_outcomes.front().expected_receivers != 2
       || first_fragment_terminal.value().logical_outcomes.front().terminal_receivers != 2
       || first_fragment_terminal.value().logical_outcomes.front().declared_frames != 2
       || first_fragment_terminal.value().logical_outcomes.front().delivered_frames != 1
       || first_fragment_terminal.value().logical_outcomes.front().side_effect_certainty
           != SideEffectCertainty::Partial)
    {
        return failure(__LINE__);
    }

    return EXIT_SUCCESS;
}
