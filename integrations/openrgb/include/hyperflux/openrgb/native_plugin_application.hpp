// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "plugin_application.hpp"
#include "upstream_razer_coordinator.hpp"

#include <functional>
#include <memory>
#include <mutex>
#include <vector>

class OpenRGBPluginAPIInterface;

namespace hyperflux::openrgb::native
{

/// Every host call happens on the Qt application thread.
class NativePluginHost : public UpstreamPluginHost
{
public:
    ~NativePluginHost() override = default;
};

struct NativePluginApplicationStatus
{
    bool loaded = false;
    UpstreamRazerCoordinatorStatus coordinator;
};

struct NativePluginRuntimeFactories
{
    NativeDiscoveryClientFactory discovery;
    NativeFeatureExchangeFactory exchange;
    OpenRgbClientIdFactory feature_client_id;
};

[[nodiscard]] NativePluginRuntimeFactories default_native_plugin_runtime_factories();

/// Production OpenRGB application path using upstream Razer controllers.
///
/// The existing semantic-lighting application remains compiled as an explicit
/// rollback implementation and is never started alongside this native path.
class OpenRgbNativePluginApplication final
{
public:
    OpenRgbNativePluginApplication();
    explicit OpenRgbNativePluginApplication(std::function<void()> on_state_changed);
    OpenRgbNativePluginApplication(
        NativePluginRuntimeFactories factories,
        UpstreamRazerCoordinatorConfig coordinator_config);
    OpenRgbNativePluginApplication(const OpenRgbNativePluginApplication&) = delete;
    OpenRgbNativePluginApplication& operator=(const OpenRgbNativePluginApplication&) = delete;
    ~OpenRgbNativePluginApplication();

    [[nodiscard]] sdk::Result<void> load(OpenRGBPluginAPIInterface* api);
    [[nodiscard]] sdk::Result<void> load(std::unique_ptr<NativePluginHost> host);
    void unload() noexcept;
    void request_rescan() noexcept;

    /// Safe from any thread; reconciliation runs on the Qt application thread.
    void detection_completed() noexcept;

    [[nodiscard]] NativePluginApplicationStatus status() const;
    [[nodiscard]] std::vector<UpstreamRazerControllerSummary> controllers() const;

private:
    enum class State
    {
        Unloaded,
        Loading,
        Loaded,
        Unloading,
    };

    void reset_loading_state() noexcept;

    NativePluginRuntimeFactories factories_;
    UpstreamRazerCoordinatorConfig coordinator_config_;
    std::unique_ptr<NativePluginHost> host_;
    std::unique_ptr<class QtApplicationDispatcher> dispatcher_;
    std::shared_ptr<UpstreamRazerPluginCoordinator> coordinator_;
    mutable std::mutex lifecycle_mutex_;
    State state_ = State::Unloaded;
    bool callbacks_enabled_ = false;
};

} // namespace hyperflux::openrgb::native
