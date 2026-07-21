// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_native_fixture.hpp"

#include <hyperflux/openrgb/plugin_coordinator.hpp>

#include <chrono>
#include <cstdlib>
#include <deque>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <set>
#include <thread>
#include <utility>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-plugin-coordinator-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

class Host final : public hyperflux::openrgb::native::ControllerHost
{
public:
    void register_controller(
        hyperflux::openrgb::native::NativeController& controller) noexcept override
    {
        std::lock_guard lock(mutex);
        if(!registered.insert(&controller).second)
        {
            valid = false;
        }
        ++register_calls;
    }

    void unregister_controller(
        hyperflux::openrgb::native::NativeController& controller) noexcept override
    {
        std::lock_guard lock(mutex);
        if(registered.erase(&controller) != 1)
        {
            valid = false;
        }
        ++unregister_calls;
    }

    [[nodiscard]] std::size_t size() const
    {
        std::lock_guard lock(mutex);
        return registered.size();
    }

    [[nodiscard]] bool is_valid() const
    {
        std::lock_guard lock(mutex);
        return valid;
    }

    mutable std::mutex mutex;
    std::set<const hyperflux::openrgb::native::NativeController*> registered;
    std::size_t register_calls = 0;
    std::size_t unregister_calls = 0;
    bool valid = true;
};

class Dispatcher final : public hyperflux::openrgb::native::ApplicationDispatcher
{
public:
    bool post(std::function<void()> task) noexcept override
    {
        try
        {
            std::lock_guard lock(mutex);
            tasks.push_back(std::move(task));
            return true;
        }
        catch(...)
        {
            return false;
        }
    }

    void drain()
    {
        std::deque<std::function<void()>> ready;
        {
            std::lock_guard lock(mutex);
            ready.swap(tasks);
        }
        for(auto& task : ready)
        {
            task();
        }
    }

private:
    std::mutex mutex;
    std::deque<std::function<void()>> tasks;
};

template <typename Predicate> bool wait_until(Dispatcher& dispatcher, Predicate predicate)
{
    for(std::size_t attempt = 0; attempt < 2'000; ++attempt)
    {
        dispatcher.drain();
        if(predicate())
        {
            return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    dispatcher.drain();
    return predicate();
}

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb;
    using namespace hyperflux::openrgb::native;
    using namespace hyperflux::test;

    Host host;
    Dispatcher dispatcher;
    if(PluginCoordinator::create(host, dispatcher, std::make_unique<FakeBridge>(view(1, 1)), {})
        || host.size() != 0)
    {
        return failure(__LINE__);
    }

    auto bridge = std::make_unique<FakeBridge>(native_integration_view(1, 1));
    auto* bridge_observer = bridge.get();
    bridge_observer->fail_integration_call = 2;
    PluginCoordinatorConfig config;
    config.component_version = "0.0.0-dev.1";
    config.worker.poll_interval_ms = 1;
    config.worker.reconnect_initial_ms = 1;
    config.worker.reconnect_max_ms = 4;
    auto created = PluginCoordinator::create(host, dispatcher, std::move(bridge), config);
    if(!created)
    {
        return failure(__LINE__);
    }
    auto coordinator = std::move(created).value();
    if(!coordinator->start() || coordinator->start())
    {
        coordinator->shutdown();
        return failure(__LINE__);
    }
    if(!wait_until(dispatcher,
           [&]
           {
               const auto status = coordinator->status();
               return status.worker_state == WorkerState::Running && status.controllers == 2
                      && status.registered_controllers == 2;
           }))
    {
        coordinator->shutdown();
        return failure(__LINE__);
    }
    if(host.size() != 2 || !host.is_valid())
    {
        coordinator->shutdown();
        return failure(__LINE__);
    }

    coordinator->detection_started();
    coordinator->detection_started();
    if(host.size() != 0 || !coordinator->status().detection_suspended)
    {
        coordinator->shutdown();
        return failure(__LINE__);
    }
    coordinator->detection_finished();
    coordinator->detection_finished();
    if(host.size() != 2 || coordinator->status().detection_suspended || !host.is_valid())
    {
        coordinator->shutdown();
        return failure(__LINE__);
    }

    if(!wait_until(dispatcher,
           [&]
           {
               const auto status = coordinator->status();
               return bridge_observer->integration_calls >= 3
                      && status.worker_state == WorkerState::Running
                      && status.registered_controllers == 2;
           }))
    {
        coordinator->shutdown();
        return failure(__LINE__);
    }
    if(host.size() != 2 || coordinator->controllers().size() != 2 || !host.is_valid())
    {
        coordinator->shutdown();
        return failure(__LINE__);
    }

    coordinator->shutdown();
    coordinator->shutdown();
    dispatcher.drain();
    coordinator->detection_started();
    coordinator->detection_finished();
    const auto stopped = coordinator->status();
    if(!stopped.stopped || stopped.worker_state != WorkerState::Stopped || stopped.controllers != 0
        || stopped.registered_controllers != 0 || host.size() != 0 || !host.is_valid())
    {
        return failure(__LINE__);
    }
    return EXIT_SUCCESS;
}
