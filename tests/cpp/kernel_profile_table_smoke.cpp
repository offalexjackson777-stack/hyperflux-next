// SPDX-License-Identifier: GPL-2.0-only

#include <array>
#include <cstdint>
#include <string_view>

struct ReceiverMatch
{
    std::string_view profile_id;
    std::uint16_t vendor_id;
    std::uint16_t product_id;
    std::uint8_t backend_id;
    std::uint16_t maximum_targets;
};

#define HFX_RECEIVER_PROFILE(id, vendor, product, backend, targets) \
    ReceiverMatch{id, vendor, product, backend, targets},

constexpr std::array receiver_matches = {
#include "../../driver/kernel/generated/hyperflux_receiver_profiles.inc"
};

int main()
{
    static_assert(receiver_matches.size() == 1);
    static_assert(receiver_matches[0].vendor_id == 0x1532);
    static_assert(receiver_matches[0].product_id == 0x00cf);
    static_assert(receiver_matches[0].maximum_targets == 115);
    return 0;
}
