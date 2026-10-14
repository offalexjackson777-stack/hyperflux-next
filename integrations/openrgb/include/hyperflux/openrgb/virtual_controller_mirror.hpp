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
/// device thread is not used: OpenRGB 1.0 keeps only CONTROLLER_FLAG_VIRTUAL
/// from a virtual controller's setup flags, so that thread clears an update
/// flag after its hook returns and drops a request made meanwhile. Zone-mode
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

    [[nodiscard]] OpenRGBPluginAPIInterface& api() const noexcept;

private:
    OpenRGBPluginAPIInterface* api_;
    VirtualControllerMirrorOptions options_;
    std::map<const RGBController*, std::unique_ptr<VirtualControllerMirror>> mirrors_;
};

} // namespace hyperflux::openrgb::native
