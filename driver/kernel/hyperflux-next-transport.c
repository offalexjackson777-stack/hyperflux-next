// SPDX-License-Identifier: GPL-2.0-only

#include <linux/delay.h>
#include <linux/slab.h>
#include <linux/uaccess.h>

#include "hyperflux-next-internal.h"
#include "hyperflux-next-wire.h"

#define HFX_CONTROL_REQUEST_TYPE 0x21
#define HFX_CONTROL_REQUEST 0x09
#define HFX_CONTROL_VALUE 0x0300
#define HFX_CONTROL_INDEX 0x0000
#define HFX_CONTROL_TIMEOUT_MS 1000

static int hfx_wire_error(enum hfx_wire_validation validation)
{
	switch (validation) {
	case HFX_WIRE_VALID:
		return 0;
	case HFX_WIRE_BACKEND:
	case HFX_WIRE_KIND:
		return -EOPNOTSUPP;
	case HFX_WIRE_LENGTH:
	case HFX_WIRE_DELAY:
		return -E2BIG;
	case HFX_WIRE_FLAGS:
	case HFX_WIRE_TRAILING_DATA:
	case HFX_WIRE_HEADER:
	case HFX_WIRE_GEOMETRY:
	case HFX_WIRE_UNUSED_DATA:
	case HFX_WIRE_CHECKSUM:
		return -EINVAL;
	}
	return -EINVAL;
}

static int hfx_submit_validate(const struct hfx_uapi_submit *submit,
			       u32 backend_id)
{
	u64 total_delay_us = 0;
	u32 index;
	int ret;

	if (submit->version != HFX_UAPI_ABI_VERSION ||
	    submit->size != sizeof(*submit))
		return -EPROTO;
	if (!submit->dispatch_nonce ||
	    !memchr_inv(submit->request_digest, 0,
			sizeof(submit->request_digest)))
		return -EINVAL;
	if (!submit->frame_count || submit->frame_count > HFX_UAPI_MAX_FRAMES)
		return -E2BIG;
	if (submit->flags || submit->kernel_sequence)
		return -EINVAL;
	for (index = 0; index < submit->frame_count; index++) {
		ret = hfx_wire_error(
			hfx_wire_validate_frame(&submit->frames[index], backend_id));
		if (ret)
			return ret;
		total_delay_us += submit->frames[index].delay_after_us;
		if (total_delay_us > HFX_UAPI_MAX_TRANSACTION_DELAY_US)
			return -E2BIG;
	}
	if (submit->frame_count < HFX_UAPI_MAX_FRAMES &&
	    memchr_inv(&submit->frames[submit->frame_count], 0,
		       sizeof(submit->frames[0]) *
			       (HFX_UAPI_MAX_FRAMES - submit->frame_count)))
		return -EINVAL;
	if (submit->frames[submit->frame_count - 1U].delay_after_us != 0)
		return -EINVAL;
	return 0;
}

static bool hfx_result_key_matches(const struct hfx_uapi_transaction_result *result,
				   u64 generation, u64 epoch, u64 nonce)
{
	return result->receiver_generation == generation &&
		result->authorization_epoch == epoch &&
		result->dispatch_nonce == nonce;
}

static struct hfx_result_record *
hfx_result_find(struct hfx_receiver *receiver, u64 generation, u64 epoch,
		u64 nonce)
{
	struct hfx_result_record *record;

	list_for_each_entry(record, &receiver->result_records, node) {
		if (hfx_result_key_matches(&record->result, generation, epoch,
					   nonce))
			return record;
	}
	return NULL;
}

static struct hfx_result_tombstone *
hfx_tombstone_find(struct hfx_receiver *receiver, u64 generation, u64 epoch,
		   u64 nonce)
{
	u32 index;

	for (index = 0; index < HFX_TOMBSTONE_CAPACITY; index++) {
		struct hfx_result_tombstone *tombstone =
			&receiver->result_tombstones[index];

		if (tombstone->valid &&
		    tombstone->receiver_generation == generation &&
		    tombstone->authorization_epoch == epoch &&
		    tombstone->dispatch_nonce == nonce)
			return tombstone;
	}
	return NULL;
}

static bool hfx_record_matches_submit(const struct hfx_result_record *record,
				      const struct hfx_uapi_submit *submit)
{
	return !memcmp(record->result.request_digest, submit->request_digest,
		       sizeof(record->result.request_digest)) &&
		record->frame_count == submit->frame_count &&
		!memcmp(record->frames, submit->frames, sizeof(record->frames));
}

static void hfx_remember_tombstone(struct hfx_receiver *receiver,
				   const struct hfx_result_record *record)
{
	struct hfx_result_tombstone *tombstone =
		&receiver->result_tombstones[receiver->tombstone_head];

	*tombstone = (struct hfx_result_tombstone) {
		.valid = true,
		.receiver_generation = record->result.receiver_generation,
		.authorization_epoch = record->result.authorization_epoch,
		.dispatch_nonce = record->result.dispatch_nonce,
	};
	memcpy(tombstone->request_digest, record->result.request_digest,
	       sizeof(tombstone->request_digest));
	receiver->tombstone_head =
		(receiver->tombstone_head + 1U) % HFX_TOMBSTONE_CAPACITY;
	if (receiver->tombstone_count < HFX_TOMBSTONE_CAPACITY)
		receiver->tombstone_count++;
}

static int hfx_ensure_result_capacity(struct hfx_receiver *receiver)
{
	struct hfx_result_record *record;

	if (receiver->result_count < HFX_RESULT_CAPACITY)
		return 0;
	record = list_first_entry_or_null(&receiver->result_records,
					  struct hfx_result_record, node);
	if (!record ||
	    (record->result.status != HFX_UAPI_TRANSPORT_STATUS_SUCCEEDED &&
	     record->result.status != HFX_UAPI_TRANSPORT_STATUS_FAILED &&
	     record->result.status != HFX_UAPI_TRANSPORT_STATUS_REVOKED))
		return -EBUSY;
	hfx_remember_tombstone(receiver, record);
	list_del(&record->node);
	receiver->result_count--;
	kfree(record);
	return 0;
}

static int hfx_record_return_code(const struct hfx_result_record *record)
{
	if (record->result.status == HFX_UAPI_TRANSPORT_STATUS_SUCCEEDED)
		return 0;
	if (record->result.status == HFX_UAPI_TRANSPORT_STATUS_RESERVED ||
	    record->result.status == HFX_UAPI_TRANSPORT_STATUS_STARTED)
		return -EBUSY;
	if (record->result.transport_errno)
		return record->result.transport_errno;
	return -EIO;
}

static void hfx_transport_delay(u32 delay_us)
{
	if (!delay_us)
		return;
	if (delay_us >= 20000U)
		msleep(DIV_ROUND_UP(delay_us, 1000U));
	else
		usleep_range(delay_us, delay_us + min(delay_us / 10U + 1U,
						  1000U));
}

static int hfx_usb_send(struct hfx_receiver *receiver,
			const struct hfx_uapi_frame *frame, u8 *transfer)
{
	int ret;

	memcpy(transfer, frame->payload, frame->payload_length);
	ret = usb_control_msg(receiver->udev,
			      usb_sndctrlpipe(receiver->udev, 0),
			      HFX_CONTROL_REQUEST, HFX_CONTROL_REQUEST_TYPE,
			      HFX_CONTROL_VALUE, HFX_CONTROL_INDEX, transfer,
			      frame->payload_length, HFX_CONTROL_TIMEOUT_MS);
	if (ret < 0)
		return ret;
	return ret == (int)frame->payload_length ? 0 : -EIO;
}

static u32 hfx_current_revoke_reason(struct hfx_receiver *receiver)
{
	unsigned long flags;
	u32 reason;

	spin_lock_irqsave(&receiver->authorization_lock, flags);
	reason = receiver->session_revoke_reason;
	spin_unlock_irqrestore(&receiver->authorization_lock, flags);
	return reason;
}

static int hfx_record_dispatch(struct hfx_receiver *receiver,
			       struct hfx_file_context *context,
			       struct hfx_result_record *record)
{
	struct usb_interface *control_interface;
	u8 *transfer;
	u32 index;
	int ret;

	transfer = kmalloc(HFX_HW001_REPORT_BYTES, GFP_KERNEL);
	if (!transfer) {
		ret = -ENOMEM;
		goto failed_safe;
	}
	control_interface = usb_ifnum_to_if(receiver->udev, 0);
	if (!control_interface) {
		ret = -ENODEV;
		goto failed_free;
	}
	ret = usb_autopm_get_interface(control_interface);
	if (ret)
		goto failed_free;

	for (index = 0; index < record->frame_count; index++) {
		ret = hfx_session_check(receiver, context,
					record->result.receiver_generation,
					record->result.authorization_epoch);
		if (ret)
			goto revoked;
		record->result.status = HFX_UAPI_TRANSPORT_STATUS_STARTED;
		record->result.flags |= HFX_UAPI_RESULT_FLAG_WRITE_STARTED;
		ret = hfx_usb_send(receiver, &record->frames[index], transfer);
		if (ret)
			goto transport_failed;
		record->result.frames_completed++;
		hfx_transport_delay(record->frames[index].delay_after_us);
	}
	record->result.status = HFX_UAPI_TRANSPORT_STATUS_SUCCEEDED;
	record->result.failed_frame = 0;
	record->result.transport_errno = 0;
	usb_mark_last_busy(receiver->udev);
	usb_autopm_put_interface(control_interface);
	memzero_explicit(transfer, HFX_HW001_REPORT_BYTES);
	kfree(transfer);
	return 0;

transport_failed:
	hfx_session_revoke(receiver,
			   HFX_UAPI_REVOKE_REASON_TRANSPORT_FAILURE);
	record->result.status = HFX_UAPI_TRANSPORT_STATUS_FAILED;
	goto failed_pm;
revoked:
	record->result.status = HFX_UAPI_TRANSPORT_STATUS_REVOKED;
failed_pm:
	record->result.failed_frame = index + 1U;
	record->result.transport_errno = ret;
	record->result.revoke_reason = hfx_current_revoke_reason(receiver);
	usb_mark_last_busy(receiver->udev);
	usb_autopm_put_interface(control_interface);
failed_free:
	memzero_explicit(transfer, HFX_HW001_REPORT_BYTES);
	kfree(transfer);
failed_safe:
	if ((record->result.flags & HFX_UAPI_RESULT_FLAG_WRITE_STARTED) == 0) {
		record->result.flags |=
			HFX_UAPI_RESULT_FLAG_AUTOMATIC_RETRY_SAFE;
		if (record->result.status !=
		    HFX_UAPI_TRANSPORT_STATUS_REVOKED)
			record->result.status =
				HFX_UAPI_TRANSPORT_STATUS_FAILED;
		record->result.transport_errno = ret;
		record->result.failed_frame = 1;
	}
	return ret;
}

static int hfx_submit_reserve(struct hfx_receiver *receiver,
			      struct hfx_file_context *context,
			      const struct hfx_uapi_submit *submit,
			      struct hfx_result_record *record)
{
	unsigned long flags;
	int ret;

	ret = hfx_ensure_result_capacity(receiver);
	if (ret)
		return ret;
	spin_lock_irqsave(&receiver->authorization_lock, flags);
	if (receiver->writer_context != context || !receiver->session_active ||
	    receiver->authorization_epoch != submit->authorization_epoch ||
	    receiver->generation != submit->receiver_generation) {
		ret = -EKEYREVOKED;
	} else if (submit->dispatch_nonce <=
		   receiver->session_max_dispatch_nonce) {
		ret = -ENODATA;
	} else if (receiver->next_kernel_sequence == U64_MAX) {
		ret = -EOVERFLOW;
	} else {
		receiver->session_max_dispatch_nonce = submit->dispatch_nonce;
		receiver->next_kernel_sequence++;
		record->result.kernel_sequence = receiver->next_kernel_sequence;
		ret = 0;
	}
	spin_unlock_irqrestore(&receiver->authorization_lock, flags);
	if (ret)
		return ret;

	record->result.version = HFX_UAPI_ABI_VERSION;
	record->result.size = sizeof(record->result);
	record->result.receiver_generation = submit->receiver_generation;
	record->result.authorization_epoch = submit->authorization_epoch;
	record->result.dispatch_nonce = submit->dispatch_nonce;
	memcpy(record->result.request_digest, submit->request_digest,
	       sizeof(record->result.request_digest));
	record->result.status = HFX_UAPI_TRANSPORT_STATUS_RESERVED;
	record->result.frames_planned = submit->frame_count;
	record->frame_count = submit->frame_count;
	memcpy(record->frames, submit->frames, sizeof(record->frames));
	list_add_tail(&record->node, &receiver->result_records);
	receiver->result_count++;
	return 0;
}

long hfx_transport_submit(struct hfx_receiver *receiver,
			  struct hfx_file_context *context,
			  void __user *user_arg)
{
	struct hfx_uapi_submit *submit;
	struct hfx_result_record *record;
	struct hfx_result_tombstone *tombstone;
	long ret;

	if (!context->writer)
		return -EACCES;
	submit = kmalloc(sizeof(*submit), GFP_KERNEL);
	if (!submit)
		return -ENOMEM;
	if (copy_from_user(submit, user_arg, sizeof(*submit))) {
		ret = -EFAULT;
		goto out_submit;
	}
	ret = hfx_submit_validate(submit, receiver->backend_id);
	if (ret)
		goto out_submit;
	if (submit->receiver_generation != receiver->generation) {
		ret = -ESTALE;
		goto out_submit;
	}

	mutex_lock(&receiver->transport_lock);
	ret = hfx_session_check(receiver, context,
				submit->receiver_generation,
				submit->authorization_epoch);
	if (ret)
		goto out_unlock;
	record = hfx_result_find(receiver, submit->receiver_generation,
				 submit->authorization_epoch,
				 submit->dispatch_nonce);
	if (record) {
		if (!hfx_record_matches_submit(record, submit)) {
			hfx_session_revoke(
				receiver, HFX_UAPI_REVOKE_REASON_SERVICE_LOSS);
			ret = -EEXIST;
			goto out_unlock;
		}
		submit->kernel_sequence = record->result.kernel_sequence;
		ret = hfx_record_return_code(record);
		goto out_copy;
	}
	tombstone = hfx_tombstone_find(receiver,
				       submit->receiver_generation,
				       submit->authorization_epoch,
				       submit->dispatch_nonce);
	if (tombstone) {
		ret = !memcmp(tombstone->request_digest, submit->request_digest,
			      sizeof(tombstone->request_digest)) ?
		      -ENODATA :
		      -EEXIST;
		goto out_unlock;
	}

	record = kzalloc(sizeof(*record), GFP_KERNEL);
	if (!record) {
		ret = -ENOMEM;
		goto out_unlock;
	}
	INIT_LIST_HEAD(&record->node);
	ret = hfx_submit_reserve(receiver, context, submit, record);
	if (ret) {
		kfree(record);
		goto out_unlock;
	}
	submit->kernel_sequence = record->result.kernel_sequence;
	ret = hfx_record_dispatch(receiver, context, record);
out_copy:
	if (copy_to_user(user_arg, submit, sizeof(*submit))) {
		hfx_session_revoke(receiver,
				   HFX_UAPI_REVOKE_REASON_SERVICE_LOSS);
		ret = -EFAULT;
	}
out_unlock:
	mutex_unlock(&receiver->transport_lock);
out_submit:
	memzero_explicit(submit, sizeof(*submit));
	kfree(submit);
	return ret;
}

static void hfx_result_synthetic(
	struct hfx_uapi_transaction_result *result, u32 status, u32 flags)
{
	result->kernel_sequence = 0;
	result->status = status;
	result->frames_planned = 0;
	result->frames_completed = 0;
	result->failed_frame = 0;
	result->transport_errno = 0;
	result->revoke_reason = HFX_UAPI_REVOKE_REASON_NONE;
	result->flags = flags;
}

long hfx_transport_get_result(struct hfx_receiver *receiver,
			      struct hfx_file_context *context,
			      void __user *user_arg)
{
	struct hfx_uapi_transaction_result query;
	struct hfx_result_record *record;
	struct hfx_result_tombstone *tombstone;
	bool not_observed;
	long ret = 0;

	if (!context->writer)
		return -EACCES;
	if (copy_from_user(&query, user_arg, sizeof(query)))
		return -EFAULT;
	if (query.version != HFX_UAPI_ABI_VERSION || query.size != sizeof(query))
		return -EPROTO;
	if (query.receiver_generation != receiver->generation)
		return -ESTALE;
	if (!query.dispatch_nonce ||
	    !memchr_inv(query.request_digest, 0, sizeof(query.request_digest)) ||
	    query.status || query.frames_planned || query.frames_completed ||
	    query.failed_frame || query.transport_errno || query.revoke_reason ||
	    query.flags)
		return -EINVAL;

	mutex_lock(&receiver->transport_lock);
	record = hfx_result_find(receiver, query.receiver_generation,
				 query.authorization_epoch,
				 query.dispatch_nonce);
	if (record) {
		if (memcmp(record->result.request_digest, query.request_digest,
			   sizeof(query.request_digest)) ||
		    (query.kernel_sequence && query.kernel_sequence !=
					      record->result.kernel_sequence)) {
			hfx_result_synthetic(&query,
					     HFX_UAPI_TRANSPORT_STATUS_CONFLICT, 0);
		} else {
			query = record->result;
		}
		goto out_copy;
	}
	tombstone = hfx_tombstone_find(receiver, query.receiver_generation,
				       query.authorization_epoch,
				       query.dispatch_nonce);
	if (tombstone) {
		hfx_result_synthetic(
			&query,
			memcmp(tombstone->request_digest, query.request_digest,
			       sizeof(query.request_digest)) ?
				HFX_UAPI_TRANSPORT_STATUS_CONFLICT :
				HFX_UAPI_TRANSPORT_STATUS_EVICTED,
			0);
		goto out_copy;
	}

	not_observed = hfx_session_nonce_not_observed(
		receiver, context, query.authorization_epoch,
		query.dispatch_nonce);
	hfx_result_synthetic(
		&query,
		not_observed ? HFX_UAPI_TRANSPORT_STATUS_NOT_OBSERVED :
			       HFX_UAPI_TRANSPORT_STATUS_UNAVAILABLE,
		not_observed ? HFX_UAPI_RESULT_FLAG_AUTOMATIC_RETRY_SAFE : 0);
out_copy:
	if (copy_to_user(user_arg, &query, sizeof(query)))
		ret = -EFAULT;
	mutex_unlock(&receiver->transport_lock);
	return ret;
}

void hfx_transport_free_journal(struct hfx_receiver *receiver)
{
	struct hfx_result_record *record;
	struct hfx_result_record *next;

	list_for_each_entry_safe(record, next, &receiver->result_records, node) {
		list_del(&record->node);
		memzero_explicit(record, sizeof(*record));
		kfree(record);
	}
	receiver->result_count = 0;
}
