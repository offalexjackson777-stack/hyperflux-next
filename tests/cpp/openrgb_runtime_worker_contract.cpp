// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_runtime_fixture.hpp"

#include <hyperflux/openrgb/runtime_bridge.hpp>
#include <hyperflux/openrgb/runtime_worker.hpp>

#include <atomic>
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
    std::uint8_t red,
    std::string receiver = "receiver-1")
{
    return {
        hyperflux::test::text<hyperflux::ReceiverId>(receiver),
        hyperflux::test::number<hyperflux::GenerationId>(1),
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
    auto startup_created = RuntimeWorker::create(std::move(startup_bridge),
        startup_config,
        {[&](RuntimeStep output)
            {
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
    bool startup_ready = false;
    {
        std::unique_lock lock(startup_mutex);
        startup_ready = startup_changed.wait_for(lock,
            std::chrono::seconds(2),
            [&]
            {
                return startup_refreshes == 1 && startup_notices == 1;
            });
    }
    if(!startup_ready)
    {
        startup_worker->stop();
        return failure(__LINE__);
    }
    if(startup_worker->state() != WorkerState::Running || startup_worker->controllers().size() != 2
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
    std::size_t preserved = 0;
    std::size_t recovery_notices = 0;
    std::optional<sdk::Error> callback_error;
    std::thread::id callback_thread;
    const auto caller_thread = std::this_thread::get_id();

    WorkerConfig config;
    config.poll_interval_ms = 1;
    config.reconnect_initial_ms = 1;
    config.reconnect_max_ms = 4;
    config.runtime.dispatch_queue.effect_window_ms = 8;
    auto created = RuntimeWorker::create(std::move(bridge),
        config,
        {
            [&](RuntimeStep output)
            {
                std::lock_guard lock(mutex);
                callback_thread = std::this_thread::get_id();
                full_refreshes += output.full_refresh ? 1U : 0U;
                for(const auto& change : output.controller_changes)
                {
                    preserved += change.kind == ReconcileKind::Retained
                            || change.kind == ReconcileKind::StateUpdated
                        ? 1U
                        : 0U;
                }
                for(const auto& outcome : output.dispatch_outcomes)
                {
                    succeeded += outcome.state == DispatchOutcomeState::Succeeded ? 1U : 0U;
                }
                recovery_notices += output.notices.size();
                changed.notify_all();
            },
            [&](sdk::Error error)
            {
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
    if(worker->enqueue_effect(mouse))
    {
        return failure(__LINE__);
    }
    if(!worker->start())
    {
        return failure(__LINE__);
    }
    bool initialized_ready = false;
    {
        std::unique_lock lock(mutex);
        initialized_ready = changed.wait_for(lock,
            std::chrono::seconds(2),
            [&]
            {
                return full_refreshes >= 1 || callback_error.has_value();
            });
    }
    if(!initialized_ready || callback_error.has_value())
    {
        worker->stop();
        return failure(__LINE__);
    }
    const auto mouse_enqueued = worker->enqueue_effect(mouse);
    const auto keyboard_enqueued = worker->enqueue_effect(keyboard);
    if(!mouse_enqueued || !keyboard_enqueued
        || mouse_enqueued.value() != EnqueueDisposition::Accepted
        || keyboard_enqueued.value() != EnqueueDisposition::Accepted)
    {
        return failure(__LINE__);
    }

    bool first_effect_ready = false;
    {
        std::unique_lock lock(mutex);
        first_effect_ready = changed.wait_for(lock,
            std::chrono::seconds(2),
            [&]
            {
                return succeeded >= 1 || callback_error.has_value();
            });
    }
    if(!first_effect_ready)
    {
        worker->stop();
        return failure(__LINE__);
    }
    if(callback_error.has_value() || worker->state() != WorkerState::Running
        || worker->controllers().size() != 2 || callback_thread == caller_thread)
    {
        worker->stop();
        return failure(__LINE__);
    }

    const auto first_stable = worker->enqueue_stable(
        sdk::LightingIntent::Static,
        {queued("receiver-1/mouse/child.test.mouse", 13, 30)});
    const auto second_stable = worker->enqueue_stable(
        sdk::LightingIntent::Static,
        {queued("receiver-1/keyboard/child.test.keyboard", 102, 40)});
    if(!first_stable || first_stable.value() != EnqueueDisposition::Accepted
       || !second_stable || second_stable.value() != EnqueueDisposition::Coalesced)
    {
        worker->stop();
        return failure(__LINE__);
    }
    bool stable_ready = false;
    {
        std::unique_lock lock(mutex);
        stable_ready = changed.wait_for(lock,
            std::chrono::seconds(2),
            [&]
            {
                return succeeded >= 2 || callback_error.has_value();
            });
    }
    if(!stable_ready)
    {
        worker->stop();
        return failure(__LINE__);
    }
    if(!worker->request_rescan())
    {
        worker->stop();
        return failure(__LINE__);
    }
    bool rescan_ready = false;
    {
        std::unique_lock lock(mutex);
        rescan_ready = changed.wait_for(lock,
            std::chrono::seconds(2),
            [&]
            {
                return preserved >= 2 || callback_error.has_value();
            });
    }
    if(!rescan_ready)
    {
        worker->stop();
        return failure(__LINE__);
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
                  << " second=" << bridge_observer->submissions.back().frames.size() << '\n';
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

    auto multi_bridge = std::make_unique<FakeBridge>(multi_receiver_view(1, 1));
    auto* multi_observer = multi_bridge.get();
    multi_observer->terminal_on_submit = true;
    multi_observer->lease_expiry_ms = std::numeric_limits<std::uint64_t>::max();
    std::atomic_size_t multi_logical_success {0};
    WorkerConfig multi_config;
    multi_config.poll_interval_ms = 1;
    multi_config.stable_callback_window_ms = 20;
    auto multi_created = RuntimeWorker::create(std::move(multi_bridge),
        multi_config,
        {[&](RuntimeStep output)
            {
                for(const auto& outcome : output.logical_outcomes)
                {
                    if(outcome.state == DispatchOutcomeState::Succeeded
                       && outcome.expected_receivers == 2
                       && outcome.terminal_receivers == 2)
                    {
                        multi_logical_success.fetch_add(1, std::memory_order_release);
                    }
                }
            },
            {}});
    if(!multi_created)
    {
        return failure(__LINE__);
    }
    auto multi_worker = std::move(multi_created).value();
    if(!multi_worker->start())
    {
        return failure(__LINE__);
    }
    for(std::size_t attempt = 0;
        attempt < 2'000 && multi_worker->controllers().size() != 2;
        ++attempt)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    const auto multi_first = multi_worker->enqueue_stable(
        sdk::LightingIntent::Static,
        {queued("receiver-1/mouse-a/child.test.mouse-a", 13, 41, "receiver-1")});
    const auto multi_second = multi_worker->enqueue_stable(
        sdk::LightingIntent::Static,
        {queued("receiver-2/mouse-b/child.test.mouse-b", 13, 42, "receiver-2")});
    if(multi_worker->controllers().size() != 2 || !multi_first || !multi_second
       || multi_first.value() != EnqueueDisposition::Accepted
       || multi_second.value() != EnqueueDisposition::Coalesced)
    {
        multi_worker->stop();
        return failure(__LINE__);
    }
    for(std::size_t attempt = 0;
        attempt < 2'000
        && (multi_observer->submission_count.load(std::memory_order_acquire) != 2
            || multi_logical_success.load(std::memory_order_acquire) != 1);
        ++attempt)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    multi_worker->stop();
    if(multi_observer->acquire_count != 1
       || multi_observer->lease_acquisitions.size() != 1
       || multi_observer->lease_acquisitions.front().size() != 2
       || multi_observer->submissions.size() != 2
       || multi_observer->submissions[0].lease_id != multi_observer->submissions[1].lease_id
       || multi_logical_success.load(std::memory_order_acquire) != 1)
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
    auto bounded_created = RuntimeWorker::create(std::move(bounded_bridge),
        bounded_config,
        {[&](RuntimeStep output)
            {
                std::lock_guard lock(bounded_mutex);
                for(const auto& change : output.controller_changes)
                {
                    bounded_retained += change.kind == ReconcileKind::Retained
                            || change.kind == ReconcileKind::StateUpdated
                        ? 1U
                        : 0U;
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
        attempt < 2'000 && bounded_observer->submission_count.load(std::memory_order_acquire) == 0;
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
        || !bounded_worker->request_rescan())
    {
        bounded_worker->stop();
        return failure(__LINE__);
    }
    bool first_bounded_rescan_ready = false;
    {
        std::unique_lock lock(bounded_mutex);
        first_bounded_rescan_ready = bounded_changed.wait_for(lock,
            std::chrono::seconds(2),
            [&]
            {
                return bounded_retained >= 2;
            });
    }
    if(!first_bounded_rescan_ready)
    {
        bounded_worker->stop();
        return failure(__LINE__);
    }
    const auto bounded_stable = bounded_worker->enqueue_stable(
        sdk::LightingIntent::Static,
        {queued("receiver-1/mouse/child.test.mouse", 13, 51)});
    if(!bounded_stable || bounded_stable.value() != EnqueueDisposition::Accepted)
    {
        bounded_worker->stop();
        return failure(__LINE__);
    }
    const auto queued_effect =
        bounded_worker->enqueue_effect(queued("receiver-1/mouse/child.test.mouse", 13, 52));
    if(!queued_effect || queued_effect.value() != EnqueueDisposition::Accepted
        || !bounded_worker->request_rescan())
    {
        bounded_worker->stop();
        return failure(__LINE__);
    }
    bool second_bounded_rescan_ready = false;
    {
        std::unique_lock lock(bounded_mutex);
        second_bounded_rescan_ready = bounded_changed.wait_for(lock,
            std::chrono::seconds(2),
            [&]
            {
                return bounded_retained >= 4;
            });
    }
    if(!second_bounded_rescan_ready)
    {
        bounded_worker->stop();
        return failure(__LINE__);
    }
    const auto rejected_effect =
        bounded_worker->enqueue_effect(queued("receiver-1/keyboard/child.test.keyboard", 102, 53));
    const auto rejected_stable = bounded_worker->enqueue_stable(
        sdk::LightingIntent::Static, {queued("receiver-1/keyboard/child.test.keyboard", 102, 54)});
    if(!rejected_effect || rejected_effect.value() != EnqueueDisposition::RejectedCapacity
        || !rejected_stable
        || rejected_stable.value() != EnqueueDisposition::RejectedCapacity)
    {
        bounded_worker->stop();
        return failure(__LINE__);
    }
    bounded_worker->stop();

    auto shutdown_bridge = std::make_unique<FakeBridge>(view(1, 1));
    auto* shutdown_observer = shutdown_bridge.get();
    std::mutex shutdown_mutex;
    std::condition_variable shutdown_changed;
    bool shutdown_initialized = false;
    std::size_t shutdown_revoked = 0;
    WorkerConfig shutdown_config;
    shutdown_config.poll_interval_ms = 1;
    shutdown_config.stable_callback_window_ms = 500;
    auto shutdown_created = RuntimeWorker::create(std::move(shutdown_bridge),
        shutdown_config,
        {[&](RuntimeStep output)
            {
                std::lock_guard lock(shutdown_mutex);
                shutdown_initialized = shutdown_initialized || output.full_refresh;
                for(const auto& outcome : output.logical_outcomes)
                {
                    shutdown_revoked += outcome.state == DispatchOutcomeState::Revoked
                        ? 1U
                        : 0U;
                }
                shutdown_changed.notify_all();
            },
            {}});
    if(!shutdown_created)
    {
        return failure(__LINE__);
    }
    auto shutdown_worker = std::move(shutdown_created).value();
    if(!shutdown_worker->start())
    {
        return failure(__LINE__);
    }
    bool shutdown_ready = false;
    {
        std::unique_lock lock(shutdown_mutex);
        shutdown_ready = shutdown_changed.wait_for(lock,
            std::chrono::seconds(2),
            [&] { return shutdown_initialized; });
    }
    const auto shutdown_command = shutdown_worker->enqueue_stable(
        sdk::LightingIntent::Static,
        {queued("receiver-1/mouse/child.test.mouse", 13, 60)});
    if(!shutdown_ready || !shutdown_command
       || shutdown_command.value() != EnqueueDisposition::Accepted)
    {
        shutdown_worker->stop();
        return failure(__LINE__);
    }
    shutdown_worker->stop();
    if(shutdown_worker->state() != WorkerState::Stopped || shutdown_revoked != 1
       || !shutdown_observer->submissions.empty())
    {
        return failure(__LINE__);
    }

    RuntimeWorker* callback_stopped_observer = nullptr;
    auto callback_stopped = RuntimeWorker::create(std::make_unique<FakeBridge>(view(1, 1)),
        config,
        {[&](RuntimeStep)
            {
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
