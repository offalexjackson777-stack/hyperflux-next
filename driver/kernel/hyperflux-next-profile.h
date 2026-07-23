/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef HYPERFLUX_NEXT_PROFILE_H
#define HYPERFLUX_NEXT_PROFILE_H

#include <linux/types.h>

enum hfx_pairing_inventory_kind {
	HFX_PAIRING_INVENTORY_NONE = 0,
	HFX_PAIRING_INVENTORY_RAZER_HYPERFLUX_V2_FEATURE_01_V1 = 1,
};

struct hfx_receiver_profile {
	u16 vendor_id;
	u16 product_id;
	u32 backend_id;
	u32 pairing_inventory_kind;
};

const struct hfx_receiver_profile *hfx_receiver_profile_find(u16 vendor_id,
							     u16 product_id);

#endif
