// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/runtime_worker.hpp>

#include <hyperflux/sdk/clock.hpp>

#include <algorithm>
#include <chrono>
#include <exception>
#include <iterator>
#include <set>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>

namespace hyperflux::openrgb
{
namespace
{

sdk::Error worker_error(std::string message)
{
    return {
        sdk::ErrorCode::RuntimeConfiguration,
        std::move(message),
        "HFX-RUNTIME-001",
    };
}

bool valid_frame(const QueuedLightingFrame& frame) noexcept
{
    return !frame.stable_id.empty() && frame.expected_slot_count > 0
        && frame.expected_slot_count <= LedCount::maximum
        && frame.colors.size() == frame.expected_slot_count;
}

bool disjoint_frames(
    const std::vector<QueuedLightingFrame>& left,
    const std::vector<QueuedLightingFrame>& right)
{
    return std::none_of(left.begin(), left.end(), [&right](const auto& candidate) {
        return std::any_of(right.begin(), right.end(), [&candidate](const auto& value) {
            return value.stable_id == candidate.stable_id;
        });
    });
}

bool contains_frame(
    const std::vector<QueuedLightingFrame>& frames,
    std::string_view stable_id)
{
    return std::any_of(frames.begin(), frames.end(), [stable_id](const auto& frame) {
        return frame.stable_id == stable_id;
    });
}

bool meaningful(const RuntimeStep& output) noexcept
{
    return output.full_refresh || output.cursor_gap_recovered
        || !output.controller_changes.empty() || !output.dispatch_outcomes.empty()
        || !output.logical_outcomes.empty() || !output.notices.empty();
}

} // namespace

RuntimeWorker::RuntimeWorker(
    std::unique_ptr<RuntimeBridge> bridge,
    RuntimeCore core,
    WorkerConfig config,
    WorkerCallbacks callbacks)
    : bridge_(std::move(bridge)),
      core_(std::move(core)),
      config_(config),
      callbacks_(std::move(callbacks))
{
}

RuntimeWorker::~RuntimeWorker()
{
    stop();
}

sdk::Result<std::unique_ptr<RuntimeWorker>> RuntimeWorker::create(
    std::unique_ptr<RuntimeBridge> bridge,
    WorkerConfig config,
    WorkerCallbacks callbacks)
{
    if(bridge == nullptr || config.poll_interval_ms == 0
       || config.stable_callback_window_ms == 0
       || config.reconnect_initial_ms == 0 || config.reconnect_max_ms == 0
       || config.reconnect_initial_ms > config.reconnect_max_ms)
    {
        return sdk::Result<std::unique_ptr<RuntimeWorker>>::failure(
            worker_error("OpenRGB worker requires a bridge and valid nonzero timing bounds"));
    }
    auto core = RuntimeCore::create(*bridge, config.runtime);
    if(!core)
    {
        return sdk::Result<std::unique_ptr<RuntimeWorker>>::failure(core.error());
    }
    return sdk::Result<std::unique_ptr<RuntimeWorker>>::success(
        std::unique_ptr<RuntimeWorker>(new RuntimeWorker(
            std::move(bridge),
            std::move(core).value(),
            config,
            std::move(callbacks))));
}

sdk::Result<void> RuntimeWorker::start()
{
    std::lock_guard lock(mutex_);
    if(state_ != WorkerState::Created)
    {
        return sdk::Result<void>::failure(
            worker_error("OpenRGB worker may be started exactly once"));
    }
    state_ = WorkerState::Starting;
    try
    {
        thread_ = std::thread(&RuntimeWorker::run, this);
    }
    catch(const std::system_error& exception)
    {
        state_ = WorkerState::Created;
        return sdk::Result<void>::failure(worker_error(
            "OpenRGB worker thread could not start: "
            + std::string(exception.what())));
    }
    return sdk::Result<void>::success();
}

void RuntimeWorker::stop() noexcept
{
    bool notify = false;
    {
        std::lock_guard lock(mutex_);
        if(state_ == WorkerState::Created)
        {
            state_ = WorkerState::Stopped;
        }
        else if(state_ != WorkerState::Stopped)
        {
            stop_requested_ = true;
            if(state_ != WorkerState::Failed)
            {
                state_ = WorkerState::Stopping;
            }
            notify = true;
        }
    }
    if(notify)
    {
        wake_.notify_all();
    }
    if(thread_.joinable() && thread_.get_id() != std::this_thread::get_id())
    {
        thread_.join();
    }
}

sdk::Result<EnqueueDisposition> RuntimeWorker::enqueue_effect(
    QueuedLightingFrame frame)
{
    if(!valid_frame(frame))
    {
        return sdk::Result<EnqueueDisposition>::success(
            EnqueueDisposition::RejectedInvalid);
    }
    auto now = sdk::monotonic_now();
    if(!now)
    {
        return sdk::Result<EnqueueDisposition>::failure(now.error());
    }
    std::lock_guard lock(mutex_);
    if(!accepts_commands())
    {
        return sdk::Result<EnqueueDisposition>::failure(
            worker_error("OpenRGB worker is not accepting lighting commands"));
    }
    const auto stable_now = std::chrono::steady_clock::now();
    for(auto& command : stable_commands_)
    {
        if(contains_frame(command.frames, frame.stable_id))
        {
            command.due_at = std::min(command.due_at, stable_now);
            break;
        }
    }
    const auto existing = effect_commands_.find(frame.stable_id);
    if(existing != effect_commands_.end())
    {
        existing->second.frame = std::move(frame);
        wake_.notify_one();
        return sdk::Result<EnqueueDisposition>::success(
            EnqueueDisposition::Coalesced);
    }
    const auto already_reserved = effect_reservations_.contains(frame.stable_id);
    if(!already_reserved
       && effect_reservations_.size()
           >= config_.runtime.dispatch_queue.effect_target_capacity)
    {
        return sdk::Result<EnqueueDisposition>::success(
            EnqueueDisposition::RejectedCapacity);
    }
    auto stable_id = frame.stable_id;
    effect_reservations_.insert(stable_id);
    effect_commands_.emplace(
        std::move(stable_id),
        EffectCommand {std::move(frame), now.value().value()});
    wake_.notify_one();
    return sdk::Result<EnqueueDisposition>::success(
        already_reserved ? EnqueueDisposition::Coalesced
                         : EnqueueDisposition::Accepted);
}

sdk::Result<EnqueueDisposition> RuntimeWorker::enqueue_stable(
    sdk::LightingIntent intent,
    std::vector<QueuedLightingFrame> frames)
{
    if((intent != sdk::LightingIntent::Static && intent != sdk::LightingIntent::Off)
       || frames.empty() || frames.size() > FrameCount::maximum)
    {
        return sdk::Result<EnqueueDisposition>::success(
            EnqueueDisposition::RejectedInvalid);
    }
    std::set<std::string> identities;
    for(const auto& frame : frames)
    {
        if(!valid_frame(frame) || !identities.insert(frame.stable_id).second)
        {
            return sdk::Result<EnqueueDisposition>::success(
                EnqueueDisposition::RejectedInvalid);
        }
    }
    const auto now = std::chrono::steady_clock::now();
    std::lock_guard lock(mutex_);
    if(!accepts_commands())
    {
        return sdk::Result<EnqueueDisposition>::failure(
            worker_error("OpenRGB worker is not accepting lighting commands"));
    }
    const auto mergeable = !stable_commands_.empty()
        && stable_commands_.back().intent == intent
        && now <= stable_commands_.back().due_at
        && disjoint_frames(stable_commands_.back().frames, frames)
        && stable_commands_.back().frames.size()
            <= FrameCount::maximum - frames.size();
    if(!mergeable
       && stable_reservations_ >= config_.runtime.dispatch_queue.stable_capacity)
    {
        return sdk::Result<EnqueueDisposition>::success(
            EnqueueDisposition::RejectedCapacity);
    }
    for(const auto& frame : frames)
    {
        effect_commands_.erase(frame.stable_id);
        effect_reservations_.erase(frame.stable_id);
    }
    if(mergeable)
    {
        auto& target = stable_commands_.back().frames;
        target.insert(
            target.end(),
            std::make_move_iterator(frames.begin()),
            std::make_move_iterator(frames.end()));
    }
    else
    {
        stable_commands_.push_back({
            intent,
            std::move(frames),
            now + std::chrono::milliseconds(config_.stable_callback_window_ms),
        });
        ++stable_reservations_;
    }
    wake_.notify_one();
    return sdk::Result<EnqueueDisposition>::success(
        mergeable ? EnqueueDisposition::Coalesced : EnqueueDisposition::Accepted);
}

sdk::Result<void> RuntimeWorker::request_rescan()
{
    std::lock_guard lock(mutex_);
    if(!accepts_commands())
    {
        return sdk::Result<void>::failure(
            worker_error("OpenRGB worker is not accepting a rescan request"));
    }
    rescan_requested_ = true;
    wake_.notify_one();
    return sdk::Result<void>::success();
}

WorkerState RuntimeWorker::state() const noexcept
{
    std::lock_guard lock(mutex_);
    return state_;
}

std::vector<ControllerModel> RuntimeWorker::controllers() const
{
    std::lock_guard lock(mutex_);
    return controllers_;
}

RuntimeSnapshot RuntimeWorker::snapshot() const
{
    std::lock_guard lock(mutex_);
    return {controllers_, inventory_};
}

std::optional<sdk::Error> RuntimeWorker::last_error() const
{
    std::lock_guard lock(mutex_);
    return last_error_;
}

bool RuntimeWorker::accepts_commands() const noexcept
{
    return state_ == WorkerState::Starting || state_ == WorkerState::Running
        || state_ == WorkerState::Recovering;
}

void RuntimeWorker::deliver(RuntimeStep output) noexcept
{
    {
        std::lock_guard lock(mutex_);
        controllers_ = core_.controllers();
        inventory_ = core_.inventory();
    }
    if(!meaningful(output) || !callbacks_.on_step)
    {
        return;
    }
    try
    {
        callbacks_.on_step(std::move(output));
    }
    catch(const std::exception& exception)
    {
        fail(worker_error(
            "OpenRGB runtime callback failed: " + std::string(exception.what())));
    }
    catch(...)
    {
        fail(worker_error("OpenRGB runtime callback failed with an unknown exception"));
    }
}

void RuntimeWorker::fail(sdk::Error error) noexcept
{
    {
        std::lock_guard lock(mutex_);
        last_error_ = error;
        state_ = WorkerState::Failed;
        stop_requested_ = true;
    }
    if(callbacks_.on_error)
    {
        try
        {
            callbacks_.on_error(std::move(error));
        }
        catch(...)
        {
        }
    }
    wake_.notify_all();
}

bool RuntimeWorker::wait_for_recovery(
    sdk::Error error,
    std::uint32_t& delay_ms) noexcept
{
    {
        std::lock_guard lock(mutex_);
        if(stop_requested_)
        {
            return false;
        }
        state_ = WorkerState::Recovering;
        last_error_ = error;
    }

    RuntimeStep notice;
    notice.notices.push_back(std::move(error));
    deliver(std::move(notice));

    std::unique_lock lock(mutex_);
    if(state_ == WorkerState::Failed || stop_requested_)
    {
        return false;
    }
    wake_.wait_for(
        lock,
        std::chrono::milliseconds(delay_ms),
        [this] { return stop_requested_; });
    if(stop_requested_)
    {
        return false;
    }
    const auto doubled = static_cast<std::uint64_t>(delay_ms) * 2;
    delay_ms = static_cast<std::uint32_t>(std::min<std::uint64_t>(
        config_.reconnect_max_ms,
        doubled));
    return true;
}

void RuntimeWorker::mark_running() noexcept
{
    std::lock_guard lock(mutex_);
    if(!stop_requested_ && state_ != WorkerState::Failed)
    {
        state_ = WorkerState::Running;
        last_error_.reset();
    }
}

void RuntimeWorker::refresh_reservations() noexcept
{
    auto effects = core_.queued_effect_targets();
    const auto stable = core_.queued_stable_count();
    std::lock_guard lock(mutex_);
    stable_reservations_ = stable_commands_.size() + stable;
    for(const auto& [stable_id, command] : effect_commands_)
    {
        (void)command;
        effects.insert(stable_id);
    }
    effect_reservations_ = std::move(effects);
}

RuntimeStep RuntimeWorker::terminalize_mailbox()
{
    std::deque<StableCommand> stable;
    std::map<std::string, EffectCommand> effects;
    {
        std::lock_guard lock(mutex_);
        stable.swap(stable_commands_);
        effects.swap(effect_commands_);
    }

    RuntimeStep output;
    for(auto& command : stable)
    {
        if(core_.enqueue_stable(command.intent, std::move(command.frames))
           != EnqueueDisposition::Accepted)
        {
            output.notices.push_back(worker_error(
                "OpenRGB could not terminally account for a stable mailbox command"));
        }
    }
    for(auto& [stable_id, command] : effects)
    {
        (void)stable_id;
        const auto disposition = core_.enqueue_effect(
            std::move(command.frame), command.first_enqueued_ms);
        if(disposition != EnqueueDisposition::Accepted
           && disposition != EnqueueDisposition::Coalesced)
        {
            output.notices.push_back(worker_error(
                "OpenRGB could not terminally account for an effect mailbox command"));
        }
    }

    auto shutdown = core_.shutdown();
    output.controller_changes = std::move(shutdown.controller_changes);
    output.dispatch_outcomes = std::move(shutdown.dispatch_outcomes);
    output.logical_outcomes = std::move(shutdown.logical_outcomes);
    output.notices.insert(
        output.notices.end(),
        std::make_move_iterator(shutdown.notices.begin()),
        std::make_move_iterator(shutdown.notices.end()));
    output.full_refresh = shutdown.full_refresh;
    output.cursor_gap_recovered = shutdown.cursor_gap_recovered;
    return output;
}

void RuntimeWorker::run() noexcept
{
    auto reconnect_delay_ms = config_.reconnect_initial_ms;
    while(true)
    {
        auto initialized = core_.initialize();
        if(initialized)
        {
            mark_running();
            deliver(std::move(initialized).value());
            break;
        }
        if(!sdk::is_connection_error(initialized.error().code))
        {
            fail(initialized.error());
            return;
        }
        if(!wait_for_recovery(initialized.error(), reconnect_delay_ms))
        {
            break;
        }
    }

    bool recovery_required = false;
    while(core_.initialized())
    {
        if(recovery_required)
        {
            auto recovered = core_.rescan();
            if(!recovered)
            {
                if(!sdk::is_connection_error(recovered.error().code))
                {
                    fail(recovered.error());
                    break;
                }
                if(!wait_for_recovery(recovered.error(), reconnect_delay_ms))
                {
                    break;
                }
                continue;
            }
            recovery_required = false;
            reconnect_delay_ms = config_.reconnect_initial_ms;
            mark_running();
            deliver(std::move(recovered).value());
        }

        std::deque<StableCommand> stable;
        std::map<std::string, EffectCommand> effects;
        bool rescan = false;
        {
            std::unique_lock lock(mutex_);
            auto wait_duration = std::chrono::milliseconds(config_.poll_interval_ms);
            if(!stable_commands_.empty())
            {
                const auto now = std::chrono::steady_clock::now();
                if(stable_commands_.front().due_at <= now)
                {
                    wait_duration = std::chrono::milliseconds::zero();
                }
                else
                {
                    wait_duration = std::min(
                        wait_duration,
                        std::chrono::ceil<std::chrono::milliseconds>(
                            stable_commands_.front().due_at - now));
                }
            }
            wake_.wait_for(lock, wait_duration, [this] {
                return stop_requested_ || rescan_requested_
                    || !effect_commands_.empty()
                    || (!stable_commands_.empty()
                        && stable_commands_.front().due_at
                            <= std::chrono::steady_clock::now());
            });
            if(stop_requested_)
            {
                break;
            }
            const auto stable_now = std::chrono::steady_clock::now();
            while(!stable_commands_.empty()
                  && stable_commands_.front().due_at <= stable_now)
            {
                stable.push_back(std::move(stable_commands_.front()));
                stable_commands_.pop_front();
            }
            effects.swap(effect_commands_);
            rescan = std::exchange(rescan_requested_, false);
        }

        for(auto& command : stable)
        {
            const auto disposition = core_.enqueue_stable(
                command.intent,
                std::move(command.frames));
            if(disposition != EnqueueDisposition::Accepted)
            {
                fail(worker_error("OpenRGB stable command exceeded the runtime queue contract"));
                break;
            }
        }
        if(state() == WorkerState::Failed)
        {
            break;
        }
        for(auto& [stable_id, command] : effects)
        {
            (void)stable_id;
            const auto disposition = core_.enqueue_effect(
                std::move(command.frame),
                command.first_enqueued_ms);
            if(disposition != EnqueueDisposition::Accepted
               && disposition != EnqueueDisposition::Coalesced)
            {
                fail(worker_error("OpenRGB effect command exceeded the runtime queue contract"));
                break;
            }
        }
        refresh_reservations();
        if(state() == WorkerState::Failed)
        {
            break;
        }
        if(rescan)
        {
            auto rescanned = core_.rescan();
            if(!rescanned)
            {
                if(sdk::is_connection_error(rescanned.error().code))
                {
                    recovery_required = true;
                    if(!wait_for_recovery(rescanned.error(), reconnect_delay_ms))
                    {
                        break;
                    }
                    continue;
                }
                fail(rescanned.error());
                break;
            }
            deliver(std::move(rescanned).value());
        }

        auto now = sdk::monotonic_now();
        if(!now)
        {
            fail(now.error());
            break;
        }
        auto output = core_.step(now.value().value());
        if(!output)
        {
            refresh_reservations();
            if(sdk::is_connection_error(output.error().code))
            {
                recovery_required = true;
                if(!wait_for_recovery(output.error(), reconnect_delay_ms))
                {
                    break;
                }
                continue;
            }
            fail(output.error());
            break;
        }
        refresh_reservations();
        reconnect_delay_ms = config_.reconnect_initial_ms;
        mark_running();
        deliver(std::move(output).value());
    }

    deliver(terminalize_mailbox());
    {
        std::lock_guard lock(mutex_);
        stable_commands_.clear();
        effect_commands_.clear();
        stable_reservations_ = 0;
        effect_reservations_.clear();
        if(state_ != WorkerState::Failed)
        {
            state_ = WorkerState::Stopped;
        }
    }
}

} // namespace hyperflux::openrgb
