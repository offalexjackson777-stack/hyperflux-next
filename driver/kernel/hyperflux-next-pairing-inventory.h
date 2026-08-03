/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef HYPERFLUX_NEXT_PAIRING_INVENTORY_H
#define HYPERFLUX_NEXT_PAIRING_INVENTORY_H

#ifdef __KERNEL__
#include <linux/string.h>
#include <linux/types.h>
#else
#include <stddef.h>
#include <string.h>
#endif

#define HFX_PAIRING_INVENTORY_INTERFACE 4
#define HFX_PAIRING_INVENTORY_REPORT_ID 0x01
#define HFX_PAIRING_INVENTORY_REPORT_BYTES 512
#define HFX_PAIRING_INVENTORY_ANCHOR 266

enum hfx_pairing_inventory_result {
	HFX_PAIRING_INVENTORY_OK = 0,
	HFX_PAIRING_INVENTORY_LENGTH,
	HFX_PAIRING_INVENTORY_BAD_REPORT_ID,
	HFX_PAIRING_INVENTORY_BAD_ANCHOR,
	HFX_PAIRING_INVENTORY_BAD_PADDING,
	HFX_PAIRING_INVENTORY_BAD_MARKER,
	HFX_PAIRING_INVENTORY_BAD_HEADER,
};

enum hfx_pairing_inventory_slot_state {
	HFX_PAIRING_INVENTORY_SLOT_UNPROVEN = 0,
	HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID,
};

struct hfx_pairing_inventory {
	unsigned short pointer_raw_product_id;
	unsigned short keyboard_raw_product_id;
	enum hfx_pairing_inventory_slot_state pointer_state;
	enum hfx_pairing_inventory_slot_state keyboard_state;
};

static inline enum hfx_pairing_inventory_slot_state
hfx_pairing_inventory_classify_slot(unsigned short product_id)
{
	/* These raw values are unproven; neither is assumed to mean empty. */
	if (product_id == 0 || product_id == 0xffffU)
		return HFX_PAIRING_INVENTORY_SLOT_UNPROVEN;
	return HFX_PAIRING_INVENTORY_SLOT_PRODUCT_ID;
}

static inline enum hfx_pairing_inventory_result
hfx_pairing_inventory_parse(const unsigned char *report, size_t length,
			    struct hfx_pairing_inventory *inventory)
{
	static const unsigned char anchor_prefix[16] = {
		0x00, 0x00, 0xa5, 0x85, 0x01, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x26, 0x00,
	};
	static const unsigned char tail_header[16] = {
		0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0xdf, 0xa6,
		0x33, 0x4e, 0x01, 0x00, 0x04, 0xa3, 0x22, 0x00,
	};
	const size_t anchor = HFX_PAIRING_INVENTORY_ANCHOR;
	unsigned short pointer_product_id;
	unsigned short keyboard_product_id;
	size_t index;

	if (!report || !inventory ||
	    length != HFX_PAIRING_INVENTORY_REPORT_BYTES)
		return HFX_PAIRING_INVENTORY_LENGTH;
	if (report[0] != HFX_PAIRING_INVENTORY_REPORT_ID)
		return HFX_PAIRING_INVENTORY_BAD_REPORT_ID;
	if (memcmp(&report[anchor - sizeof(anchor_prefix)], anchor_prefix,
		   sizeof(anchor_prefix)))
		return HFX_PAIRING_INVENTORY_BAD_ANCHOR;
	for (index = anchor + 15; index < anchor + 20; index++)
		if (report[index] != 0)
			return HFX_PAIRING_INVENTORY_BAD_PADDING;
	if (report[anchor + 20] != 0xff || report[anchor + 21] != 0xff)
		return HFX_PAIRING_INVENTORY_BAD_MARKER;
	if (memcmp(&report[anchor + 22], tail_header, sizeof(tail_header)))
		return HFX_PAIRING_INVENTORY_BAD_HEADER;

	pointer_product_id = ((unsigned short)report[anchor + 38] << 8) |
		report[anchor + 39];
	keyboard_product_id = ((unsigned short)report[anchor + 40] << 8) |
		report[anchor + 41];

	inventory->pointer_raw_product_id = pointer_product_id;
	inventory->keyboard_raw_product_id = keyboard_product_id;
	inventory->pointer_state =
		hfx_pairing_inventory_classify_slot(pointer_product_id);
	inventory->keyboard_state =
		hfx_pairing_inventory_classify_slot(keyboard_product_id);
	return HFX_PAIRING_INVENTORY_OK;
}

#endif
