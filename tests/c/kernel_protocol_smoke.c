/* SPDX-License-Identifier: GPL-2.0-only */

#include <assert.h>
#include <string.h>

#include "hyperflux-next-battery.h"
#include "hyperflux-next-pairing-inventory.h"
#include "hyperflux-next-passive.h"
#include "hyperflux-next-wire.h"

static void pairing_inventory_report(
	unsigned char report[HFX_PAIRING_INVENTORY_REPORT_BYTES],
	unsigned short pointer_product_id,
	unsigned short keyboard_product_id)
{
	static const unsigned char prefix[16] = {
		0x00, 0x00, 0xa5, 0x85, 0x01, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x26, 0x00,
	};
	static const unsigned char header[16] = {
		0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0xdf, 0xa6,
		0x33, 0x4e, 0x01, 0x00, 0x04, 0xa3, 0x22, 0x00,
	};
	const size_t anchor = HFX_PAIRING_INVENTORY_ANCHOR;

	memset(report, 0, HFX_PAIRING_INVENTORY_REPORT_BYTES);
	report[0] = HFX_PAIRING_INVENTORY_REPORT_ID;
	memcpy(&report[anchor - sizeof(prefix)], prefix, sizeof(prefix));
	memset(&report[anchor], 'X', 15);
	report[anchor + 20] = 0xff;
	report[anchor + 21] = 0xff;
	memcpy(&report[anchor + 22], header, sizeof(header));
	report[anchor + 38] = pointer_product_id >> 8;
	report[anchor + 39] = pointer_product_id;
	report[anchor + 40] = keyboard_product_id >> 8;
	report[anchor + 41] = keyboard_product_id;
}

static struct hfx_pairing_inventory parsed_pairing_inventory(
	unsigned short pointer_product_id,
	unsigned short keyboard_product_id)
{
	struct hfx_pairing_inventory inventory;
	unsigned char report[HFX_PAIRING_INVENTORY_REPORT_BYTES];

	pairing_inventory_report(report, pointer_product_id,
				 keyboard_product_id);
	assert(hfx_pairing_inventory_parse(report, sizeof(report), &inventory) ==
	       HFX_PAIRING_INVENTORY_OK);
	return inventory;
}

static void assert_pairing_inventory(
	unsigned short pointer_product_id,
	enum hfx_pairing_inventory_slot_state pointer_state,
	unsigned short keyboard_product_id,
	enum hfx_pairing_inventory_slot_state keyboard_state)
{
	const struct hfx_pairing_inventory inventory =
		parsed_pairing_inventory(pointer_product_id, keyboard_product_id);

	assert(inventory.pointer_raw_product_id == pointer_product_id);
	assert(inventory.pointer_state == pointer_state);
	assert(inventory.keyboard_raw_product_id == keyboard_product_id);
	assert(inventory.keyboard_state == keyboard_state);
}

static void assert_pairing_inventory_rejected(
	const unsigned char report[HFX_PAIRING_INVENTORY_REPORT_BYTES],
	size_t length, enum hfx_pairing_inventory_result expected)
{
	struct hfx_pairing_inventory before;
	struct hfx_pairing_inventory inventory;

	memset(&inventory, 0xa5, sizeof(inventory));
	before = inventory;
	assert(hfx_pairing_inventory_parse(report, length, &inventory) == expected);
	assert(memcmp(&inventory, &before, sizeof(inventory)) == 0);
}

static void test_pairing_inventory(void)
{
	static const unsigned short official_mouse_product_ids[] = {
		0x00ab, 0x00cd, 0x00b0, 0x00a8, 0x00db,
	};
	static const unsigned short official_keyboard_product_ids[] = {
		0x0290, 0x0296, 0x025c, 0x02ba, 0x02d5, 0x02c9, 0x02d2,
	};
	unsigned char report[HFX_PAIRING_INVENTORY_REPORT_BYTES];
	struct hfx_pairing_inventory inventory;
	size_t index;

	for (index = 0;
	     index < sizeof(official_mouse_product_ids) /
			     sizeof(official_mouse_product_ids[0]);
	     index++)
		assert_pairing_inventory(
			official_mouse_product_ids[index],
			HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID, 0x0000,
			HFX_PAIRING_INVENTORY_SLOT_UNPROVEN);
	for (index = 0;
	     index < sizeof(official_keyboard_product_ids) /
			     sizeof(official_keyboard_product_ids[0]);
	     index++)
		assert_pairing_inventory(
			0xffff, HFX_PAIRING_INVENTORY_SLOT_UNPROVEN,
			official_keyboard_product_ids[index],
			HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID);

	assert_pairing_inventory(
		0x00cd, HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID, 0x0296,
		HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID);
	assert_pairing_inventory(
		0x00cd, HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID, 0x7777,
		HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID);
	assert_pairing_inventory(
		0x7777, HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID, 0x0296,
		HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID);
	assert_pairing_inventory(
		0x0000, HFX_PAIRING_INVENTORY_SLOT_UNPROVEN, 0x0296,
		HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID);
	assert_pairing_inventory(
		0x00cd, HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID, 0xffff,
		HFX_PAIRING_INVENTORY_SLOT_UNPROVEN);
	assert_pairing_inventory(
		0x0000, HFX_PAIRING_INVENTORY_SLOT_UNPROVEN, 0xffff,
		HFX_PAIRING_INVENTORY_SLOT_UNPROVEN);

	/* A later valid sibling is independent of the first slot. */
	inventory = parsed_pairing_inventory(0x00cd, 0xffff);
	assert(inventory.pointer_state == HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID);
	assert(inventory.keyboard_state == HFX_PAIRING_INVENTORY_SLOT_UNPROVEN);
	inventory = parsed_pairing_inventory(0x00cd, 0x0296);
	assert(inventory.pointer_state == HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID);
	assert(inventory.keyboard_state == HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID);

	/* An unproven later value does not alias a previously valid sibling PID. */
	inventory = parsed_pairing_inventory(0x00cd, 0x0296);
	assert(inventory.keyboard_raw_product_id == 0x0296);
	inventory = parsed_pairing_inventory(0x00cd, 0x0000);
	assert(inventory.pointer_raw_product_id == 0x00cd);
	assert(inventory.pointer_state == HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID);
	assert(inventory.keyboard_raw_product_id == 0x0000);
	assert(inventory.keyboard_state == HFX_PAIRING_INVENTORY_SLOT_UNPROVEN);

	pairing_inventory_report(report, 0x00cd, 0x0296);
	assert_pairing_inventory_rejected(
		report, sizeof(report) - 1, HFX_PAIRING_INVENTORY_LENGTH);
	pairing_inventory_report(report, 0x00cd, 0x0296);
	report[0] = 0;
	assert_pairing_inventory_rejected(
		report, sizeof(report), HFX_PAIRING_INVENTORY_BAD_REPORT_ID);
	pairing_inventory_report(report, 0x00cd, 0x0296);
	report[HFX_PAIRING_INVENTORY_ANCHOR - 1] ^= 1;
	assert_pairing_inventory_rejected(
		report, sizeof(report), HFX_PAIRING_INVENTORY_BAD_ANCHOR);
	pairing_inventory_report(report, 0x00cd, 0x0296);
	report[HFX_PAIRING_INVENTORY_ANCHOR + 15] = 1;
	assert_pairing_inventory_rejected(
		report, sizeof(report), HFX_PAIRING_INVENTORY_BAD_PADDING);
	pairing_inventory_report(report, 0x00cd, 0x0296);
	report[HFX_PAIRING_INVENTORY_ANCHOR + 20] = 0;
	assert_pairing_inventory_rejected(
		report, sizeof(report), HFX_PAIRING_INVENTORY_BAD_MARKER);
	pairing_inventory_report(report, 0x00cd, 0x0296);
	report[HFX_PAIRING_INVENTORY_ANCHOR + 22] ^= 1;
	assert_pairing_inventory_rejected(
		report, sizeof(report), HFX_PAIRING_INVENTORY_BAD_HEADER);
}

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

static struct hfx_uapi_child_battery present_battery(void)
{
	struct hfx_uapi_child_battery report;

	memset(&report, 0, sizeof(report));
	report.version = HFX_UAPI_POWER_SUPPLY_ABI_VERSION;
	report.size = sizeof(report);
	report.receiver_generation = 7;
	report.endpoint_slot = HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE;
	report.presence = HFX_UAPI_BATTERY_PRESENCE_PRESENT;
	report.capacity = 62;
	report.status = HFX_UAPI_BATTERY_STATUS_UNKNOWN;
	memcpy(report.model_name, "Razer Viper Ultimate", 20);
	return report;
}

static void test_child_battery(void)
{
	struct hfx_uapi_child_battery report = present_battery();

	assert(hfx_battery_check_report(&report) == HFX_BATTERY_VALID);
	assert(hfx_battery_lane_index(HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE) ==
	       HFX_BATTERY_POINTER);
	assert(hfx_battery_lane_index(HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE) ==
	       HFX_BATTERY_KEYBOARD);
	assert(hfx_battery_lane_index(HFX_UAPI_ENDPOINT_SLOT_RECEIVER) < 0);

	report.capacity = HFX_UAPI_BATTERY_MAX_CAPACITY;
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_VALID);
	report.capacity = HFX_UAPI_BATTERY_MAX_CAPACITY + 1;
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_VALUE);

	report = present_battery();
	report.status = HFX_UAPI_BATTERY_STATUS_FULL + 1;
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_VALUE);
	report = present_battery();
	report.presence = 2;
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_VALUE);
	report = present_battery();
	report.version = HFX_UAPI_POWER_SUPPLY_ABI_VERSION + 1;
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_HEADER);
	report = present_battery();
	report.size = sizeof(report) - 1;
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_HEADER);
	report = present_battery();
	report.endpoint_slot = HFX_UAPI_ENDPOINT_SLOT_RECEIVER;
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_SLOT);

	report = present_battery();
	report.model_name[3] = '\n';
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_MODEL_NAME);
	report = present_battery();
	report.model_name[25] = 'X';
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_MODEL_NAME);
	report = present_battery();
	memset(report.model_name, 'A', sizeof(report.model_name));
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_MODEL_NAME);
	report = present_battery();
	memset(report.model_name, 0, sizeof(report.model_name));
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_VALID);

	report = present_battery();
	report.presence = HFX_UAPI_BATTERY_PRESENCE_ABSENT;
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_VALUE);
	report.capacity = 0;
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_MODEL_NAME);
	memset(report.model_name, 0, sizeof(report.model_name));
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_VALID);
	report.status = HFX_UAPI_BATTERY_STATUS_DISCHARGING;
	assert(hfx_battery_check_report(&report) == HFX_BATTERY_VALUE);
}

int main(void)
{
	struct hfx_uapi_frame frame = mouse_frame();
	struct hfx_passive_report report;
	__u32 activity_slot;
	const __u8 pointer_identity[] = { 0x05, 0x3b, 0x01, 0x00, 0xcd };
	const __u8 keyboard_status[] = { 0x09, 0x31, 0x32, 0x12, 0x34, 0x56 };

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

	assert(hfx_activity_slot(HFX_PASSIVE_REPORT_INTERFACE,
				 HFX_COMPOSITE_KEYBOARD_REPORT, 16,
				 &activity_slot));
	assert(activity_slot == HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE);
	assert(hfx_activity_slot(HFX_PASSIVE_REPORT_INTERFACE,
				 HFX_COMPOSITE_CONSUMER_REPORT, 16,
				 &activity_slot));
	assert(activity_slot == HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE);
	assert(hfx_activity_slot(HFX_PASSIVE_REPORT_INTERFACE,
				 HFX_COMPOSITE_SYSTEM_REPORT, 16,
				 &activity_slot));
	assert(activity_slot == HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE);
	assert(hfx_activity_slot(HFX_PASSIVE_REPORT_INTERFACE,
				 HFX_COMPOSITE_POINTER_REPORT, 16,
				 &activity_slot));
	assert(activity_slot == HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE);
	assert(hfx_activity_slot(HFX_POINTER_INPUT_INTERFACE, 0, 8,
				 &activity_slot));
	assert(activity_slot == HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE);
	assert(hfx_activity_slot(HFX_KEYBOARD_INPUT_INTERFACE, 0, 8,
				 &activity_slot));
	assert(activity_slot == HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE);
	assert(!hfx_activity_slot(HFX_PASSIVE_REPORT_INTERFACE, 9, 16,
				  &activity_slot));
	assert(!hfx_activity_slot(4, HFX_COMPOSITE_KEYBOARD_REPORT, 16,
				  &activity_slot));

	test_pairing_inventory();
	test_child_battery();
	return 0;
}
