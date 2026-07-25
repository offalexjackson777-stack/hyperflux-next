// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <hyperflux/sdk.hpp>

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>
#include <vector>

namespace hyperflux::openrgb::native
{

enum class NativeRouteWorkerState
{
    Created,
    Starting,
    Running,
    Recovering,
    Stopping,
    Stopped,
    Failed,
};

struct NativeRouteWorkerConfig
{
    std::uint32_t poll_interval_ms = 50;
    std::uint32_t reconnect_initial_ms = 25;
    std::uint32_t reconnect_max_ms = 2'000;
    std::uint16_t max_events = 64;
};

struct NativeRouteSnapshot
{
    v7::EventCursor cursor;
    std::vector<v7::NativeRoute> routes;

    friend bool operator==(const NativeRouteSnapshot&, const NativeRouteSnapshot&) = default;
};

struct NativeRouteWorkerCallbacks
{
    std::function<void(NativeRouteSnapshot)> on_snapshot;
    std::function<void(sdk::Error)> on_error;
};

using NativeDiscoveryClientFactory =
    std::function<sdk::Result<std::unique_ptr<sdk::NativeClientApi>>() >;

[[nodiscard]] NativeDiscoveryClientFactory make_unix_native_discovery_factory(
    sdk::UnixChannelConfig channel_config);

/// Serialized protocol-v7 discovery owner.
///
/// Route events are hints. Every event or cursor gap is reconciled from one
/// complete native-enumerate projection before the application sees it.
class NativeRouteWorker
{
public:
    NativeRouteWorker(const NativeRouteWorker&) = delete;
    NativeRouteWorker& operator=(const NativeRouteWorker&) = delete;
    ~NativeRouteWorker();

    [[nodiscard]] static sdk::Result<std::unique_ptr<NativeRouteWorker>> create(
        NativeDiscoveryClientFactory factory,
        NativeRouteWorkerConfig config = {},
        NativeRouteWorkerCallbacks callbacks = {});

    [[nodiscard]] sdk::Result<void> start();
    void stop() noexcept;
    void request_rescan() noexcept;

    [[nodiscard]] NativeRouteWorkerState state() const noexcept;
    [[nodiscard]] std::optional<NativeRouteSnapshot> snapshot() const;
    [[nodiscard]] std::optional<sdk::Error> last_error() const;

private:
    NativeRouteWorker(
        NativeDiscoveryClientFactory factory,
        NativeRouteWorkerConfig config,
    NativeRouteWorkerCallbacks callbacks);

    void run() noexcept;
    [[nodiscard]] bool wait_interruptible(std::uint32_t delay_ms) noexcept;
    void recover(sdk::Error error, std::uint32_t& delay_ms) noexcept;
    [[nodiscard]] bool publish(NativeRouteSnapshot snapshot) noexcept;
    void fail_unexpected(const char* operation, const char* detail = nullptr) noexcept;
    [[nodiscard]] bool should_stop() const noexcept;

    NativeDiscoveryClientFactory factory_;
    NativeRouteWorkerConfig config_;
    NativeRouteWorkerCallbacks callbacks_;

    mutable std::mutex mutex_;
    std::condition_variable wake_;
    std::thread thread_;
    NativeRouteWorkerState state_ = NativeRouteWorkerState::Created;
    bool stop_requested_ = false;
    bool rescan_requested_ = false;
    std::optional<NativeRouteSnapshot> snapshot_;
    std::optional<sdk::Error> last_error_;
};

} // namespace hyperflux::openrgb::native
