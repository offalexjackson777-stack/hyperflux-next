// SPDX-License-Identifier: GPL-2.0-only

#include <linux/delay.h>
#include <linux/slab.h>
#include <linux/uaccess.h>

#include "hyperflux-next-internal.h"

#define HFX_NATIVE_SET_REQUEST_TYPE 0x21
#define HFX_NATIVE_SET_REQUEST 0x09
#define HFX_NATIVE_GET_REQUEST_TYPE 0xa1
#define HFX_NATIVE_GET_REQUEST 0x01
#define HFX_NATIVE_REPORT_VALUE 0x0300
#define HFX_NATIVE_TIMEOUT_MS \
	DIV_ROUND_UP_ULL(HFX_UAPI_NATIVE_CONTROL_TIMEOUT_NS, NSEC_PER_MSEC)

static bool hfx_native_profile_valid(const struct hfx_receiver_profile *profile)
{
	u16 previous_end = 0;
	u16 index;

	if (!profile ||
	    profile->native_exchange_kind !=
		    HFX_NATIVE_EXCHANGE_RAZER_FEATURE_REPORT_V1 ||
	    profile->native_report_bytes != HFX_UAPI_NATIVE_REPORT_BYTES ||
	    !profile->native_max_response_attempts ||
	    profile->native_max_response_attempts > 16U ||
	    !profile->native_match_range_count ||
	    profile->native_match_range_count > HFX_NATIVE_MATCH_RANGE_CAPACITY ||
	    profile->native_request_status_offset >= profile->native_report_bytes ||
	    profile->native_response_status_offset >= profile->native_report_bytes ||
	    profile->native_response_status_minimum >
		    profile->native_response_status_maximum ||
	    profile->native_protocol_type_offset >= profile->native_report_bytes ||
	    profile->native_data_size_offset >= profile->native_report_bytes ||
	    profile->native_checksum_kind != HFX_NATIVE_CHECKSUM_XOR8 ||
	    profile->native_checksum_start_offset >=
		    profile->native_checksum_end_offset ||
	    profile->native_checksum_end_offset > profile->native_report_bytes ||
	    profile->native_checksum_result_offset >= profile->native_report_bytes ||
	    (profile->native_checksum_result_offset >=
		     profile->native_checksum_start_offset &&
	     profile->native_checksum_result_offset <
		     profile->native_checksum_end_offset) ||
	    profile->native_trailer_offset >= profile->native_report_bytes ||
	    profile->native_transaction_id_offset >= profile->native_report_bytes ||
	    (profile->native_route_selector_kind ==
		     HFX_NATIVE_ROUTE_SELECTOR_NONE &&
	     (profile->native_pointer_route_selector ||
	      profile->native_keyboard_route_selector ||
	      profile->native_restore_original_response)) ||
	    (profile->native_route_selector_kind !=
		     HFX_NATIVE_ROUTE_SELECTOR_NONE &&
	     (profile->native_route_selector_kind !=
		      HFX_NATIVE_ROUTE_SELECTOR_TRANSACTION_ID ||
	      !profile->native_pointer_route_selector ||
	      !profile->native_keyboard_route_selector ||
	      profile->native_pointer_route_selector ==
		      profile->native_keyboard_route_selector ||
	      !profile->native_restore_original_response)))
		return false;

	for (index = 0; index < profile->native_match_range_count; index++) {
		const struct hfx_native_match_range *range =
			&profile->native_match_ranges[index];

		if (!range->length || range->offset < previous_end ||
		    range->offset + range->length > profile->native_report_bytes)
			return false;
		previous_end = range->offset + range->length;
	}
	return true;
}

static u8 hfx_native_checksum(
	const struct hfx_receiver_profile *profile, const u8 *report)
{
	u8 checksum = 0;
	u16 index;

	for (index = profile->native_checksum_start_offset;
	     index < profile->native_checksum_end_offset; index++)
		checksum ^= report[index];
	return checksum;
}

static bool hfx_native_checksum_valid(
	const struct hfx_receiver_profile *profile, const u8 *report)
{
	return profile->native_checksum_kind == HFX_NATIVE_CHECKSUM_XOR8 &&
		hfx_native_checksum(profile, report) ==
			report[profile->native_checksum_result_offset];
}

static void hfx_native_checksum_update(
	const struct hfx_receiver_profile *profile, u8 *report)
{
	report[profile->native_checksum_result_offset] =
		hfx_native_checksum(profile, report);
}

static bool hfx_native_request_report_valid(
	const struct hfx_receiver_profile *profile, const u8 *report)
{
	return report[profile->native_request_status_offset] ==
		       profile->native_request_status_value &&
		report[profile->native_protocol_type_offset] ==
		       profile->native_protocol_type_value &&
		report[profile->native_data_size_offset] <=
		       profile->native_max_data_size &&
		hfx_native_checksum_valid(profile, report) &&
		report[profile->native_trailer_offset] ==
		       profile->native_trailer_value;
}

static bool hfx_native_response_report_valid(
	const struct hfx_receiver_profile *profile, const u8 *report)
{
	u8 status = report[profile->native_response_status_offset];

	return status >= profile->native_response_status_minimum &&
		status <= profile->native_response_status_maximum &&
		report[profile->native_protocol_type_offset] ==
		       profile->native_protocol_type_value &&
		report[profile->native_data_size_offset] <=
		       profile->native_max_data_size &&
		hfx_native_checksum_valid(profile, report) &&
		report[profile->native_trailer_offset] ==
		       profile->native_trailer_value;
}

static int hfx_native_route_selector(
	const struct hfx_receiver_profile *profile, u32 endpoint_slot,
	u8 *selector)
{
	if (profile->native_route_selector_kind ==
	    HFX_NATIVE_ROUTE_SELECTOR_NONE)
		return 0;
	if (profile->native_route_selector_kind !=
	    HFX_NATIVE_ROUTE_SELECTOR_TRANSACTION_ID)
		return -EOPNOTSUPP;
	if (endpoint_slot == HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE)
		*selector = profile->native_pointer_route_selector;
	else if (endpoint_slot == HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE)
		*selector = profile->native_keyboard_route_selector;
	else
		return -EINVAL;
	return 1;
}

static int hfx_native_route_request(
	const struct hfx_receiver_profile *profile, u32 endpoint_slot,
	u8 *request, u8 *application_selector, bool *translated)
{
	u16 offset = profile->native_transaction_id_offset;
	u8 receiver_selector = 0;
	int ret;

	*application_selector = request[offset];
	*translated = false;
	ret = hfx_native_route_selector(profile, endpoint_slot,
					&receiver_selector);
	if (ret <= 0)
		return ret;
	if (request[offset] == receiver_selector)
		return 0;
	request[offset] = receiver_selector;
	if (offset >= profile->native_checksum_start_offset &&
	    offset < profile->native_checksum_end_offset)
		hfx_native_checksum_update(profile, request);
	if (!hfx_native_request_report_valid(profile, request))
		return -EINVAL;
	*translated = true;
	return 0;
}

static int hfx_native_restore_response(
	const struct hfx_receiver_profile *profile, u8 application_selector,
	u8 receiver_selector, bool translated, u8 *response)
{
	u16 offset = profile->native_transaction_id_offset;

	if (!translated)
		return 0;
	if (!profile->native_restore_original_response)
		return -EOPNOTSUPP;
	if (response[offset] != receiver_selector)
		return -EPROTO;
	response[offset] = application_selector;
	if (offset >= profile->native_checksum_start_offset &&
	    offset < profile->native_checksum_end_offset)
		hfx_native_checksum_update(profile, response);
	return hfx_native_response_report_valid(profile, response) ? 0 : -EPROTO;
}

static int hfx_native_submit_validate(
	const struct hfx_receiver *receiver,
	const struct hfx_uapi_native_exchange_submit *submit)
{
	u64 now_ns = ktime_get_boottime_ns();

	if (submit->version != HFX_UAPI_NATIVE_ABI_VERSION ||
	    submit->size != sizeof(*submit))
		return -EPROTO;
	if (!hfx_native_profile_valid(receiver->profile))
		return -EOPNOTSUPP;
	if (!submit->dispatch_nonce ||
	    !memchr_inv(submit->route_token_digest, 0,
			sizeof(submit->route_token_digest)) ||
	    !memchr_inv(submit->request_digest, 0,
			sizeof(submit->request_digest)))
		return -EINVAL;
	if (submit->endpoint_slot != HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE &&
	    submit->endpoint_slot != HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE)
		return -EINVAL;
	if (submit->report_length != HFX_UAPI_NATIVE_REPORT_BYTES ||
	    submit->kernel_sequence ||
	    !hfx_native_request_report_valid(receiver->profile, submit->report))
		return -EINVAL;
	if (submit->deadline_boottime_ns <= now_ns ||
	    submit->deadline_boottime_ns - now_ns >
		    HFX_UAPI_NATIVE_CONTROL_TIMEOUT_NS)
		return -ETIME;
	return 0;
}

static bool hfx_native_key_matches(
	const struct hfx_uapi_native_exchange_result *result,
	u64 generation, u64 epoch, u64 nonce)
{
	return result->receiver_generation == generation &&
		result->authorization_epoch == epoch &&
		result->dispatch_nonce == nonce;
}

static bool hfx_native_exact_matches(
	const struct hfx_uapi_native_exchange_result *result,
	u64 generation, u64 nonce, u32 endpoint_slot,
	const u8 *route_token_digest, const u8 *request_digest)
{
	return result->receiver_generation == generation &&
		result->dispatch_nonce == nonce &&
		result->endpoint_slot == endpoint_slot &&
		!memcmp(result->route_token_digest, route_token_digest,
			sizeof(result->route_token_digest)) &&
		!memcmp(result->request_digest, request_digest,
			sizeof(result->request_digest));
}

static struct hfx_native_result_record *
hfx_native_record_find(struct hfx_receiver *receiver, u64 generation,
		       u64 epoch, u64 nonce)
{
	struct hfx_native_result_record *record;

	list_for_each_entry(record, &receiver->native_result_records, node) {
		if (hfx_native_key_matches(&record->result, generation, epoch,
					   nonce))
			return record;
	}
	return NULL;
}

static struct hfx_native_result_record *
hfx_native_record_find_exact(struct hfx_receiver *receiver, u64 generation,
			     u64 nonce, u32 endpoint_slot,
			     const u8 *route_token_digest,
			     const u8 *request_digest)
{
	struct hfx_native_result_record *record;

	list_for_each_entry(record, &receiver->native_result_records, node) {
		if (hfx_native_exact_matches(&record->result, generation, nonce,
					     endpoint_slot, route_token_digest,
					     request_digest))
			return record;
	}
	return NULL;
}

static struct hfx_native_result_tombstone *
hfx_native_tombstone_find(struct hfx_receiver *receiver, u64 generation,
			  u64 epoch, u64 nonce)
{
	u32 index;

	for (index = 0; index < HFX_UAPI_NATIVE_MAX_RESULTS; index++) {
		struct hfx_native_result_tombstone *tombstone =
			&receiver->native_result_tombstones[index];

		if (tombstone->valid &&
		    hfx_native_key_matches(&tombstone->result, generation, epoch,
					   nonce))
			return tombstone;
	}
	return NULL;
}

static struct hfx_native_result_tombstone *
hfx_native_tombstone_find_exact(struct hfx_receiver *receiver,
				u64 generation, u64 nonce,
				u32 endpoint_slot,
				const u8 *route_token_digest,
				const u8 *request_digest)
{
	u32 index;

	for (index = 0; index < HFX_UAPI_NATIVE_MAX_RESULTS; index++) {
		struct hfx_native_result_tombstone *tombstone =
			&receiver->native_result_tombstones[index];

		if (tombstone->valid &&
		    hfx_native_exact_matches(&tombstone->result, generation, nonce,
					     endpoint_slot, route_token_digest,
					     request_digest))
			return tombstone;
	}
	return NULL;
}

static bool hfx_native_record_matches_submit(
	const struct hfx_native_result_record *record,
	const struct hfx_uapi_native_exchange_submit *submit)
{
	return !memcmp(record->request, submit->report, sizeof(record->request));
}

static bool hfx_native_tombstone_matches_submit(
	const struct hfx_native_result_tombstone *tombstone,
	const struct hfx_uapi_native_exchange_submit *submit)
{
	return !memcmp(tombstone->request, submit->report,
		       sizeof(tombstone->request));
}

static void hfx_native_remember_tombstone(
	struct hfx_receiver *receiver,
	const struct hfx_native_result_record *record)
{
	struct hfx_native_result_tombstone *tombstone =
		&receiver->native_result_tombstones[
			receiver->native_tombstone_head];

	memzero_explicit(tombstone, sizeof(*tombstone));
	tombstone->valid = true;
	tombstone->result = record->result;
	memcpy(tombstone->request, record->request,
	       sizeof(tombstone->request));
	receiver->native_tombstone_head =
		(receiver->native_tombstone_head + 1U) %
		HFX_UAPI_NATIVE_MAX_RESULTS;
	if (receiver->native_tombstone_count < HFX_UAPI_NATIVE_MAX_RESULTS)
		receiver->native_tombstone_count++;
}

static int hfx_native_ensure_result_capacity(struct hfx_receiver *receiver)
{
	struct hfx_native_result_record *record;

	if (receiver->native_result_count < HFX_UAPI_NATIVE_MAX_RESULTS)
		return 0;
	record = list_first_entry_or_null(&receiver->native_result_records,
					  struct hfx_native_result_record,
					  node);
	if (!record)
		return -EBUSY;
	hfx_native_remember_tombstone(receiver, record);
	list_del(&record->node);
	receiver->native_result_count--;
	memzero_explicit(record, sizeof(*record));
	kfree(record);
	return 0;
}

static int hfx_native_result_return_code(
	const struct hfx_uapi_native_exchange_result *result)
{
	if (result->certainty ==
	    HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_MATCHED_RESPONSE)
		return 0;
	if (result->transport_errno)
		return result->transport_errno;
	return -EIO;
}

static int hfx_native_delay(u32 delay_us, u64 deadline_ns)
{
	u64 now_ns;
	u64 remaining_us;

	if (!delay_us)
		return 0;
	now_ns = ktime_get_boottime_ns();
	if (now_ns >= deadline_ns)
		return -ETIME;
	remaining_us = div_u64(deadline_ns - now_ns, NSEC_PER_USEC);
	if (remaining_us < delay_us)
		return -ETIME;
	if (delay_us >= 20000U)
		msleep(DIV_ROUND_UP(delay_us, 1000U));
	else
		usleep_range(delay_us,
			     delay_us + min(delay_us / 10U + 1U, 1000U));
	return ktime_get_boottime_ns() < deadline_ns ? 0 : -ETIME;
}

static bool hfx_native_response_matches(
	const struct hfx_receiver_profile *profile,
	const u8 *request, const u8 *response)
{
	u16 index;
	u8 transaction_difference;

	if (!hfx_native_response_report_valid(profile, response))
		return false;
	transaction_difference =
		request[profile->native_transaction_id_offset] ^
		response[profile->native_transaction_id_offset];
	if (transaction_difference &
	    profile->native_transaction_id_match_mask)
		return false;

	for (index = 0; index < profile->native_match_range_count; index++) {
		const struct hfx_native_match_range *range =
			&profile->native_match_ranges[index];

		if (memcmp(&request[range->offset], &response[range->offset],
			   range->length))
			return false;
	}
	return true;
}

static int hfx_native_usb_set(struct hfx_receiver *receiver,
			      const struct hfx_receiver_profile *profile,
			      u8 *request)
{
	int ret;

	ret = usb_control_msg(receiver->udev,
			      usb_sndctrlpipe(receiver->udev, 0),
			      HFX_NATIVE_SET_REQUEST,
			      HFX_NATIVE_SET_REQUEST_TYPE,
			      HFX_NATIVE_REPORT_VALUE,
			      profile->native_set_report_index,
			      request, profile->native_report_bytes,
			      HFX_NATIVE_TIMEOUT_MS);
	if (ret < 0)
		return ret;
	return ret == profile->native_report_bytes ? 0 : -EIO;
}

static int hfx_native_usb_get(struct hfx_receiver *receiver,
			      const struct hfx_receiver_profile *profile,
			      u8 *response)
{
	int ret;

	ret = usb_control_msg(receiver->udev,
			      usb_rcvctrlpipe(receiver->udev, 0),
			      HFX_NATIVE_GET_REQUEST,
			      HFX_NATIVE_GET_REQUEST_TYPE,
			      HFX_NATIVE_REPORT_VALUE,
			      profile->native_response_report_index,
			      response, profile->native_report_bytes,
			      HFX_NATIVE_TIMEOUT_MS);
	if (ret < 0)
		return ret;
	return ret == profile->native_report_bytes ? 0 : -EIO;
}

static int hfx_native_record_dispatch(
	struct hfx_receiver *receiver, struct hfx_file_context *context,
	struct hfx_native_result_record *record, u64 deadline_ns)
{
	const struct hfx_receiver_profile *profile = receiver->profile;
	struct usb_interface *control_interface;
	u8 *request;
	u8 *response;
	u8 application_selector;
	u8 receiver_selector;
	bool route_translated;
	u16 attempt;
	int ret;

	request = kmemdup(record->request, sizeof(record->request), GFP_KERNEL);
	if (!request) {
		ret = -ENOMEM;
		goto not_sent;
	}
	response = kzalloc(HFX_UAPI_NATIVE_REPORT_BYTES, GFP_KERNEL);
	if (!response) {
		ret = -ENOMEM;
		goto free_request;
	}
	ret = hfx_native_route_request(profile, record->result.endpoint_slot,
				       request, &application_selector,
				       &route_translated);
	if (ret)
		goto free_buffers;
	receiver_selector = request[profile->native_transaction_id_offset];
	control_interface = usb_ifnum_to_if(
		receiver->udev, profile->native_set_report_index);
	if (!control_interface) {
		ret = -ENODEV;
		goto free_buffers;
	}
	ret = usb_autopm_get_interface(control_interface);
	if (ret)
		goto free_buffers;
	ret = hfx_session_check(receiver, context,
				record->result.receiver_generation,
				record->result.authorization_epoch);
	if (ret)
		goto put_interface;
	if (ktime_get_boottime_ns() >= deadline_ns) {
		ret = -ETIME;
		goto put_interface;
	}

	record->result.certainty =
		HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_MAY_HAVE_BEEN_SENT;
	ret = hfx_native_usb_set(receiver, profile, request);
	if (ret)
		goto uncertain;
	ret = hfx_native_delay(profile->native_initial_response_delay_us,
			       deadline_ns);
	if (ret)
		goto uncertain;

	for (attempt = 0; attempt < profile->native_max_response_attempts;
	     attempt++) {
		ret = hfx_session_check(receiver, context,
					record->result.receiver_generation,
					record->result.authorization_epoch);
		if (ret)
			goto uncertain;
		if (ktime_get_boottime_ns() >= deadline_ns) {
			ret = -ETIME;
			goto uncertain;
		}
		memzero_explicit(response, HFX_UAPI_NATIVE_REPORT_BYTES);
		ret = hfx_native_usb_get(receiver, profile, response);
		if (!ret && hfx_native_response_matches(
				    profile, request, response)) {
			ret = hfx_native_restore_response(
				profile, application_selector, receiver_selector,
				route_translated, response);
			if (ret)
				goto uncertain;
			record->result.certainty =
				HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_MATCHED_RESPONSE;
			record->result.transport_errno = 0;
			record->result.response_length =
				HFX_UAPI_NATIVE_REPORT_BYTES;
			memcpy(record->result.response, response,
			       sizeof(record->result.response));
			ret = 0;
			goto put_interface;
		}
		if (!ret)
			ret = -ENOMSG;
		if (attempt + 1U < profile->native_max_response_attempts) {
			int delay_ret = hfx_native_delay(
				profile->native_response_retry_delay_us,
				deadline_ns);

			if (delay_ret) {
				ret = delay_ret;
				goto uncertain;
			}
		}
	}

uncertain:
	record->result.transport_errno = ret;
put_interface:
	usb_mark_last_busy(receiver->udev);
	usb_autopm_put_interface(control_interface);
free_buffers:
	memzero_explicit(response, HFX_UAPI_NATIVE_REPORT_BYTES);
	kfree(response);
free_request:
	memzero_explicit(request, HFX_UAPI_NATIVE_REPORT_BYTES);
	kfree(request);
not_sent:
	if (record->result.certainty ==
	    HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_NOT_SENT)
		record->result.transport_errno = ret;
	return ret;
}

static int hfx_native_reserve(
	struct hfx_receiver *receiver, struct hfx_file_context *context,
	const struct hfx_uapi_native_exchange_submit *submit,
	struct hfx_native_result_record *record)
{
	unsigned long flags;
	int ret;

	ret = hfx_native_ensure_result_capacity(receiver);
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

	record->result.version = HFX_UAPI_NATIVE_ABI_VERSION;
	record->result.size = sizeof(record->result);
	record->result.receiver_generation = submit->receiver_generation;
	record->result.authorization_epoch = submit->authorization_epoch;
	record->result.dispatch_nonce = submit->dispatch_nonce;
	record->result.endpoint_slot = submit->endpoint_slot;
	record->result.certainty =
		HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_NOT_SENT;
	memcpy(record->result.route_token_digest,
	       submit->route_token_digest,
	       sizeof(record->result.route_token_digest));
	memcpy(record->result.request_digest, submit->request_digest,
	       sizeof(record->result.request_digest));
	memcpy(record->request, submit->report, sizeof(record->request));
	list_add_tail(&record->node, &receiver->native_result_records);
	receiver->native_result_count++;
	return 0;
}

long hfx_native_exchange_submit(struct hfx_receiver *receiver,
				struct hfx_file_context *context,
				void __user *user_arg)
{
	struct hfx_uapi_native_exchange_submit *submit;
	struct hfx_native_result_record *record;
	struct hfx_native_result_tombstone *tombstone;
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
	ret = hfx_native_submit_validate(receiver, submit);
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
	record = hfx_native_record_find_exact(
		receiver, submit->receiver_generation, submit->dispatch_nonce,
		submit->endpoint_slot, submit->route_token_digest,
		submit->request_digest);
	if (record) {
		if (!hfx_native_record_matches_submit(record, submit)) {
			hfx_session_revoke(
				receiver, HFX_UAPI_REVOKE_REASON_SERVICE_LOSS);
			ret = -EEXIST;
			goto out_unlock;
		}
		submit->kernel_sequence = record->result.kernel_sequence;
		ret = hfx_native_result_return_code(&record->result);
		goto out_copy;
	}
	tombstone = hfx_native_tombstone_find_exact(
		receiver, submit->receiver_generation, submit->dispatch_nonce,
		submit->endpoint_slot, submit->route_token_digest,
		submit->request_digest);
	if (tombstone) {
		if (!hfx_native_tombstone_matches_submit(tombstone, submit)) {
			hfx_session_revoke(
				receiver, HFX_UAPI_REVOKE_REASON_SERVICE_LOSS);
			ret = -EEXIST;
			goto out_unlock;
		}
		submit->kernel_sequence = tombstone->result.kernel_sequence;
		ret = hfx_native_result_return_code(&tombstone->result);
		goto out_copy;
	}
	if (hfx_native_record_find(receiver, submit->receiver_generation,
				   submit->authorization_epoch,
				   submit->dispatch_nonce) ||
	    hfx_native_tombstone_find(receiver, submit->receiver_generation,
				      submit->authorization_epoch,
				      submit->dispatch_nonce)) {
		hfx_session_revoke(receiver,
				   HFX_UAPI_REVOKE_REASON_SERVICE_LOSS);
		ret = -EEXIST;
		goto out_unlock;
	}

	record = kzalloc(sizeof(*record), GFP_KERNEL);
	if (!record) {
		ret = -ENOMEM;
		goto out_unlock;
	}
	INIT_LIST_HEAD(&record->node);
	ret = hfx_native_reserve(receiver, context, submit, record);
	if (ret) {
		memzero_explicit(record, sizeof(*record));
		kfree(record);
		goto out_unlock;
	}
	submit->kernel_sequence = record->result.kernel_sequence;
	ret = hfx_native_record_dispatch(receiver, context, record,
					 submit->deadline_boottime_ns);
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

static void hfx_native_synthetic(
	struct hfx_uapi_native_exchange_result *result,
	u32 certainty, int transport_errno)
{
	result->kernel_sequence = 0;
	result->certainty = certainty;
	result->transport_errno = transport_errno;
	result->response_length = 0;
	memzero_explicit(result->response, sizeof(result->response));
	result->reserved = 0;
}

long hfx_native_exchange_get_result(struct hfx_receiver *receiver,
				    struct hfx_file_context *context,
				    void __user *user_arg)
{
	struct hfx_uapi_native_exchange_result query;
	struct hfx_native_result_record *record;
	struct hfx_native_result_tombstone *tombstone;
	bool not_observed;
	long ret = 0;

	if (!context->writer)
		return -EACCES;
	if (copy_from_user(&query, user_arg, sizeof(query)))
		return -EFAULT;
	if (query.version != HFX_UAPI_NATIVE_ABI_VERSION ||
	    query.size != sizeof(query))
		return -EPROTO;
	if (query.receiver_generation != receiver->generation)
		return -ESTALE;
	if (!query.dispatch_nonce ||
	    (query.endpoint_slot != HFX_UAPI_ENDPOINT_SLOT_POINTER_LANE &&
	     query.endpoint_slot != HFX_UAPI_ENDPOINT_SLOT_KEYBOARD_LANE) ||
	    !memchr_inv(query.route_token_digest, 0,
			sizeof(query.route_token_digest)) ||
	    !memchr_inv(query.request_digest, 0,
			sizeof(query.request_digest)) ||
	    query.certainty || query.transport_errno || query.response_length ||
	    memchr_inv(query.response, 0, sizeof(query.response)) ||
	    query.reserved)
		return -EINVAL;

	mutex_lock(&receiver->transport_lock);
	record = hfx_native_record_find_exact(
		receiver, query.receiver_generation, query.dispatch_nonce,
		query.endpoint_slot, query.route_token_digest,
		query.request_digest);
	if (record) {
		if (query.kernel_sequence &&
		    query.kernel_sequence != record->result.kernel_sequence) {
			hfx_native_synthetic(
				&query,
				HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_MAY_HAVE_BEEN_SENT,
				-EEXIST);
		} else {
			query = record->result;
		}
		goto out_copy;
	}
	tombstone = hfx_native_tombstone_find_exact(
		receiver, query.receiver_generation, query.dispatch_nonce,
		query.endpoint_slot, query.route_token_digest,
		query.request_digest);
	if (tombstone) {
		if (query.kernel_sequence &&
		    query.kernel_sequence != tombstone->result.kernel_sequence) {
			hfx_native_synthetic(
				&query,
				HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_MAY_HAVE_BEEN_SENT,
				-EEXIST);
		} else {
			query = tombstone->result;
		}
		goto out_copy;
	}
	if (hfx_native_record_find(receiver, query.receiver_generation,
				   query.authorization_epoch,
				   query.dispatch_nonce) ||
	    hfx_native_tombstone_find(receiver, query.receiver_generation,
				      query.authorization_epoch,
				      query.dispatch_nonce)) {
		hfx_native_synthetic(
			&query,
			HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_MAY_HAVE_BEEN_SENT,
			-EEXIST);
		goto out_copy;
	}
	not_observed = hfx_session_nonce_not_observed(
		receiver, context, query.authorization_epoch,
		query.dispatch_nonce);
	hfx_native_synthetic(
		&query,
		not_observed ? HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_NOT_SENT :
			       HFX_UAPI_NATIVE_EXCHANGE_CERTAINTY_MAY_HAVE_BEEN_SENT,
		-ENODATA);
out_copy:
	if (copy_to_user(user_arg, &query, sizeof(query)))
		ret = -EFAULT;
	mutex_unlock(&receiver->transport_lock);
	memzero_explicit(&query, sizeof(query));
	return ret;
}

void hfx_native_exchange_free_journal(struct hfx_receiver *receiver)
{
	struct hfx_native_result_record *record;
	struct hfx_native_result_record *next;

	list_for_each_entry_safe(record, next,
				 &receiver->native_result_records, node) {
		list_del(&record->node);
		memzero_explicit(record, sizeof(*record));
		kfree(record);
	}
	receiver->native_result_count = 0;
}
