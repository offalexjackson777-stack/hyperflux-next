/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef HYPERFLUX_NEXT_PROFILE_H
#define HYPERFLUX_NEXT_PROFILE_H

#include <linux/types.h>

struct hfx_receiver_profile {
	u16 vendor_id;
	u16 product_id;
	u32 backend_id;
};

const struct hfx_receiver_profile *hfx_receiver_profile_find(u16 vendor_id,
							     u16 product_id);

#endif
