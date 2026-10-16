/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef HYPERFLUX_NEXT_BATTERY_H
#define HYPERFLUX_NEXT_BATTERY_H

#include <linux/types.h>

#include "uapi/hyperflux_next.h"

#define HFX_BATTERY_POINTER 0
#define HFX_BATTERY_KEYBOARD 1
#define HFX_BATTERY_LANES 2

enum hfx_battery_check {
	HFX_BATTERY_VALID = 0,
	HFX_BATTERY_HEADER,
	HFX_BATTERY_SLOT,
	HFX_BATTERY_VALUE,
	HFX_BATTERY_MODEL_NAME,
};

static inline int hfx_battery_lane_index(__u32 endpoint_slot)
{
	switch (endpoint_slot) {
	case HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE:
		return HFX_BATTERY_POINTER;
	case HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE:
		return HFX_BATTERY_KEYBOARD;
	default:
		return -1;
	}
}

/*
 * An absent lane carries no value at all.  A present lane carries a bounded
 * capacity, a known status code, and a NUL-terminated printable ASCII model
 * name whose tail is zero.
 */
static inline enum hfx_battery_check
hfx_battery_check_report(const struct hfx_uapi_child_battery *report)
{
	int terminated = 0;
	__u32 index;

	if (report->version != HFX_UAPI_POWER_SUPPLY_ABI_VERSION ||
	    report->size != sizeof(*report))
		return HFX_BATTERY_HEADER;
	if (hfx_battery_lane_index(report->endpoint_slot) < 0)
		return HFX_BATTERY_SLOT;
	if (report->presence == HFX_UAPI_BATTERY_PRESENCE_ABSENT) {
		if (report->capacity ||
		    report->status != HFX_UAPI_BATTERY_STATUS_UNKNOWN)
			return HFX_BATTERY_VALUE;
		for (index = 0; index < HFX_UAPI_BATTERY_MODEL_NAME_BYTES;
		     index++) {
			if (report->model_name[index])
				return HFX_BATTERY_MODEL_NAME;
		}
		return HFX_BATTERY_VALID;
	}
	if (report->presence != HFX_UAPI_BATTERY_PRESENCE_PRESENT ||
	    report->capacity > HFX_UAPI_BATTERY_MAX_CAPACITY ||
	    report->status > HFX_UAPI_BATTERY_STATUS_FULL)
		return HFX_BATTERY_VALUE;
	for (index = 0; index < HFX_UAPI_BATTERY_MODEL_NAME_BYTES; index++) {
		__u8 value = report->model_name[index];

		if (terminated) {
			if (value)
				return HFX_BATTERY_MODEL_NAME;
		} else if (!value) {
			terminated = 1;
		} else if (value < 0x20 || value > 0x7e) {
			return HFX_BATTERY_MODEL_NAME;
		}
	}
	return terminated ? HFX_BATTERY_VALID : HFX_BATTERY_MODEL_NAME;
}

#endif
