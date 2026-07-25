// SPDX-License-Identifier: GPL-2.0-only

#include <array>
#include <cstdint>

struct NativeMatchRange
{
    std::uint16_t offset;
    std::uint16_t length;
};

struct ReceiverMatch
{
    std::uint16_t vendor_id;
    std::uint16_t product_id;
    std::uint32_t backend_id;
    std::uint32_t pairing_inventory_kind;
    std::uint32_t native_exchange_kind;
    std::uint16_t native_report_bytes;
    std::uint16_t native_set_report_index;
    std::uint16_t native_response_report_index;
    std::uint32_t native_initial_response_delay_us;
    std::uint32_t native_response_retry_delay_us;
    std::uint16_t native_max_response_attempts;
    std::uint16_t native_match_range_count;
    NativeMatchRange native_match_ranges[4];
};

#define HFX_PAIRING_INVENTORY_NONE 0
#define HFX_PAIRING_INVENTORY_RAZER_HYPERFLUX_V2_FEATURE_01_V1 1
#define HFX_NATIVE_EXCHANGE_NONE 0
#define HFX_NATIVE_EXCHANGE_RAZER_FEATURE_REPORT_V1 1
#define HFX_RECEIVER_PROFILE(vendor, product, backend, ...) \
    ReceiverMatch{.vendor_id = vendor, .product_id = product, \
                  .backend_id = backend, __VA_ARGS__},

constexpr std::array receiver_matches = {
#include "../../driver/kernel/generated/hyperflux_receiver_profiles.inc"
};

int main()
{
    static_assert(receiver_matches.size() == 1);
    static_assert(receiver_matches[0].vendor_id == 0x1532);
    static_assert(receiver_matches[0].product_id == 0x00cf);
    static_assert(receiver_matches[0].backend_id == 1);
    static_assert(receiver_matches[0].pairing_inventory_kind == 1);
    static_assert(receiver_matches[0].native_exchange_kind == 1);
    static_assert(receiver_matches[0].native_report_bytes == 90);
    static_assert(receiver_matches[0].native_set_report_index == 0);
    static_assert(receiver_matches[0].native_response_report_index == 0);
    static_assert(receiver_matches[0].native_initial_response_delay_us == 8000);
    static_assert(receiver_matches[0].native_response_retry_delay_us == 10000);
    static_assert(receiver_matches[0].native_max_response_attempts == 5);
    static_assert(receiver_matches[0].native_match_range_count == 2);
    static_assert(receiver_matches[0].native_match_ranges[0].offset == 2);
    static_assert(receiver_matches[0].native_match_ranges[0].length == 2);
    static_assert(receiver_matches[0].native_match_ranges[1].offset == 6);
    static_assert(receiver_matches[0].native_match_ranges[1].length == 2);
    return 0;
}
