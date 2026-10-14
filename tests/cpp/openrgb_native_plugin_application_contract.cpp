// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/native_plugin_application.hpp>

#include "RGBController.h"
#include "RazerDevices.h"

#include <QCoreApplication>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-native-plugin-application-contract failed at line "
              << line << '\n';
    return EXIT_FAILURE;
}

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

hyperflux::v7::NativeRoute route(
    hyperflux::DeviceKind kind,
    std::string_view device_id,
    std::string_view endpoint_id,
    std::uint16_t product_id,
    char token_fill,
    std::uint64_t generation)
{
    using namespace hyperflux;
    const v7::ResourceKey resource {
        text<ReceiverId>("receiver-1"),
        number<GenerationId>(generation),
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

std::vector<hyperflux::v7::NativeRoute> routes(std::uint64_t generation)
{
    return {
        route(
            hyperflux::DeviceKind::Mouse,
            "mouse-00cd",
            "endpoint-mouse",
            RAZER_BASILISK_V3_PRO_35K_WIRELESS_PID,
            'a',
            generation),
        route(
            hyperflux::DeviceKind::Keyboard,
            "keyboard-0296",
            "endpoint-keyboard",
            RAZER_DEATHSTALKER_V2_PRO_TKL_WIRELESS_PID,
            'b',
            generation),
    };
}

struct DiscoveryState
{
    std::atomic_size_t enumerate_calls {0};
    std::atomic_uint64_t generation {4};
};

class DiscoveryClient final : public hyperflux::sdk::NativeClientApi
{
public:
    explicit DiscoveryClient(std::shared_ptr<DiscoveryState> state)
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
        const auto sequence = state_->enumerate_calls.fetch_add(1) + 1;
        return hyperflux::sdk::Result<hyperflux::v7::NativeRouteInventory>::success(
            {cursor(sequence), routes(state_->generation.load())});
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
        hyperflux::sdk::NativeEventSubscription subscription) override
    {
        const auto current = state_->enumerate_calls.load();
        return hyperflux::sdk::Result<hyperflux::v7::NativeRouteEventBatch>::success({
            subscription.subscription_id.value_or(text<hyperflux::SubscriptionId>("routes-1")),
            cursor(current),
            {},
            number<hyperflux::SequenceNumber>(current),
            number<hyperflux::SequenceNumber>(current),
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
            "outside native plugin application contract",
            std::nullopt,
        });
    }

    std::shared_ptr<DiscoveryState> state_;
};

struct FeatureClientState
{
    std::mutex mutex;
    std::set<std::string, std::less<>> active;
    std::vector<std::string> observed;
};

class ScriptedExchange final
    : public hyperflux::openrgb::native::NativeFeatureExchange
{
public:
    ScriptedExchange(
        std::string serial,
        std::shared_ptr<FeatureClientState> clients,
        std::string client_id)
        : serial_(std::move(serial)),
          clients_(std::move(clients)),
          client_id_(std::move(client_id))
    {
    }

    ~ScriptedExchange() override
    {
        if(clients_ != nullptr)
        {
            std::lock_guard lock(clients_->mutex);
            clients_->active.erase(client_id_);
        }
    }

    hyperflux::openrgb::native::NativeFeatureExchangeResult exchange(
        const std::array<std::uint8_t,
            hyperflux::openrgb::native::razer_native_report_bytes>& request) override
    {
        auto response = request;
        response[0] = 0x02;
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
        else if(request[6] == 0x0f && request[7] == 0x84)
        {
            response[10] = 0x55;
        }
        response[88] = 0;
        for(std::size_t index = 2; index < 88; ++index)
        {
            response[88] ^= response[index];
        }
        return {
            hyperflux::openrgb::native::NativeExchangeCertainty::MatchedResponse,
            response,
        };
    }

private:
    std::string serial_;
    std::shared_ptr<FeatureClientState> clients_;
    std::string client_id_;
};

struct HostState
{
    std::mutex mutex;
    std::set<RGBController*> controllers;
    std::set<std::uintptr_t> original_identities;
    std::size_t register_calls = 0;
    std::size_t unregister_calls = 0;
    bool valid = true;
};

class Host final : public hyperflux::openrgb::native::NativePluginHost
{
public:
    explicit Host(std::shared_ptr<HostState> state)
        : state_(std::move(state))
    {
    }

    void register_controller(RGBController& controller) noexcept override
    {
        std::lock_guard lock(state_->mutex);
        if(!state_->controllers.insert(&controller).second)
        {
            state_->valid = false;
        }
        state_->original_identities.insert(reinterpret_cast<std::uintptr_t>(&controller));
        ++state_->register_calls;
    }

    void unregister_controller(RGBController& controller) noexcept override
    {
        std::lock_guard lock(state_->mutex);
        if(state_->controllers.erase(&controller) != 1)
        {
            state_->valid = false;
        }
        ++state_->unregister_calls;
    }

    std::vector<RGBControllerInterface*> controllers() const override
    {
        std::lock_guard lock(state_->mutex);
        return {state_->controllers.begin(), state_->controllers.end()};
    }

private:
    std::shared_ptr<HostState> state_;
};

template<typename Predicate>
bool wait_until(Predicate predicate)
{
    for(std::size_t attempt = 0; attempt < 2'000; ++attempt)
    {
        QCoreApplication::processEvents();
        if(predicate())
        {
            return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    QCoreApplication::processEvents();
    return predicate();
}

} // namespace

int main(int argc, char** argv)
{
    QCoreApplication application(argc, argv);
    using namespace hyperflux;
    using namespace hyperflux::openrgb::native;

    auto host_state = std::make_shared<HostState>();
    auto discovery_state = std::make_shared<DiscoveryState>();
    auto feature_clients = std::make_shared<FeatureClientState>();
    std::atomic_size_t notifications {0};
    NativePluginRuntimeFactories factories {
        [discovery_state]() -> sdk::Result<std::unique_ptr<sdk::NativeClientApi>>
        {
            std::unique_ptr<sdk::NativeClientApi> client =
                std::make_unique<DiscoveryClient>(discovery_state);
            return sdk::Result<std::unique_ptr<sdk::NativeClientApi>>::success(
                std::move(client));
        },
        [feature_clients](
            const NativeRouteIdentity& identity,
            ClientId client_id)
            -> sdk::Result<std::unique_ptr<NativeFeatureExchange>>
        {
            const std::string identity_value(client_id.value());
            {
                std::lock_guard lock(feature_clients->mutex);
                if(!feature_clients->active.insert(identity_value).second)
                {
                    return sdk::Result<std::unique_ptr<NativeFeatureExchange>>::failure({
                        sdk::ErrorCode::RuntimeConfiguration,
                        "test bridge rejected a duplicate active client identity",
                        "HFX-TEST-CLIENT-ID",
                    });
                }
                feature_clients->observed.push_back(identity_value);
            }
            const auto serial = identity.device_kind == DeviceKind::Mouse
                                    ? "MOUSE-SERIAL-0001"
                                    : "KEYBOARD-SERIAL-0001";
            std::unique_ptr<NativeFeatureExchange> exchange =
                std::make_unique<ScriptedExchange>(
                    serial,
                    feature_clients,
                    identity_value);
            return sdk::Result<std::unique_ptr<NativeFeatureExchange>>::success(
                std::move(exchange));
        },
        process_openrgb_client_id_factory(OpenRgbClientRole::Feature),
    };
    UpstreamRazerCoordinatorConfig config;
    config.worker = {1, 1, 4, 8};
    config.on_state_changed = [&notifications]
    {
        notifications.fetch_add(1, std::memory_order_relaxed);
    };
    OpenRgbNativePluginApplication plugin(std::move(factories), std::move(config));

    if(!plugin.load(std::make_unique<Host>(host_state))
       || !wait_until([&]
       {
           return plugin.status().coordinator.registered_controllers == 2;
       }))
    {
        return failure(__LINE__);
    }
    {
        std::lock_guard lock(feature_clients->mutex);
        if(feature_clients->active.size() != 2
           || feature_clients->observed.size() != 2
           || !std::all_of(
               feature_clients->active.begin(),
               feature_clients->active.end(),
               [](const auto& identity)
               {
                   return identity.starts_with("openrgb-feature-");
               }))
        {
            return failure(__LINE__);
        }
    }
    std::set<std::uintptr_t> original;
    {
        std::lock_guard lock(host_state->mutex);
        if(!host_state->valid || host_state->controllers.size() != 2
           || notifications.load(std::memory_order_relaxed) == 0)
        {
            return failure(__LINE__);
        }
        original = host_state->original_identities;
    }

    plugin.request_rescan();
    if(!wait_until([&] { return discovery_state->enumerate_calls.load() >= 2; })
       || plugin.controllers().size() != 2)
    {
        return failure(__LINE__);
    }

    discovery_state->generation.store(5);
    plugin.request_rescan();
    // The old pair stays registered while its replacement is prepared, so
    // the registered count alone does not show that the rescan finished.
    if(!wait_until([&]
       {
           if(discovery_state->enumerate_calls.load() < 3
              || plugin.status().coordinator.registered_controllers != 2)
           {
               return false;
           }
           std::lock_guard lock(feature_clients->mutex);
           return feature_clients->observed.size() >= 4
               && feature_clients->active.size() == 2;
       }))
    {
        return failure(__LINE__);
    }
    {
        std::lock_guard lock(feature_clients->mutex);
        const std::set<std::string, std::less<>> observed(
            feature_clients->observed.begin(),
            feature_clients->observed.end());
        if(feature_clients->active.size() != 2
           || feature_clients->observed.size() != 4
           || observed.size() != feature_clients->observed.size())
        {
            return failure(__LINE__);
        }
    }
    std::set<std::uintptr_t> replacement_identities;
    {
        std::lock_guard lock(host_state->mutex);
        for(const auto* controller : host_state->controllers)
        {
            replacement_identities.insert(
                reinterpret_cast<std::uintptr_t>(controller));
        }
        if(replacement_identities == original
           || host_state->register_calls != 4
           || host_state->unregister_calls != 2)
        {
            return failure(__LINE__);
        }
    }

    // OpenRGB 1.0 keeps plugin controllers across rescans and reports
    // completion from its worker threads: reconcile without churn.
    const auto enumerations = discovery_state->enumerate_calls.load();
    std::thread detection_thread([&plugin]
    {
        plugin.detection_completed();
        plugin.detection_completed();
    });
    detection_thread.join();
    if(!wait_until([&]
       {
           return discovery_state->enumerate_calls.load() >= enumerations + 1;
       }))
    {
        return failure(__LINE__);
    }
    {
        std::lock_guard lock(host_state->mutex);
        std::set<std::uintptr_t> current;
        for(const auto* controller : host_state->controllers)
        {
            current.insert(reinterpret_cast<std::uintptr_t>(controller));
        }
        if(!host_state->valid || current != replacement_identities
           || host_state->register_calls != 4 || host_state->unregister_calls != 2
           || plugin.status().coordinator.detection_suspended)
        {
            return failure(__LINE__);
        }
    }

    plugin.unload();
    plugin.unload();
    // Completion after unload is ignored.
    plugin.detection_completed();
    QCoreApplication::processEvents();
    {
        std::lock_guard lock(host_state->mutex);
        if(!host_state->valid || !host_state->controllers.empty()
           || host_state->unregister_calls != 4)
        {
            return failure(__LINE__);
        }
    }
    if(plugin.status().loaded || !plugin.controllers().empty())
    {
        return failure(__LINE__);
    }

    {
        std::lock_guard lock(feature_clients->mutex);
        if(!feature_clients->active.empty())
        {
            return failure(__LINE__);
        }
    }

    auto reloaded_host_state = std::make_shared<HostState>();
    if(!plugin.load(std::make_unique<Host>(reloaded_host_state))
       || !wait_until([&]
       {
           return plugin.status().coordinator.registered_controllers == 2;
       }))
    {
        return failure(__LINE__);
    }
    plugin.unload();
    {
        std::lock_guard lock(feature_clients->mutex);
        const std::set<std::string, std::less<>> observed(
            feature_clients->observed.begin(),
            feature_clients->observed.end());
        if(!feature_clients->active.empty()
           || feature_clients->observed.size() != 6
           || observed.size() != feature_clients->observed.size())
        {
            return failure(__LINE__);
        }
    }

    auto duplicate_host_state = std::make_shared<HostState>();
    auto duplicate_discovery_state = std::make_shared<DiscoveryState>();
    auto duplicate_clients = std::make_shared<FeatureClientState>();
    NativePluginRuntimeFactories duplicate_factories {
        [duplicate_discovery_state]()
            -> sdk::Result<std::unique_ptr<sdk::NativeClientApi>>
        {
            std::unique_ptr<sdk::NativeClientApi> client =
                std::make_unique<DiscoveryClient>(duplicate_discovery_state);
            return sdk::Result<std::unique_ptr<sdk::NativeClientApi>>::success(
                std::move(client));
        },
        [duplicate_clients](const NativeRouteIdentity& identity, ClientId client_id)
            -> sdk::Result<std::unique_ptr<NativeFeatureExchange>>
        {
            const std::string identity_value(client_id.value());
            {
                std::lock_guard lock(duplicate_clients->mutex);
                if(!duplicate_clients->active.insert(identity_value).second)
                {
                    return sdk::Result<std::unique_ptr<NativeFeatureExchange>>::failure({
                        sdk::ErrorCode::ServerRejected,
                        "synthetic bridge rejected duplicate client identity",
                        "HFX-TEST-DUPLICATE-CLIENT",
                    });
                }
                duplicate_clients->observed.push_back(identity_value);
            }
            const auto serial = identity.device_kind == DeviceKind::Mouse
                                    ? "MOUSE-SERIAL-0001"
                                    : "KEYBOARD-SERIAL-0001";
            std::unique_ptr<NativeFeatureExchange> exchange =
                std::make_unique<ScriptedExchange>(
                    serial,
                    duplicate_clients,
                    identity_value);
            return sdk::Result<std::unique_ptr<NativeFeatureExchange>>::success(
                std::move(exchange));
        },
        []() -> sdk::Result<ClientId>
        {
            auto identity = ClientId::from("openrgb-native-route");
            if(!identity.has_value())
            {
                std::abort();
            }
            return sdk::Result<ClientId>::success(*identity);
        },
    };
    OpenRgbNativePluginApplication duplicate_plugin(
        std::move(duplicate_factories),
        {{1, 1, 4, 8}, {}});
    if(!duplicate_plugin.load(std::make_unique<Host>(duplicate_host_state))
       || !wait_until([&]
       {
           return duplicate_plugin.status().coordinator.last_error.has_value();
       }))
    {
        return failure(__LINE__);
    }
    {
        std::lock_guard host_lock(duplicate_host_state->mutex);
        std::lock_guard client_lock(duplicate_clients->mutex);
        if(!duplicate_host_state->controllers.empty()
           || duplicate_host_state->register_calls != 0
           || !duplicate_clients->active.empty()
           || duplicate_clients->observed.size() != 1
           || duplicate_plugin.status().coordinator.controllers != 0)
        {
            return failure(__LINE__);
        }
    }
    duplicate_plugin.unload();
    return EXIT_SUCCESS;
}
