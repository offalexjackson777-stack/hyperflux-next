/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef HYPERFLUX_NEXT_WIRE_H
#define HYPERFLUX_NEXT_WIRE_H

#include "uapi/hyperflux_next.h"

#define HFX_HW001_BACKEND_ID 1U
#define HFX_HW001_REPORT_BYTES 90U
#define HFX_HW001_CHECKSUM_INDEX 88U
#define HFX_HW001_TRAILER_INDEX 89U
#define HFX_HW001_COLOR_OFFSET 13U
#define HFX_HW001_MAX_COLUMNS 25U
#define HFX_HW001_MAX_SELECTORS 8U
#define HFX_HW001_MOUSE_COMMAND 0x2cU
#define HFX_HW001_KEYBOARD_COMMAND 0x38U

enum hfx_wire_validation {
	HFX_WIRE_VALID = 0,
	HFX_WIRE_BACKEND,
	HFX_WIRE_KIND,
	HFX_WIRE_LENGTH,
	HFX_WIRE_DELAY,
	HFX_WIRE_FLAGS,
	HFX_WIRE_TRAILING_DATA,
	HFX_WIRE_HEADER,
	HFX_WIRE_GEOMETRY,
	HFX_WIRE_UNUSED_DATA,
	HFX_WIRE_CHECKSUM,
};

static inline __u8 hfx_hw001_checksum(const __u8 *payload)
{
	__u8 checksum = 0;
	unsigned int index;

	for (index = 2; index < HFX_HW001_CHECKSUM_INDEX; index++)
		checksum ^= payload[index];
	return checksum;
}

static inline int hfx_wire_range_is_zero(const __u8 *bytes,
					 unsigned int start,
					 unsigned int end)
{
	unsigned int index;

	for (index = start; index < end; index++) {
		if (bytes[index] != 0)
			return 0;
	}
	return 1;
}

static inline enum hfx_wire_validation
hfx_wire_validate_hw001_payload(const __u8 *payload)
{
	unsigned int color_count;
	unsigned int color_end;
	__u8 selector;

	if (payload[0] != 0 || payload[2] != 0 || payload[3] != 0 ||
	    payload[4] != 0 || payload[6] != 0x0f || payload[7] != 0x03 ||
	    payload[8] != 0 || payload[9] != 0 || payload[11] != 0 ||
	    payload[HFX_HW001_TRAILER_INDEX] != 0)
		return HFX_WIRE_HEADER;

	color_count = (unsigned int)payload[12] + 1U;
	if (color_count == 0 || color_count > HFX_HW001_MAX_COLUMNS)
		return HFX_WIRE_GEOMETRY;
	color_end = HFX_HW001_COLOR_OFFSET + color_count * 3U;
	if (color_end > HFX_HW001_CHECKSUM_INDEX)
		return HFX_WIRE_GEOMETRY;

	if (payload[5] == HFX_HW001_MOUSE_COMMAND) {
		if (payload[1] != 0 || payload[10] != 0)
			return HFX_WIRE_HEADER;
	} else if (payload[5] == HFX_HW001_KEYBOARD_COMMAND) {
		selector = payload[10];
		if (selector >= HFX_HW001_MAX_SELECTORS ||
		    payload[1] != (__u8)(0x80U + selector))
			return HFX_WIRE_GEOMETRY;
	} else {
		return HFX_WIRE_HEADER;
	}

	if (!hfx_wire_range_is_zero(payload, color_end,
				    HFX_HW001_CHECKSUM_INDEX))
		return HFX_WIRE_UNUSED_DATA;
	if (payload[HFX_HW001_CHECKSUM_INDEX] !=
	    hfx_hw001_checksum(payload))
		return HFX_WIRE_CHECKSUM;
	return HFX_WIRE_VALID;
}

static inline enum hfx_wire_validation
hfx_wire_validate_frame(const struct hfx_uapi_frame *frame,
			unsigned int expected_backend_id)
{
	if (frame->backend_id != expected_backend_id ||
	    frame->backend_id != HFX_HW001_BACKEND_ID)
		return HFX_WIRE_BACKEND;
	if (frame->kind != HFX_UAPI_FRAME_KIND_USB_CLASS_SET_REPORT)
		return HFX_WIRE_KIND;
	if (frame->payload_length != HFX_HW001_REPORT_BYTES)
		return HFX_WIRE_LENGTH;
	if (frame->delay_after_us > HFX_UAPI_MAX_FRAME_DELAY_US)
		return HFX_WIRE_DELAY;
	if (frame->flags != 0)
		return HFX_WIRE_FLAGS;
	if (!hfx_wire_range_is_zero(frame->payload, HFX_HW001_REPORT_BYTES,
				    HFX_UAPI_MAX_FRAME_BYTES))
		return HFX_WIRE_TRAILING_DATA;
	return hfx_wire_validate_hw001_payload(frame->payload);
}

#endif
