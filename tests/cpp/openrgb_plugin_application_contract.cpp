// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_native_fixture.hpp"

#include <hyperflux/openrgb/plugin_application.hpp>

#include <QCoreApplication>

#include <chrono>
#include <cstdlib>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <set>
#include <thread>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-plugin-application-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

struct HostState
{
    std::mutex mutex;
    hyperflux::openrgb::native::DetectionCallback start = nullptr;
    hyperflux::openrgb::native::DetectionCallback end = nullptr;
    void* start_context = nullptr;
    void* end_context = nullptr;
    std::set<const hyperflux::openrgb::native::NativeController*> controllers;
    std::set<std::uintptr_t> original_identities;
    std::size_t register_calls = 0;
    std::size_t unregister_calls = 0;
    std::size_t waits = 0;
    bool valid = true;
};

class Host final : public hyperflux::openrgb::native::PluginHost
{
public:
    explicit Host(std::shared_ptr<HostState> state) : state_(std::move(state)) {}

    void register_controller(
        hyperflux::openrgb::native::NativeController& controller) noexcept override
    {
        std::lock_guard lock(state_->mutex);
        if(!state_->controllers.insert(&controller).second)
        {
            state_->valid = false;
        }
        state_->original_identities.insert(reinterpret_cast<std::uintptr_t>(&controller));
        ++state_->register_calls;
    }

    void unregister_controller(
        hyperflux::openrgb::native::NativeController& controller) noexcept override
    {
        std::lock_guard lock(state_->mutex);
        if(state_->controllers.erase(&controller) != 1)
        {
            state_->valid = false;
        }
        ++state_->unregister_calls;
    }

    void register_detection_start(
        hyperflux::openrgb::native::DetectionCallback callback,
        void* context) noexcept override
    {
        std::lock_guard lock(state_->mutex);
        state_->start = callback;
        state_->start_context = context;
    }

    void register_detection_end(
        hyperflux::openrgb::native::DetectionCallback callback,
        void* context) noexcept override
    {
        std::lock_guard lock(state_->mutex);
        state_->end = callback;
        state_->end_context = context;
    }

    void unregister_detection_start(
        hyperflux::openrgb::native::DetectionCallback callback,
        void* context) noexcept override
    {
        std::lock_guard lock(state_->mutex);
        if(state_->start != callback || state_->start_context != context)
        {
            state_->valid = false;
        }
        state_->start = nullptr;
        state_->start_context = nullptr;
    }

    void unregister_detection_end(
        hyperflux::openrgb::native::DetectionCallback callback,
        void* context) noexcept override
    {
        std::lock_guard lock(state_->mutex);
        if(state_->end != callback || state_->end_context != context)
        {
            state_->valid = false;
        }
        state_->end = nullptr;
        state_->end_context = nullptr;
    }

    void wait_for_detection() noexcept override
    {
        std::lock_guard lock(state_->mutex);
        ++state_->waits;
    }

private:
    std::shared_ptr<HostState> state_;
};

template<typename Predicate> bool wait_until(Predicate predicate)
{
    for(std::size_t attempt = 0; attempt < 2'000; ++attempt)
    {
        QCoreApplication::processEvents();
        if(predicate())
        {
            return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    QCoreApplication::processEvents();
    return predicate();
}

void detection(const std::shared_ptr<HostState>& state, bool start)
{
    hyperflux::openrgb::native::DetectionCallback callback = nullptr;
    void* context = nullptr;
    {
        std::lock_guard lock(state->mutex);
        callback = start ? state->start : state->end;
        context = start ? state->start_context : state->end_context;
    }
    if(callback != nullptr)
    {
        callback(context);
    }
}

} // namespace

int main(int argc, char** argv)
{
    QCoreApplication application(argc, argv);
    using namespace hyperflux;
    using namespace hyperflux::openrgb;
    using namespace hyperflux::openrgb::native;

    auto state = std::make_shared<HostState>();
    PluginCoordinatorConfig config;
    config.component_version = "0.0.0-dev.1";
    config.worker.poll_interval_ms = 1;
    config.worker.reconnect_initial_ms = 1;
    config.worker.reconnect_max_ms = 4;
    OpenRgbPluginApplication plugin(
        []() -> sdk::Result<std::unique_ptr<RuntimeBridge>>
        {
            return sdk::Result<std::unique_ptr<RuntimeBridge>>::success(
                std::make_unique<hyperflux::test::FakeBridge>(
                    hyperflux::test::native_integration_view(1, 1)));
        },
        config);

    if(!plugin.load(std::make_unique<Host>(state))
       || !wait_until([&] { return plugin.status().coordinator.controllers == 2; }))
    {
        return failure(__LINE__);
    }
    std::set<std::uintptr_t> original;
    {
        std::lock_guard lock(state->mutex);
        if(!state->valid || state->controllers.size() != 2)
        {
            return failure(__LINE__);
        }
        original = state->original_identities;
    }

    detection(state, true);
    detection(state, true);
    {
        std::lock_guard lock(state->mutex);
        if(!state->controllers.empty() || state->unregister_calls != 2)
        {
            return failure(__LINE__);
        }
    }
    detection(state, false);
    detection(state, false);
    if(!wait_until([&]
       {
           std::lock_guard lock(state->mutex);
           return state->controllers.size() == 2;
       }))
    {
        return failure(__LINE__);
    }
    {
        std::lock_guard lock(state->mutex);
        std::set<std::uintptr_t> current;
        for(const auto* controller : state->controllers)
        {
            current.insert(reinterpret_cast<std::uintptr_t>(controller));
        }
        if(!state->valid || current != original || state->register_calls != 4)
        {
            return failure(__LINE__);
        }
    }

    plugin.unload();
    plugin.unload();
    QCoreApplication::processEvents();
    {
        std::lock_guard lock(state->mutex);
        if(!state->valid || !state->controllers.empty() || state->start != nullptr
           || state->end != nullptr || state->waits != 1
           || state->unregister_calls != 4)
        {
            return failure(__LINE__);
        }
    }
    if(plugin.status().loaded || !plugin.controllers().empty())
    {
        return failure(__LINE__);
    }
    return EXIT_SUCCESS;
}
