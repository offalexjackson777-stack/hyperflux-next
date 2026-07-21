/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef HYPERFLUX_NEXT_PASSIVE_H
#define HYPERFLUX_NEXT_PASSIVE_H

#include <linux/types.h>

#define HFX_PASSIVE_REPORT_INTERFACE 1
#define HFX_POINTER_INPUT_INTERFACE 0
#define HFX_KEYBOARD_INPUT_INTERFACE 2

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

#endif
