/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef HYPERFLUX_NEXT_PROFILE_H
#define HYPERFLUX_NEXT_PROFILE_H

#include <linux/types.h>

enum hfx_pairing_inventory_kind {
	HFX_PAIRING_INVENTORY_NONE = 0,
	HFX_PAIRING_INVENTORY_RAZER_HYPERFLUX_V2_FEATURE_01_V1 = 1,
};

enum hfx_native_exchange_kind {
	HFX_NATIVE_EXCHANGE_NONE = 0,
	HFX_NATIVE_EXCHANGE_RAZER_FEATURE_REPORT_V1 = 1,
};

#define HFX_NATIVE_MATCH_RANGE_CAPACITY 4U

struct hfx_native_match_range {
	u16 offset;
	u16 length;
};

struct hfx_receiver_profile {
	u16 vendor_id;
	u16 product_id;
	u32 backend_id;
	u32 pairing_inventory_kind;
	u32 native_exchange_kind;
	u16 native_report_bytes;
	u16 native_set_report_index;
	u16 native_response_report_index;
	u16 native_max_response_attempts;
	u16 native_match_range_count;
	u32 native_initial_response_delay_us;
	u32 native_response_retry_delay_us;
	struct hfx_native_match_range
		native_match_ranges[HFX_NATIVE_MATCH_RANGE_CAPACITY];
};

const struct hfx_receiver_profile *hfx_receiver_profile_find(u16 vendor_id,
							     u16 product_id);

#endif
