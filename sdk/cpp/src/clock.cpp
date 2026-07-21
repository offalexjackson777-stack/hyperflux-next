// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/sdk/clock.hpp>

#include <cerrno>
#include <cstdint>
#include <limits>
#include <string>
#include <time.h>

namespace hyperflux::sdk
{

Result<MonotonicMs> monotonic_now() noexcept
{
    timespec value {};
    if(clock_gettime(CLOCK_MONOTONIC, &value) != 0 || value.tv_sec < 0
       || value.tv_nsec < 0)
    {
        return Result<MonotonicMs>::failure({
            ErrorCode::ClockUnavailable,
            "CLOCK_MONOTONIC is unavailable: errno " + std::to_string(errno),
            "HFX-RUNTIME-001",
        });
    }
    constexpr auto milliseconds_per_second = std::uint64_t {1'000};
    const auto seconds = static_cast<std::uint64_t>(value.tv_sec);
    if(seconds > std::numeric_limits<std::uint64_t>::max() / milliseconds_per_second)
    {
        return Result<MonotonicMs>::failure({
            ErrorCode::ClockUnavailable,
            "CLOCK_MONOTONIC value exceeds the protocol clock domain",
            "HFX-RUNTIME-001",
        });
    }
    const auto milliseconds = seconds * milliseconds_per_second
        + static_cast<std::uint64_t>(value.tv_nsec) / 1'000'000;
    return Result<MonotonicMs>::success(MonotonicMs::from(milliseconds).value());
}

} // namespace hyperflux::sdk
