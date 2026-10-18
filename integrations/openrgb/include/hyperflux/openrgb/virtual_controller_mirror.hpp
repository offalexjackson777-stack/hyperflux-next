// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <atomic>
#include <map>
#include <memory>

class OpenRGBPluginAPIInterface;
class RGBController;
class RGBControllerInterface;

namespace hyperflux::openrgb::native
{

struct VirtualControllerMirrorOptions
{
    /// Ignore the host's all-black LED buffer until a nonblack frame arrives.
    /// OpenRGB provides no callback metadata that distinguishes its initial
    /// zero buffer from an intentional first black Direct frame.
    bool skip_initial_black_frame = false;
};

/// Presents one plugin-owned controller through OpenRGB plugin API 5.
///
/// API 5 hosts only list controllers they created themselves, so the
/// plugin-owned controller stays the device implementation and the host's
/// virtual controller forwards every device update to it.
///
/// LED and mode updates are forwarded from the host's update callback, which
/// runs synchronously on the thread that requested the update. The host's
/// device thread is not used: OpenRGB 1.0 and 1.1 keep only
/// CONTROLLER_FLAG_VIRTUAL from a virtual controller's setup flags, so that
/// thread clears an update flag after its hook returns and drops a request
/// made meanwhile. Zone-mode
/// and save-mode updates already run synchronously through their hooks.
///
/// Create and destroy mirrors on the Qt application thread. Registration and
/// removal refresh the OpenRGB device list synchronously on that thread and
/// block any other caller until it has processed the refresh. The local
/// controller must outlive its mirror.
class VirtualControllerMirror final
{
public:
    VirtualControllerMirror(const VirtualControllerMirror&) = delete;
    VirtualControllerMirror& operator=(const VirtualControllerMirror&) = delete;
    VirtualControllerMirror(VirtualControllerMirror&&) = delete;
    VirtualControllerMirror& operator=(VirtualControllerMirror&&) = delete;
    ~VirtualControllerMirror();

    /// Returns nullptr when the host rejects the controller description.
    [[nodiscard]] static std::unique_ptr<VirtualControllerMirror> create(
        OpenRGBPluginAPIInterface& api,
        RGBController& local,
        VirtualControllerMirrorOptions options = {}) noexcept;

    [[nodiscard]] RGBControllerInterface* host_controller() const noexcept;

    /// Unregisters the host controller and stops forwarding like the
    /// destructor, but leaves the host controller allocated and returns it.
    /// Its hooks no longer reach this mirror or plugin code.
    [[nodiscard]] RGBControllerInterface* retire() noexcept;

private:
    VirtualControllerMirror(
        OpenRGBPluginAPIInterface& api,
        RGBController& local,
        VirtualControllerMirrorOptions options) noexcept;

    static void host_updated(void* object, unsigned int reason, void* controller) noexcept;
    static void update_zone_mode(void* object, int zone) noexcept;
    static void save_mode(void* object) noexcept;

    [[nodiscard]] bool copy_colors() noexcept;
    [[nodiscard]] bool copy_active_mode() noexcept;
    [[nodiscard]] bool copy_zone_mode(unsigned int zone) noexcept;

    OpenRGBPluginAPIInterface* api_;
    RGBController* local_;
    RGBControllerInterface* host_controller_ = nullptr;
    bool skip_initial_black_frame_;
    bool callback_registered_ = false;
    std::atomic<bool> colors_initialized_ {false};
};

/// Host-side registration state for plugin-owned controllers.
///
/// Registering an already mirrored controller is a no-op, so a controller is
/// never presented twice. Same threading contract as VirtualControllerMirror.
///
/// After begin_unload(), removals retire host controllers instead of deleting
/// them. OpenRGB 1.0 and 1.1 stop their GUI device-list updates before they
/// unload plugins on exit, so their device pages still reference those
/// controllers and use them in their destructors after Unload has returned. Retired
/// controllers stay allocated until a later set is created for the same
/// plugin API. That only happens on a reload, and the GUI has dropped the
/// pages synchronously during the unregistration that preceded it. On exit
/// they are never freed.
class VirtualControllerMirrorSet final
{
public:
    explicit VirtualControllerMirrorSet(
        OpenRGBPluginAPIInterface& api,
        VirtualControllerMirrorOptions options = {}) noexcept;
    VirtualControllerMirrorSet(const VirtualControllerMirrorSet&) = delete;
    VirtualControllerMirrorSet& operator=(const VirtualControllerMirrorSet&) = delete;
    VirtualControllerMirrorSet(VirtualControllerMirrorSet&&) = delete;
    VirtualControllerMirrorSet& operator=(VirtualControllerMirrorSet&&) = delete;
    ~VirtualControllerMirrorSet();

    void add(RGBController& controller) noexcept;
    void remove(RGBController& controller) noexcept;
    void clear() noexcept;
    /// Irreversible: later additions are ignored.
    void begin_unload() noexcept;

    [[nodiscard]] OpenRGBPluginAPIInterface& api() const noexcept;

private:
    void release(std::unique_ptr<VirtualControllerMirror> mirror) noexcept;

    OpenRGBPluginAPIInterface* api_;
    VirtualControllerMirrorOptions options_;
    std::map<const RGBController*, std::unique_ptr<VirtualControllerMirror>> mirrors_;
    bool unloading_ = false;
};

} // namespace hyperflux::openrgb::native
