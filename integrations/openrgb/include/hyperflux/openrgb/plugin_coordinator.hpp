// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "controller_registry.hpp"

#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace hyperflux::openrgb::native
{

class ApplicationDispatcher
{
public:
    virtual ~ApplicationDispatcher() = default;

    /// Queue work for the application-owned thread. Implementations may not
    /// invoke the task inline.
    [[nodiscard]] virtual bool post(std::function<void()> task) noexcept = 0;
};

struct PluginCoordinatorConfig
{
    WorkerConfig worker {};
    KeyboardLayoutVariant keyboard_layout = KeyboardLayoutVariant::AnsiQwerty;
    std::string component_version;
    /// Edge-triggered state notification. The callback can run on the runtime
    /// worker thread, must be thread-safe and should return promptly.
    std::function<void()> on_state_changed;
};

struct PluginCoordinatorStatus
{
    WorkerState worker_state = WorkerState::Created;
    bool started = false;
    bool stopped = false;
    bool detection_suspended = false;
    std::size_t controllers = 0;
    std::size_t registered_controllers = 0;
    std::optional<sdk::Error> last_error;
};

/// Coordinates one OpenRGB plugin instance without owning Qt or host policy.
///
/// The host and dispatcher must outlive the coordinator. Runtime callbacks use
/// weak ownership, so queued work cannot resurrect or access an unloaded
/// plugin. OpenRGB detection suspension remains synchronous by design.
class PluginCoordinator final : public std::enable_shared_from_this<PluginCoordinator>
{
public:
    PluginCoordinator(const PluginCoordinator&) = delete;
    PluginCoordinator& operator=(const PluginCoordinator&) = delete;
    PluginCoordinator(PluginCoordinator&&) = delete;
    PluginCoordinator& operator=(PluginCoordinator&&) = delete;
    ~PluginCoordinator();

    [[nodiscard]] static sdk::Result<std::shared_ptr<PluginCoordinator>> create(
        ControllerHost& host,
        ApplicationDispatcher& dispatcher,
        std::unique_ptr<RuntimeBridge> bridge,
        PluginCoordinatorConfig config);

    [[nodiscard]] sdk::Result<void> start();
    void detection_started() noexcept;
    void detection_finished() noexcept;
    void shutdown() noexcept;

    [[nodiscard]] PluginCoordinatorStatus status() const;
    [[nodiscard]] std::vector<ControllerModel> controllers() const;
    [[nodiscard]] std::vector<InventoryReceiverModel> inventory() const;

private:
    explicit PluginCoordinator(ApplicationDispatcher& dispatcher) noexcept;

    void queue_runtime_snapshot() noexcept;
    void apply_runtime_snapshot(RuntimeSnapshot snapshot) noexcept;
    void record_error(sdk::Error error) noexcept;
    void notify_state_changed() noexcept;

    ApplicationDispatcher* dispatcher_;

    // Declaration order is the lifetime contract: registry is destroyed before
    // its factory and sink, and the sink is destroyed before its worker.
    std::unique_ptr<RuntimeWorker> worker_;
    std::unique_ptr<WorkerLightingCommandSink> sink_;
    std::unique_ptr<RazerNativeControllerFactory> factory_;
    std::unique_ptr<ControllerRegistry> registry_;

    mutable std::mutex state_mutex_;
    bool started_ = false;
    bool stopped_ = false;
    std::optional<sdk::Error> last_error_;
    std::vector<InventoryReceiverModel> inventory_;
    // Assigned before the worker starts and immutable for the coordinator's
    // lifetime, so notification never needs a potentially-throwing copy.
    std::function<void()> on_state_changed_;
};

} // namespace hyperflux::openrgb::native
