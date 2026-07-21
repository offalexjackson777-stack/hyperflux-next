// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/plugin_coordinator.hpp>

#include <utility>

namespace hyperflux::openrgb::native
{
namespace
{

sdk::Error coordinator_error(std::string message)
{
    return {
        sdk::ErrorCode::RuntimeConfiguration,
        std::move(message),
        "HFX-RUNTIME-001",
    };
}

} // namespace

PluginCoordinator::PluginCoordinator(
    ControllerHost& host, ApplicationDispatcher& dispatcher) noexcept
    : host_(&host),
      dispatcher_(&dispatcher)
{
}

PluginCoordinator::~PluginCoordinator()
{
    shutdown();
}

sdk::Result<std::shared_ptr<PluginCoordinator>> PluginCoordinator::create(ControllerHost& host,
    ApplicationDispatcher& dispatcher,
    std::unique_ptr<RuntimeBridge> bridge,
    PluginCoordinatorConfig config)
{
    if(config.component_version.empty())
    {
        return sdk::Result<std::shared_ptr<PluginCoordinator>>::failure(
            coordinator_error("OpenRGB plugin coordinator requires a component version"));
    }

    auto coordinator = std::shared_ptr<PluginCoordinator>(new PluginCoordinator(host, dispatcher));
    coordinator->on_state_changed_ = std::move(config.on_state_changed);
    const std::weak_ptr<PluginCoordinator> weak = coordinator;
    auto worker = RuntimeWorker::create(std::move(bridge),
        config.worker,
        {
            [weak](RuntimeStep)
            {
                if(const auto owner = weak.lock())
                {
                    owner->queue_runtime_snapshot();
                }
            },
            [weak](sdk::Error error)
            {
                if(const auto owner = weak.lock())
                {
                    owner->record_error(std::move(error));
                }
            },
        });
    if(!worker)
    {
        return sdk::Result<std::shared_ptr<PluginCoordinator>>::failure(worker.error());
    }

    coordinator->worker_ = std::move(worker).value();
    coordinator->sink_ = std::make_unique<WorkerLightingCommandSink>(*coordinator->worker_);
    coordinator->factory_ = std::make_unique<RazerNativeControllerFactory>(
        *coordinator->sink_, config.keyboard_layout, std::move(config.component_version));
    coordinator->registry_ = std::make_unique<ControllerRegistry>(host, *coordinator->factory_);
    return sdk::Result<std::shared_ptr<PluginCoordinator>>::success(std::move(coordinator));
}

sdk::Result<void> PluginCoordinator::start()
{
    {
        std::lock_guard lock(state_mutex_);
        if(stopped_)
        {
            return sdk::Result<void>::failure(
                coordinator_error("OpenRGB plugin coordinator is already stopped"));
        }
        if(started_)
        {
            return sdk::Result<void>::failure(
                coordinator_error("OpenRGB plugin coordinator may be started exactly once"));
        }
        started_ = true;
    }

    auto started = worker_->start();
    if(!started)
    {
        std::lock_guard lock(state_mutex_);
        started_ = false;
        last_error_ = started.error();
    }
    notify_state_changed();
    return started;
}

void PluginCoordinator::queue_runtime_snapshot() noexcept
{
    {
        std::lock_guard lock(state_mutex_);
        if(stopped_)
        {
            return;
        }
    }
    auto snapshot = worker_->snapshot();
    const std::weak_ptr<PluginCoordinator> weak = weak_from_this();
    const bool posted = dispatcher_->post(
        [weak, snapshot = std::move(snapshot)]() mutable
        {
            if(const auto owner = weak.lock())
            {
                owner->apply_runtime_snapshot(std::move(snapshot));
            }
        });
    if(!posted)
    {
        record_error(
            coordinator_error("OpenRGB application dispatcher rejected a runtime snapshot"));
    }
}

void PluginCoordinator::apply_runtime_snapshot(RuntimeSnapshot snapshot) noexcept
{
    {
        std::lock_guard lock(state_mutex_);
        if(stopped_)
        {
            return;
        }
    }
    auto applied = registry_->apply(std::move(snapshot.controllers));
    if(!applied)
    {
        record_error(applied.error());
        return;
    }
    {
        std::lock_guard lock(state_mutex_);
        inventory_ = std::move(snapshot.inventory);
        if(worker_->state() != WorkerState::Failed)
        {
            last_error_.reset();
        }
    }
    notify_state_changed();
}

void PluginCoordinator::record_error(sdk::Error error) noexcept
{
    {
        std::lock_guard lock(state_mutex_);
        if(stopped_)
        {
            return;
        }
        last_error_ = std::move(error);
    }
    notify_state_changed();
}

void PluginCoordinator::notify_state_changed() noexcept
{
    if(on_state_changed_)
    {
        try
        {
            on_state_changed_();
        }
        catch(...)
        {
        }
    }
}

void PluginCoordinator::detection_started() noexcept
{
    {
        std::lock_guard lock(state_mutex_);
        if(stopped_)
        {
            return;
        }
    }
    registry_->suspend_for_detection();
    notify_state_changed();
}

void PluginCoordinator::detection_finished() noexcept
{
    {
        std::lock_guard lock(state_mutex_);
        if(stopped_)
        {
            return;
        }
    }
    registry_->resume_after_detection();
    auto requested = worker_->request_rescan();
    if(!requested)
    {
        record_error(requested.error());
    }
    else
    {
        notify_state_changed();
    }
}

void PluginCoordinator::shutdown() noexcept
{
    {
        std::lock_guard lock(state_mutex_);
        if(stopped_)
        {
            return;
        }
        stopped_ = true;
    }
    if(worker_ != nullptr)
    {
        worker_->stop();
    }
    if(registry_ != nullptr)
    {
        registry_->shutdown();
    }
    notify_state_changed();
}

PluginCoordinatorStatus PluginCoordinator::status() const
{
    PluginCoordinatorStatus result;
    {
        std::lock_guard lock(state_mutex_);
        result.started = started_;
        result.stopped = stopped_;
        result.last_error = last_error_;
    }
    if(worker_ != nullptr)
    {
        result.worker_state = worker_->state();
    }
    if(registry_ != nullptr)
    {
        result.detection_suspended = registry_->suspended();
        result.controllers = registry_->size();
        result.registered_controllers = registry_->registered_count();
    }
    return result;
}

std::vector<ControllerModel> PluginCoordinator::controllers() const
{
    return registry_ == nullptr ? std::vector<ControllerModel> {} : registry_->models();
}

std::vector<InventoryReceiverModel> PluginCoordinator::inventory() const
{
    std::lock_guard lock(state_mutex_);
    return inventory_;
}

} // namespace hyperflux::openrgb::native
