/* SPDX-License-Identifier: GPL-2.0-only */

#include <assert.h>
#include <string.h>

#include "hyperflux-next-passive.h"
#include "hyperflux-next-wire.h"

static struct hfx_uapi_frame mouse_frame(void)
{
	struct hfx_uapi_frame frame;
	unsigned int index;

	memset(&frame, 0, sizeof(frame));
	frame.backend_id = HFX_HW001_BACKEND_ID;
	frame.kind = HFX_UAPI_FRAME_KIND_USB_CLASS_SET_REPORT;
	frame.payload_length = HFX_HW001_REPORT_BYTES;
	frame.delay_after_us = 0;
	frame.payload[5] = HFX_HW001_MOUSE_COMMAND;
	frame.payload[6] = 0x0f;
	frame.payload[7] = 0x03;
	frame.payload[12] = 12;
	for (index = 0; index < 13; index++) {
		frame.payload[13 + index * 3] = (__u8)index;
		frame.payload[14 + index * 3] = 0x40;
		frame.payload[15 + index * 3] = 0x80;
	}
	frame.payload[HFX_HW001_CHECKSUM_INDEX] =
		hfx_hw001_checksum(frame.payload);
	return frame;
}

int main(void)
{
	struct hfx_uapi_frame frame = mouse_frame();
	struct hfx_passive_report report;
	__u32 activity_slot;
	const __u8 pointer_identity[] = { 0x05, 0x3b, 0x01, 0x00, 0xcd };
	const __u8 keyboard_status[] = { 0x09, 0x31, 0x32, 0x12, 0x34, 0x56 };
	const __u8 keyboard_input[] = { HFX_COMPOSITE_KEYBOARD_REPORT, 0x00 };
	const __u8 consumer_input[] = { HFX_COMPOSITE_CONSUMER_REPORT, 0x00 };
	const __u8 system_input[] = { HFX_COMPOSITE_SYSTEM_REPORT, 0x00 };
	const __u8 pointer_input[] = { HFX_COMPOSITE_POINTER_REPORT, 0x00 };

	assert(hfx_wire_validate_frame(&frame, HFX_HW001_BACKEND_ID) ==
	       HFX_WIRE_VALID);
	frame.payload[20] ^= 1;
	assert(hfx_wire_validate_frame(&frame, HFX_HW001_BACKEND_ID) ==
	       HFX_WIRE_CHECKSUM);
	frame = mouse_frame();
	frame.payload[87] = 1;
	frame.payload[HFX_HW001_CHECKSUM_INDEX] =
		hfx_hw001_checksum(frame.payload);
	assert(hfx_wire_validate_frame(&frame, HFX_HW001_BACKEND_ID) ==
	       HFX_WIRE_UNUSED_DATA);
	frame = mouse_frame();
	frame.delay_after_us = HFX_UAPI_MAX_FRAME_DELAY_US + 1;
	assert(hfx_wire_validate_frame(&frame, HFX_HW001_BACKEND_ID) ==
	       HFX_WIRE_DELAY);

	assert(hfx_passive_parse(HFX_PASSIVE_REPORT_INTERFACE,
				 pointer_identity, sizeof(pointer_identity), &report) ==
	       HFX_PASSIVE_POINTER_IDENTITY);
	assert(report.product_id == 0x00cd);
	assert(report.contact_raw == 1);
	assert(hfx_passive_parse(HFX_PASSIVE_REPORT_INTERFACE,
				 keyboard_status, sizeof(keyboard_status), &report) ==
	       HFX_PASSIVE_KEYBOARD_STATUS);
	assert(report.battery_raw == 0x32);
	assert(report.route_raw == 0x1234);
	assert(report.status_raw == 0x56);
	assert(hfx_passive_parse(4, pointer_identity,
				 sizeof(pointer_identity), &report) == HFX_PASSIVE_NONE);

	assert(hfx_activity_slot(HFX_PASSIVE_REPORT_INTERFACE, keyboard_input,
				 sizeof(keyboard_input), &activity_slot));
	assert(activity_slot == HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE);
	assert(hfx_activity_slot(HFX_PASSIVE_REPORT_INTERFACE, consumer_input,
				 sizeof(consumer_input), &activity_slot));
	assert(activity_slot == HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE);
	assert(hfx_activity_slot(HFX_PASSIVE_REPORT_INTERFACE, system_input,
				 sizeof(system_input), &activity_slot));
	assert(activity_slot == HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE);
	assert(hfx_activity_slot(HFX_PASSIVE_REPORT_INTERFACE, pointer_input,
				 sizeof(pointer_input), &activity_slot));
	assert(activity_slot == HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE);
	assert(hfx_activity_slot(HFX_POINTER_INPUT_INTERFACE, pointer_input,
				 sizeof(pointer_input), &activity_slot));
	assert(activity_slot == HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE);
	assert(hfx_activity_slot(HFX_KEYBOARD_INPUT_INTERFACE, keyboard_input,
				 sizeof(keyboard_input), &activity_slot));
	assert(activity_slot == HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE);
	assert(!hfx_activity_slot(HFX_PASSIVE_REPORT_INTERFACE, keyboard_status,
				  sizeof(keyboard_status), &activity_slot));
	assert(!hfx_activity_slot(4, keyboard_input, sizeof(keyboard_input),
				  &activity_slot));
	return 0;
}
