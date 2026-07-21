// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_native_fixture.hpp"

#include <hyperflux/openrgb/controller_registry.hpp>

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <set>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-controller-registry-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

class Sink final : public hyperflux::openrgb::native::LightingCommandSink
{
public:
    hyperflux::sdk::Result<hyperflux::openrgb::EnqueueDisposition> enqueue_effect(
        hyperflux::openrgb::QueuedLightingFrame) override
    {
        return hyperflux::sdk::Result<hyperflux::openrgb::EnqueueDisposition>::success(
            hyperflux::openrgb::EnqueueDisposition::Accepted);
    }

    hyperflux::sdk::Result<hyperflux::openrgb::EnqueueDisposition> enqueue_stable(
        hyperflux::sdk::LightingIntent,
        std::vector<hyperflux::openrgb::QueuedLightingFrame>) override
    {
        return hyperflux::sdk::Result<hyperflux::openrgb::EnqueueDisposition>::success(
            hyperflux::openrgb::EnqueueDisposition::Accepted);
    }
};

class Host final : public hyperflux::openrgb::native::ControllerHost
{
public:
    void register_controller(
        hyperflux::openrgb::native::NativeController& controller) noexcept override
    {
        if(!registered.insert(&controller).second)
        {
            valid = false;
        }
        ++register_calls;
    }

    void unregister_controller(
        hyperflux::openrgb::native::NativeController& controller) noexcept override
    {
        if(registered.erase(&controller) != 1)
        {
            valid = false;
        }
        ++unregister_calls;
    }

    std::set<const hyperflux::openrgb::native::NativeController*> registered;
    std::size_t register_calls = 0;
    std::size_t unregister_calls = 0;
    bool valid = true;
};

class Factory final : public hyperflux::openrgb::native::NativeControllerFactory
{
public:
    explicit Factory(hyperflux::openrgb::native::NativeControllerFactory& delegate)
        : delegate_(&delegate)
    {
    }

    hyperflux::sdk::Result<std::unique_ptr<hyperflux::openrgb::native::NativeController>> create(
        const hyperflux::openrgb::ControllerModel& model) override
    {
        if(model.stable_id == rejected_stable_id)
        {
            return hyperflux::sdk::Result<
                std::unique_ptr<hyperflux::openrgb::native::NativeController>>::failure({
                hyperflux::sdk::ErrorCode::InvalidController,
                "injected controller construction failure",
                "HFX-INTEGRATION-001",
            });
        }
        ++create_calls;
        return delegate_->create(model);
    }

    std::string rejected_stable_id;
    std::size_t create_calls = 0;

private:
    hyperflux::openrgb::native::NativeControllerFactory* delegate_;
};

bool concurrent_rescan_contract()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb;
    using namespace hyperflux::openrgb::native;
    using namespace hyperflux::test;

    Sink sink;
    RazerNativeControllerFactory native_factory(
        sink, KeyboardLayoutVariant::AnsiQwerty, "0.0.0-dev.1");
    Factory factory(native_factory);
    Host host;
    ControllerRegistry registry(host, factory);

    std::vector<ControllerModel> models {
        native_controller_model(DeviceKind::Mouse),
        native_controller_model(DeviceKind::Keyboard),
    };
    std::sort(models.begin(),
        models.end(),
        [](const auto& left, const auto& right)
        {
            return left.stable_id < right.stable_id;
        });
    if(!registry.apply(models))
    {
        return false;
    }

    std::atomic<bool> start = false;
    std::atomic<bool> failed = false;
    std::thread detection_thread(
        [&]
        {
            while(!start.load(std::memory_order_acquire))
            {
                std::this_thread::yield();
            }
            for(std::size_t index = 0; index < 250; ++index)
            {
                registry.suspend_for_detection();
                registry.resume_after_detection();
            }
        });
    std::thread update_thread(
        [&]
        {
            start.store(true, std::memory_order_release);
            for(std::size_t index = 0; index < 250; ++index)
            {
                auto next = models;
                next.front().availability = index % 2 == 0 ? ControllerAvailability::Sleeping
                                                           : ControllerAvailability::Ready;
                if(!registry.apply(std::move(next)))
                {
                    failed.store(true, std::memory_order_relaxed);
                    return;
                }
            }
        });
    detection_thread.join();
    update_thread.join();

    registry.resume_after_detection();
    const bool valid = !failed.load(std::memory_order_relaxed) && !registry.suspended()
                       && registry.size() == 2 && registry.registered_count() == 2
                       && host.registered.size() == 2
                       && host.register_calls == host.unregister_calls + 2
                       && factory.create_calls == 2 && host.valid;
    registry.shutdown();
    return valid && host.registered.empty() && host.valid;
}

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb;
    using namespace hyperflux::openrgb::native;
    using namespace hyperflux::test;

    Sink sink;
    RazerNativeControllerFactory native_factory(
        sink, KeyboardLayoutVariant::AnsiQwerty, "0.0.0-dev.1");
    Factory factory(native_factory);
    Host host;
    ControllerRegistry registry(host, factory);

    auto mouse = native_controller_model(DeviceKind::Mouse);
    auto keyboard = native_controller_model(DeviceKind::Keyboard);
    if(registry.apply({mouse, keyboard}) || registry.size() != 0 || !host.registered.empty())
    {
        return failure(__LINE__);
    }

    std::vector<ControllerModel> both {keyboard, mouse};
    std::sort(both.begin(),
        both.end(),
        [](const auto& left, const auto& right)
        {
            return left.stable_id < right.stable_id;
        });

    auto initial = registry.apply(both);
    if(!initial || initial.value().added != 2 || initial.value().registered != 2
        || registry.size() != 2 || host.registered.size() != 2 || !host.valid)
    {
        return failure(__LINE__);
    }
    const auto first_mouse = registry.controller_identity(mouse.stable_id);
    const auto first_keyboard = registry.controller_identity(keyboard.stable_id);

    auto retained = registry.apply(both);
    if(!retained || retained.value().retained != 2 || factory.create_calls != 2
        || host.register_calls != 2 || registry.controller_identity(mouse.stable_id) != first_mouse
        || registry.controller_identity(keyboard.stable_id) != first_keyboard)
    {
        return failure(__LINE__);
    }

    auto state_changed = both;
    state_changed.front().availability = ControllerAvailability::Sleeping;
    auto state_update = registry.apply(state_changed);
    const auto expected_preserved =
        state_changed.front().device_kind == DeviceKind::Mouse ? first_mouse : first_keyboard;
    if(!state_update || state_update.value().state_updated != 1
        || state_update.value().retained != 1 || factory.create_calls != 2
        || registry.controller_identity(state_changed.front().stable_id) != expected_preserved)
    {
        return failure(__LINE__);
    }

    registry.suspend_for_detection();
    registry.suspend_for_detection();
    if(!registry.suspended() || registry.registered_count() != 0 || !host.registered.empty()
        || host.unregister_calls != 2)
    {
        return failure(__LINE__);
    }
    auto suspended_update = registry.apply(state_changed);
    if(!suspended_update || suspended_update.value().registered != 0 || host.register_calls != 2)
    {
        return failure(__LINE__);
    }
    registry.resume_after_detection();
    registry.resume_after_detection();
    if(registry.suspended() || registry.registered_count() != 2 || host.registered.size() != 2
        || host.register_calls != 4 || !host.valid)
    {
        return failure(__LINE__);
    }

    std::vector<ControllerModel> mouse_only {mouse};
    auto removed = registry.apply(mouse_only);
    if(!removed || removed.value().removed != 1 || removed.value().retained != 1
        || registry.size() != 1 || registry.controller_identity(mouse.stable_id) != first_mouse
        || host.unregister_calls != 3)
    {
        return failure(__LINE__);
    }

    auto added_back = registry.apply(both);
    if(!added_back || added_back.value().added != 1 || registry.size() != 2
        || factory.create_calls != 3 || host.register_calls != 5)
    {
        return failure(__LINE__);
    }
    const auto second_keyboard = registry.controller_identity(keyboard.stable_id);

    auto replacement = both;
    for(auto& model : replacement)
    {
        if(model.device_kind == DeviceKind::Keyboard)
        {
            model.model_name = text<ModelName>("Presentation revision");
        }
    }
    auto replaced = registry.apply(replacement);
    if(!replaced || replaced.value().presentation_replaced != 1
        || registry.controller_identity(keyboard.stable_id) == second_keyboard
        || host.unregister_calls != 4 || host.register_calls != 6 || !host.valid)
    {
        return failure(__LINE__);
    }

    auto rejected = native_controller_model(DeviceKind::Keyboard);
    rejected.stable_id = "receiver-1/keyboard/rejected-profile";
    rejected.device_profile.profile_id = text<ProfileId>("child.test.rejected");
    factory.rejected_stable_id = rejected.stable_id;
    auto before_failure = registry.models();
    auto with_rejected = before_failure;
    with_rejected.push_back(rejected);
    std::sort(with_rejected.begin(),
        with_rejected.end(),
        [](const auto& left, const auto& right)
        {
            return left.stable_id < right.stable_id;
        });
    const auto calls_before_failure = host.register_calls;
    auto failed = registry.apply(std::move(with_rejected));
    if(failed || registry.models() != before_failure || host.register_calls != calls_before_failure
        || host.registered.size() != 2 || !host.valid)
    {
        return failure(__LINE__);
    }

    registry.shutdown();
    registry.shutdown();
    if(!registry.stopped() || registry.size() != 0 || !host.registered.empty()
        || host.unregister_calls != 6 || !host.valid)
    {
        return failure(__LINE__);
    }
    if(!concurrent_rescan_contract())
    {
        return failure(__LINE__);
    }
    return EXIT_SUCCESS;
}
