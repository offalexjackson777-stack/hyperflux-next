// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/upstream_razer_coordinator.hpp>

#include <exception>
#include <memory>
#include <string>
#include <string_view>
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
        "HFX-NATIVE-OPENRGB-003",
    };
}

} // namespace

UpstreamRazerPluginCoordinator::UpstreamRazerPluginCoordinator(
    UpstreamPluginHost& host,
    ApplicationDispatcher& dispatcher,
    UpstreamRazerCoordinatorConfig config) noexcept
    : host_(&host),
      dispatcher_(&dispatcher),
      on_state_changed_(std::move(config.on_state_changed))
{
}

UpstreamRazerPluginCoordinator::~UpstreamRazerPluginCoordinator()
{
    shutdown();
}

sdk::Result<std::shared_ptr<UpstreamRazerPluginCoordinator>>
UpstreamRazerPluginCoordinator::create(
    UpstreamPluginHost& host,
    ApplicationDispatcher& dispatcher,
    NativeDiscoveryClientFactory discovery_factory,
    NativeFeatureExchangeFactory exchange_factory,
    OpenRgbClientIdFactory client_id_factory,
    UpstreamRazerCoordinatorConfig config)
{
    if(!discovery_factory || !exchange_factory || !client_id_factory)
    {
        return sdk::Result<std::shared_ptr<UpstreamRazerPluginCoordinator>>::failure(
            coordinator_error("native OpenRGB coordinator factories are incomplete"));
    }
    auto coordinator = std::shared_ptr<UpstreamRazerPluginCoordinator>(
        new UpstreamRazerPluginCoordinator(host, dispatcher, config));
    coordinator->factory_ = std::make_unique<UpstreamRazerControllerFactory>(
        std::move(exchange_factory),
        std::move(client_id_factory));
    coordinator->registry_ = std::make_unique<UpstreamRazerControllerRegistry>(
        host, *coordinator->factory_);

    std::weak_ptr<UpstreamRazerPluginCoordinator> weak = coordinator;
    auto worker = NativeRouteWorker::create(
        std::move(discovery_factory),
        config.worker,
        {
            [weak](NativeRouteSnapshot snapshot)
            {
                if(auto owner = weak.lock())
                {
                    owner->queue_snapshot(std::move(snapshot));
                }
            },
            [weak](sdk::Error error)
            {
                if(auto owner = weak.lock())
                {
                    owner->queue_error(std::move(error));
                }
            },
            [weak](std::optional<v8::BridgeSnapshot> status)
            {
                if(auto owner = weak.lock())
                {
                    owner->record_device_status(std::move(status));
                }
            },
        });
    if(!worker)
    {
        return sdk::Result<std::shared_ptr<UpstreamRazerPluginCoordinator>>::failure(
            worker.error());
    }
    coordinator->worker_ = std::move(worker).value();
    return sdk::Result<std::shared_ptr<UpstreamRazerPluginCoordinator>>::success(
        std::move(coordinator));
}

sdk::Result<void> UpstreamRazerPluginCoordinator::start()
{
    {
        std::lock_guard lock(state_mutex_);
        if(started_ || stopped_)
        {
            return sdk::Result<void>::failure(
                coordinator_error("native OpenRGB coordinator cannot start in its current state"));
        }
        started_ = true;
    }
    auto started = worker_->start();
    if(!started)
    {
        {
            std::lock_guard lock(state_mutex_);
            started_ = false;
        }
        record_error(started.error());
        return started;
    }
    notify_state_changed();
    return sdk::Result<void>::success();
}

void UpstreamRazerPluginCoordinator::queue_snapshot(NativeRouteSnapshot snapshot) noexcept
{
    try
    {
        std::weak_ptr<UpstreamRazerPluginCoordinator> weak = weak_from_this();
        if(!dispatcher_->post([weak, snapshot = std::move(snapshot)]() mutable
           {
               if(auto owner = weak.lock())
               {
                   owner->apply_snapshot(std::move(snapshot));
               }
           }))
        {
            record_error(coordinator_error(
                "OpenRGB application dispatcher rejected a native route snapshot"));
        }
    }
    catch(const std::exception& error)
    {
        record_exception("queue a native route snapshot", error);
    }
    catch(...)
    {
        record_exception("queue a native route snapshot");
    }
}

void UpstreamRazerPluginCoordinator::apply_snapshot(NativeRouteSnapshot snapshot) noexcept
{
    {
        std::lock_guard lock(state_mutex_);
        if(stopped_)
        {
            return;
        }
    }
    try
    {
        auto applied = registry_->apply_routes(std::move(snapshot.routes));
        if(!applied)
        {
            record_error(applied.error());
            return;
        }
        static_cast<void>(registry_->apply_direct_serials(
            collect_direct_razer_serials(host_->controllers())));
        refresh_registry_state();
    }
    catch(const std::exception& error)
    {
        record_exception("reconcile native OpenRGB routes", error);
        return;
    }
    catch(...)
    {
        record_exception("reconcile native OpenRGB routes");
        return;
    }
    {
        std::lock_guard lock(state_mutex_);
        last_error_.reset();
    }
    notify_state_changed();
}

void UpstreamRazerPluginCoordinator::queue_error(sdk::Error error) noexcept
{
    try
    {
        std::weak_ptr<UpstreamRazerPluginCoordinator> weak = weak_from_this();
        if(!dispatcher_->post([weak, error = std::move(error)]() mutable
           {
               if(auto owner = weak.lock())
               {
                   owner->record_error(std::move(error));
               }
           }))
        {
            record_error(coordinator_error(
                "OpenRGB application dispatcher rejected a native route error"));
        }
    }
    catch(const std::exception& exception)
    {
        record_exception("queue a native route error", exception);
    }
    catch(...)
    {
        record_exception("queue a native route error");
    }
}

void UpstreamRazerPluginCoordinator::record_error(sdk::Error error) noexcept
{
    try
    {
        std::lock_guard lock(state_mutex_);
        if(stopped_)
        {
            return;
        }
        last_error_ = std::move(error);
    }
    catch(...)
    {
        return;
    }
    notify_state_changed();
}

void UpstreamRazerPluginCoordinator::record_exception(
    std::string_view operation, const std::exception& error) noexcept
{
    try
    {
        record_error(coordinator_error(
            "native OpenRGB failed to " + std::string(operation) + ": " + error.what()));
    }
    catch(...)
    {
    }
}

void UpstreamRazerPluginCoordinator::record_exception(std::string_view operation) noexcept
{
    try
    {
        record_error(coordinator_error(
            "native OpenRGB failed to " + std::string(operation)));
    }
    catch(...)
    {
    }
}

void UpstreamRazerPluginCoordinator::refresh_registry_state()
{
    auto controllers = registry_->summaries();
    const auto count = registry_->size();
    const auto registered = registry_->registered_count();
    const auto suppressed = registry_->suppressed_count();
    std::lock_guard lock(state_mutex_);
    controllers_ = std::move(controllers);
    controller_count_ = count;
    registered_controller_count_ = registered;
    direct_suppressed_controller_count_ = suppressed;
}

void UpstreamRazerPluginCoordinator::detection_finished() noexcept
{
    {
        std::lock_guard lock(state_mutex_);
        if(stopped_)
        {
            return;
        }
    }
    try
    {
        static_cast<void>(registry_->apply_direct_serials(
            collect_direct_razer_serials(host_->controllers())));
        refresh_registry_state();
        worker_->request_rescan();
    }
    catch(const std::exception& error)
    {
        record_exception("reconcile OpenRGB detection", error);
        return;
    }
    catch(...)
    {
        record_exception("reconcile OpenRGB detection");
        return;
    }
    notify_state_changed();
}

void UpstreamRazerPluginCoordinator::request_rescan() noexcept
{
    if(worker_ != nullptr)
    {
        worker_->request_rescan();
    }
}

void UpstreamRazerPluginCoordinator::shutdown() noexcept
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
    {
        std::lock_guard lock(state_mutex_);
        controller_count_ = 0;
        registered_controller_count_ = 0;
        direct_suppressed_controller_count_ = 0;
        controllers_.clear();
        device_status_.reset();
    }
    notify_state_changed();
}

UpstreamRazerCoordinatorStatus UpstreamRazerPluginCoordinator::status() const
{
    const auto worker_state = worker_ == nullptr
        ? NativeRouteWorkerState::Created
        : worker_->state();
    std::lock_guard lock(state_mutex_);
    return {
        worker_state,
        started_,
        stopped_,
        controller_count_,
        registered_controller_count_,
        direct_suppressed_controller_count_,
        last_error_,
    };
}

std::vector<UpstreamRazerControllerSummary>
UpstreamRazerPluginCoordinator::controllers() const
{
    std::lock_guard lock(state_mutex_);
    auto result = controllers_;
    if(!device_status_.has_value())
    {
        return result;
    }
    for(auto& controller : result)
    {
        for(const auto& receiver : device_status_->receivers)
        {
            if(receiver.receiver_id != controller.receiver_id
               || receiver.generation_id != controller.generation_id)
            {
                continue;
            }
            for(const auto& device : receiver.devices)
            {
                if(device.device_id == controller.device_id)
                {
                    controller.battery = device.battery;
                    controller.presence = device.presence;
                }
            }
        }
    }
    return result;
}

void UpstreamRazerPluginCoordinator::record_device_status(
    std::optional<v8::BridgeSnapshot> status) noexcept
{
    try
    {
        std::lock_guard lock(state_mutex_);
        if(stopped_)
        {
            return;
        }
        device_status_ = std::move(status);
    }
    catch(...)
    {
        return;
    }
    notify_state_changed();
}

void UpstreamRazerPluginCoordinator::notify_state_changed() noexcept
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

} // namespace hyperflux::openrgb::native
