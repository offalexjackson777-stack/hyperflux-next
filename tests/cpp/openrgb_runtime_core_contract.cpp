// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_runtime_fixture.hpp"

#include <cstdlib>
#include <iostream>
#include <utility>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-runtime-core-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb;
    using namespace hyperflux::test;

    FakeBridge bridge(view(1, 1));
    auto invalid_config = RuntimeConfig {};
    invalid_config.lease_renew_margin_ms = invalid_config.lease_duration_ms;
    if(RuntimeCore::create(bridge, invalid_config))
    {
        return failure(__LINE__);
    }

    auto created = RuntimeCore::create(bridge);
    if(!created)
    {
        return failure(__LINE__);
    }
    auto runtime = std::move(created).value();
    const auto initialized = runtime.initialize();
    if(!initialized || !runtime.initialized() || runtime.controllers().size() != 2
       || initialized.value().controller_changes.size() != 2
       || initialized.value().controller_changes.front().kind != ReconcileKind::Added)
    {
        return failure(__LINE__);
    }
    const auto rescanned = runtime.rescan();
    if(!rescanned || rescanned.value().controller_changes.size() != 2
       || rescanned.value().controller_changes.front().kind != ReconcileKind::Retained)
    {
        return failure(__LINE__);
    }

    const auto mouse = frame(model(runtime, DeviceKind::Mouse), 1);
    const auto keyboard = frame(model(runtime, DeviceKind::Keyboard), 3);
    if(runtime.enqueue_effect(mouse, 100) != EnqueueDisposition::Accepted
       || runtime.enqueue_effect(frame(model(runtime, DeviceKind::Mouse), 2), 101)
           != EnqueueDisposition::Coalesced
       || runtime.enqueue_effect(keyboard, 102) != EnqueueDisposition::Accepted
       || !runtime.step(103) || !bridge.submissions.empty())
    {
        return failure(__LINE__);
    }
    const auto first_dispatch = runtime.step(104);
    if(!first_dispatch || bridge.acquire_count != 1 || bridge.leased_resources.size() != 2
       || bridge.submissions.size() != 1 || bridge.submissions.back().frames.size() != 2
       || runtime.pending_transaction_count() != 1)
    {
        return failure(__LINE__);
    }
    bool latest_mouse_frame = false;
    for(const auto& submitted_frame : bridge.submissions.back().frames)
    {
        if(submitted_frame.device_id.value() == "mouse")
        {
            latest_mouse_frame = submitted_frame.colors.front().red.value() == 2;
        }
    }
    if(!latest_mouse_frame)
    {
        return failure(__LINE__);
    }
    bridge.complete_last(
        TransactionState::Succeeded,
        2,
        SideEffectCertainty::Committed,
        true);
    const auto first_terminal = runtime.step(105);
    if(!first_terminal || first_terminal.value().dispatch_outcomes.size() != 1
       || first_terminal.value().dispatch_outcomes.front().state
           != DispatchOutcomeState::Succeeded
       || runtime.pending_transaction_count() != 0)
    {
        return failure(__LINE__);
    }

    if(runtime.enqueue_effect(frame(model(runtime, DeviceKind::Mouse), 10), 110)
           != EnqueueDisposition::Accepted
       || !runtime.step(114) || bridge.submissions.size() != 2
       || runtime.enqueue_effect(frame(model(runtime, DeviceKind::Mouse), 20), 114)
           != EnqueueDisposition::Accepted
       || runtime.enqueue_effect(frame(model(runtime, DeviceKind::Mouse), 21), 115)
           != EnqueueDisposition::Coalesced
       || runtime.enqueue_effect(frame(model(runtime, DeviceKind::Keyboard), 22), 115)
           != EnqueueDisposition::Accepted
       || !runtime.step(119) || bridge.submissions.size() != 2)
    {
        return failure(__LINE__);
    }
    bridge.complete_last(
        TransactionState::Succeeded,
        1,
        SideEffectCertainty::Committed,
        true);
    const auto coalesced_dispatch = runtime.step(120);
    if(!coalesced_dispatch || bridge.submissions.size() != 3
       || bridge.submissions.back().frames.size() != 2)
    {
        return failure(__LINE__);
    }
    latest_mouse_frame = false;
    for(const auto& submitted_frame : bridge.submissions.back().frames)
    {
        if(submitted_frame.device_id.value() == "mouse")
        {
            latest_mouse_frame = submitted_frame.colors.front().red.value() == 21;
        }
    }
    if(!latest_mouse_frame)
    {
        return failure(__LINE__);
    }
    bridge.complete_last(
        TransactionState::Succeeded,
        1,
        SideEffectCertainty::Committed,
        true);
    const auto incomplete_terminal = runtime.step(121);
    if(!incomplete_terminal || incomplete_terminal.value().dispatch_outcomes.size() != 1
       || incomplete_terminal.value().dispatch_outcomes.front().state
           != DispatchOutcomeState::Failed)
    {
        return failure(__LINE__);
    }

    bridge.current = view(2, 2);
    bridge.event(EventKind::GenerationReplaced);
    const auto generation = runtime.step(122);
    if(!generation || !generation.value().full_refresh
       || model(runtime, DeviceKind::Mouse).authority.generation_id.value() != 2)
    {
        return failure(__LINE__);
    }
    if(runtime.enqueue_stable(
           sdk::LightingIntent::Static,
           {frame(model(runtime, DeviceKind::Mouse), 30)})
           != EnqueueDisposition::Accepted)
    {
        return failure(__LINE__);
    }
    const auto new_generation_dispatch = runtime.step(123);
    if(!new_generation_dispatch || bridge.acquire_count != 2
       || bridge.submissions.back().generation_id.value() != 2)
    {
        return failure(__LINE__);
    }
    bridge.complete_last(
        TransactionState::Succeeded,
        1,
        SideEffectCertainty::Committed,
        true);
    if(!runtime.step(124))
    {
        return failure(__LINE__);
    }

    bridge.current = view(2, 3);
    bridge.event(EventKind::BatteryUpdated, true);
    const auto gap = runtime.step(125);
    if(!gap || !gap.value().cursor_gap_recovered || !gap.value().full_refresh)
    {
        return failure(__LINE__);
    }

    bridge.current = view(3, 4);
    bridge.event(EventKind::GenerationReplaced);
    if(!runtime.step(126))
    {
        return failure(__LINE__);
    }
    bridge.conflict = true;
    if(runtime.enqueue_stable(
           sdk::LightingIntent::Static,
           {frame(model(runtime, DeviceKind::Mouse), 40)})
           != EnqueueDisposition::Accepted)
    {
        return failure(__LINE__);
    }
    const auto conflicted = runtime.step(127);
    if(!conflicted || conflicted.value().dispatch_outcomes.size() != 1
       || conflicted.value().dispatch_outcomes.front().state
           != DispatchOutcomeState::Rejected
       || !conflicted.value().dispatch_outcomes.front().local_error.has_value()
       || conflicted.value().dispatch_outcomes.front().local_error->code
           != sdk::ErrorCode::OwnershipConflict)
    {
        return failure(__LINE__);
    }

    bridge.conflict = false;
    bridge.current = view(3, 5, ControllerAvailability::Sleeping);
    bridge.event(EventKind::DeviceSleeping);
    if(!runtime.step(128)
       || runtime.enqueue_effect(frame(model(runtime, DeviceKind::Mouse), 50), 128)
           != EnqueueDisposition::Accepted)
    {
        return failure(__LINE__);
    }
    const auto sleeping = runtime.step(132);
    if(!sleeping || sleeping.value().dispatch_outcomes.size() != 1
       || sleeping.value().dispatch_outcomes.front().state
           != DispatchOutcomeState::Rejected
       || !sleeping.value().dispatch_outcomes.front().local_error.has_value()
       || sleeping.value().dispatch_outcomes.front().local_error->finding_id
           != "HFX-LIFECYCLE-001")
    {
        return failure(__LINE__);
    }

    if(runtime.enqueue_effect(frame(model(runtime, DeviceKind::Mouse), 51), 133)
           != EnqueueDisposition::Accepted
       || runtime.enqueue_effect(frame(model(runtime, DeviceKind::Keyboard), 52), 133)
           != EnqueueDisposition::Accepted)
    {
        return failure(__LINE__);
    }
    bridge.terminal_on_submit = true;
    const auto sleeping_sibling = runtime.step(137);
    if(!sleeping_sibling || sleeping_sibling.value().dispatch_outcomes.size() != 2
       || sleeping_sibling.value().dispatch_outcomes[0].state
           != DispatchOutcomeState::Rejected
       || sleeping_sibling.value().dispatch_outcomes[1].state
           != DispatchOutcomeState::Succeeded
       || bridge.submissions.back().frames.size() != 1
       || bridge.submissions.back().frames.front().device_id.value() != "keyboard")
    {
        return failure(__LINE__);
    }

    bridge.current = view(3, 6);
    bridge.event(EventKind::DeviceAvailable);
    const auto releases_before_return = bridge.release_count;
    if(!runtime.step(138)
       || bridge.release_count != releases_before_return + 1
       || runtime.enqueue_effect(frame(model(runtime, DeviceKind::Mouse), 53), 139)
           != EnqueueDisposition::Accepted
       || runtime.enqueue_effect(frame(model(runtime, DeviceKind::Keyboard), 54), 139)
           != EnqueueDisposition::Accepted)
    {
        return failure(__LINE__);
    }
    const auto returned_siblings = runtime.step(143);
    if(!returned_siblings || returned_siblings.value().dispatch_outcomes.size() != 1
       || returned_siblings.value().dispatch_outcomes.front().state
           != DispatchOutcomeState::Succeeded
       || bridge.submissions.back().frames.size() != 2)
    {
        return failure(__LINE__);
    }

    bridge.current = view(4, 7);
    bridge.event(EventKind::GenerationReplaced);
    if(!runtime.step(144)
       || runtime.enqueue_stable(
              sdk::LightingIntent::Static,
              {frame(model(runtime, DeviceKind::Mouse), 60)})
           != EnqueueDisposition::Accepted)
    {
        return failure(__LINE__);
    }
    const auto final_dispatch = runtime.step(145);
    if(!final_dispatch || final_dispatch.value().dispatch_outcomes.size() != 1
       || final_dispatch.value().dispatch_outcomes.front().state
           != DispatchOutcomeState::Succeeded)
    {
        return failure(__LINE__);
    }
    const auto releases_before_shutdown = bridge.release_count;
    const auto shutdown = runtime.shutdown();
    if(runtime.initialized()
       || bridge.release_count != releases_before_shutdown + 1
       || !shutdown.dispatch_outcomes.empty())
    {
        return failure(__LINE__);
    }
    return EXIT_SUCCESS;
}
