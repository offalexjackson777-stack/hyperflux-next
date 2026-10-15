// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "openrgb_plugin_api_stub.hpp"

#include "RGBController.h"
#include "RGBController_Virtual.h"

#include <set>
#include <vector>

namespace hyperflux::test
{

/// Host side of API 5 backed by OpenRGB's own virtual controller.
class OpenRgbVirtualPluginApi final : public OpenRgbPluginApiStub
{
public:
    OpenRgbVirtualPluginApi() = default;
    OpenRgbVirtualPluginApi(const OpenRgbVirtualPluginApi&) = delete;
    OpenRgbVirtualPluginApi& operator=(const OpenRgbVirtualPluginApi&) = delete;
    OpenRgbVirtualPluginApi(OpenRgbVirtualPluginApi&&) = delete;
    OpenRgbVirtualPluginApi& operator=(OpenRgbVirtualPluginApi&&) = delete;

    ~OpenRgbVirtualPluginApi()
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

    void UpdateVirtualRGBController(
        RGBControllerInterface* controller, RGBController_Setup* setup) override
    {
        if(!created_.contains(controller))
        {
            valid = false;
            return;
        }
        static_cast<RGBController_Virtual*>(controller)->UpdateVirtualController(setup);
        ++updates;
    }

    std::vector<RGBControllerInterface*> GetRGBControllers() override
    {
        return {registered_.begin(), registered_.end()};
    }

    [[nodiscard]] bool is_registered(RGBControllerInterface* controller) const
    {
        return registered_.contains(controller);
    }

    /// Created and not yet deleted, so OpenRGB may still dereference it.
    [[nodiscard]] bool is_allocated(RGBControllerInterface* controller) const
    {
        return created_.contains(controller);
    }

    unsigned int creates = 0;
    unsigned int deletes = 0;
    unsigned int updates = 0;
    bool valid = true;

private:
    // Same as OpenRGBPluginAPI::DeleteVirtualRGBController: the host deletes the
    // virtual controller without calling Shutdown(), and the base destructor
    // stops its device thread.
    static void destroy(RGBControllerInterface* controller)
    {
        delete static_cast<RGBController*>(controller);
    }

    std::set<RGBControllerInterface*> created_;
    std::set<RGBControllerInterface*> registered_;
};

/// OpenRGB's device page: it subscribes to its controller when built and
/// unsubscribes in its destructor. On exit OpenRGB stops following the device
/// list before it unloads plugins, so that destructor runs only after Unload
/// has returned.
class OpenRgbDevicePage
{
public:
    explicit OpenRgbDevicePage(RGBControllerInterface& controller)
        : controller_(&controller)
    {
        controller_->RegisterUpdateCallback(&OpenRgbDevicePage::updated, this);
    }

    OpenRgbDevicePage(const OpenRgbDevicePage&) = delete;
    OpenRgbDevicePage& operator=(const OpenRgbDevicePage&) = delete;
    OpenRgbDevicePage(OpenRgbDevicePage&&) = delete;
    OpenRgbDevicePage& operator=(OpenRgbDevicePage&&) = delete;
    ~OpenRgbDevicePage() = default;

    /// The page destructor. False where OpenRGB would touch freed memory;
    /// the call is skipped so the contract fails without undefined behavior.
    [[nodiscard]] bool destroy(const OpenRgbVirtualPluginApi& api)
    {
        if(controller_ == nullptr || !api.is_allocated(controller_))
        {
            return false;
        }
        controller_->UnregisterUpdateCallback(this);
        controller_ = nullptr;
        return true;
    }

    [[nodiscard]] RGBControllerInterface* controller() const noexcept
    {
        return controller_;
    }

private:
    static void updated(void*, unsigned int, void*) {}

    RGBControllerInterface* controller_;
};

} // namespace hyperflux::test
