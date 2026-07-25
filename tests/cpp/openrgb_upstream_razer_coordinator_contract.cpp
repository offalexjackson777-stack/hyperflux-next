// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/upstream_razer_coordinator.hpp>

#include "RGBController.h"
#include "RazerDevices.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
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

hyperflux::v7::EventCursor cursor()
{
    using namespace hyperflux;
    return {
        text<StreamId>("native-routes"),
        number<StreamEpoch>(1),
        number<ProjectionRevision>(1),
        number<SequenceNumber>(1),
    };
}

hyperflux::v7::NativeRoute route(
    hyperflux::DeviceKind kind,
    std::string_view device_id,
    std::string_view endpoint_id,
    std::uint16_t product_id,
    char token_fill)
{
    using namespace hyperflux;
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
        text<EndpointId>(endpoint_id),
        kind,
        number<VendorId>(0x1532),
        number<ProductId>(product_id),
        NativeRouteState::Available,
        NativeRouteAccess::Qualified,
        text<NativeProtocolFamily>("razer-feature-report-v1"),
        number<NativeReportByteCount>(90),
        text<NativeRouteToken>(std::string(64, token_fill)),
        resource,
    };
}

std::vector<hyperflux::v7::NativeRoute> routes()
{
    return {
        route(
            hyperflux::DeviceKind::Keyboard,
            "keyboard-0296",
            "endpoint-keyboard",
            RAZER_DEATHSTALKER_V2_PRO_TKL_WIRELESS_PID,
            'b'),
        route(
            hyperflux::DeviceKind::Mouse,
            "mouse-00cd",
            "endpoint-mouse",
            RAZER_BASILISK_V3_PRO_35K_WIRELESS_PID,
            'a'),
    };
}

class DiscoveryClient final : public hyperflux::sdk::NativeClientApi
{
public:
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
        return hyperflux::sdk::Result<hyperflux::v7::NativeRouteInventory>::success(
            {cursor(), routes()});
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
        return hyperflux::sdk::Result<hyperflux::v7::NativeRouteEventBatch>::success({
            text<hyperflux::SubscriptionId>("subscription-1"),
            cursor(),
            {},
            number<hyperflux::SequenceNumber>(1),
            number<hyperflux::SequenceNumber>(1),
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
            "outside coordinator discovery contract",
            std::nullopt,
        });
    }
};

class ScriptedExchange final
    : public hyperflux::openrgb::native::NativeFeatureExchange
{
public:
    explicit ScriptedExchange(std::string serial)
        : serial_(std::move(serial))
    {
    }

    hyperflux::openrgb::native::NativeFeatureExchangeResult exchange(
        const std::array<std::uint8_t,
            hyperflux::openrgb::native::razer_native_report_bytes>& request) override
    {
        auto response = request;
        response[0] = 0x02;
        response[5] = 0x00;
        response[8] = 0x00;
        if(request[6] == 0x00 && request[7] == RAZER_COMMAND_ID_GET_FIRMWARE_VERSION)
        {
            response[8] = 3;
            response[9] = 7;
        }
        else if(request[6] == 0x00 && request[7] == RAZER_COMMAND_ID_GET_SERIAL_STRING)
        {
            std::copy(serial_.begin(), serial_.end(), response.begin() + 8);
        }
        else if(request[6] == 0x00 && request[7] == RAZER_COMMAND_ID_GET_KEYBOARD_INFO)
        {
            response[8] = RAZER_KEYBOARD_LAYOUT_US;
            response[9] = RAZER_KEYBOARD_VARIANT_BLACK;
        }
        return {
            hyperflux::openrgb::native::NativeExchangeCertainty::MatchedResponse,
            response,
        };
    }

private:
    std::string serial_;
};

class TestController final : public RGBController
{
public:
    void SetupZones() override {}
    void ResizeZone(int, int) override {}
    void DeviceUpdateLEDs() override {}
    void UpdateZoneLEDs(int) override {}
    void UpdateSingleLED(int) override {}
    void DeviceUpdateMode() override {}
};

class Host final : public hyperflux::openrgb::native::UpstreamPluginHost
{
public:
    void register_controller(RGBController& controller) noexcept override
    {
        if(std::find(registered.begin(), registered.end(), &controller) == registered.end())
        {
            registered.push_back(&controller);
        }
    }

    void unregister_controller(RGBController& controller) noexcept override
    {
        registered.erase(
            std::remove(registered.begin(), registered.end(), &controller),
            registered.end());
    }

    std::vector<RGBController*> controllers() const override
    {
        auto result = registered;
        result.insert(result.end(), direct.begin(), direct.end());
        return result;
    }

    std::vector<RGBController*> registered;
    std::vector<RGBController*> direct;
};

class Dispatcher final : public hyperflux::openrgb::native::ApplicationDispatcher
{
public:
    bool post(std::function<void()> task) noexcept override
    {
        try
        {
            {
                std::lock_guard lock(mutex);
                tasks.push_back(std::move(task));
            }
            wake.notify_all();
            return true;
        }
        catch(...)
        {
            return false;
        }
    }

    bool run_one()
    {
        std::function<void()> task;
        {
            std::unique_lock lock(mutex);
            if(!wake.wait_for(
                   lock,
                   std::chrono::seconds(2),
                   [&] { return !tasks.empty(); }))
            {
                return false;
            }
            task = std::move(tasks.front());
            tasks.pop_front();
        }
        task();
        return true;
    }

private:
    std::mutex mutex;
    std::condition_variable wake;
    std::deque<std::function<void()>> tasks;
};

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb::native;

    Host host;
    Dispatcher dispatcher;
    auto discovery_factory = []()
        -> sdk::Result<std::unique_ptr<sdk::NativeClientApi>>
    {
        std::unique_ptr<sdk::NativeClientApi> client =
            std::make_unique<DiscoveryClient>();
        return sdk::Result<std::unique_ptr<sdk::NativeClientApi>>::success(
            std::move(client));
    };
    auto exchange_factory = [](const NativeRouteIdentity& identity)
        -> sdk::Result<std::unique_ptr<NativeFeatureExchange>>
    {
        const auto serial = identity.device_kind == DeviceKind::Mouse
                                ? "MOUSE-SERIAL-0001"
                                : "KEYBOARD-SERIAL-0001";
        std::unique_ptr<NativeFeatureExchange> exchange =
            std::make_unique<ScriptedExchange>(serial);
        return sdk::Result<std::unique_ptr<NativeFeatureExchange>>::success(
            std::move(exchange));
    };
    auto coordinator = UpstreamRazerPluginCoordinator::create(
        host,
        dispatcher,
        discovery_factory,
        exchange_factory,
        {{5, 1, 4, 8}, {}});
    if(!coordinator || !coordinator.value()->start() || !dispatcher.run_one())
    {
        return 1;
    }
    auto initial = coordinator.value()->status();
    if(initial.controllers != 2 || initial.registered_controllers != 2
       || initial.direct_suppressed_controllers != 0 || host.registered.size() != 2)
    {
        return 2;
    }

    TestController direct_mouse;
    direct_mouse.vendor = "Razer";
    direct_mouse.location = "HID: /dev/hidraw-direct-mouse";
    direct_mouse.serial = "MOUSE-SERIAL-0001     ";
    coordinator.value()->detection_started();
    host.direct = {&direct_mouse};
    coordinator.value()->detection_finished();
    auto suppressed = coordinator.value()->status();
    if(suppressed.detection_suspended || suppressed.registered_controllers != 1
       || suppressed.direct_suppressed_controllers != 1
       || host.registered.size() != 1
       || canonical_queried_serial(host.registered.front()->serial)
              != "KEYBOARD-SERIAL-0001")
    {
        return 3;
    }

    coordinator.value()->detection_started();
    host.direct.clear();
    coordinator.value()->detection_finished();
    auto restored = coordinator.value()->status();
    if(restored.registered_controllers != 2
       || restored.direct_suppressed_controllers != 0 || host.registered.size() != 2)
    {
        return 4;
    }

    coordinator.value()->shutdown();
    if(!coordinator.value()->status().stopped || !host.registered.empty())
    {
        return 5;
    }

    return 0;
}
