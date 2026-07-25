// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/native_route_worker.hpp>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <memory>
#include <string>
#include <string_view>
#include <utility>

namespace hyperflux::openrgb::native
{
namespace
{

sdk::Error worker_error(sdk::ErrorCode code, std::string message)
{
    return {code, std::move(message), "HFX-NATIVE-OPENRGB-002"};
}

template<typename T, typename Value>
sdk::Result<T> checked_domain(Value value, std::string_view label)
{
    auto decoded = T::from(value);
    if(!decoded.has_value())
    {
        return sdk::Result<T>::failure(worker_error(
            sdk::ErrorCode::InvalidArgument,
            "invalid native discovery domain value for " + std::string(label)));
    }
    return sdk::Result<T>::success(std::move(*decoded));
}

} // namespace

NativeDiscoveryClientFactory make_unix_native_discovery_factory(
    sdk::UnixChannelConfig channel_config)
{
    return [channel_config = std::move(channel_config)]()
               -> sdk::Result<std::unique_ptr<sdk::NativeClientApi>>
    {
        auto client_id = checked_domain<ClientId>(
            "openrgb-native-discovery", "client ID");
        auto client_name = checked_domain<ClientName>(
            "OpenRGB HyperFlux native discovery", "client name");
        auto feature = checked_domain<ProtocolFeatureId>(
            "native-receiver-tunnel", "required feature");
        if(!client_id || !client_name || !feature)
        {
            return sdk::Result<std::unique_ptr<sdk::NativeClientApi>>::failure(
                !client_id ? client_id.error()
                           : (!client_name ? client_name.error() : feature.error()));
        }
        auto connected = sdk::NativeClient::connect_unix(
            channel_config,
            {
                std::move(client_id).value(),
                std::move(client_name).value(),
                {std::move(feature).value()},
                {},
            });
        if(!connected)
        {
            return sdk::Result<std::unique_ptr<sdk::NativeClientApi>>::failure(
                connected.error());
        }
        std::unique_ptr<sdk::NativeClientApi> erased =
            std::make_unique<sdk::NativeClient>(std::move(connected).value());
        return sdk::Result<std::unique_ptr<sdk::NativeClientApi>>::success(
            std::move(erased));
    };
}

NativeRouteWorker::NativeRouteWorker(
    NativeDiscoveryClientFactory factory,
    NativeRouteWorkerConfig config,
    NativeRouteWorkerCallbacks callbacks)
    : factory_(std::move(factory)),
      config_(config),
      callbacks_(std::move(callbacks))
{
}

NativeRouteWorker::~NativeRouteWorker()
{
    stop();
}

sdk::Result<std::unique_ptr<NativeRouteWorker>> NativeRouteWorker::create(
    NativeDiscoveryClientFactory factory,
    NativeRouteWorkerConfig config,
    NativeRouteWorkerCallbacks callbacks)
{
    if(!factory || config.poll_interval_ms == 0 || config.reconnect_initial_ms == 0
       || config.reconnect_max_ms < config.reconnect_initial_ms
       || config.max_events == 0 || config.max_events > 256)
    {
        return sdk::Result<std::unique_ptr<NativeRouteWorker>>::failure(worker_error(
            sdk::ErrorCode::InvalidArgument,
            "native OpenRGB route worker configuration is invalid"));
    }
    return sdk::Result<std::unique_ptr<NativeRouteWorker>>::success(
        std::unique_ptr<NativeRouteWorker>(new NativeRouteWorker(
            std::move(factory), config, std::move(callbacks))));
}

sdk::Result<void> NativeRouteWorker::start()
{
    std::lock_guard lock(mutex_);
    if(state_ != NativeRouteWorkerState::Created)
    {
        return sdk::Result<void>::failure(worker_error(
            sdk::ErrorCode::RuntimeConfiguration,
            "native OpenRGB route worker can only start once"));
    }
    state_ = NativeRouteWorkerState::Starting;
    try
    {
        thread_ = std::thread(&NativeRouteWorker::run, this);
    }
    catch(const std::exception& error)
    {
        state_ = NativeRouteWorkerState::Failed;
        return sdk::Result<void>::failure(worker_error(
            sdk::ErrorCode::RuntimeConfiguration,
            "native OpenRGB route worker thread failed: " + std::string(error.what())));
    }
    return sdk::Result<void>::success();
}

void NativeRouteWorker::stop() noexcept
{
    {
        std::lock_guard lock(mutex_);
        if(state_ == NativeRouteWorkerState::Created)
        {
            state_ = NativeRouteWorkerState::Stopped;
            return;
        }
        if(state_ == NativeRouteWorkerState::Stopped)
        {
            return;
        }
        stop_requested_ = true;
        state_ = NativeRouteWorkerState::Stopping;
    }
    wake_.notify_all();
    if(thread_.joinable())
    {
        thread_.join();
    }
    std::lock_guard lock(mutex_);
    state_ = NativeRouteWorkerState::Stopped;
}

void NativeRouteWorker::request_rescan() noexcept
{
    {
        std::lock_guard lock(mutex_);
        if(state_ == NativeRouteWorkerState::Stopped
           || state_ == NativeRouteWorkerState::Failed)
        {
            return;
        }
        rescan_requested_ = true;
    }
    wake_.notify_all();
}

NativeRouteWorkerState NativeRouteWorker::state() const noexcept
{
    std::lock_guard lock(mutex_);
    return state_;
}

std::optional<NativeRouteSnapshot> NativeRouteWorker::snapshot() const
{
    std::lock_guard lock(mutex_);
    return snapshot_;
}

std::optional<sdk::Error> NativeRouteWorker::last_error() const
{
    std::lock_guard lock(mutex_);
    return last_error_;
}

bool NativeRouteWorker::should_stop() const noexcept
{
    std::lock_guard lock(mutex_);
    return stop_requested_;
}

bool NativeRouteWorker::wait_interruptible(std::uint32_t delay_ms) noexcept
{
    std::unique_lock lock(mutex_);
    wake_.wait_for(
        lock,
        std::chrono::milliseconds(delay_ms),
        [this] { return stop_requested_ || rescan_requested_; });
    return !stop_requested_;
}

bool NativeRouteWorker::publish(NativeRouteSnapshot snapshot) noexcept
{
    try
    {
        {
            std::lock_guard lock(mutex_);
            snapshot_ = snapshot;
            last_error_.reset();
            state_ = NativeRouteWorkerState::Running;
        }
        if(callbacks_.on_snapshot)
        {
            try
            {
                callbacks_.on_snapshot(std::move(snapshot));
            }
        catch(...)
        {
            fail_unexpected("route snapshot callback threw");
            return false;
        }
        }
        return true;
    }
    catch(...)
    {
        fail_unexpected("route snapshot publication failed");
        return false;
    }
}

void NativeRouteWorker::fail_unexpected(const char* operation, const char* detail) noexcept
{
    try
    {
        std::string message = "native OpenRGB ";
        message += operation;
        if(detail != nullptr && *detail != '\0')
        {
            message += ": ";
            message += detail;
        }
        sdk::Error error = worker_error(
            sdk::ErrorCode::RuntimeConfiguration, std::move(message));
        {
            std::lock_guard lock(mutex_);
            last_error_ = error;
            state_ = NativeRouteWorkerState::Failed;
            stop_requested_ = true;
        }
        wake_.notify_all();
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
    }
    catch(...)
    {
        try
        {
            std::lock_guard lock(mutex_);
            state_ = NativeRouteWorkerState::Failed;
            stop_requested_ = true;
        }
        catch(...)
        {
        }
        wake_.notify_all();
    }
}

void NativeRouteWorker::recover(sdk::Error error, std::uint32_t& delay_ms) noexcept
{
    {
        std::lock_guard lock(mutex_);
        last_error_ = error;
        if(!stop_requested_)
        {
            state_ = NativeRouteWorkerState::Recovering;
        }
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
    static_cast<void>(wait_interruptible(delay_ms));
    delay_ms = std::min(config_.reconnect_max_ms, delay_ms * 2);
}

void NativeRouteWorker::run() noexcept
{
    try
    {
        std::uint32_t recovery_delay = config_.reconnect_initial_ms;
        while(!should_stop())
        {
            auto connected = factory_();
            if(!connected)
            {
                recover(connected.error(), recovery_delay);
                continue;
            }
            auto client = std::move(connected).value();
            auto inventory = client->native_enumerate();
            if(!inventory)
            {
                recover(inventory.error(), recovery_delay);
                continue;
            }

            auto cursor = inventory.value().cursor;
            if(!publish({cursor, std::move(inventory.value().routes)}))
            {
                break;
            }
            recovery_delay = config_.reconnect_initial_ms;
            std::optional<SubscriptionId> subscription_id;

            while(!should_stop())
            {
                bool force_rescan = false;
                {
                    std::lock_guard lock(mutex_);
                    force_rescan = std::exchange(rescan_requested_, false);
                }
                if(force_rescan)
                {
                    auto refreshed = client->native_enumerate();
                    if(!refreshed)
                    {
                        recover(refreshed.error(), recovery_delay);
                        break;
                    }
                    cursor = refreshed.value().cursor;
                    if(!publish({cursor, std::move(refreshed.value().routes)}))
                    {
                        break;
                    }
                    subscription_id.reset();
                }

                auto limit = EventBatchLimit::from(config_.max_events);
                if(!limit.has_value())
                {
                    recover(
                        worker_error(
                            sdk::ErrorCode::InvalidArgument,
                            "native OpenRGB event limit is outside the protocol domain"),
                        recovery_delay);
                    break;
                }
                auto events = client->native_watch({subscription_id, cursor, *limit});
                if(!events)
                {
                    recover(events.error(), recovery_delay);
                    break;
                }
                subscription_id = events.value().subscription_id;
                cursor = events.value().next_cursor;
                if(events.value().cursor_gap || !events.value().events.empty())
                {
                    auto refreshed = client->native_enumerate();
                    if(!refreshed)
                    {
                        recover(refreshed.error(), recovery_delay);
                        break;
                    }
                    cursor = refreshed.value().cursor;
                    if(!publish({cursor, std::move(refreshed.value().routes)}))
                    {
                        break;
                    }
                    subscription_id.reset();
                }
                if(!events.value().has_more
                   && !wait_interruptible(config_.poll_interval_ms))
                {
                    break;
                }
            }
        }
    }
    catch(const std::exception& error)
    {
        fail_unexpected("route worker threw", error.what());
    }
    catch(...)
    {
        fail_unexpected("route worker threw");
    }
    try
    {
        std::lock_guard lock(mutex_);
        if(state_ != NativeRouteWorkerState::Failed)
        {
            state_ = NativeRouteWorkerState::Stopped;
        }
    }
    catch(...)
    {
    }
}

} // namespace hyperflux::openrgb::native
