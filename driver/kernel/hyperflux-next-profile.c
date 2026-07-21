// SPDX-License-Identifier: GPL-2.0-only

#include <linux/kernel.h>

#include "hyperflux-next-profile.h"

#define HFX_RECEIVER_PROFILE(vendor, product, backend) \
	{ .vendor_id = (vendor), .product_id = (product), .backend_id = (backend) },

static const struct hfx_receiver_profile hfx_receiver_profiles[] = {
#include "generated/hyperflux_receiver_profiles.inc"
};

#undef HFX_RECEIVER_PROFILE

const struct hfx_receiver_profile *hfx_receiver_profile_find(u16 vendor_id,
							     u16 product_id)
{
	size_t index;

	for (index = 0; index < ARRAY_SIZE(hfx_receiver_profiles); index++) {
		if (hfx_receiver_profiles[index].vendor_id == vendor_id &&
		    hfx_receiver_profiles[index].product_id == product_id)
			return &hfx_receiver_profiles[index];
	}
	return NULL;
}
