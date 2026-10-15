// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/virtual_controller_mirror.hpp>

#include "OpenRGBPluginInterface.h"
#include "RGBController.h"

#include <algorithm>
#include <mutex>
#include <new>
#include <string>
#include <utility>
#include <vector>

namespace hyperflux::openrgb::native
{
namespace
{

/// UpdateZoneMode runs the zone-mode hook and then signals a mode update on
/// the same thread. The hook records its mirror here so that signal does not
/// also reapply the whole-device mode.
thread_local const VirtualControllerMirror* zone_mode_signal = nullptr;

std::string json_string(const nlohmann::json& description, const char* key)
{
    const auto found = description.find(key);
    return found != description.end() && found->is_string()
               ? found->get<std::string>()
               : std::string {};
}

template <typename Value>
Value json_number(const nlohmann::json& description, const char* key, Value fallback)
{
    const auto found = description.find(key);
    return found != description.end() && found->is_number()
               ? found->get<Value>()
               : fallback;
}

template <typename Item, typename Convert>
std::vector<Item> json_items(const nlohmann::json& description, const char* key, Convert convert)
{
    std::vector<Item> items;
    const auto found = description.find(key);
    if(found == description.end() || !found->is_array())
    {
        return items;
    }
    items.reserve(found->size());
    for(const auto& item : *found)
    {
        items.push_back(convert(item));
    }
    return items;
}

RGBController_Setup setup_from_local(RGBController& local)
{
    const nlohmann::json description = RGBController::GetDeviceDescriptionJSON(&local);

    RGBController_Setup setup {};
    setup.description = json_string(description, "description");
    setup.location = json_string(description, "location");
    setup.name = json_string(description, "name");
    setup.serial = json_string(description, "serial");
    setup.vendor = json_string(description, "vendor");
    setup.version = json_string(description, "version");
    // Device-specific configuration stays empty: mirrored controllers define
    // none, and the mirror installs no configuration callbacks.
    setup.active_mode = json_number<int>(description, "active_mode", 0);
    setup.flags = json_number<controller_flags>(description, "flags", 0U);
    setup.type = json_number<device_type>(description, "type", DEVICE_TYPE_UNKNOWN);
    setup.modes = json_items<mode>(description, "modes",
        [](const nlohmann::json& item) { return RGBController::SetModeDescriptionJSON(item); });
    setup.zones = json_items<zone>(description, "zones",
        [](const nlohmann::json& item)
        {
            zone result = RGBController::SetZoneDescriptionJSON(item);
            // The local controller owns the zone layout; resizing only the
            // host copy would desynchronize LED indices.
            result.flags &= ~static_cast<zone_flags>(ZONE_FLAGS_MANUALLY_CONFIGURABLE);
            return result;
        });
    setup.leds = json_items<led>(description, "leds",
        [](const nlohmann::json& item) { return RGBController::SetLEDDescriptionJSON(item); });
    return setup;
}

/// Host controllers retired by an unloading set, keyed by the plugin API
/// that created them. OpenRGB keeps one API object per plugin entry, so a
/// reload of the same plugin presents the same key.
class RetiredHostControllers
{
public:
    void park(OpenRGBPluginAPIInterface* api, RGBControllerInterface* controller) noexcept
    {
        const std::lock_guard lock(mutex_);
        try
        {
            entries_.push_back({ api, controller });
        }
        catch(...)
        {
            // Leaked: freeing it here is what this store exists to avoid.
        }
    }

    [[nodiscard]] std::vector<RGBControllerInterface*> take(OpenRGBPluginAPIInterface* api) noexcept
    {
        std::vector<RGBControllerInterface*> taken;
        const std::lock_guard lock(mutex_);
        try
        {
            for(const Entry& entry : entries_)
            {
                if(entry.api == api)
                {
                    taken.push_back(entry.controller);
                }
            }
        }
        catch(...)
        {
            // Nothing is released; the entries stay parked.
            return {};
        }
        std::erase_if(entries_, [api](const Entry& entry) { return entry.api == api; });
        return taken;
    }

private:
    struct Entry
    {
        OpenRGBPluginAPIInterface* api;
        RGBControllerInterface* controller;
    };

    std::mutex mutex_;
    std::vector<Entry> entries_;
};

/// Never destroyed: parked controllers must stay reachable until the plugin
/// is loaded again, and are deliberately not freed at exit.
RetiredHostControllers* retired_host_controllers() noexcept
{
    static auto* const store = new(std::nothrow) RetiredHostControllers;
    return store;
}

void neutralize_hooks(RGBController_Setup& setup) noexcept
{
    setup.object_ptr = nullptr;
    setup.DeviceConfigureZone = nullptr;
    setup.DeviceUpdateLEDs = nullptr;
    setup.DeviceUpdateZoneLEDs = nullptr;
    setup.DeviceUpdateSingleLED = nullptr;
    setup.DeviceUpdateMode = nullptr;
    setup.DeviceUpdateZoneMode = nullptr;
    setup.DeviceSaveMode = nullptr;
    setup.DeviceUpdateDeviceSpecificConfiguration = nullptr;
    setup.DeviceUpdateDeviceSpecificZoneConfiguration = nullptr;
}

} // namespace

VirtualControllerMirror::VirtualControllerMirror(
    OpenRGBPluginAPIInterface& api,
    RGBController& local,
    VirtualControllerMirrorOptions options) noexcept
    : api_(&api),
      local_(&local),
      skip_initial_black_frame_(options.skip_initial_black_frame)
{
}

std::unique_ptr<VirtualControllerMirror> VirtualControllerMirror::create(
    OpenRGBPluginAPIInterface& api,
    RGBController& local,
    VirtualControllerMirrorOptions options) noexcept
{
    try
    {
        std::unique_ptr<VirtualControllerMirror> mirror(
            new VirtualControllerMirror(api, local, options));
        RGBController_Setup setup = setup_from_local(local);
        setup.object_ptr = mirror.get();
        setup.DeviceConfigureZone = nullptr;
        // LED and mode updates are forwarded from the update callback.
        setup.DeviceUpdateLEDs = nullptr;
        setup.DeviceUpdateZoneLEDs = nullptr;
        setup.DeviceUpdateSingleLED = nullptr;
        setup.DeviceUpdateMode = nullptr;
        setup.DeviceSaveMode = &VirtualControllerMirror::save_mode;
        setup.DeviceUpdateZoneMode = &VirtualControllerMirror::update_zone_mode;
        setup.DeviceUpdateDeviceSpecificConfiguration = nullptr;
        setup.DeviceUpdateDeviceSpecificZoneConfiguration = nullptr;

        mirror->host_controller_ = api.CreateVirtualRGBController(&setup);
        if(mirror->host_controller_ == nullptr)
        {
            return nullptr;
        }
        // Registration publishes the controller to profiles and the GUI, so
        // forwarding must already be in place.
        RGBControllerInterface* host = mirror->host_controller_;
        host->RegisterUpdateCallback(&VirtualControllerMirror::host_updated, mirror.get());
        mirror->callback_registered_ = true;
        api.RegisterVirtualRGBController(host);
        return mirror;
    }
    catch(...)
    {
        return nullptr;
    }
}

VirtualControllerMirror::~VirtualControllerMirror()
{
    if(host_controller_ == nullptr)
    {
        return;
    }
    if(callback_registered_)
    {
        api_->UnregisterVirtualRGBController(host_controller_);
        // OpenRGB holds the update mutex while it runs update callbacks, so
        // once this returns no forward is running or can start.
        host_controller_->UnregisterUpdateCallback(this);
    }
    // Deleting the host controller joins its device thread, so no hook can
    // reach this mirror or the local controller afterwards.
    api_->DeleteVirtualRGBController(host_controller_);
    host_controller_ = nullptr;
}

RGBControllerInterface* VirtualControllerMirror::host_controller() const noexcept
{
    return host_controller_;
}

RGBControllerInterface* VirtualControllerMirror::retire() noexcept
{
    RGBControllerInterface* const host = std::exchange(host_controller_, nullptr);
    if(host == nullptr)
    {
        return nullptr;
    }
    if(!callback_registered_)
    {
        // Never published, so nothing in OpenRGB can reference it.
        api_->DeleteVirtualRGBController(host);
        return nullptr;
    }
    api_->UnregisterVirtualRGBController(host);
    host->UnregisterUpdateCallback(this);
    // The hooks point into this mirror and the plugin library, both of which
    // can be gone while OpenRGB still references the host controller.
    // UpdateVirtualController swaps them under the exclusive AccessMutex, so
    // no hook call starts afterwards.
    try
    {
        RGBController_Setup setup = setup_from_local(*local_);
        neutralize_hooks(setup);
        api_->UpdateVirtualRGBController(host, &setup);
    }
    catch(...)
    {
        try
        {
            RGBController_Setup setup {};
            neutralize_hooks(setup);
            api_->UpdateVirtualRGBController(host, &setup);
        }
        catch(...)
        {
            // The hooks stay; nothing in OpenRGB calls them on a controller
            // it no longer lists.
        }
    }
    return host;
}

bool VirtualControllerMirror::copy_colors() noexcept
{
    try
    {
        const unsigned int count =
            std::min(host_controller_->GetLEDCount(), local_->GetLEDCount());
        bool has_explicit_color = false;
        for(unsigned int led = 0; led < count; ++led)
        {
            const RGBColor color = host_controller_->GetColor(led);
            has_explicit_color = has_explicit_color || color != ToRGBColor(0, 0, 0);
            local_->SetColor(led, color);
        }
        if(!skip_initial_black_frame_ || colors_initialized_.load())
        {
            return true;
        }
        if(!has_explicit_color)
        {
            return false;
        }
        colors_initialized_.store(true);
        return true;
    }
    catch(...)
    {
        return false;
    }
}

bool VirtualControllerMirror::copy_active_mode() noexcept
{
    try
    {
        const int active = host_controller_->GetActiveMode();
        if(active < 0 || static_cast<unsigned int>(active) >= local_->GetModeCount())
        {
            return false;
        }
        const auto mode = static_cast<unsigned int>(active);
        local_->SetModeSpeed(mode, host_controller_->GetModeSpeed(mode));
        local_->SetModeBrightness(mode, host_controller_->GetModeBrightness(mode));
        local_->SetModeDirection(mode, host_controller_->GetModeDirection(mode));
        local_->SetModeColorMode(mode, host_controller_->GetModeColorMode(mode));
        const unsigned int colors = host_controller_->GetModeColorsCount(mode);
        local_->SetModeColorsCount(mode, colors);
        for(unsigned int color = 0; color < colors; ++color)
        {
            local_->SetModeColor(mode, color, host_controller_->GetModeColor(mode, color));
        }
        // SetActiveMode queues the local mode update.
        local_->SetActiveMode(active);
        return true;
    }
    catch(...)
    {
        return false;
    }
}

bool VirtualControllerMirror::copy_zone_mode(unsigned int zone) noexcept
{
    try
    {
        if(zone >= local_->GetZoneCount())
        {
            return false;
        }
        const int active = host_controller_->GetZoneActiveMode(zone);
        if(active >= 0 && static_cast<unsigned int>(active) < local_->GetZoneModeCount(zone))
        {
            const auto mode = static_cast<unsigned int>(active);
            local_->SetZoneModeSpeed(zone, mode, host_controller_->GetZoneModeSpeed(zone, mode));
            local_->SetZoneModeBrightness(
                zone, mode, host_controller_->GetZoneModeBrightness(zone, mode));
            local_->SetZoneModeDirection(
                zone, mode, host_controller_->GetZoneModeDirection(zone, mode));
            local_->SetZoneModeColorMode(
                zone, mode, host_controller_->GetZoneModeColorMode(zone, mode));
            const unsigned int colors = host_controller_->GetZoneModeColorsCount(zone, mode);
            local_->SetZoneModeColorsCount(zone, mode, colors);
            for(unsigned int color = 0; color < colors; ++color)
            {
                local_->SetZoneModeColor(
                    zone, mode, color, host_controller_->GetZoneModeColor(zone, mode, color));
            }
        }
        // SetZoneActiveMode applies the zone mode on the local controller.
        local_->SetZoneActiveMode(zone, active);
        return true;
    }
    catch(...)
    {
        return false;
    }
}

void VirtualControllerMirror::host_updated(void* object, unsigned int reason, void*) noexcept
{
    // Registered on the host controller only.
    auto* mirror = static_cast<VirtualControllerMirror*>(object);
    if(mirror == nullptr)
    {
        return;
    }
    switch(reason)
    {
        case RGBCONTROLLER_UPDATE_REASON_UPDATELEDS:
            // Covers UpdateLEDs, UpdateZoneLEDs and UpdateSingleLED. Device
            // I/O stays on the local device thread.
            if(mirror->copy_colors())
            {
                mirror->local_->UpdateLEDs();
            }
            return;
        case RGBCONTROLLER_UPDATE_REASON_UPDATEMODE:
            if(zone_mode_signal == mirror)
            {
                zone_mode_signal = nullptr;
                return;
            }
            static_cast<void>(mirror->copy_active_mode());
            return;
        default:
            return;
    }
}

void VirtualControllerMirror::update_zone_mode(void* object, int zone) noexcept
{
    auto* mirror = static_cast<VirtualControllerMirror*>(object);
    if(mirror != nullptr && zone >= 0)
    {
        zone_mode_signal = mirror;
        static_cast<void>(mirror->copy_zone_mode(static_cast<unsigned int>(zone)));
    }
}

void VirtualControllerMirror::save_mode(void* object) noexcept
{
    // The host applies the mode through UpdateMode before saving it.
    auto* mirror = static_cast<VirtualControllerMirror*>(object);
    if(mirror != nullptr)
    {
        mirror->local_->SaveMode();
    }
}

VirtualControllerMirrorSet::VirtualControllerMirrorSet(
    OpenRGBPluginAPIInterface& api,
    VirtualControllerMirrorOptions options) noexcept
    : api_(&api),
      options_(options)
{
    // A new set means the plugin is loading again. The GUI dropped its pages
    // for the retired controllers while the previous set unregistered them,
    // because OpenRGB only skips that update when it is exiting.
    if(RetiredHostControllers* const retired = retired_host_controllers())
    {
        for(RGBControllerInterface* controller : retired->take(api_))
        {
            api_->DeleteVirtualRGBController(controller);
        }
    }
}

VirtualControllerMirrorSet::~VirtualControllerMirrorSet()
{
    clear();
}

void VirtualControllerMirrorSet::add(RGBController& controller) noexcept
{
    if(unloading_ || mirrors_.contains(&controller))
    {
        return;
    }
    auto mirror = VirtualControllerMirror::create(*api_, controller, options_);
    if(mirror == nullptr)
    {
        return;
    }
    try
    {
        mirrors_.emplace(&controller, std::move(mirror));
    }
    catch(...)
    {
        // The unowned mirror unregisters itself while unwinding.
        return;
    }
}

void VirtualControllerMirrorSet::remove(RGBController& controller) noexcept
{
    const auto found = mirrors_.find(&controller);
    if(found == mirrors_.end())
    {
        return;
    }
    auto mirror = std::move(found->second);
    mirrors_.erase(found);
    release(std::move(mirror));
}

void VirtualControllerMirrorSet::clear() noexcept
{
    while(!mirrors_.empty())
    {
        auto mirror = std::move(mirrors_.begin()->second);
        mirrors_.erase(mirrors_.begin());
        release(std::move(mirror));
    }
}

void VirtualControllerMirrorSet::begin_unload() noexcept
{
    unloading_ = true;
}

void VirtualControllerMirrorSet::release(std::unique_ptr<VirtualControllerMirror> mirror) noexcept
{
    if(!unloading_)
    {
        mirror.reset();
        return;
    }
    RGBControllerInterface* const host = mirror->retire();
    mirror.reset();
    if(host == nullptr)
    {
        return;
    }
    if(RetiredHostControllers* const retired = retired_host_controllers())
    {
        retired->park(api_, host);
    }
    // Without a store the controller is leaked, never freed under the GUI.
}

OpenRGBPluginAPIInterface& VirtualControllerMirrorSet::api() const noexcept
{
    return *api_;
}

} // namespace hyperflux::openrgb::native
