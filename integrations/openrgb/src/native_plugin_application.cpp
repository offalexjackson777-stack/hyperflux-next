// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/native_plugin_application.hpp>
#include <hyperflux/openrgb/plugin_runtime.hpp>

#include "qt_dispatcher.hpp"

#include "ResourceManagerInterface.h"

#include <exception>
#include <memory>
#include <optional>
#include <string>
#include <utility>

namespace hyperflux::openrgb::native
{
namespace
{

sdk::Error application_error(std::string message)
{
    return {
        sdk::ErrorCode::RuntimeConfiguration,
        std::move(message),
        "HFX-NATIVE-OPENRGB-004",
    };
}

sdk::UnixChannelConfig native_channel_config()
{
    const auto runtime = default_production_runtime_config();
    return {
        runtime.socket_path,
        runtime.timeout_ms,
        std::nullopt,
        runtime.expected_peer_user.empty()
            ? std::nullopt
            : std::optional<std::string>(runtime.expected_peer_user),
    };
}

class ResourceManagerNativePluginHost final : public NativePluginHost
{
public:
    explicit ResourceManagerNativePluginHost(ResourceManagerInterface& manager) noexcept
        : manager_(&manager)
    {
    }

    void register_controller(RGBController& controller) noexcept override
    {
        manager_->RegisterRGBController(&controller);
    }

    void unregister_controller(RGBController& controller) noexcept override
    {
        manager_->UnregisterRGBController(&controller);
    }

    std::vector<RGBController*> controllers() const override
    {
        return manager_->GetRGBControllers();
    }

    void register_detection_start(
        DetectionCallback callback, void* context) noexcept override
    {
        manager_->RegisterDetectionStartCallback(callback, context);
    }

    void register_detection_end(
        DetectionCallback callback, void* context) noexcept override
    {
        manager_->RegisterDetectionEndCallback(callback, context);
    }

    void unregister_detection_start(
        DetectionCallback callback, void* context) noexcept override
    {
        manager_->UnregisterDetectionStartCallback(callback, context);
    }

    void unregister_detection_end(
        DetectionCallback callback, void* context) noexcept override
    {
        manager_->UnregisterDetectionEndCallback(callback, context);
    }

    void wait_for_detection() noexcept override
    {
        manager_->WaitForDeviceDetection();
    }

private:
    ResourceManagerInterface* manager_;
};

} // namespace

NativePluginRuntimeFactories default_native_plugin_runtime_factories()
{
    const auto channel = native_channel_config();
    return {
        make_unix_native_discovery_factory(channel),
        make_unix_native_feature_exchange_factory(channel),
    };
}

OpenRgbNativePluginApplication::OpenRgbNativePluginApplication()
    : OpenRgbNativePluginApplication(
          default_native_plugin_runtime_factories(),
          UpstreamRazerCoordinatorConfig {})
{
}

OpenRgbNativePluginApplication::OpenRgbNativePluginApplication(
    std::function<void()> on_state_changed)
    : OpenRgbNativePluginApplication()
{
    coordinator_config_.on_state_changed = std::move(on_state_changed);
}

OpenRgbNativePluginApplication::OpenRgbNativePluginApplication(
    NativePluginRuntimeFactories factories,
    UpstreamRazerCoordinatorConfig coordinator_config)
    : factories_(std::move(factories)),
      coordinator_config_(std::move(coordinator_config))
{
}

OpenRgbNativePluginApplication::~OpenRgbNativePluginApplication()
{
    unload();
}

sdk::Result<void> OpenRgbNativePluginApplication::load(ResourceManagerInterface* manager)
{
    if(manager == nullptr)
    {
        return sdk::Result<void>::failure(
            application_error("OpenRGB native plugin requires a resource manager"));
    }
    return load(std::make_unique<ResourceManagerNativePluginHost>(*manager));
}

sdk::Result<void> OpenRgbNativePluginApplication::load(
    std::unique_ptr<NativePluginHost> host)
{
    {
        std::lock_guard lock(lifecycle_mutex_);
        if(state_ != State::Unloaded)
        {
            return sdk::Result<void>::failure(
                application_error("OpenRGB native plugin is already loaded"));
        }
        if(host == nullptr || !factories_.discovery || !factories_.exchange)
        {
            return sdk::Result<void>::failure(
                application_error("OpenRGB native plugin factories are incomplete"));
        }
        state_ = State::Loading;
    }

    std::unique_ptr<QtApplicationDispatcher> dispatcher;
    try
    {
        dispatcher = std::make_unique<QtApplicationDispatcher>();
    }
    catch(const std::exception& error)
    {
        reset_loading_state();
        return sdk::Result<void>::failure(application_error(
            "OpenRGB native dispatcher failed: " + std::string(error.what())));
    }
    catch(...)
    {
        reset_loading_state();
        return sdk::Result<void>::failure(
            application_error("OpenRGB native dispatcher failed"));
    }

    auto coordinator = UpstreamRazerPluginCoordinator::create(
        *host,
        *dispatcher,
        factories_.discovery,
        factories_.exchange,
        coordinator_config_);
    if(!coordinator)
    {
        reset_loading_state();
        return sdk::Result<void>::failure(coordinator.error());
    }

    {
        std::lock_guard lock(lifecycle_mutex_);
        host_ = std::move(host);
        dispatcher_ = std::move(dispatcher);
        coordinator_ = std::move(coordinator).value();
        callbacks_enabled_ = true;
    }
    host_->register_detection_start(&detection_started_callback, this);
    host_->register_detection_end(&detection_finished_callback, this);
    {
        std::lock_guard lock(lifecycle_mutex_);
        callbacks_registered_ = true;
    }

    auto started = coordinator_->start();
    if(!started)
    {
        auto error = started.error();
        unload();
        return sdk::Result<void>::failure(std::move(error));
    }
    {
        std::lock_guard lock(lifecycle_mutex_);
        state_ = State::Loaded;
    }
    return sdk::Result<void>::success();
}

void OpenRgbNativePluginApplication::unload() noexcept
{
    NativePluginHost* host = nullptr;
    bool callbacks_registered = false;
    {
        std::lock_guard lock(lifecycle_mutex_);
        if(state_ == State::Unloaded || state_ == State::Unloading)
        {
            return;
        }
        state_ = State::Unloading;
        callbacks_enabled_ = false;
        host = host_.get();
        callbacks_registered = callbacks_registered_;
    }
    if(host != nullptr)
    {
        host->wait_for_detection();
        if(callbacks_registered)
        {
            host->unregister_detection_start(&detection_started_callback, this);
            host->unregister_detection_end(&detection_finished_callback, this);
        }
    }

    std::shared_ptr<UpstreamRazerPluginCoordinator> coordinator;
    std::unique_ptr<QtApplicationDispatcher> dispatcher;
    std::unique_ptr<NativePluginHost> owned_host;
    {
        std::lock_guard lock(lifecycle_mutex_);
        callbacks_registered_ = false;
        coordinator = std::move(coordinator_);
        dispatcher = std::move(dispatcher_);
        owned_host = std::move(host_);
    }
    if(coordinator != nullptr)
    {
        coordinator->shutdown();
    }
    if(dispatcher != nullptr)
    {
        dispatcher->stop();
    }
    coordinator.reset();
    dispatcher.reset();
    owned_host.reset();

    std::lock_guard lock(lifecycle_mutex_);
    state_ = State::Unloaded;
}

void OpenRgbNativePluginApplication::request_rescan() noexcept
{
    std::shared_ptr<UpstreamRazerPluginCoordinator> coordinator;
    {
        std::lock_guard lock(lifecycle_mutex_);
        coordinator = coordinator_;
    }
    if(coordinator != nullptr)
    {
        coordinator->request_rescan();
    }
}

NativePluginApplicationStatus OpenRgbNativePluginApplication::status() const
{
    std::shared_ptr<UpstreamRazerPluginCoordinator> coordinator;
    NativePluginApplicationStatus result;
    {
        std::lock_guard lock(lifecycle_mutex_);
        result.loaded = state_ == State::Loaded;
        coordinator = coordinator_;
    }
    if(coordinator != nullptr)
    {
        result.coordinator = coordinator->status();
    }
    return result;
}

std::vector<UpstreamRazerControllerSummary>
OpenRgbNativePluginApplication::controllers() const
{
    std::shared_ptr<UpstreamRazerPluginCoordinator> coordinator;
    {
        std::lock_guard lock(lifecycle_mutex_);
        coordinator = coordinator_;
    }
    return coordinator == nullptr ? std::vector<UpstreamRazerControllerSummary> {}
                                  : coordinator->controllers();
}

void OpenRgbNativePluginApplication::detection_started_callback(void* context) noexcept
{
    if(context != nullptr)
    {
        static_cast<OpenRgbNativePluginApplication*>(context)->detection_started();
    }
}

void OpenRgbNativePluginApplication::detection_finished_callback(void* context) noexcept
{
    if(context != nullptr)
    {
        static_cast<OpenRgbNativePluginApplication*>(context)->detection_finished();
    }
}

void OpenRgbNativePluginApplication::detection_started() noexcept
{
    std::shared_ptr<UpstreamRazerPluginCoordinator> coordinator;
    {
        std::lock_guard lock(lifecycle_mutex_);
        if(!callbacks_enabled_)
        {
            return;
        }
        coordinator = coordinator_;
    }
    if(coordinator != nullptr)
    {
        coordinator->detection_started();
    }
}

void OpenRgbNativePluginApplication::detection_finished() noexcept
{
    std::shared_ptr<UpstreamRazerPluginCoordinator> coordinator;
    {
        std::lock_guard lock(lifecycle_mutex_);
        if(!callbacks_enabled_)
        {
            return;
        }
        coordinator = coordinator_;
    }
    if(coordinator != nullptr)
    {
        coordinator->detection_finished();
    }
}

void OpenRgbNativePluginApplication::reset_loading_state() noexcept
{
    std::lock_guard lock(lifecycle_mutex_);
    state_ = State::Unloaded;
}

} // namespace hyperflux::openrgb::native
