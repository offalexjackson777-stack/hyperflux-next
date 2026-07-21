// SPDX-License-Identifier: GPL-2.0-only

#include <array>
#include <cstdint>
struct ReceiverMatch
{
    std::uint16_t vendor_id;
    std::uint16_t product_id;
    std::uint8_t backend_id;
};

#define HFX_RECEIVER_PROFILE(vendor, product, backend) \
    ReceiverMatch{vendor, product, backend},

constexpr std::array receiver_matches = {
#include "../../driver/kernel/generated/hyperflux_receiver_profiles.inc"
};

int main()
{
    static_assert(receiver_matches.size() == 1);
    static_assert(receiver_matches[0].vendor_id == 0x1532);
    static_assert(receiver_matches[0].product_id == 0x00cf);
    static_assert(receiver_matches[0].backend_id == 1);
    return 0;
}
