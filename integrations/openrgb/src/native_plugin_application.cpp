// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/native_plugin_application.hpp>
#include <hyperflux/openrgb/plugin_runtime.hpp>

#include "qt_dispatcher.hpp"

#include <hyperflux/openrgb/virtual_controller_mirror.hpp>

#include "OpenRGBPluginInterface.h"

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
    return default_production_native_channel_config();
}

class OpenRgbApiNativePluginHost final : public NativePluginHost
{
public:
    explicit OpenRgbApiNativePluginHost(OpenRGBPluginAPIInterface& api) noexcept
        : mirrors_(api, VirtualControllerMirrorOptions {.skip_initial_black_frame = true})
    {
    }

    void register_controller(RGBController& controller) noexcept override
    {
        mirrors_.add(controller);
    }

    void unregister_controller(RGBController& controller) noexcept override
    {
        mirrors_.remove(controller);
    }

    void begin_unload() noexcept override
    {
        mirrors_.begin_unload();
    }

    std::vector<RGBControllerInterface*> controllers() const override
    {
        return mirrors_.api().GetRGBControllers();
    }

private:
    VirtualControllerMirrorSet mirrors_;
};

} // namespace

NativePluginRuntimeFactories default_native_plugin_runtime_factories()
{
    const auto channel = native_channel_config();
    return {
        make_unix_native_discovery_factory(channel),
        make_unix_native_feature_exchange_factory(channel),
        process_openrgb_client_id_factory(OpenRgbClientRole::Feature),
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

sdk::Result<void> OpenRgbNativePluginApplication::load(OpenRGBPluginAPIInterface* api)
{
    if(api == nullptr)
    {
        return sdk::Result<void>::failure(
            application_error("OpenRGB native plugin requires the plugin API"));
    }
    return load(std::make_unique<OpenRgbApiNativePluginHost>(*api));
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
        if(host == nullptr || !factories_.discovery || !factories_.exchange
           || !factories_.feature_client_id)
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
        factories_.feature_client_id,
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

void OpenRgbNativePluginApplication::unload() noexcept
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

    std::shared_ptr<UpstreamRazerPluginCoordinator> coordinator;
    std::unique_ptr<QtApplicationDispatcher> dispatcher;
    std::unique_ptr<NativePluginHost> owned_host;
    {
        std::lock_guard lock(lifecycle_mutex_);
        coordinator = std::move(coordinator_);
        dispatcher = std::move(dispatcher_);
        owned_host = std::move(host_);
    }
    if(owned_host != nullptr)
    {
        // On exit OpenRGB unloads plugins after its GUI stopped following
        // the device list, and its device pages release the controllers we
        // present only after Unload returns.
        owned_host->begin_unload();
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

void OpenRgbNativePluginApplication::detection_completed() noexcept
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
        std::weak_ptr<UpstreamRazerPluginCoordinator> weak = coordinator_;
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

void OpenRgbNativePluginApplication::reset_loading_state() noexcept
{
    std::lock_guard lock(lifecycle_mutex_);
    state_ = State::Unloaded;
}

} // namespace hyperflux::openrgb::native
