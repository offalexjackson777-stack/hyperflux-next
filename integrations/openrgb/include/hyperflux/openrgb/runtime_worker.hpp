// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "runtime_core.hpp"

#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <thread>
#include <utility>
#include <vector>

namespace hyperflux::openrgb
{

enum class WorkerState
{
    Created,
    Starting,
    Running,
    Recovering,
    Stopping,
    Stopped,
    Failed,
};

struct WorkerConfig
{
    RuntimeConfig runtime {};
    std::uint32_t poll_interval_ms = 10;
    std::uint32_t reconnect_initial_ms = 25;
    std::uint32_t reconnect_max_ms = 2'000;
};

struct WorkerCallbacks
{
    std::function<void(RuntimeStep)> on_step;
    std::function<void(sdk::Error)> on_error;
};

/// Thread boundary between OpenRGB callbacks and the serialized SDK client.
///
/// Public methods only touch a bounded mailbox. The worker alone owns the
/// runtime core and therefore performs every socket, lease, event, and
/// transaction operation in one deterministic order.
class RuntimeWorker
{
public:
    RuntimeWorker(const RuntimeWorker&) = delete;
    RuntimeWorker& operator=(const RuntimeWorker&) = delete;
    RuntimeWorker(RuntimeWorker&&) = delete;
    RuntimeWorker& operator=(RuntimeWorker&&) = delete;
    ~RuntimeWorker();

    [[nodiscard]] static sdk::Result<std::unique_ptr<RuntimeWorker>> create(
        std::unique_ptr<RuntimeBridge> bridge,
        WorkerConfig config = {},
        WorkerCallbacks callbacks = {});

    [[nodiscard]] sdk::Result<void> start();
    void stop() noexcept;

    [[nodiscard]] sdk::Result<EnqueueDisposition> enqueue_effect(
        QueuedLightingFrame frame);
    [[nodiscard]] EnqueueDisposition enqueue_stable(
        sdk::LightingIntent intent,
        std::vector<QueuedLightingFrame> frames);
    [[nodiscard]] sdk::Result<void> request_rescan();

    [[nodiscard]] WorkerState state() const noexcept;
    [[nodiscard]] std::vector<ControllerModel> controllers() const;
    [[nodiscard]] std::optional<sdk::Error> last_error() const;

private:
    struct EffectCommand
    {
        QueuedLightingFrame frame;
        std::uint64_t first_enqueued_ms;
    };

    RuntimeWorker(
        std::unique_ptr<RuntimeBridge> bridge,
        RuntimeCore core,
        WorkerConfig config,
        WorkerCallbacks callbacks);

    void run() noexcept;
    void deliver(RuntimeStep output) noexcept;
    void fail(sdk::Error error) noexcept;
    [[nodiscard]] bool wait_for_recovery(
        sdk::Error error,
        std::uint32_t& delay_ms) noexcept;
    void mark_running() noexcept;
    void refresh_reservations() noexcept;
    [[nodiscard]] bool accepts_commands() const noexcept;

    std::unique_ptr<RuntimeBridge> bridge_;
    RuntimeCore core_;
    WorkerConfig config_;
    WorkerCallbacks callbacks_;

    mutable std::mutex mutex_;
    std::condition_variable wake_;
    std::thread thread_;
    WorkerState state_ = WorkerState::Created;
    bool stop_requested_ = false;
    bool rescan_requested_ = false;
    std::deque<std::pair<sdk::LightingIntent, std::vector<QueuedLightingFrame>>>
        stable_commands_;
    std::map<std::string, EffectCommand> effect_commands_;
    std::size_t stable_reservations_ = 0;
    std::set<std::string> effect_reservations_;
    std::vector<ControllerModel> controllers_;
    std::optional<sdk::Error> last_error_;
};

} // namespace hyperflux::openrgb
