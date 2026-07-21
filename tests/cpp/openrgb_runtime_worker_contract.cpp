// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_runtime_fixture.hpp"

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

    auto bridge = std::make_unique<FakeBridge>(view(1, 1));
    auto* bridge_observer = bridge.get();
    bridge_observer->terminal_on_submit = true;
    bridge_observer->lease_expiry_ms = std::numeric_limits<std::uint64_t>::max();

    std::mutex mutex;
    std::condition_variable changed;
    std::size_t full_refreshes = 0;
    std::size_t succeeded = 0;
    std::size_t retained = 0;
    std::optional<sdk::Error> callback_error;
    std::thread::id callback_thread;
    const auto caller_thread = std::this_thread::get_id();

    WorkerConfig config;
    config.poll_interval_ms = 1;
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
       || worker->last_error().has_value())
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
