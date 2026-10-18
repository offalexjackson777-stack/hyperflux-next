// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <mutex>

class RGBControllerInterface;

namespace hyperflux::openrgb::native
{

/// Confirms that an OpenRGB 1.0 or 1.1 controller's device thread is running and
/// holds no barrier probe before anyone else can reach the controller.
///
/// RGBController starts its device thread in its constructor, and the thread
/// clears every pending UpdateLEDs/UpdateMode request when it first runs. A
/// request made before that point is silently lost, for example a startup
/// profile applied right after the controller is registered.
///
/// The owner calls await() once, before publishing the controller. It sets
/// the device-thread LED and mode update flags through the controller's own
/// UpdateLEDs and UpdateMode. The controller's DeviceUpdateLEDs and
/// DeviceUpdateMode hooks call absorb_leds() and absorb_mode() first and
/// return without touching the device when they report a probe.
///
/// The controller must set CONTROLLER_FLAG_RESET_BEFORE_UPDATE, and await()
/// returns false without one. The thread then clears a flag before its hook
/// runs, so an update requested while a hook runs is never lost. LED probes
/// are repeated until one reaches the thread; a repeat that arrived late is
/// consumed by the end of the next thread loop pass. The thread checks the
/// mode flag before the LED flag in every pass, so two consecutive mode
/// probes prove that pass has finished. Without the flag the thread clears
/// it after the hook returns, and nothing can prove that the clear of the
/// last probe is done before an owner's own request.
/// Probes are issued under the barrier lock; UpdateLEDs and UpdateMode only
/// set a flag and never wait for the device thread.
class DeviceThreadBarrier final
{
public:
    /// Returns false when the controller does not reset before update or its
    /// device thread did not respond within the deadline.
    [[nodiscard]] bool await(RGBControllerInterface& controller) noexcept;

    /// Returns true when the caller is an LED barrier probe.
    [[nodiscard]] bool absorb_leds() noexcept;

    /// Returns true when the caller is a mode barrier probe.
    [[nodiscard]] bool absorb_mode() noexcept;

private:
    [[nodiscard]] bool absorb(std::size_t& observations) noexcept;

    static constexpr std::chrono::milliseconds kProbeInterval {10};
    static constexpr int kProbeLimit = 200;
    static constexpr int kModeRounds = 2;

    std::mutex mutex_;
    std::condition_variable observed_changed_;
    bool armed_ = false;
    std::size_t led_observations_ = 0;
    std::size_t mode_observations_ = 0;
};

} // namespace hyperflux::openrgb::native
