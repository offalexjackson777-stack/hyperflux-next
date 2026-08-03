// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/client_identity.hpp>

#include <array>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <iomanip>
#include <sstream>
#include <string>
#include <string_view>
#include <sys/random.h>
#include <utility>

namespace hyperflux::openrgb::native {
namespace {

constexpr std::size_t client_nonce_bytes = 32;

sdk::Error identity_error(sdk::ErrorCode code, std::string message) {
  return {code, std::move(message), "HFX-INTEGRATION-001"};
}

std::string_view role_name(OpenRgbClientRole role) noexcept {
  switch (role) {
  case OpenRgbClientRole::Runtime:
    return "runtime";
  case OpenRgbClientRole::Discovery:
    return "discovery";
  case OpenRgbClientRole::Feature:
    return "feature";
  }
  return "invalid";
}

sdk::Result<std::string> client_nonce() {
  std::array<std::uint8_t, client_nonce_bytes> bytes{};
  std::size_t received = 0;
  while (received < bytes.size()) {
    const auto count = ::getrandom(bytes.data() + received,
                                   bytes.size() - received, GRND_NONBLOCK);
    if (count < 0) {
      if (errno == EINTR) {
        continue;
      }
      return sdk::Result<std::string>::failure(
          identity_error(sdk::ErrorCode::IdentityUnavailable,
                         "operating-system entropy is unavailable for OpenRGB "
                         "client identity"));
    }
    if (count == 0) {
      return sdk::Result<std::string>::failure(
          identity_error(sdk::ErrorCode::IdentityUnavailable,
                         "operating-system entropy ended during OpenRGB client "
                         "identity creation"));
    }
    received += static_cast<std::size_t>(count);
  }

  std::ostringstream output;
  output << std::hex << std::setfill('0');
  for (const auto byte : bytes) {
    output << std::setw(2) << static_cast<unsigned int>(byte);
  }
  return sdk::Result<std::string>::success(output.str());
}

} // namespace

sdk::Result<ClientId> next_process_openrgb_client_id(OpenRgbClientRole role) {
  auto nonce = client_nonce();
  if (!nonce) {
    return sdk::Result<ClientId>::failure(nonce.error());
  }
  auto identity = ClientId::from("openrgb-" + std::string(role_name(role)) +
                                 "-" + std::move(nonce).value());
  if (!identity.has_value()) {
    return sdk::Result<ClientId>::failure(identity_error(
        sdk::ErrorCode::IdentityUnavailable,
        "generated OpenRGB client identity violates the domain contract"));
  }
  return sdk::Result<ClientId>::success(std::move(*identity));
}

OpenRgbClientIdFactory
process_openrgb_client_id_factory(OpenRgbClientRole role) {
  return [role] { return next_process_openrgb_client_id(role); };
}

} // namespace hyperflux::openrgb::native
