// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "result.hpp"

#include <hyperflux/generated/domain_types.hpp>

namespace hyperflux::sdk
{

/// Reads the same Linux monotonic clock domain used by bridge deadlines.
[[nodiscard]] Result<MonotonicMs> monotonic_now() noexcept;

} // namespace hyperflux::sdk
