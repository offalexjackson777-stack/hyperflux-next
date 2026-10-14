// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/build_config.hpp>
#include <hyperflux/openrgb/plugin_application.hpp>
#include <hyperflux/openrgb/plugin_runtime.hpp>

#include "qt_dispatcher.hpp"

#include <hyperflux/openrgb/virtual_controller_mirror.hpp>

#include "OpenRGBPluginInterface.h"

#include <exception>
#include <memory>
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
        "HFX-RUNTIME-001",
    };
}

class OpenRgbApiPluginHost final : public PluginHost
{
public:
    explicit OpenRgbApiPluginHost(OpenRGBPluginAPIInterface& api) noexcept
        : mirrors_(api)
    {
    }

    void register_controller(NativeController& controller) noexcept override
    {
        mirrors_.add(controller);
    }

    void unregister_controller(NativeController& controller) noexcept override
    {
        mirrors_.remove(controller);
    }

private:
    VirtualControllerMirrorSet mirrors_;
};

} // namespace

OpenRgbPluginApplication::OpenRgbPluginApplication()
    : OpenRgbPluginApplication(
          [] { return create_production_runtime(); },
          []
          {
              PluginCoordinatorConfig config;
              config.component_version = std::string(build_config::component_version);
              return config;
          }())
{
}

OpenRgbPluginApplication::OpenRgbPluginApplication(
    std::function<void()> on_state_changed)
    : OpenRgbPluginApplication()
{
    coordinator_config_.on_state_changed = std::move(on_state_changed);
}

OpenRgbPluginApplication::OpenRgbPluginApplication(
    RuntimeFactory runtime_factory,
    PluginCoordinatorConfig coordinator_config)
    : runtime_factory_(std::move(runtime_factory)),
      coordinator_config_(std::move(coordinator_config))
{
}

OpenRgbPluginApplication::~OpenRgbPluginApplication()
{
    unload();
}

sdk::Result<void> OpenRgbPluginApplication::load(OpenRGBPluginAPIInterface* api)
{
    if(api == nullptr)
    {
        return sdk::Result<void>::failure(
            application_error("OpenRGB plugin requires the plugin API"));
    }
    return load(std::make_unique<OpenRgbApiPluginHost>(*api));
}

sdk::Result<void> OpenRgbPluginApplication::load(std::unique_ptr<PluginHost> host)
{
    {
        std::lock_guard lock(lifecycle_mutex_);
        if(state_ != State::Unloaded)
        {
            return sdk::Result<void>::failure(
                application_error("OpenRGB plugin application is already loaded"));
        }
        if(host == nullptr || !runtime_factory_)
        {
            return sdk::Result<void>::failure(
                application_error("OpenRGB plugin requires host and runtime factories"));
        }
        state_ = State::Loading;
    }

    sdk::Result<std::unique_ptr<RuntimeBridge>> runtime =
        sdk::Result<std::unique_ptr<RuntimeBridge>>::failure(
            application_error("OpenRGB runtime factory did not run"));
    try
    {
        runtime = runtime_factory_();
    }
    catch(const std::exception& error)
    {
        reset_loading_state();
        return sdk::Result<void>::failure(application_error(
            "OpenRGB runtime factory failed: " + std::string(error.what())));
    }
    catch(...)
    {
        reset_loading_state();
        return sdk::Result<void>::failure(
            application_error("OpenRGB runtime factory failed with an unknown exception"));
    }
    if(!runtime)
    {
        reset_loading_state();
        return sdk::Result<void>::failure(runtime.error());
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
            "OpenRGB application dispatcher failed: " + std::string(error.what())));
    }
    catch(...)
    {
        reset_loading_state();
        return sdk::Result<void>::failure(application_error(
            "OpenRGB application dispatcher failed with an unknown exception"));
    }
    auto coordinator = PluginCoordinator::create(
        *host,
        *dispatcher,
        std::move(runtime).value(),
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

void OpenRgbPluginApplication::unload() noexcept
{
    {
        std::lock_guard lock(lifecycle_mutex_);
        if(state_ == State::Unloaded || state_ == State::Unloading)
        {
            return;
        }
        state_ = State::Unloading;
        callbacks_enabled_ = false;
    }


    std::shared_ptr<PluginCoordinator> coordinator;
    std::unique_ptr<QtApplicationDispatcher> dispatcher;
    std::unique_ptr<PluginHost> owned_host;
    {
        std::lock_guard lock(lifecycle_mutex_);
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

PluginApplicationStatus OpenRgbPluginApplication::status() const
{
    std::shared_ptr<PluginCoordinator> coordinator;
    PluginApplicationStatus result;
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

std::vector<ControllerModel> OpenRgbPluginApplication::controllers() const
{
    std::shared_ptr<PluginCoordinator> coordinator;
    {
        std::lock_guard lock(lifecycle_mutex_);
        coordinator = coordinator_;
    }
    return coordinator == nullptr ? std::vector<ControllerModel> {}
                                  : coordinator->controllers();
}

std::vector<InventoryReceiverModel> OpenRgbPluginApplication::inventory() const
{
    std::shared_ptr<PluginCoordinator> coordinator;
    {
        std::lock_guard lock(lifecycle_mutex_);
        coordinator = coordinator_;
    }
    return coordinator == nullptr ? std::vector<InventoryReceiverModel> {}
                                  : coordinator->inventory();
}

void OpenRgbPluginApplication::detection_completed() noexcept
{
    // Plugin controllers survive OpenRGB 1.0 rescans, so only the completed
    // device list needs reconciling. Registration changes must run on the Qt
    // application thread: OpenRGB refreshes its device list there and blocks
    // any other registering thread until the refresh is done.
    try
    {
        std::lock_guard lock(lifecycle_mutex_);
        if(!callbacks_enabled_ || dispatcher_ == nullptr || coordinator_ == nullptr)
        {
            return;
        }
        std::weak_ptr<PluginCoordinator> weak = coordinator_;
        static_cast<void>(dispatcher_->post([weak]
        {
            if(auto coordinator = weak.lock())
            {
                coordinator->detection_finished();
            }
        }));
    }
    catch(...)
    {
        return;
    }
}

void OpenRgbPluginApplication::reset_loading_state() noexcept
{
    std::lock_guard lock(lifecycle_mutex_);
    state_ = State::Unloaded;
}

} // namespace hyperflux::openrgb::native
