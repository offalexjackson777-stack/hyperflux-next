/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef HYPERFLUX_NEXT_PASSIVE_H
#define HYPERFLUX_NEXT_PASSIVE_H

#include <linux/types.h>

#include "uapi/hyperflux_next.h"

#define HFX_PASSIVE_REPORT_INTERFACE 1
#define HFX_POINTER_INPUT_INTERFACE 0
#define HFX_KEYBOARD_INPUT_INTERFACE 2

#define HFX_COMPOSITE_KEYBOARD_REPORT 0x01
#define HFX_COMPOSITE_CONSUMER_REPORT 0x02
#define HFX_COMPOSITE_SYSTEM_REPORT 0x03
#define HFX_COMPOSITE_POINTER_REPORT 0x0a

static inline int hfx_activity_slot(int interface_number,
				    unsigned int report_id, int size,
				    __u32 *slot)
{
	if (size <= 0 || !slot)
		return 0;
	if (interface_number == HFX_POINTER_INPUT_INTERFACE) {
		*slot = HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE;
		return 1;
	}
	if (interface_number == HFX_KEYBOARD_INPUT_INTERFACE) {
		*slot = HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE;
		return 1;
	}
	if (interface_number != HFX_PASSIVE_REPORT_INTERFACE)
		return 0;

	switch (report_id) {
	case HFX_COMPOSITE_KEYBOARD_REPORT:
	case HFX_COMPOSITE_CONSUMER_REPORT:
	case HFX_COMPOSITE_SYSTEM_REPORT:
		*slot = HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE;
		return 1;
	case HFX_COMPOSITE_POINTER_REPORT:
		*slot = HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE;
		return 1;
	default:
		return 0;
	}
}

enum hfx_passive_kind {
	HFX_PASSIVE_NONE = 0,
	HFX_PASSIVE_POINTER_IDENTITY,
	HFX_PASSIVE_POINTER_STATUS,
	HFX_PASSIVE_KEYBOARD_IDENTITY,
	HFX_PASSIVE_KEYBOARD_STATUS,
	HFX_PASSIVE_INVALID,
};

struct hfx_passive_report {
	enum hfx_passive_kind kind;
	__u16 product_id;
	__u16 route_raw;
	__u8 battery_raw;
	__u8 contact_raw;
	__u8 charge_raw;
	__u8 status_raw;
};

static inline void hfx_passive_report_reset(struct hfx_passive_report *report)
{
	report->kind = HFX_PASSIVE_NONE;
	report->product_id = 0;
	report->route_raw = 0;
	report->battery_raw = 0;
	report->contact_raw = 0;
	report->charge_raw = 0;
	report->status_raw = 0;
}

static inline int hfx_passive_product_id_valid(__u16 product_id)
{
	return product_id != 0 && product_id != 0xffffU;
}

static inline enum hfx_passive_kind
hfx_passive_parse(int interface_number, const __u8 *data, int size,
		  struct hfx_passive_report *report)
{
	__u16 product_id;

	hfx_passive_report_reset(report);
	if (interface_number != HFX_PASSIVE_REPORT_INTERFACE || !data || size <= 0)
		return HFX_PASSIVE_NONE;

	if (size >= 5 && data[0] == 0x05 && data[1] == 0x3b) {
		product_id = ((__u16)data[3] << 8) | data[4];
		if (!hfx_passive_product_id_valid(product_id))
			return HFX_PASSIVE_INVALID;
		report->kind = HFX_PASSIVE_POINTER_IDENTITY;
		report->product_id = product_id;
		report->contact_raw = data[2];
	} else if (size >= 6 && data[0] == 0x05 && data[1] == 0x31) {
		report->kind = HFX_PASSIVE_POINTER_STATUS;
		report->battery_raw = data[2];
		report->contact_raw = data[3];
		report->charge_raw = data[4];
		report->status_raw = data[5];
	} else if (size >= 6 && data[0] == 0x09 && data[1] == 0x35) {
		product_id = ((__u16)data[4] << 8) | data[5];
		if (!hfx_passive_product_id_valid(product_id))
			return HFX_PASSIVE_INVALID;
		report->kind = HFX_PASSIVE_KEYBOARD_IDENTITY;
		report->product_id = product_id;
		report->route_raw = data[2];
		report->status_raw = data[3];
	} else if (size >= 6 && data[0] == 0x09 && data[1] == 0x31) {
		report->kind = HFX_PASSIVE_KEYBOARD_STATUS;
		report->battery_raw = data[2];
		report->route_raw = ((__u16)data[3] << 8) | data[4];
		report->status_raw = data[5];
	}
	return report->kind;
}

#define HFX_FEATURE_STATUS_SUCCESS 0x02
#define HFX_FEATURE_BATTERY_CLASS 0x07
#define HFX_FEATURE_BATTERY_LEVEL 0x80
#define HFX_FEATURE_BATTERY_MIN_BYTES 10

/*
 * A battery-level query (two argument bytes, class 07, ID 80, both arguments
 * zero) answered with status 02 carries the child's charge in byte 9, on the
 * same 0-255 scale as its passive status report. A child that rarely sends
 * that report is still read whenever a host application asks for its level.
 */
static inline int hfx_feature_battery_parse(const __u8 *request,
					    const __u8 *response, int size,
					    __u8 *battery_raw)
{
	if (!request || !response || !battery_raw ||
	    size < HFX_FEATURE_BATTERY_MIN_BYTES)
		return 0;
	if (request[5] != 0x02 || request[6] != HFX_FEATURE_BATTERY_CLASS ||
	    request[7] != HFX_FEATURE_BATTERY_LEVEL || request[8] != 0 ||
	    request[9] != 0)
		return 0;
	if (response[0] != HFX_FEATURE_STATUS_SUCCESS ||
	    response[6] != HFX_FEATURE_BATTERY_CLASS ||
	    response[7] != HFX_FEATURE_BATTERY_LEVEL)
		return 0;
	*battery_raw = response[9];
	return 1;
}

#endif
