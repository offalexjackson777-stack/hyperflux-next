// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "runtime_bridge.hpp"

#include <cstdint>
#include <span>
#include <string>
#include <string_view>

namespace hyperflux::openrgb::native
{

struct ProductionRuntimeConfig
{
    std::string socket_path;
    std::uint32_t timeout_ms = 5'000;
    std::string expected_peer_user;
    std::string client_id;
    std::string client_name;
};

/// Canonical protocol semantics required by the native OpenRGB adapter.
[[nodiscard]] std::span<const std::string_view> required_runtime_features() noexcept;
[[nodiscard]] std::span<const std::string_view> optional_runtime_features() noexcept;

/// Builds the default from generated repository authorities only.
[[nodiscard]] ProductionRuntimeConfig default_production_runtime_config();

/// Creates a disconnected, recovery-capable runtime.
///
/// No socket connection or hardware operation occurs until the worker performs
/// its first read. This lets OpenRGB load safely before the bridge service.
[[nodiscard]] sdk::Result<std::unique_ptr<RuntimeBridge>> create_production_runtime(
    ProductionRuntimeConfig config = default_production_runtime_config());

} // namespace hyperflux::openrgb::native
