// SPDX-License-Identifier: GPL-2.0-only

#include <linux/ktime.h>
#include <linux/slab.h>
#include <linux/uaccess.h>

#include "hyperflux-next-internal.h"
#include "hyperflux-next-passive.h"

static void hfx_observation_append_locked(struct hfx_receiver *receiver,
					  u64 now_ns, u32 kind,
					  u32 endpoint_slot, u32 source,
					  u32 confidence, u32 value,
					  u32 auxiliary)
{
	struct hfx_uapi_observation *observation;

	if (receiver->next_observation_sequence == U64_MAX)
		return;
	receiver->next_observation_sequence++;
	observation = &receiver->observation_ring[receiver->observation_head];
	observation->sequence = receiver->next_observation_sequence;
	observation->observed_boottime_ns = now_ns;
	observation->kind = kind;
	observation->endpoint_slot = endpoint_slot;
	observation->source = source;
	observation->confidence = confidence;
	observation->value = value;
	observation->auxiliary = auxiliary;
	receiver->observation_head =
		(receiver->observation_head + 1U) % HFX_OBSERVATION_CAPACITY;
	if (receiver->observation_count < HFX_OBSERVATION_CAPACITY)
		receiver->observation_count++;
}

void hfx_observation_emit(struct hfx_receiver *receiver, u32 kind,
			  u32 endpoint_slot, u32 source, u32 confidence,
			  u32 value, u32 auxiliary)
{
	unsigned long flags;

	spin_lock_irqsave(&receiver->observation_lock, flags);
	hfx_observation_append_locked(receiver, ktime_get_boottime_ns(), kind,
				      endpoint_slot, source, confidence, value,
				      auxiliary);
	spin_unlock_irqrestore(&receiver->observation_lock, flags);
}

void hfx_observe_activity(struct hfx_receiver *receiver, int interface_number)
{
	unsigned long flags;
	u64 now_ns = ktime_get_boottime_ns();
	u32 slot;

	if (interface_number == HFX_POINTER_INPUT_INTERFACE)
		slot = HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE;
	else if (interface_number == HFX_KEYBOARD_INPUT_INTERFACE)
		slot = HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE;
	else
		return;

	spin_lock_irqsave(&receiver->observation_lock, flags);
	if (receiver->last_activity_boottime_ns[slot] != 0 &&
	    now_ns - receiver->last_activity_boottime_ns[slot] <
		    HFX_ACTIVITY_INTERVAL_NS)
		goto out;
	receiver->last_activity_boottime_ns[slot] = now_ns;
	hfx_observation_append_locked(
		receiver, now_ns, HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ACTIVITY,
		slot, HFX_UAPI_OBSERVATION_SOURCE_HID_INPUT,
		HFX_UAPI_OBSERVATION_CONFIDENCE_OBSERVED, 1, 0);
out:
	spin_unlock_irqrestore(&receiver->observation_lock, flags);
}

static bool hfx_observe_product_id_locked(struct hfx_receiver *receiver,
					  u64 now_ns, u32 slot,
					  u16 product_id)
{
	if (receiver->observed_product_id_valid[slot] &&
	    receiver->observed_product_id[slot] != product_id) {
		receiver->identity_conflict = true;
		hfx_observation_append_locked(
			receiver, now_ns,
			HFX_UAPI_OBSERVATION_KIND_IDENTITY_CONFLICT, slot,
			HFX_UAPI_OBSERVATION_SOURCE_HID_PASSIVE,
			HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT,
			receiver->observed_product_id[slot], product_id);
		return true;
	}
	receiver->observed_product_id[slot] = product_id;
	receiver->observed_product_id_valid[slot] = true;
	hfx_observation_append_locked(
		receiver, now_ns,
		HFX_UAPI_OBSERVATION_KIND_ENDPOINT_PRODUCT_ID, slot,
		HFX_UAPI_OBSERVATION_SOURCE_HID_PASSIVE,
		HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT, product_id, 0);
	return false;
}

bool hfx_observation_identity_conflicted(struct hfx_receiver *receiver)
{
	unsigned long flags;
	bool conflicted;

	spin_lock_irqsave(&receiver->observation_lock, flags);
	conflicted = receiver->identity_conflict;
	spin_unlock_irqrestore(&receiver->observation_lock, flags);
	return conflicted;
}

void hfx_observe_passive(struct hfx_receiver *receiver, int interface_number,
			 const u8 *data, int size)
{
	struct hfx_passive_report report;
	unsigned long flags;
	u64 now_ns;
	u32 slot;
	bool conflict = false;

	if (hfx_passive_parse(interface_number, data, size, &report) <=
	    HFX_PASSIVE_NONE || report.kind == HFX_PASSIVE_INVALID)
		return;
	now_ns = ktime_get_boottime_ns();
	spin_lock_irqsave(&receiver->observation_lock, flags);
	switch (report.kind) {
	case HFX_PASSIVE_POINTER_IDENTITY:
		slot = HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE;
		conflict = hfx_observe_product_id_locked(receiver, now_ns, slot,
							 report.product_id);
		hfx_observation_append_locked(
			receiver, now_ns,
			HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CONTACT_RAW, slot,
			HFX_UAPI_OBSERVATION_SOURCE_HID_PASSIVE,
			HFX_UAPI_OBSERVATION_CONFIDENCE_RAW, report.contact_raw, 0);
		break;
	case HFX_PASSIVE_POINTER_STATUS:
		slot = HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE;
		hfx_observation_append_locked(
			receiver, now_ns,
			HFX_UAPI_OBSERVATION_KIND_ENDPOINT_BATTERY_RAW, slot,
			HFX_UAPI_OBSERVATION_SOURCE_HID_PASSIVE,
			HFX_UAPI_OBSERVATION_CONFIDENCE_RAW, report.battery_raw,
			report.status_raw);
		hfx_observation_append_locked(
			receiver, now_ns,
			HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CONTACT_RAW, slot,
			HFX_UAPI_OBSERVATION_SOURCE_HID_PASSIVE,
			HFX_UAPI_OBSERVATION_CONFIDENCE_RAW, report.contact_raw, 0);
		hfx_observation_append_locked(
			receiver, now_ns,
			HFX_UAPI_OBSERVATION_KIND_ENDPOINT_CHARGE_RAW, slot,
			HFX_UAPI_OBSERVATION_SOURCE_HID_PASSIVE,
			HFX_UAPI_OBSERVATION_CONFIDENCE_RAW, report.charge_raw, 0);
		break;
	case HFX_PASSIVE_KEYBOARD_IDENTITY:
		slot = HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE;
		conflict = hfx_observe_product_id_locked(receiver, now_ns, slot,
							 report.product_id);
		hfx_observation_append_locked(
			receiver, now_ns,
			HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ROUTE_RAW, slot,
			HFX_UAPI_OBSERVATION_SOURCE_HID_PASSIVE,
			HFX_UAPI_OBSERVATION_CONFIDENCE_RAW, report.route_raw,
			report.status_raw);
		break;
	case HFX_PASSIVE_KEYBOARD_STATUS:
		slot = HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE;
		hfx_observation_append_locked(
			receiver, now_ns,
			HFX_UAPI_OBSERVATION_KIND_ENDPOINT_BATTERY_RAW, slot,
			HFX_UAPI_OBSERVATION_SOURCE_HID_PASSIVE,
			HFX_UAPI_OBSERVATION_CONFIDENCE_RAW, report.battery_raw,
			report.status_raw);
		hfx_observation_append_locked(
			receiver, now_ns,
			HFX_UAPI_OBSERVATION_KIND_ENDPOINT_ROUTE_RAW, slot,
			HFX_UAPI_OBSERVATION_SOURCE_HID_PASSIVE,
			HFX_UAPI_OBSERVATION_CONFIDENCE_RAW, report.route_raw,
			report.status_raw);
		break;
	case HFX_PASSIVE_NONE:
	case HFX_PASSIVE_INVALID:
		break;
	}
	spin_unlock_irqrestore(&receiver->observation_lock, flags);
	if (conflict)
		hfx_session_revoke(receiver,
				   HFX_UAPI_REVOKE_REASON_GENERATION_CHANGE);
}

long hfx_observation_read(struct hfx_receiver *receiver, void __user *user_arg)
{
	struct hfx_uapi_read_observations *query;
	unsigned long spin_flags;
	u32 start;
	u32 offset;
	u64 oldest = 0;
	u64 latest = 0;
	long ret = 0;

	query = kzalloc(sizeof(*query), GFP_KERNEL);
	if (!query)
		return -ENOMEM;
	if (copy_from_user(query, user_arg, sizeof(*query))) {
		ret = -EFAULT;
		goto out;
	}
	if (query->version != HFX_UAPI_ABI_VERSION ||
	    query->size != sizeof(*query)) {
		ret = -EPROTO;
		goto out;
	}
	if (query->receiver_generation != receiver->generation) {
		ret = -ESTALE;
		goto out;
	}
	if (query->oldest_sequence || query->latest_sequence || query->flags ||
	    query->count ||
	    memchr_inv(query->observations, 0, sizeof(query->observations))) {
		ret = -EINVAL;
		goto out;
	}

	spin_lock_irqsave(&receiver->observation_lock, spin_flags);
	latest = receiver->next_observation_sequence;
	if (query->after_sequence > latest) {
		ret = -ERANGE;
		goto unlock;
	}
	if (receiver->observation_count != 0) {
		start = (receiver->observation_head + HFX_OBSERVATION_CAPACITY -
			 receiver->observation_count) % HFX_OBSERVATION_CAPACITY;
		oldest = receiver->observation_ring[start].sequence;
		if (query->after_sequence != 0 &&
		    query->after_sequence < oldest &&
		    oldest - query->after_sequence > 1U)
			query->flags |=
				HFX_UAPI_OBSERVATION_BATCH_FLAG_CURSOR_GAP;
		for (offset = 0;
		     offset < receiver->observation_count &&
		     query->count < HFX_UAPI_MAX_OBSERVATIONS;
		     offset++) {
			const struct hfx_uapi_observation *observation =
				&receiver->observation_ring[
					(start + offset) % HFX_OBSERVATION_CAPACITY];

			if (observation->sequence <= query->after_sequence)
				continue;
			query->observations[query->count++] = *observation;
		}
	}
	query->oldest_sequence = oldest;
	query->latest_sequence = latest;
unlock:
	spin_unlock_irqrestore(&receiver->observation_lock, spin_flags);
	if (!ret && copy_to_user(user_arg, query, sizeof(*query)))
		ret = -EFAULT;
out:
	kfree(query);
	return ret;
}
