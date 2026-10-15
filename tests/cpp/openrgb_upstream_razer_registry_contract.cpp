// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/upstream_razer_registry.hpp>

#include "RGBController.h"
#include "RGBController_Razer.h"
#include "RazerDevices.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <map>
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

hyperflux::v7::NativeRoute route(
    hyperflux::DeviceKind kind,
    std::string_view device_id,
    std::string_view endpoint_id,
    std::uint16_t product_id,
    std::uint64_t generation = 4,
    char token_fill = 'a')
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

using NativeRequest = std::array<std::uint8_t,
    hyperflux::openrgb::native::razer_native_report_bytes>;

class RequestTrace final
{
public:
    void append(const NativeRequest& request)
    {
        std::lock_guard lock(mutex_);
        requests_.push_back(request);
    }

    [[nodiscard]] std::vector<NativeRequest> snapshot() const
    {
        std::lock_guard lock(mutex_);
        return requests_;
    }

    [[nodiscard]] std::size_t size() const
    {
        std::lock_guard lock(mutex_);
        return requests_.size();
    }

    template<typename Predicate>
    [[nodiscard]] bool wait_for(Predicate predicate) const
    {
        for(std::size_t attempt = 0; attempt < 500; ++attempt)
        {
            const auto requests = snapshot();
            if(predicate(requests))
            {
                return true;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        return false;
    }

private:
    mutable std::mutex mutex_;
    std::vector<NativeRequest> requests_;
};

struct ExchangeScript
{
    std::uint8_t layout = RAZER_KEYBOARD_LAYOUT_UK;
    std::uint8_t variant = RAZER_KEYBOARD_VARIANT_BLACK;
};

std::uint8_t report_crc(const NativeRequest& report)
{
    std::uint8_t value = 0;
    for(std::size_t index = 2; index < 88; ++index)
    {
        value ^= report[index];
    }
    return value;
}

class ScriptedExchange final
    : public hyperflux::openrgb::native::NativeFeatureExchange
{
public:
    ScriptedExchange(
        std::string serial,
        std::shared_ptr<RequestTrace> requests,
        ExchangeScript script = {})
        : serial_(std::move(serial)),
          requests_(std::move(requests)),
          script_(script)
    {
    }

    hyperflux::openrgb::native::NativeFeatureExchangeResult exchange(
        const NativeRequest& request) override
    {
        requests_->append(request);
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
            if(request[5] == 0x02 && request[8] == 0x00 && request[9] == 0x00)
            {
                response[5] = 0x02;
                response[8] = script_.layout;
                response[9] = script_.variant;
            }
        }
        response[88] = report_crc(response);
        return {
            hyperflux::openrgb::native::NativeExchangeCertainty::MatchedResponse,
            response,
        };
    }

private:
    std::string serial_;
    std::shared_ptr<RequestTrace> requests_;
    ExchangeScript script_;
};

class RecordingHost final : public hyperflux::openrgb::native::UpstreamControllerHost
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

    std::vector<RGBController*> registered;
};

class TestController final : public RGBController
{
public:
    TestController(std::string device_vendor, std::string device_location, std::string device_serial)
    {
        vendor = std::move(device_vendor);
        location = std::move(device_location);
        serial = std::move(device_serial);
    }
};

// OpenRGB 1.0 offers no getter for a mode's protocol value; read the
// controller's mode table directly for contract inspection.
struct ControllerState : RGBController
{
    static const std::vector<mode>& modes_of(const RGBController& controller)
    {
        return controller.*(&ControllerState::modes);
    }
};

std::size_t mode_index(const RGBController& controller, int value)
{
    const auto& modes = ControllerState::modes_of(controller);
    const auto found = std::find_if(
        modes.begin(),
        modes.end(),
        [value](const mode& candidate) { return candidate.value == value; });
    if(found == modes.end())
    {
        throw std::runtime_error("test controller mode is absent");
    }
    return static_cast<std::size_t>(std::distance(modes.begin(), found));
}

bool all_colors_are(RGBController& controller, RGBColor color)
{
    for(unsigned int led = 0; led < controller.GetLEDCount(); ++led)
    {
        if(controller.GetColor(led) != color)
        {
            return false;
        }
    }
    return true;
}

void set_all_colors(RGBController& controller, RGBColor color)
{
    for(unsigned int led = 0; led < controller.GetLEDCount(); ++led)
    {
        controller.SetColor(led, color);
    }
}

bool is_request(
    const NativeRequest& request,
    std::uint8_t command_class,
    std::uint8_t command_id)
{
    return request[6] == command_class && request[7] == command_id;
}

std::size_t request_count(
    const std::vector<NativeRequest>& requests,
    std::uint8_t command_class,
    std::uint8_t command_id)
{
    return static_cast<std::size_t>(std::count_if(
        requests.begin(),
        requests.end(),
        [command_class, command_id](const auto& request)
        {
            return is_request(request, command_class, command_id);
        }));
}

} // namespace

int main()
{
    using namespace hyperflux;
    using namespace hyperflux::openrgb::native;

    std::set<std::string, std::less<>> observed_feature_client_ids;
    std::map<std::string, std::shared_ptr<RequestTrace>, std::less<>>
        request_traces;
    auto exchange_factory = [&observed_feature_client_ids, &request_traces](
                                const NativeRouteIdentity& identity,
                                ClientId client_id)
        -> sdk::Result<std::unique_ptr<NativeFeatureExchange>>
    {
        if(!observed_feature_client_ids.insert(std::string(client_id.value())).second)
        {
            return sdk::Result<std::unique_ptr<NativeFeatureExchange>>::failure({
                sdk::ErrorCode::RuntimeConfiguration,
                "test bridge rejected a duplicate active client identity",
                "HFX-TEST-CLIENT-ID",
            });
        }
        const auto serial = identity.device_kind == DeviceKind::Keyboard
                                ? identity.product_id.value() == 0x02d5
                                      ? "FRENCH-SERIAL-00001"
                                  : identity.product_id.value() == 0x02d2
                                      ? "NO-GETTER-SERIAL-01"
                                      : "KEYBOARD-SERIAL-0001"
                            : identity.product_id.value() == RAZER_BASILISK_V3_PRO_WIRELESS_PID
                                ? "ORIGINAL-SERIAL-01"
                                : "MOUSE-SERIAL-0001";
        auto requests = std::make_shared<RequestTrace>();
        request_traces.emplace(serial, requests);
        ExchangeScript script;
        if(identity.product_id.value() == 0x02d5)
        {
            script.layout = RAZER_KEYBOARD_LAYOUT_FRENCH;
            script.variant = RAZER_KEYBOARD_VARIANT_QUARTZ;
        }
        std::unique_ptr<NativeFeatureExchange> exchange =
            std::make_unique<ScriptedExchange>(serial, std::move(requests), script);
        return sdk::Result<std::unique_ptr<NativeFeatureExchange>>::success(
            std::move(exchange));
    };
    UpstreamRazerControllerFactory factory(exchange_factory);
    RecordingHost host;
    UpstreamRazerControllerRegistry registry(host, factory);

    const auto mouse = route(
        DeviceKind::Mouse,
        "mouse-00cd",
        "endpoint-mouse",
        RAZER_BASILISK_V3_PRO_35K_WIRELESS_PID);
    const auto original_basilisk = route(
        DeviceKind::Mouse,
        "mouse-00ab",
        "endpoint-original-mouse",
        RAZER_BASILISK_V3_PRO_WIRELESS_PID,
        4,
        'c');
    const auto keyboard = route(
        DeviceKind::Keyboard,
        "keyboard-0296",
        "endpoint-keyboard",
        RAZER_DEATHSTALKER_V2_PRO_TKL_WIRELESS_PID,
        4,
        'b');
    const auto openrazer_only = route(
        DeviceKind::Keyboard,
        "keyboard-02ba",
        "endpoint-openrazer-only",
        0x02ba,
        4,
        'd');
    const auto openrgb_only = route(
        DeviceKind::Keyboard,
        "keyboard-02d2",
        "endpoint-openrgb-only",
        0x02d2,
        4,
        'e');
    const auto nonofficial_upstream = route(
        DeviceKind::Keyboard,
        "keyboard-02b4",
        "endpoint-nonofficial",
        0x02b4,
        4,
        'g');
    const auto receiver_transport = route(
        DeviceKind::Mouse,
        "receiver-00cf",
        "endpoint-receiver",
        0x00cf,
        4,
        'f');
    if(is_eligible_upstream_razer_route(openrazer_only)
       || !is_eligible_upstream_razer_route(openrgb_only)
       || is_eligible_upstream_razer_route(nonofficial_upstream)
       || is_eligible_upstream_razer_route(receiver_transport))
    {
        return 15;
    }
    auto initial = registry.apply_routes({keyboard, mouse, original_basilisk});
    if(!initial || initial.value().added != 3 || initial.value().registered != 3
       || registry.size() != 3 || host.registered.size() != 3
       || observed_feature_client_ids.size() != 3
       || !std::all_of(
           observed_feature_client_ids.begin(),
           observed_feature_client_ids.end(),
           [](const auto& identity)
           {
               return identity.starts_with("openrgb-feature-");
           }))
    {
        return 1;
    }

    const auto mouse_controller = std::find_if(
        host.registered.begin(),
        host.registered.end(),
        [](RGBController* controller)
        {
            return canonical_queried_serial(controller->GetSerial()) == "MOUSE-SERIAL-0001";
        });
    if(mouse_controller == host.registered.end()
       || (*mouse_controller)->GetLEDCount() == 0
       || !all_colors_are(**mouse_controller, ToRGBColor(0, 0, 0)))
    {
        return 17;
    }
    auto mouse_requests = request_traces.at("MOUSE-SERIAL-0001");
    const auto initial_mouse_requests = mouse_requests->snapshot();
    const auto mouse_brightness_modes = static_cast<std::size_t>(std::count_if(
        ControllerState::modes_of(**mouse_controller).begin(),
        ControllerState::modes_of(**mouse_controller).end(),
        [](const mode& candidate)
        {
            return (candidate.flags & MODE_FLAG_HAS_BRIGHTNESS) != 0;
        }));
    if(request_count(initial_mouse_requests, 0x0f, 0x84) != 0
       || mouse_brightness_modes == 0
       || !std::all_of(
           ControllerState::modes_of(**mouse_controller).begin(),
           ControllerState::modes_of(**mouse_controller).end(),
           [](const mode& candidate)
           {
               return (candidate.flags & MODE_FLAG_HAS_BRIGHTNESS) == 0
                   || candidate.brightness == candidate.brightness_max;
           }))
    {
        return 22;
    }

    const auto direct_mode = mode_index(**mouse_controller, RAZER_MODE_DIRECT);
    (*mouse_controller)->SetModeBrightness(static_cast<unsigned int>(direct_mode), 60);
    const auto before_brightness_callback = mouse_requests->snapshot();
    const auto custom_frames_before = request_count(
        before_brightness_callback, 0x0f, 0x03);
    (*mouse_controller)->SetActiveMode(static_cast<int>(direct_mode));
    if(!mouse_requests->wait_for(
           [before = before_brightness_callback.size()](const auto& requests)
           {
               return std::any_of(
                   requests.begin() + static_cast<std::ptrdiff_t>(before),
                   requests.end(),
                   [](const auto& request)
                   {
                       return is_request(request, 0x0f, 0x04)
                           && request[8] == 0x00 && request[9] == 0x00
                           && request[10] == 60;
                   });
           }))
    {
        return 23;
    }
    if(request_count(mouse_requests->snapshot(), 0x0f, 0x03) != custom_frames_before)
    {
        return 24;
    }

    // The plugin-local controller forwards every Direct frame. The initial
    // all-black guard lives in the API 5 mirror (virtual-controller-mirror
    // contract).
    set_all_colors(**mouse_controller, ToRGBColor(0x20, 0x00, 0x00));
    const auto custom_modes_before = static_cast<std::size_t>(std::count_if(
        before_brightness_callback.begin(),
        before_brightness_callback.end(),
        [](const auto& request)
        {
            return is_request(request, 0x0f, 0x02)
                && request[5] == 0x0c && request[10] == 0x08;
        }));
    (*mouse_controller)->UpdateLEDs();
    if(!mouse_requests->wait_for(
           [custom_frames_before, custom_modes_before](const auto& requests)
           {
               return request_count(requests, 0x0f, 0x03) > custom_frames_before
                   && static_cast<std::size_t>(std::count_if(
                          requests.begin(),
                          requests.end(),
                          [](const auto& request)
                          {
                              return is_request(request, 0x0f, 0x02)
                                  && request[5] == 0x0c && request[10] == 0x08;
                          })) > custom_modes_before;
           }))
    {
        return 19;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
    const auto after_initialized_direct = request_count(
        mouse_requests->snapshot(), 0x0f, 0x03);
    set_all_colors(**mouse_controller, ToRGBColor(0x00, 0x00, 0x00));
    (*mouse_controller)->UpdateLEDs();
    if(!mouse_requests->wait_for(
           [after_initialized_direct](const auto& requests)
           {
               return request_count(requests, 0x0f, 0x03)
                   > after_initialized_direct;
           }))
    {
        return 20;
    }

    const auto static_mode = mode_index(**mouse_controller, RAZER_MODE_STATIC);
    (*mouse_controller)->SetModeColorsCount(static_cast<unsigned int>(static_mode), 1);
    (*mouse_controller)->SetModeColor(
        static_cast<unsigned int>(static_mode), 0, ToRGBColor(0, 0, 0));
    (*mouse_controller)->SetModeBrightness(static_cast<unsigned int>(static_mode), 80);
    const auto before_static = mouse_requests->size();
    (*mouse_controller)->SetActiveMode(static_cast<int>(static_mode));
    if(!mouse_requests->wait_for(
           [before_static](const auto& requests)
           {
               return std::any_of(
                   requests.begin() + static_cast<std::ptrdiff_t>(before_static),
                   requests.end(),
                   [](const auto& request)
                   {
                       return is_request(request, 0x0f, 0x02)
                           && request[5] == 0x09 && request[10] == 0x01
                           && request[14] == 0x00 && request[15] == 0x00
                           && request[16] == 0x00;
                   });
           }))
    {
        return 25;
    }

    const auto off_mode = mode_index(**mouse_controller, RAZER_MODE_OFF);
    const auto before_off = mouse_requests->size();
    (*mouse_controller)->SetActiveMode(static_cast<int>(off_mode));
    if(!mouse_requests->wait_for(
           [before_off](const auto& requests)
           {
               return std::any_of(
                   requests.begin() + static_cast<std::ptrdiff_t>(before_off),
                   requests.end(),
                   [](const auto& request)
                   {
                       return is_request(request, 0x0f, 0x02)
                           && request[5] == 0x06 && request[10] == 0x00;
                   });
           }))
    {
        return 26;
    }

    const auto keyboard_controller = std::find_if(
        host.registered.begin(),
        host.registered.end(),
        [](RGBController* controller)
        {
            return canonical_queried_serial(controller->GetSerial()) == "KEYBOARD-SERIAL-0001";
        });
    const auto keyboard_requests = request_traces.at("KEYBOARD-SERIAL-0001")->snapshot();
    const auto keyboard_info = std::find_if(
        keyboard_requests.begin(),
        keyboard_requests.end(),
        [](const auto& request)
        {
            return request[6] == 0x00
                && request[7] == RAZER_COMMAND_ID_GET_KEYBOARD_INFO;
        });
    if(keyboard_controller == host.registered.end())
    {
        return 16;
    }
    if((*keyboard_controller)->GetDescription() != "Razer Device, UK (ISO), Black")
    {
        return 34;
    }
    if(keyboard_info == keyboard_requests.end())
    {
        return 35;
    }
    if((*keyboard_info)[5] != 0x02
       || (*keyboard_info)[8] != 0x00 || (*keyboard_info)[9] != 0x00)
    {
        return 36;
    }
    const auto keyboard_brightness_modes = static_cast<std::size_t>(std::count_if(
        ControllerState::modes_of(**keyboard_controller).begin(),
        ControllerState::modes_of(**keyboard_controller).end(),
        [](const mode& candidate)
        {
            return (candidate.flags & MODE_FLAG_HAS_BRIGHTNESS) != 0;
        }));
    if(request_count(keyboard_requests, 0x0f, 0x84) != 0
       || keyboard_brightness_modes == 0
       || !std::all_of(
           ControllerState::modes_of(**keyboard_controller).begin(),
           ControllerState::modes_of(**keyboard_controller).end(),
           [](const mode& candidate)
           {
               return (candidate.flags & MODE_FLAG_HAS_BRIGHTNESS) == 0
                   || candidate.brightness == candidate.brightness_max;
           }))
    {
        return 38;
    }
    const auto keyboard_map = (*keyboard_controller)->GetZoneMatrixMap(0).map;
    if(keyboard_map.size() != 6 * 17
       || (*keyboard_controller)->GetLEDName(4 * 17 + 1) != "Key: \\ (ISO)"
       || !(*keyboard_controller)->GetLEDName(2 * 17 + 13).empty()
       || keyboard_map[4 * 17 + 1] == std::numeric_limits<unsigned int>::max()
       || keyboard_map[2 * 17 + 13] != std::numeric_limits<unsigned int>::max())
    {
        return 21;
    }

    const auto french_keyboard = route(
        DeviceKind::Keyboard,
        "keyboard-02d5-fr",
        "endpoint-keyboard-fr",
        0x02d5,
        4,
        'h');
    auto french_instance = factory.create(french_keyboard);
    if(!french_instance
       || french_instance.value()->controller().GetDescription()
              != "Razer Device, French (ISO), Quartz")
    {
        return 27;
    }
    const auto french_requests = request_traces.at("FRENCH-SERIAL-00001")->snapshot();
    const auto french_info = std::find_if(
        french_requests.begin(),
        french_requests.end(),
        [](const auto& request)
        {
            return is_request(request, 0x00, RAZER_COMMAND_ID_GET_KEYBOARD_INFO);
        });
    if(french_info == french_requests.end()
       || (*french_info)[8] != 0x00 || (*french_info)[9] != 0x00)
    {
        return 28;
    }

    auto no_getter_instance = factory.create(openrgb_only);
    const auto no_getter_requests = request_traces.at("NO-GETTER-SERIAL-01")->snapshot();
    if(!no_getter_instance
       || request_count(no_getter_requests, 0x0f, 0x84) != 0
       || std::none_of(
           ControllerState::modes_of(no_getter_instance.value()->controller()).begin(),
           ControllerState::modes_of(no_getter_instance.value()->controller()).end(),
           [](const mode& candidate)
           {
               return (candidate.flags & MODE_FLAG_HAS_BRIGHTNESS) != 0;
           }))
    {
        return 29;
    }

    auto unsupported_route = mouse;
    unsupported_route.product_id = number<ProductId>(0xffff);
    if(factory.create(unsupported_route))
    {
        return 31;
    }

    const auto original_controller = std::find_if(
        host.registered.begin(),
        host.registered.end(),
        [](RGBController* controller)
        {
            return canonical_queried_serial(controller->GetSerial()) == "ORIGINAL-SERIAL-01";
        });
    if(original_controller == host.registered.end()
       || (*original_controller)->GetLEDCount() != 13
       || (*original_controller)->GetZoneCount() != 3)
    {
        return 13;
    }

    const auto original_removed = registry.apply_routes({keyboard, mouse});
    if(!original_removed || original_removed.value().removed != 1 || registry.size() != 2
       || host.registered.size() != 2)
    {
        return 14;
    }

    const auto mouse_identity = registry.controller_identity(
        "receiver-1/mouse-00cd/endpoint-mouse");
    if(mouse_identity == 0
       || registry.serials()
              != std::vector<std::string> {
                  "KEYBOARD-SERIAL-0001  ",
                  "MOUSE-SERIAL-0001     ",
              })
    {
        return 2;
    }

    const auto unrelated_direct = registry.apply_direct_serials({
        "",
        "                      ",
        "DIFFERENT-SERIAL-SAME-PID-NAME-KIND",
    });
    if(unrelated_direct.newly_suppressed != 0 || registry.registered_count() != 2)
    {
        return 3;
    }

    TestController direct_mouse_controller("Razer", "HID: /dev/hidraw-direct-mouse", "MOUSE-SERIAL-0001     ");
    TestController network_controller("Razer", "Network: 127.0.0.1", "MOUSE-SERIAL-0001");
    TestController unrelated_controller("Other", "HID: /dev/hidraw-other", "MOUSE-SERIAL-0001");
    TestController blank_serial_controller("Razer", "HID: /dev/hidraw-blank", "                      ");
    const auto direct_serials = collect_direct_razer_serials({
        &direct_mouse_controller,
        &network_controller,
        &unrelated_controller,
        &blank_serial_controller,
        nullptr,
    });
    if(direct_serials != std::vector<std::string> {"MOUSE-SERIAL-0001"})
    {
        return 12;
    }

    const auto direct_mouse = registry.apply_direct_serials(direct_serials);
    if(direct_mouse.newly_suppressed != 1 || registry.suppressed_count() != 1
       || registry.registered_count() != 1 || host.registered.size() != 1
       || canonical_queried_serial(host.registered.front()->GetSerial())
              != "KEYBOARD-SERIAL-0001")
    {
        return 4;
    }

    const auto repeat_direct = registry.apply_direct_serials({"MOUSE-SERIAL-0001"});
    if(repeat_direct.newly_suppressed != 0 || repeat_direct.newly_restored != 0
       || registry.registered_count() != 1)
    {
        return 5;
    }

    const auto direct_removed = registry.apply_direct_serials({});
    if(direct_removed.newly_restored != 1 || registry.registered_count() != 2
       || registry.controller_identity("receiver-1/mouse-00cd/endpoint-mouse")
              != mouse_identity)
    {
        return 6;
    }

    auto next_mouse = mouse;
    next_mouse.generation_id = number<GenerationId>(5);
    next_mouse.route_token = text<NativeRouteToken>(std::string(64, 'c'));
    next_mouse.resource->generation_id = next_mouse.generation_id;
    auto replacement = registry.apply_routes({keyboard, next_mouse});
    if(!replacement || replacement.value().replaced != 1
       || replacement.value().retained != 1
       || registry.controller_identity("receiver-1/mouse-00cd/endpoint-mouse")
              == mouse_identity)
    {
        return 9;
    }

    auto candidate = keyboard;
    candidate.access = NativeRouteAccess::Candidate;
    auto unavailable = mouse;
    unavailable.route_state = NativeRouteState::TemporarilyUnavailable;
    auto filtered = registry.apply_routes({candidate, unavailable});
    if(!filtered || filtered.value().removed != 2 || registry.size() != 0
       || !host.registered.empty())
    {
        return 10;
    }

    auto duplicate = registry.apply_routes({mouse, mouse});
    if(duplicate || registry.size() != 0)
    {
        return 11;
    }

    return 0;
}
