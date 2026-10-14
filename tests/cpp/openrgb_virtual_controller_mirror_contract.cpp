// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_plugin_api_stub.hpp"

#include <hyperflux/openrgb/device_thread_barrier.hpp>
#include <hyperflux/openrgb/virtual_controller_mirror.hpp>

#include "RGBController.h"
#include "RGBController_Virtual.h"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <set>
#include <vector>

namespace
{

using hyperflux::openrgb::native::DeviceThreadBarrier;
using hyperflux::openrgb::native::VirtualControllerMirror;
using hyperflux::openrgb::native::VirtualControllerMirrorOptions;
using hyperflux::openrgb::native::VirtualControllerMirrorSet;

int failure(int line)
{
    std::cerr << "openrgb-virtual-controller-mirror-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

constexpr int kDirect = 0;
constexpr int kStatic = 1;
constexpr unsigned int kLedCount = 3;

struct ModeUpdate
{
    int active_mode;
    RGBColor color;
    unsigned int brightness;
};

/// Plugin-local device: records what reaches its device-thread hooks.
class LocalController final : public RGBController
{
public:
    LocalController()
    {
        name = "Local Mouse";
        vendor = "Razer";
        description = "Mirror contract device";
        location = "HID: hyperflux-next/native/mirror";
        serial = "MIRROR0001";
        type = DEVICE_TYPE_MOUSE;

        mode direct;
        direct.name = "Direct";
        direct.value = 0;
        direct.flags = MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_HAS_BRIGHTNESS;
        direct.brightness_min = 0;
        direct.brightness_max = 100;
        direct.brightness = 100;
        direct.color_mode = MODE_COLORS_PER_LED;
        modes.push_back(direct);

        mode fixed;
        fixed.name = "Static";
        fixed.value = 1;
        fixed.flags = MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_BRIGHTNESS;
        fixed.brightness_min = 0;
        fixed.brightness_max = 100;
        fixed.brightness = 100;
        fixed.colors_min = 1;
        fixed.colors_max = 1;
        fixed.color_mode = MODE_COLORS_MODE_SPECIFIC;
        fixed.colors.resize(1);
        modes.push_back(fixed);

        zone strip;
        strip.name = "Strip";
        strip.type = ZONE_TYPE_LINEAR;
        strip.leds_min = kLedCount;
        strip.leds_max = kLedCount;
        strip.leds_count = kLedCount;
        strip.flags = ZONE_FLAG_MANUALLY_CONFIGURABLE_SIZE;
        zones.push_back(strip);
        for(unsigned int index = 0; index < kLedCount; ++index)
        {
            led entry;
            entry.name = "LED " + std::to_string(index);
            entry.value = index;
            leds.push_back(entry);
        }
        SetupColors();
        flags = CONTROLLER_FLAG_RESET_BEFORE_UPDATE;
        static_cast<void>(barrier_.await(*this));
    }

    LocalController(const LocalController&) = delete;
    LocalController& operator=(const LocalController&) = delete;
    LocalController(LocalController&&) = delete;
    LocalController& operator=(LocalController&&) = delete;

    ~LocalController() override
    {
        Shutdown();
    }

    void DeviceUpdateLEDs() override
    {
        if(barrier_.absorb_leds())
        {
            return;
        }
        {
            std::lock_guard lock(mutex_);
            frames_.push_back(colors);
        }
        changed_.notify_all();
    }

    void DeviceUpdateMode() override
    {
        if(barrier_.absorb_mode())
        {
            return;
        }
        {
            std::lock_guard lock(mutex_);
            const auto& selected = modes[static_cast<std::size_t>(active_mode)];
            mode_updates_.push_back({
                active_mode,
                selected.colors.empty() ? ToRGBColor(0, 0, 0) : selected.colors.front(),
                selected.brightness,
            });
        }
        changed_.notify_all();
    }

    [[nodiscard]] bool wait_for_frames(std::size_t count)
    {
        std::unique_lock lock(mutex_);
        return changed_.wait_for(
            lock, std::chrono::seconds(5), [&] { return frames_.size() >= count; });
    }

    [[nodiscard]] bool wait_for_modes(std::size_t count)
    {
        std::unique_lock lock(mutex_);
        return changed_.wait_for(
            lock, std::chrono::seconds(5), [&] { return mode_updates_.size() >= count; });
    }

    [[nodiscard]] std::vector<std::vector<RGBColor>> frames()
    {
        std::lock_guard lock(mutex_);
        return frames_;
    }

    [[nodiscard]] std::vector<ModeUpdate> mode_updates()
    {
        std::lock_guard lock(mutex_);
        return mode_updates_;
    }

private:
    DeviceThreadBarrier barrier_;
    std::mutex mutex_;
    std::condition_variable changed_;
    std::vector<std::vector<RGBColor>> frames_;
    std::vector<ModeUpdate> mode_updates_;
};

/// Host side of API 5 backed by OpenRGB's own virtual controller.
class PluginApi final : public hyperflux::test::OpenRgbPluginApiStub
{
public:
    PluginApi() = default;
    PluginApi(const PluginApi&) = delete;
    PluginApi& operator=(const PluginApi&) = delete;
    PluginApi(PluginApi&&) = delete;
    PluginApi& operator=(PluginApi&&) = delete;

    ~PluginApi()
    {
        for(auto* controller : created_)
        {
            destroy(controller);
        }
    }

    RGBControllerInterface* CreateVirtualRGBController(RGBController_Setup* setup) override
    {
        auto* controller = new RGBController_Virtual(setup);
        created_.insert(controller);
        ++creates;
        return controller;
    }

    void DeleteVirtualRGBController(RGBControllerInterface* controller) override
    {
        if(registered_.contains(controller) || created_.erase(controller) == 0)
        {
            valid = false;
            return;
        }
        destroy(controller);
        ++deletes;
    }

    void RegisterVirtualRGBController(RGBControllerInterface* controller) override
    {
        if(!created_.contains(controller) || !registered_.insert(controller).second)
        {
            valid = false;
        }
    }

    void UnregisterVirtualRGBController(RGBControllerInterface* controller) override
    {
        if(registered_.erase(controller) == 0)
        {
            valid = false;
        }
    }

    std::vector<RGBControllerInterface*> GetRGBControllers() override
    {
        return {registered_.begin(), registered_.end()};
    }

    [[nodiscard]] bool is_registered(RGBControllerInterface* controller) const
    {
        return registered_.contains(controller);
    }

    unsigned int creates = 0;
    unsigned int deletes = 0;
    bool valid = true;

private:
    static void destroy(RGBControllerInterface* controller)
    {
        auto* owned = static_cast<RGBController_Virtual*>(controller);
        owned->Shutdown();
        delete owned;
    }

    std::set<RGBControllerInterface*> created_;
    std::set<RGBControllerInterface*> registered_;
};

void set_all_colors(RGBControllerInterface& controller, RGBColor color)
{
    for(unsigned int led = 0; led < controller.GetLEDCount(); ++led)
    {
        controller.SetColor(led, color);
    }
}

bool all_colors_are(const std::vector<RGBColor>& frame, RGBColor color)
{
    return frame.size() == kLedCount
        && std::all_of(frame.begin(), frame.end(), [color](RGBColor entry) { return entry == color; });
}

} // namespace

int main()
{
    PluginApi api;
    LocalController local;

    auto mirror = VirtualControllerMirror::create(
        api, local, VirtualControllerMirrorOptions {.skip_initial_black_frame = true});
    if(mirror == nullptr || mirror->host_controller() == nullptr || api.creates != 1
       || !api.is_registered(mirror->host_controller()))
    {
        return failure(__LINE__);
    }
    auto& host = *mirror->host_controller();
    if(host.GetName() != "Local Mouse" || host.GetSerial() != "MIRROR0001"
       || host.GetLocation() != "HID: hyperflux-next/native/mirror"
       || host.GetDeviceType() != DEVICE_TYPE_MOUSE || host.GetModeCount() != 2
       || host.GetModeName(kStatic) != "Static" || host.GetZoneCount() != 1
       || host.GetZoneLEDsCount(0) != kLedCount || host.GetLEDCount() != kLedCount
       || host.GetLEDName(2) != "LED 2"
       || (host.GetZoneFlags(0) & ZONE_FLAG_MANUALLY_CONFIGURABLE_SIZE) != 0)
    {
        return failure(__LINE__);
    }

    // A mode change issued right after registration must reach the device:
    // the mirror confirmed the host device thread before registering it.
    host.SetModeColor(kStatic, 0, ToRGBColor(10, 20, 30));
    host.SetModeBrightness(kStatic, 40);
    host.SetActiveMode(kStatic);
    if(!local.wait_for_modes(1))
    {
        return failure(__LINE__);
    }
    auto modes = local.mode_updates();
    if(modes.size() != 1 || modes.front().active_mode != kStatic
       || modes.front().color != ToRGBColor(10, 20, 30) || modes.front().brightness != 40
       || local.GetActiveMode() != kStatic)
    {
        return failure(__LINE__);
    }

    host.SetActiveMode(kDirect);
    if(!local.wait_for_modes(2) || local.mode_updates().back().active_mode != kDirect)
    {
        return failure(__LINE__);
    }

    // OpenRGB cannot tell its zeroed initial buffer from an intentional black
    // frame, so the mirror holds black frames until a nonblack one arrives.
    // UpdateSingleLED runs the mirror synchronously on this thread.
    host.UpdateSingleLED(0);
    set_all_colors(host, ToRGBColor(0x20, 0, 0));
    host.UpdateSingleLED(1);
    if(!local.wait_for_frames(1))
    {
        return failure(__LINE__);
    }
    auto frames = local.frames();
    if(!all_colors_are(frames.front(), ToRGBColor(0x20, 0, 0)))
    {
        return failure(__LINE__);
    }

    // Once initialized, black is an ordinary frame.
    set_all_colors(host, ToRGBColor(0, 0, 0));
    host.UpdateLEDs();
    if(!local.wait_for_frames(frames.size() + 1)
       || !all_colors_are(local.frames().back(), ToRGBColor(0, 0, 0)))
    {
        return failure(__LINE__);
    }

    // Without the guard the first black frame is forwarded.
    {
        LocalController plain_local;
        auto plain = VirtualControllerMirror::create(api, plain_local);
        if(plain == nullptr)
        {
            return failure(__LINE__);
        }
        plain->host_controller()->UpdateLEDs();
        if(!plain_local.wait_for_frames(1))
        {
            return failure(__LINE__);
        }
        if(!all_colors_are(plain_local.frames().front(), ToRGBColor(0, 0, 0)))
        {
            return failure(__LINE__);
        }
    }
    if(api.creates != 2 || api.deletes != 1 || !api.valid)
    {
        return failure(__LINE__);
    }

    mirror.reset();
    if(api.deletes != 2 || !api.valid || !api.GetRGBControllers().empty())
    {
        return failure(__LINE__);
    }

    {
        VirtualControllerMirrorSet set(api);
        set.add(local);
        set.add(local);
        if(api.creates != 3 || api.GetRGBControllers().size() != 1)
        {
            return failure(__LINE__);
        }
        set.remove(local);
        if(api.deletes != 3 || !api.GetRGBControllers().empty())
        {
            return failure(__LINE__);
        }
        set.add(local);
    }
    if(api.creates != 4 || api.deletes != 4 || !api.valid)
    {
        return failure(__LINE__);
    }
    return EXIT_SUCCESS;
}
