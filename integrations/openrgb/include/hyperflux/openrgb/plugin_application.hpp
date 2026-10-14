// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "plugin_coordinator.hpp"

#include <functional>
#include <memory>
#include <mutex>
#include <vector>

class OpenRGBPluginAPIInterface;

namespace hyperflux::openrgb::native
{

/// Narrow host surface isolated from OpenRGB API churn.
///
/// Every host call happens on the Qt application thread.
class PluginHost : public ControllerHost
{
public:
    ~PluginHost() override = default;
};

struct PluginApplicationStatus
{
    bool loaded = false;
    PluginCoordinatorStatus coordinator;
};

/// Owns the complete native plugin lifecycle independently of metadata or UI.
class OpenRgbPluginApplication final
{
public:
    using RuntimeFactory =
        std::function<sdk::Result<std::unique_ptr<RuntimeBridge>>()>;

    OpenRgbPluginApplication();
    explicit OpenRgbPluginApplication(std::function<void()> on_state_changed);
    OpenRgbPluginApplication(
        RuntimeFactory runtime_factory,
        PluginCoordinatorConfig coordinator_config);
    OpenRgbPluginApplication(const OpenRgbPluginApplication&) = delete;
    OpenRgbPluginApplication& operator=(const OpenRgbPluginApplication&) = delete;
    OpenRgbPluginApplication(OpenRgbPluginApplication&&) = delete;
    OpenRgbPluginApplication& operator=(OpenRgbPluginApplication&&) = delete;
    ~OpenRgbPluginApplication();

    [[nodiscard]] sdk::Result<void> load(OpenRGBPluginAPIInterface* api);
    [[nodiscard]] sdk::Result<void> load(std::unique_ptr<PluginHost> host);
    void unload() noexcept;

    /// Safe from any thread; reconciliation runs on the Qt application thread.
    void detection_completed() noexcept;

    [[nodiscard]] PluginApplicationStatus status() const;
    [[nodiscard]] std::vector<ControllerModel> controllers() const;
    [[nodiscard]] std::vector<InventoryReceiverModel> inventory() const;

private:
    enum class State
    {
        Unloaded,
        Loading,
        Loaded,
        Unloading,
    };

    void reset_loading_state() noexcept;

    RuntimeFactory runtime_factory_;
    PluginCoordinatorConfig coordinator_config_;
    std::unique_ptr<PluginHost> host_;
    std::unique_ptr<class QtApplicationDispatcher> dispatcher_;
    std::shared_ptr<PluginCoordinator> coordinator_;
    mutable std::mutex lifecycle_mutex_;
    State state_ = State::Unloaded;
    bool callbacks_enabled_ = false;
};

} // namespace hyperflux::openrgb::native
