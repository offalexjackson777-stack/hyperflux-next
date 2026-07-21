// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "plugin_coordinator.hpp"

#include <functional>
#include <memory>
#include <mutex>
#include <vector>

class ResourceManagerInterface;

namespace hyperflux::openrgb::native
{

using DetectionCallback = void (*)(void* context);

/// Narrow host surface isolated from OpenRGB API churn.
class PluginHost : public ControllerHost
{
public:
    ~PluginHost() override = default;

    virtual void register_detection_start(
        DetectionCallback callback, void* context) noexcept = 0;
    virtual void register_detection_end(
        DetectionCallback callback, void* context) noexcept = 0;
    virtual void unregister_detection_start(
        DetectionCallback callback, void* context) noexcept = 0;
    virtual void unregister_detection_end(
        DetectionCallback callback, void* context) noexcept = 0;
    virtual void wait_for_detection() noexcept = 0;
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
    OpenRgbPluginApplication(
        RuntimeFactory runtime_factory,
        PluginCoordinatorConfig coordinator_config);
    OpenRgbPluginApplication(const OpenRgbPluginApplication&) = delete;
    OpenRgbPluginApplication& operator=(const OpenRgbPluginApplication&) = delete;
    OpenRgbPluginApplication(OpenRgbPluginApplication&&) = delete;
    OpenRgbPluginApplication& operator=(OpenRgbPluginApplication&&) = delete;
    ~OpenRgbPluginApplication();

    [[nodiscard]] sdk::Result<void> load(ResourceManagerInterface* manager);
    [[nodiscard]] sdk::Result<void> load(std::unique_ptr<PluginHost> host);
    void unload() noexcept;

    [[nodiscard]] PluginApplicationStatus status() const;
    [[nodiscard]] std::vector<ControllerModel> controllers() const;

private:
    enum class State
    {
        Unloaded,
        Loading,
        Loaded,
        Unloading,
    };

    static void detection_started_callback(void* context) noexcept;
    static void detection_finished_callback(void* context) noexcept;
    void detection_started() noexcept;
    void detection_finished() noexcept;
    void reset_loading_state() noexcept;

    RuntimeFactory runtime_factory_;
    PluginCoordinatorConfig coordinator_config_;
    std::unique_ptr<PluginHost> host_;
    std::unique_ptr<class QtApplicationDispatcher> dispatcher_;
    std::shared_ptr<PluginCoordinator> coordinator_;
    mutable std::mutex lifecycle_mutex_;
    State state_ = State::Unloaded;
    bool callbacks_registered_ = false;
    bool callbacks_enabled_ = false;
};

} // namespace hyperflux::openrgb::native
