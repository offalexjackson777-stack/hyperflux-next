// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/device_thread_barrier.hpp>

#include "RGBControllerInterface.h"

namespace hyperflux::openrgb::native
{

bool DeviceThreadBarrier::await(RGBControllerInterface& controller) noexcept
{
    try
    {
        if((controller.GetFlags() & CONTROLLER_FLAG_RESET_BEFORE_UPDATE) == 0)
        {
            return false;
        }
        std::unique_lock lock(mutex_);
        armed_ = true;
        led_observations_ = 0;
        mode_observations_ = 0;
        bool running = false;
        for(int probe = 0; probe < kProbeLimit && !running; ++probe)
        {
            // A probe issued before the thread starts is cleared by it, so
            // repeat until one reaches the hook.
            controller.UpdateLEDs();
            running = observed_changed_.wait_for(
                lock, kProbeInterval, [this] { return led_observations_ > 0; });
        }
        // The thread is past its start, so a mode probe cannot be lost.
        for(int round = 0; round < kModeRounds && running; ++round)
        {
            const std::size_t target = mode_observations_ + 1;
            controller.UpdateMode();
            running = observed_changed_.wait_for(
                lock,
                kProbeInterval * kProbeLimit,
                [this, target] { return mode_observations_ >= target; });
        }
        armed_ = false;
        return running;
    }
    catch(...)
    {
        std::lock_guard lock(mutex_);
        armed_ = false;
        return false;
    }
}

bool DeviceThreadBarrier::absorb_leds() noexcept
{
    return absorb(led_observations_);
}

bool DeviceThreadBarrier::absorb_mode() noexcept
{
    return absorb(mode_observations_);
}

bool DeviceThreadBarrier::absorb(std::size_t& observations) noexcept
{
    std::lock_guard lock(mutex_);
    if(!armed_)
    {
        return false;
    }
    ++observations;
    observed_changed_.notify_all();
    return true;
}

} // namespace hyperflux::openrgb::native
