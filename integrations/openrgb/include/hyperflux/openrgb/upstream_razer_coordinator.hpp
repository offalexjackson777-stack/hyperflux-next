// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "native_route_worker.hpp"
#include "plugin_coordinator.hpp"
#include "upstream_razer_registry.hpp"

#include <cstddef>
#include <exception>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <string_view>
#include <vector>

class RGBController;

namespace hyperflux::openrgb::native
{

class UpstreamPluginHost : public UpstreamControllerHost
{
public:
    ~UpstreamPluginHost() override = default;
    [[nodiscard]] virtual std::vector<RGBController*> controllers() const = 0;
};

struct UpstreamRazerCoordinatorConfig
{
    NativeRouteWorkerConfig worker {};
    std::function<void()> on_state_changed;
};

struct UpstreamRazerCoordinatorStatus
{
    NativeRouteWorkerState worker_state = NativeRouteWorkerState::Created;
    bool started = false;
    bool stopped = false;
    bool detection_suspended = false;
    std::size_t controllers = 0;
    std::size_t registered_controllers = 0;
    std::size_t direct_suppressed_controllers = 0;
    std::optional<sdk::Error> last_error;
};

class UpstreamRazerPluginCoordinator final
    : public std::enable_shared_from_this<UpstreamRazerPluginCoordinator>
{
public:
    UpstreamRazerPluginCoordinator(const UpstreamRazerPluginCoordinator&) = delete;
    UpstreamRazerPluginCoordinator& operator=(const UpstreamRazerPluginCoordinator&) = delete;
    ~UpstreamRazerPluginCoordinator();

    [[nodiscard]] static sdk::Result<std::shared_ptr<UpstreamRazerPluginCoordinator>>
    create(
        UpstreamPluginHost& host,
        ApplicationDispatcher& dispatcher,
        NativeDiscoveryClientFactory discovery_factory,
        NativeFeatureExchangeFactory exchange_factory,
        OpenRgbClientIdFactory client_id_factory,
        UpstreamRazerCoordinatorConfig config = {});

    [[nodiscard]] sdk::Result<void> start();
    void detection_started() noexcept;
    void detection_finished() noexcept;
    void shutdown() noexcept;
    void request_rescan() noexcept;

    [[nodiscard]] UpstreamRazerCoordinatorStatus status() const;
    [[nodiscard]] std::vector<UpstreamRazerControllerSummary> controllers() const;

private:
    UpstreamRazerPluginCoordinator(
        UpstreamPluginHost& host,
        ApplicationDispatcher& dispatcher,
        UpstreamRazerCoordinatorConfig config) noexcept;

    void queue_snapshot(NativeRouteSnapshot snapshot) noexcept;
    void apply_snapshot(NativeRouteSnapshot snapshot) noexcept;
    void queue_error(sdk::Error error) noexcept;
    void record_error(sdk::Error error) noexcept;
    void record_exception(std::string_view operation) noexcept;
    void record_exception(std::string_view operation, const std::exception& error) noexcept;
    void refresh_registry_state();
    void notify_state_changed() noexcept;

    UpstreamPluginHost* host_;
    ApplicationDispatcher* dispatcher_;
    std::unique_ptr<NativeRouteWorker> worker_;
    std::unique_ptr<UpstreamRazerControllerFactory> factory_;
    std::unique_ptr<UpstreamRazerControllerRegistry> registry_;

    mutable std::mutex state_mutex_;
    bool started_ = false;
    bool stopped_ = false;
    bool detection_suspended_ = false;
    std::size_t controller_count_ = 0;
    std::size_t registered_controller_count_ = 0;
    std::size_t direct_suppressed_controller_count_ = 0;
    std::optional<sdk::Error> last_error_;
    std::vector<UpstreamRazerControllerSummary> controllers_;
    std::function<void()> on_state_changed_;
};

} // namespace hyperflux::openrgb::native
