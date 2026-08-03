// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <hyperflux/sdk.hpp>

#include <functional>

namespace hyperflux::openrgb::native {

enum class OpenRgbClientRole {
  Runtime,
  Discovery,
  Feature,
};

using OpenRgbClientIdFactory = std::function<sdk::Result<ClientId>()>;

/// Allocates a hardware-neutral identity for one OpenRGB bridge client.
///
/// Every allocation obtains a fresh 256-bit nonce from the operating system.
/// No inherited process mutex or cached initialization state is involved, so
/// concurrent factories, module copies, reloads, and post-fork children cannot
/// reuse or deadlock on allocator state.
[[nodiscard]] sdk::Result<ClientId>
next_process_openrgb_client_id(OpenRgbClientRole role);

[[nodiscard]] OpenRgbClientIdFactory
process_openrgb_client_id_factory(OpenRgbClientRole role);

} // namespace hyperflux::openrgb::native
