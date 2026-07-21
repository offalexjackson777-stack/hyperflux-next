// SPDX-License-Identifier: GPL-2.0-only

#include <linux/atomic.h>
#include <linux/hid.h>
#include <linux/module.h>
#include <linux/random.h>
#include <linux/slab.h>

#include "hyperflux-next-internal.h"
#include "hyperflux-next-passive.h"
#include "hyperflux-next-version.h"

/* Lock order: registry, transport, state, authorization, observation. */
static LIST_HEAD(hfx_receivers);
static DEFINE_MUTEX(hfx_registry_lock);
static atomic64_t hfx_next_generation = ATOMIC64_INIT(0);

/*
 * Generation values are opaque lifetime epochs, not reconnect counters.  A
 * randomized module-lifetime seed prevents a durable userspace claim from
 * matching a different receiver lifetime after module reload or reboot.  The
 * remaining positive range is intentionally enormous for per-module increments.
 */
static s64 hfx_next_receiver_generation(void)
{
	s64 seed;

	if (!atomic64_read(&hfx_next_generation)) {
		seed = (s64)(get_random_u64() & ((1ULL << 62) - 1));
		if (!seed)
			seed = 1;
		atomic64_cmpxchg(&hfx_next_generation, 0, seed);
	}
	return atomic64_inc_return(&hfx_next_generation);
}

static struct usb_device *hfx_usb_device(struct hid_device *hdev,
					 int *interface_number)
{
	struct usb_interface *interface;

	if (hdev->bus != BUS_USB || !hdev->dev.parent)
		return NULL;
	interface = to_usb_interface(hdev->dev.parent);
	if (!interface->cur_altsetting)
		return NULL;
	*interface_number =
		interface->cur_altsetting->desc.bInterfaceNumber;
	return interface_to_usbdev(interface);
}

static struct hfx_receiver *hfx_receiver_find_locked(struct usb_device *udev)
{
	struct hfx_receiver *receiver;

	list_for_each_entry(receiver, &hfx_receivers, node) {
		if (receiver->udev == udev)
			return receiver;
	}
	return NULL;
}

void hfx_receiver_release(struct kref *refcount)
{
	struct hfx_receiver *receiver =
		container_of(refcount, struct hfx_receiver, refcount);

	hfx_transport_free_journal(receiver);
	memzero_explicit(receiver->result_tombstones,
			 sizeof(*receiver->result_tombstones) *
				 HFX_TOMBSTONE_CAPACITY);
	kfree(receiver->result_tombstones);
	kfree(receiver->observation_ring);
	usb_put_dev(receiver->udev);
	kfree(receiver->session_name);
	kfree(receiver);
}

static struct hfx_receiver *
hfx_receiver_candidate(struct usb_device *udev,
		       const struct hfx_receiver_profile *profile)
{
	struct hfx_receiver *candidate;

	candidate = kzalloc(sizeof(*candidate), GFP_KERNEL);
	if (!candidate)
		return ERR_PTR(-ENOMEM);
	candidate->observation_ring =
		kcalloc(HFX_OBSERVATION_CAPACITY,
			sizeof(*candidate->observation_ring), GFP_KERNEL);
	if (!candidate->observation_ring)
		goto err_candidate;
	candidate->result_tombstones =
		kcalloc(HFX_TOMBSTONE_CAPACITY,
			sizeof(*candidate->result_tombstones), GFP_KERNEL);
	if (!candidate->result_tombstones)
		goto err_observations;

	INIT_LIST_HEAD(&candidate->node);
	INIT_LIST_HEAD(&candidate->result_records);
	mutex_init(&candidate->state_lock);
	mutex_init(&candidate->transport_lock);
	spin_lock_init(&candidate->authorization_lock);
	spin_lock_init(&candidate->observation_lock);
	kref_init(&candidate->refcount);
	candidate->udev = usb_get_dev(udev);
	candidate->backend_id = profile->backend_id;
	candidate->next_observation_sequence = 0;
	candidate->next_kernel_sequence = 0;
	return candidate;

err_observations:
	kfree(candidate->observation_ring);
err_candidate:
	kfree(candidate);
	return ERR_PTR(-ENOMEM);
}

static struct hfx_receiver *
hfx_receiver_get(struct usb_device *udev, int interface_number,
		 const struct hfx_receiver_profile *profile)
{
	struct hfx_receiver *candidate;
	struct hfx_receiver *receiver;
	s64 generation;
	bool duplicate;
	int ret;

	if (interface_number < 0 ||
	    interface_number >= HFX_USB_INTERFACE_LIMIT)
		return ERR_PTR(-ERANGE);
	candidate = hfx_receiver_candidate(udev, profile);
	if (IS_ERR(candidate))
		return candidate;

	mutex_lock(&hfx_registry_lock);
	receiver = hfx_receiver_find_locked(udev);
	if (receiver) {
		mutex_lock(&receiver->state_lock);
		duplicate = test_and_set_bit(interface_number,
					     receiver->bound_interface_numbers);
		if (!duplicate)
			receiver->bound_interfaces++;
		ret = receiver->backend_id == profile->backend_id ? 0 : -EPROTO;
		if (ret && !duplicate) {
			clear_bit(interface_number,
				  receiver->bound_interface_numbers);
			receiver->bound_interfaces--;
		}
		mutex_unlock(&receiver->state_lock);
		mutex_unlock(&hfx_registry_lock);
		kref_put(&candidate->refcount, hfx_receiver_release);
		if (duplicate)
			return ERR_PTR(-EEXIST);
		return ret ? ERR_PTR(ret) : receiver;
	}

	generation = hfx_next_receiver_generation();
	if (generation <= 0) {
		ret = -EOVERFLOW;
		goto err_unlock;
	}
	candidate->generation = generation;
	set_bit(interface_number, candidate->bound_interface_numbers);
	candidate->bound_interfaces = 1;
	ret = hfx_session_device_register(candidate);
	if (ret)
		goto err_unlock;
	list_add_tail(&candidate->node, &hfx_receivers);
	mutex_unlock(&hfx_registry_lock);
	hfx_observation_emit(
		candidate, HFX_UAPI_OBSERVATION_KIND_RECEIVER_AVAILABLE,
		HFX_UAPI_ENDPOINT_SLOT_RECEIVER,
		HFX_UAPI_OBSERVATION_SOURCE_POWER_MANAGEMENT,
		HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT, 1,
		candidate->backend_id);
	return candidate;

err_unlock:
	mutex_unlock(&hfx_registry_lock);
	kref_put(&candidate->refcount, hfx_receiver_release);
	return ERR_PTR(ret);
}

static void hfx_receiver_put(struct hfx_interface *interface)
{
	struct hfx_receiver *receiver = interface->receiver;
	bool destroy = false;
	bool removed = false;

	if (!receiver)
		return;

	mutex_lock(&hfx_registry_lock);
	mutex_lock(&receiver->transport_lock);
	mutex_lock(&receiver->state_lock);
	if (interface->suspended && receiver->suspended_interfaces)
		receiver->suspended_interfaces--;
	interface->suspended = false;
	if (test_and_clear_bit(interface->interface_number,
			       receiver->bound_interface_numbers)) {
		removed = true;
		if (receiver->bound_interfaces)
			receiver->bound_interfaces--;
	}
	if (!receiver->bound_interfaces) {
		receiver->disconnecting = true;
		list_del_init(&receiver->node);
		destroy = true;
	}
	mutex_unlock(&receiver->state_lock);
	if (removed)
		hfx_session_revoke(receiver,
				   HFX_UAPI_REVOKE_REASON_DISCONNECT);
	mutex_unlock(&receiver->transport_lock);
	mutex_unlock(&hfx_registry_lock);

	interface->receiver = NULL;
	if (!destroy)
		return;
	hfx_observation_emit(
		receiver, HFX_UAPI_OBSERVATION_KIND_RECEIVER_AVAILABLE,
		HFX_UAPI_ENDPOINT_SLOT_RECEIVER,
		HFX_UAPI_OBSERVATION_SOURCE_POWER_MANAGEMENT,
		HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT, 0,
		receiver->backend_id);
	if (receiver->session_registered) {
		misc_deregister(&receiver->session_device);
		receiver->session_registered = false;
	}
	kref_put(&receiver->refcount, hfx_receiver_release);
}

static void hfx_set_suspended(struct hfx_interface *interface, bool suspended)
{
	struct hfx_receiver *receiver = interface->receiver;
	bool entered = false;
	bool left = false;

	if (!receiver)
		return;
	mutex_lock(&receiver->state_lock);
	if (interface->suspended == suspended || receiver->disconnecting)
		goto out;
	if (suspended) {
		entered = receiver->suspended_interfaces == 0;
		receiver->suspended_interfaces++;
		interface->suspended = true;
	} else {
		if (receiver->suspended_interfaces)
			receiver->suspended_interfaces--;
		interface->suspended = false;
		left = receiver->suspended_interfaces == 0;
	}
out:
	mutex_unlock(&receiver->state_lock);
	if (entered) {
		hfx_session_revoke(receiver, HFX_UAPI_REVOKE_REASON_SUSPEND);
		hfx_observation_emit(
			receiver, HFX_UAPI_OBSERVATION_KIND_RECEIVER_SUSPENDED,
			HFX_UAPI_ENDPOINT_SLOT_RECEIVER,
			HFX_UAPI_OBSERVATION_SOURCE_POWER_MANAGEMENT,
			HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT, 1, 0);
	} else if (left) {
		hfx_observation_emit(
			receiver, HFX_UAPI_OBSERVATION_KIND_RECEIVER_SUSPENDED,
			HFX_UAPI_ENDPOINT_SLOT_RECEIVER,
			HFX_UAPI_OBSERVATION_SOURCE_POWER_MANAGEMENT,
			HFX_UAPI_OBSERVATION_CONFIDENCE_EXACT, 0, 0);
	}
}

static int hfx_raw_event(struct hid_device *hdev, struct hid_report *report,
			 u8 *data, int size)
{
	struct hfx_interface *interface = hid_get_drvdata(hdev);

	(void)report;
	if (!interface || !interface->receiver || !data || size <= 0)
		return 0;
	hfx_observe_activity(interface->receiver, interface->interface_number);
	hfx_observe_passive(interface->receiver, interface->interface_number,
			    data, size);
	return 0;
}

static int hfx_probe(struct hid_device *hdev, const struct hid_device_id *id)
{
	const struct hfx_receiver_profile *profile;
	struct hfx_interface *interface;
	struct usb_device *udev;
	u16 product_id;
	u16 vendor_id;
	int interface_number;
	int ret;

	udev = hfx_usb_device(hdev, &interface_number);
	if (!udev)
		return -ENODEV;
	vendor_id = le16_to_cpu(udev->descriptor.idVendor);
	product_id = le16_to_cpu(udev->descriptor.idProduct);
	profile = hfx_receiver_profile_find(vendor_id, product_id);
	if (!profile || profile->backend_id != id->driver_data)
		return -ENODEV;

	interface = devm_kzalloc(&hdev->dev, sizeof(*interface), GFP_KERNEL);
	if (!interface)
		return -ENOMEM;
	interface->hdev = hdev;
	interface->interface_number = interface_number;
	hid_set_drvdata(hdev, interface);

	ret = hid_parse(hdev);
	if (ret)
		goto err_clear;
	interface->receiver =
		hfx_receiver_get(udev, interface_number, profile);
	if (IS_ERR(interface->receiver)) {
		ret = PTR_ERR(interface->receiver);
		interface->receiver = NULL;
		goto err_clear;
	}
	ret = hid_hw_start(hdev, HID_CONNECT_DEFAULT);
	if (ret)
		goto err_receiver;
	interface->started = true;
	hid_info(hdev, "HyperFlux Next bound interface %d, generation %llu\n",
		 interface_number, interface->receiver->generation);
	return 0;

err_receiver:
	hfx_receiver_put(interface);
err_clear:
	hid_set_drvdata(hdev, NULL);
	return ret;
}

static void hfx_remove(struct hid_device *hdev)
{
	struct hfx_interface *interface = hid_get_drvdata(hdev);

	if (!interface)
		return;
	if (interface->started) {
		hid_hw_stop(hdev);
		interface->started = false;
	}
	hfx_receiver_put(interface);
	hid_set_drvdata(hdev, NULL);
}

static int hfx_suspend(struct hid_device *hdev, pm_message_t message)
{
	struct hfx_interface *interface = hid_get_drvdata(hdev);

	(void)message;
	if (interface)
		hfx_set_suspended(interface, true);
	return 0;
}

static int hfx_resume(struct hid_device *hdev)
{
	struct hfx_interface *interface = hid_get_drvdata(hdev);

	if (interface)
		hfx_set_suspended(interface, false);
	return 0;
}

static const struct hid_device_id hfx_devices[] = {
#define HFX_RECEIVER_PROFILE(vendor, product, backend) \
	{ HID_USB_DEVICE((vendor), (product)), .driver_data = (backend) },
#include "generated/hyperflux_receiver_profiles.inc"
#undef HFX_RECEIVER_PROFILE
	{ }
};
MODULE_DEVICE_TABLE(hid, hfx_devices);

static struct hid_driver hfx_driver = {
	.name = "hid-hyperflux-next",
	.id_table = hfx_devices,
	.probe = hfx_probe,
	.remove = hfx_remove,
	.raw_event = hfx_raw_event,
	.suspend = hfx_suspend,
	.resume = hfx_resume,
	.reset_resume = hfx_resume,
};
module_hid_driver(hfx_driver);

MODULE_AUTHOR("HyperFlux Next contributors");
MODULE_DESCRIPTION("Minimal HyperFlux receiver HID transport");
MODULE_VERSION(HYPERFLUX_NEXT_MODULE_VERSION);
MODULE_LICENSE("GPL");
