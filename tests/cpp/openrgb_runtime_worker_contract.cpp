// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_runtime_fixture.hpp"

#include <hyperflux/openrgb/runtime_bridge.hpp>
#include <hyperflux/openrgb/runtime_worker.hpp>

#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-runtime-worker-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

hyperflux::openrgb::QueuedLightingFrame queued(
    std::string stable_id,
    std::size_t slots,
    std::uint8_t red)
{
    return {
        std::move(stable_id),
        slots,
        std::vector<hyperflux::v5::RgbColor>(slots, hyperflux::test::color(red)),
    };
}

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb;
    using namespace hyperflux::test;

    if(ClientRuntimeBridge::create(std::unique_ptr<sdk::ClientApi> {}))
    {
        return failure(__LINE__);
    }

    auto startup_bridge = std::make_unique<FakeBridge>(view(1, 1));
    startup_bridge->fail_integration_call = 1;
    std::mutex startup_mutex;
    std::condition_variable startup_changed;
    std::size_t startup_refreshes = 0;
    std::size_t startup_notices = 0;
    WorkerConfig startup_config;
    startup_config.poll_interval_ms = 1;
    startup_config.reconnect_initial_ms = 1;
    startup_config.reconnect_max_ms = 4;
    auto startup_created = RuntimeWorker::create(
        std::move(startup_bridge),
        startup_config,
        {[&](RuntimeStep output) {
             std::lock_guard lock(startup_mutex);
             startup_refreshes += output.full_refresh ? 1U : 0U;
             startup_notices += output.notices.size();
             startup_changed.notify_all();
         },
         {}});
    if(!startup_created)
    {
        return failure(__LINE__);
    }
    auto startup_worker = std::move(startup_created).value();
    if(!startup_worker->start())
    {
        return failure(__LINE__);
    }
    {
        std::unique_lock lock(startup_mutex);
        if(!startup_changed.wait_for(lock, std::chrono::seconds(2), [&] {
               return startup_refreshes == 1 && startup_notices == 1;
           }))
        {
            startup_worker->stop();
            return failure(__LINE__);
        }
    }
    if(startup_worker->state() != WorkerState::Running
       || startup_worker->controllers().size() != 2
       || startup_worker->last_error().has_value())
    {
        startup_worker->stop();
        return failure(__LINE__);
    }
    startup_worker->stop();

    auto bridge = std::make_unique<FakeBridge>(view(1, 1));
    auto* bridge_observer = bridge.get();
    bridge_observer->terminal_on_submit = true;
    bridge_observer->lease_expiry_ms = std::numeric_limits<std::uint64_t>::max();
    bridge_observer->fail_integration_call = 2;

    std::mutex mutex;
    std::condition_variable changed;
    std::size_t full_refreshes = 0;
    std::size_t succeeded = 0;
    std::size_t retained = 0;
    std::size_t recovery_notices = 0;
    std::optional<sdk::Error> callback_error;
    std::thread::id callback_thread;
    const auto caller_thread = std::this_thread::get_id();

    WorkerConfig config;
    config.poll_interval_ms = 1;
    config.reconnect_initial_ms = 1;
    config.reconnect_max_ms = 4;
    config.runtime.dispatch_queue.effect_window_ms = 8;
    auto created = RuntimeWorker::create(
        std::move(bridge),
        config,
        {
            [&](RuntimeStep output) {
                std::lock_guard lock(mutex);
                callback_thread = std::this_thread::get_id();
                full_refreshes += output.full_refresh ? 1U : 0U;
                for(const auto& change : output.controller_changes)
                {
                    retained += change.kind == ReconcileKind::Retained ? 1U : 0U;
                }
                for(const auto& outcome : output.dispatch_outcomes)
                {
                    succeeded += outcome.state == DispatchOutcomeState::Succeeded ? 1U : 0U;
                }
                recovery_notices += output.notices.size();
                changed.notify_all();
            },
            [&](sdk::Error error) {
                std::lock_guard lock(mutex);
                callback_error = std::move(error);
                changed.notify_all();
            },
        });
    if(!created)
    {
        return failure(__LINE__);
    }
    auto worker = std::move(created).value();

    const auto mouse = queued("receiver-1/mouse/child.test.mouse", 13, 10);
    const auto keyboard = queued("receiver-1/keyboard/child.test.keyboard", 102, 20);
    const auto mouse_enqueued = worker->enqueue_effect(mouse);
    const auto keyboard_enqueued = worker->enqueue_effect(keyboard);
    if(!mouse_enqueued || !keyboard_enqueued
       || mouse_enqueued.value() != EnqueueDisposition::Accepted
       || keyboard_enqueued.value() != EnqueueDisposition::Accepted
       || !worker->start())
    {
        return failure(__LINE__);
    }

    {
        std::unique_lock lock(mutex);
        if(!changed.wait_for(lock, std::chrono::seconds(2), [&] {
               return succeeded >= 1 || callback_error.has_value();
           }))
        {
            worker->stop();
            return failure(__LINE__);
        }
    }
    if(callback_error.has_value() || worker->state() != WorkerState::Running
       || worker->controllers().size() != 2 || callback_thread == caller_thread)
    {
        worker->stop();
        return failure(__LINE__);
    }

    if(worker->enqueue_stable(
           sdk::LightingIntent::Static,
           {
               queued("receiver-1/mouse/child.test.mouse", 13, 30),
               queued("receiver-1/keyboard/child.test.keyboard", 102, 40),
           }) != EnqueueDisposition::Accepted)
    {
        worker->stop();
        return failure(__LINE__);
    }
    {
        std::unique_lock lock(mutex);
        if(!changed.wait_for(lock, std::chrono::seconds(2), [&] {
               return succeeded >= 2 || callback_error.has_value();
           }))
        {
            worker->stop();
            return failure(__LINE__);
        }
    }
    if(!worker->request_rescan())
    {
        worker->stop();
        return failure(__LINE__);
    }
    {
        std::unique_lock lock(mutex);
        if(!changed.wait_for(lock, std::chrono::seconds(2), [&] {
               return retained >= 2 || callback_error.has_value();
           }))
        {
            worker->stop();
            return failure(__LINE__);
        }
    }

    worker->stop();
    if(callback_error.has_value() || worker->state() != WorkerState::Stopped
       || worker->last_error().has_value() || recovery_notices != 1)
    {
        return failure(__LINE__);
    }
    if(bridge_observer->submissions.size() != 2)
    {
        return failure(__LINE__);
    }
    if(bridge_observer->submissions.front().frames.size() != 2
       || bridge_observer->submissions.back().frames.size() != 2)
    {
        std::cerr << "observed frame counts: first="
                  << bridge_observer->submissions.front().frames.size()
                  << " second="
                  << bridge_observer->submissions.back().frames.size() << '\n';
        for(const auto& submission : bridge_observer->submissions)
        {
            std::cerr << "submission";
            for(const auto& submitted_frame : submission.frames)
            {
                std::cerr << ' ' << submitted_frame.device_id.value();
            }
            std::cerr << '\n';
        }
        return failure(__LINE__);
    }
    if(bridge_observer->release_count != 1)
    {
        return failure(__LINE__);
    }
    if(full_refreshes < 2)
    {
        return failure(__LINE__);
    }

    auto bounded_bridge = std::make_unique<FakeBridge>(view(1, 1));
    auto* bounded_observer = bounded_bridge.get();
    bounded_observer->lease_expiry_ms = std::numeric_limits<std::uint64_t>::max();
    std::mutex bounded_mutex;
    std::condition_variable bounded_changed;
    std::size_t bounded_retained = 0;
    WorkerConfig bounded_config;
    bounded_config.poll_interval_ms = 1;
    bounded_config.reconnect_initial_ms = 1;
    bounded_config.reconnect_max_ms = 4;
    bounded_config.runtime.dispatch_queue.effect_window_ms = 1;
    bounded_config.runtime.dispatch_queue.stable_capacity = 1;
    bounded_config.runtime.dispatch_queue.effect_target_capacity = 1;
    auto bounded_created = RuntimeWorker::create(
        std::move(bounded_bridge),
        bounded_config,
        {[&](RuntimeStep output) {
             std::lock_guard lock(bounded_mutex);
             for(const auto& change : output.controller_changes)
             {
                 bounded_retained += change.kind == ReconcileKind::Retained ? 1U : 0U;
             }
             bounded_changed.notify_all();
         },
         {}});
    if(!bounded_created)
    {
        return failure(__LINE__);
    }
    auto bounded_worker = std::move(bounded_created).value();
    if(!bounded_worker->start())
    {
        return failure(__LINE__);
    }
    for(std::size_t attempt = 0;
        attempt < 2'000
        && bounded_observer->submission_count.load(std::memory_order_acquire) == 0;
        ++attempt)
    {
        if(attempt == 0)
        {
            const auto enqueued = bounded_worker->enqueue_effect(
                queued("receiver-1/keyboard/child.test.keyboard", 102, 50));
            if(!enqueued || enqueued.value() != EnqueueDisposition::Accepted)
            {
                bounded_worker->stop();
                return failure(__LINE__);
            }
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    if(bounded_observer->submission_count.load(std::memory_order_acquire) != 1
       || bounded_worker->enqueue_stable(
              sdk::LightingIntent::Static,
              {queued("receiver-1/mouse/child.test.mouse", 13, 51)})
           != EnqueueDisposition::Accepted)
    {
        bounded_worker->stop();
        return failure(__LINE__);
    }
    const auto queued_effect = bounded_worker->enqueue_effect(
        queued("receiver-1/mouse/child.test.mouse", 13, 52));
    if(!queued_effect || queued_effect.value() != EnqueueDisposition::Accepted
       || !bounded_worker->request_rescan())
    {
        bounded_worker->stop();
        return failure(__LINE__);
    }
    {
        std::unique_lock lock(bounded_mutex);
        if(!bounded_changed.wait_for(lock, std::chrono::seconds(2), [&] {
               return bounded_retained >= 2;
           }))
        {
            bounded_worker->stop();
            return failure(__LINE__);
        }
    }
    const auto rejected_effect = bounded_worker->enqueue_effect(
        queued("receiver-1/keyboard/child.test.keyboard", 102, 53));
    const auto rejected_stable = bounded_worker->enqueue_stable(
        sdk::LightingIntent::Static,
        {queued("receiver-1/keyboard/child.test.keyboard", 102, 54)});
    if(!rejected_effect
       || rejected_effect.value() != EnqueueDisposition::RejectedCapacity
       || rejected_stable != EnqueueDisposition::RejectedCapacity)
    {
        bounded_worker->stop();
        return failure(__LINE__);
    }
    bounded_worker->stop();

    RuntimeWorker* callback_stopped_observer = nullptr;
    auto callback_stopped = RuntimeWorker::create(
        std::make_unique<FakeBridge>(view(1, 1)),
        config,
        {[&](RuntimeStep) {
             if(callback_stopped_observer != nullptr)
             {
                 callback_stopped_observer->stop();
             }
         },
         {}});
    if(!callback_stopped)
    {
        return failure(__LINE__);
    }
    auto callback_stopped_worker = std::move(callback_stopped).value();
    callback_stopped_observer = callback_stopped_worker.get();
    if(!callback_stopped_worker->start())
    {
        return failure(__LINE__);
    }
    for(std::size_t attempt = 0;
        attempt < 200 && callback_stopped_worker->state() != WorkerState::Stopped;
        ++attempt)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    if(callback_stopped_worker->state() != WorkerState::Stopped)
    {
        callback_stopped_worker->stop();
        return failure(__LINE__);
    }
    callback_stopped_worker->stop();
    callback_stopped_worker.reset();
    return EXIT_SUCCESS;
}
