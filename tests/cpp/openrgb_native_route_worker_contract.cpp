// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/native_route_worker.hpp>

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

namespace
{

template<typename T>
T text(std::string_view value)
{
    auto decoded = T::from(value);
    if(!decoded.has_value())
    {
        throw std::runtime_error("invalid test string domain value");
    }
    return *decoded;
}

template<typename T>
T number(std::uint64_t value)
{
    auto decoded = T::from(static_cast<typename T::value_type>(value));
    if(!decoded.has_value())
    {
        throw std::runtime_error("invalid test numeric domain value");
    }
    return *decoded;
}

hyperflux::v7::EventCursor cursor(std::uint64_t sequence)
{
    using namespace hyperflux;
    return {
        text<StreamId>("native-routes"),
        number<StreamEpoch>(1),
        number<ProjectionRevision>(1),
        number<SequenceNumber>(sequence),
    };
}

hyperflux::v7::NativeRoute route(std::string_view device_id, std::uint16_t product_id)
{
    using namespace hyperflux;
    const auto kind = product_id == 0x00cd ? DeviceKind::Mouse : DeviceKind::Keyboard;
    const v7::ResourceKey resource {
        text<ReceiverId>("receiver-1"),
        number<GenerationId>(4),
        text<LogicalDeviceId>(device_id),
        ResourceKind::NativeControl,
    };
    return {
        resource.receiver_id,
        resource.generation_id,
        resource.device_id,
        text<EndpointId>(kind == DeviceKind::Mouse ? "endpoint-mouse" : "endpoint-keyboard"),
        kind,
        number<VendorId>(0x1532),
        number<ProductId>(product_id),
        RouteState::Available,
        NativeRouteAccess::Qualified,
        text<NativeProtocolFamily>("razer-feature-report-v1"),
        number<NativeReportByteCount>(90),
        text<NativeRouteToken>(std::string(64, kind == DeviceKind::Mouse ? 'a' : 'b')),
        resource,
    };
}

struct FakeState
{
    std::mutex mutex;
    std::size_t enumerate_calls = 0;
    std::size_t watch_calls = 0;
};

class FakeClient final : public hyperflux::sdk::NativeClientApi
{
public:
    explicit FakeClient(std::shared_ptr<FakeState> state)
        : state_(std::move(state))
    {
    }

    hyperflux::sdk::Result<hyperflux::v7::LeaseResult> acquire_lease(
        std::vector<hyperflux::v7::ResourceKey>, hyperflux::LeaseDurationMs) override
    {
        return unsupported<hyperflux::v7::LeaseResult>();
    }

    hyperflux::sdk::Result<hyperflux::v7::LeaseResult> renew_lease(
        hyperflux::LeaseId, hyperflux::LeaseDurationMs) override
    {
        return unsupported<hyperflux::v7::LeaseResult>();
    }

    hyperflux::sdk::Result<hyperflux::v7::LeaseResult> release_lease(
        hyperflux::LeaseId) override
    {
        return unsupported<hyperflux::v7::LeaseResult>();
    }

    hyperflux::sdk::Result<hyperflux::v7::NativeRouteInventory> native_enumerate() override
    {
        std::lock_guard lock(state_->mutex);
        ++state_->enumerate_calls;
        if(state_->enumerate_calls == 1)
        {
            return hyperflux::sdk::Result<hyperflux::v7::NativeRouteInventory>::success(
                {cursor(1), {route("mouse-00cd", 0x00cd)}});
        }
        return hyperflux::sdk::Result<hyperflux::v7::NativeRouteInventory>::success(
            {cursor(2), {route("keyboard-0296", 0x0296)}});
    }

    hyperflux::sdk::Result<hyperflux::sdk::NativeExchangeSubmission>
    prepare_native_exchange(
        hyperflux::LeaseId,
        hyperflux::NativeRouteToken,
        hyperflux::NativeReportHex,
        hyperflux::NativeExchangeTimeoutMs) override
    {
        return unsupported<hyperflux::sdk::NativeExchangeSubmission>();
    }

    hyperflux::sdk::Result<hyperflux::v7::NativeExchangeOutcome> native_transact(
        const hyperflux::sdk::NativeExchangeSubmission&) override
    {
        return unsupported<hyperflux::v7::NativeExchangeOutcome>();
    }

    hyperflux::sdk::Result<hyperflux::v7::NativeRouteEventBatch> native_watch(
        hyperflux::sdk::NativeEventSubscription) override
    {
        std::lock_guard lock(state_->mutex);
        ++state_->watch_calls;
        if(state_->watch_calls == 1)
        {
            const auto keyboard = route("keyboard-0296", 0x0296);
            return hyperflux::sdk::Result<hyperflux::v7::NativeRouteEventBatch>::success({
                text<hyperflux::SubscriptionId>("subscription-1"),
                cursor(2),
                {{number<hyperflux::SequenceNumber>(2),
                    hyperflux::NativeRouteEventKind::Available,
                    keyboard}},
                number<hyperflux::SequenceNumber>(1),
                number<hyperflux::SequenceNumber>(2),
                number<hyperflux::DroppedEventCount>(0),
                false,
                false,
            });
        }
        return hyperflux::sdk::Result<hyperflux::v7::NativeRouteEventBatch>::success({
            text<hyperflux::SubscriptionId>("subscription-2"),
            cursor(2),
            {},
            number<hyperflux::SequenceNumber>(1),
            number<hyperflux::SequenceNumber>(2),
            number<hyperflux::DroppedEventCount>(0),
            false,
            false,
        });
    }

private:
    template<typename T>
    hyperflux::sdk::Result<T> unsupported()
    {
        return hyperflux::sdk::Result<T>::failure({
            hyperflux::sdk::ErrorCode::UnexpectedResponse,
            "outside native route worker contract",
            std::nullopt,
        });
    }

    std::shared_ptr<FakeState> state_;
};

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb::native;

    auto state = std::make_shared<FakeState>();
    std::mutex callback_mutex;
    std::condition_variable callback_wake;
    std::vector<NativeRouteSnapshot> snapshots;
    std::size_t factory_calls = 0;

    auto worker = NativeRouteWorker::create(
        [&]() -> sdk::Result<std::unique_ptr<sdk::NativeClientApi>>
        {
            ++factory_calls;
            if(factory_calls == 1)
            {
                return sdk::Result<std::unique_ptr<sdk::NativeClientApi>>::failure({
                    sdk::ErrorCode::SocketConnect,
                    "expected first connection failure",
                    std::nullopt,
                });
            }
            std::unique_ptr<sdk::NativeClientApi> client =
                std::make_unique<FakeClient>(state);
            return sdk::Result<std::unique_ptr<sdk::NativeClientApi>>::success(
                std::move(client));
        },
        {5, 1, 4, 8},
        {
            [&](NativeRouteSnapshot snapshot)
            {
                {
                    std::lock_guard lock(callback_mutex);
                    snapshots.push_back(std::move(snapshot));
                }
                callback_wake.notify_all();
            },
            [](sdk::Error) {},
        });
    if(!worker || !worker.value()->start())
    {
        return 1;
    }

    {
        std::unique_lock lock(callback_mutex);
        if(!callback_wake.wait_for(
               lock,
               std::chrono::seconds(2),
               [&] { return snapshots.size() >= 2; }))
        {
            worker.value()->stop();
            return 2;
        }
    }
    worker.value()->stop();

    if(factory_calls != 2 || snapshots.size() < 2
       || snapshots[0].routes.size() != 1
       || snapshots[0].routes.front().device_id.value() != "mouse-00cd"
       || snapshots[1].routes.size() != 1
       || snapshots[1].routes.front().device_id.value() != "keyboard-0296"
       || worker.value()->state() != NativeRouteWorkerState::Stopped)
    {
        return 3;
    }
    {
        std::lock_guard lock(state->mutex);
        if(state->enumerate_calls < 2 || state->watch_calls < 1)
        {
            return 4;
        }
    }

    auto invalid = NativeRouteWorker::create(
        []() -> sdk::Result<std::unique_ptr<sdk::NativeClientApi>>
        {
            return sdk::Result<std::unique_ptr<sdk::NativeClientApi>>::failure({
                sdk::ErrorCode::SocketConnect,
                "not called",
                std::nullopt,
            });
        },
        {0, 1, 4, 8});
    if(invalid)
    {
        return 5;
    }

    auto callback_failure = NativeRouteWorker::create(
        [state]() -> sdk::Result<std::unique_ptr<sdk::NativeClientApi>>
        {
            std::unique_ptr<sdk::NativeClientApi> client =
                std::make_unique<FakeClient>(state);
            return sdk::Result<std::unique_ptr<sdk::NativeClientApi>>::success(
                std::move(client));
        },
        {5, 1, 4, 8},
        {
            [](NativeRouteSnapshot) { throw std::runtime_error("expected callback failure"); },
            [](sdk::Error) {},
        });
    if(!callback_failure || !callback_failure.value()->start())
    {
        return 6;
    }
    for(std::size_t attempt = 0;
        attempt < 2'000
        && callback_failure.value()->state() != NativeRouteWorkerState::Failed;
        ++attempt)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    if(callback_failure.value()->state() != NativeRouteWorkerState::Failed
       || !callback_failure.value()->last_error().has_value())
    {
        callback_failure.value()->stop();
        return 7;
    }
    callback_failure.value()->stop();

    auto factory_failure = NativeRouteWorker::create(
        []() -> sdk::Result<std::unique_ptr<sdk::NativeClientApi>>
        {
            throw std::runtime_error("expected factory failure");
        },
        {5, 1, 4, 8});
    if(!factory_failure || !factory_failure.value()->start())
    {
        return 8;
    }
    for(std::size_t attempt = 0;
        attempt < 2'000
        && factory_failure.value()->state() != NativeRouteWorkerState::Failed;
        ++attempt)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    if(factory_failure.value()->state() != NativeRouteWorkerState::Failed
       || !factory_failure.value()->last_error().has_value()
       || factory_failure.value()->last_error()->message.find("expected factory failure")
              == std::string::npos)
    {
        factory_failure.value()->stop();
        return 9;
    }
    factory_failure.value()->stop();

    return 0;
}
