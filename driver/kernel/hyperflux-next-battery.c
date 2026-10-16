// SPDX-License-Identifier: GPL-2.0-only

#include <linux/hid.h>
#include <linux/power_supply.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/uaccess.h>

#include "hyperflux-next-internal.h"
#include "hyperflux-next-passive.h"

/*
 * Child battery publication.  The bridge already decodes passive battery
 * reports; its writer file forwards the resulting lane state here and the
 * module publishes exactly that state in the power_supply class.  Nothing in
 * this file sends a report to the receiver: a lane without a forwarded value
 * has no power_supply at all.
 *
 * battery_lock serializes registration and teardown and is taken before
 * state_lock and authorization_lock.  It is never taken with registry_lock or
 * transport_lock held.  get_property reads a lane only under its value_lock,
 * so unregistering under battery_lock cannot wait on a reader that waits on
 * battery_lock.
 */

static struct hfx_battery_lane *
hfx_battery_lane_for_interface(struct hfx_receiver *receiver,
			       int interface_number)
{
	switch (interface_number) {
	case HFX_POINTER_INPUT_INTERFACE:
		return &receiver->battery[HFX_BATTERY_POINTER];
	case HFX_KEYBOARD_INPUT_INTERFACE:
		return &receiver->battery[HFX_BATTERY_KEYBOARD];
	default:
		return NULL;
	}
}

#if IS_REACHABLE(CONFIG_POWER_SUPPLY)

static const enum power_supply_property hfx_battery_properties[] = {
	POWER_SUPPLY_PROP_PRESENT,
	POWER_SUPPLY_PROP_SCOPE,
	POWER_SUPPLY_PROP_STATUS,
	POWER_SUPPLY_PROP_CAPACITY,
	POWER_SUPPLY_PROP_MODEL_NAME,
	POWER_SUPPLY_PROP_MANUFACTURER,
};

static int hfx_battery_status(u32 status)
{
	switch (status) {
	case HFX_UAPI_BATTERY_STATUS_DISCHARGING:
		return POWER_SUPPLY_STATUS_DISCHARGING;
	case HFX_UAPI_BATTERY_STATUS_CHARGING:
		return POWER_SUPPLY_STATUS_CHARGING;
	case HFX_UAPI_BATTERY_STATUS_FULL:
		return POWER_SUPPLY_STATUS_FULL;
	default:
		return POWER_SUPPLY_STATUS_UNKNOWN;
	}
}

static int hfx_battery_get_property(struct power_supply *psy,
				    enum power_supply_property property,
				    union power_supply_propval *value)
{
	struct hfx_battery_lane *lane = power_supply_get_drvdata(psy);
	unsigned long flags;
	int ret = 0;

	spin_lock_irqsave(&lane->value_lock, flags);
	switch (property) {
	case POWER_SUPPLY_PROP_PRESENT:
		value->intval = 1;
		break;
	case POWER_SUPPLY_PROP_SCOPE:
		value->intval = POWER_SUPPLY_SCOPE_DEVICE;
		break;
	case POWER_SUPPLY_PROP_STATUS:
		value->intval = hfx_battery_status(lane->status);
		break;
	case POWER_SUPPLY_PROP_CAPACITY:
		value->intval = lane->capacity;
		break;
	case POWER_SUPPLY_PROP_MODEL_NAME:
		/* The name is fixed for one registration; see hfx_battery_report. */
		if (lane->model_name[0])
			value->strval = lane->model_name;
		else
			ret = -ENODATA;
		break;
	case POWER_SUPPLY_PROP_MANUFACTURER:
		value->strval = "Razer";
		break;
	default:
		ret = -EINVAL;
		break;
	}
	spin_unlock_irqrestore(&lane->value_lock, flags);
	return ret;
}

static void hfx_battery_unregister_locked(struct hfx_battery_lane *lane)
{
	struct power_supply *psy = lane->psy;
	unsigned long flags;

	if (!psy)
		return;
	lane->psy = NULL;
	lane->owner = NULL;
	power_supply_unregister(psy);
	spin_lock_irqsave(&lane->value_lock, flags);
	lane->capacity = 0;
	lane->status = HFX_UAPI_BATTERY_STATUS_UNKNOWN;
	memset(lane->model_name, 0, sizeof(lane->model_name));
	spin_unlock_irqrestore(&lane->value_lock, flags);
}

static int hfx_battery_register_locked(struct hfx_receiver *receiver,
				       struct hfx_battery_lane *lane,
				       struct hfx_file_context *context)
{
	struct power_supply_config config = { .drv_data = lane };
	struct power_supply *psy;

	/*
	 * The lane's own input interface is the parent so desktop battery
	 * services find the mouse or keyboard input device as a sibling.
	 */
	snprintf(lane->name, sizeof(lane->name), "%s-%s",
		 receiver->session_name, lane->suffix);
	lane->desc.name = lane->name;
	lane->desc.type = POWER_SUPPLY_TYPE_BATTERY;
	lane->desc.properties = hfx_battery_properties;
	lane->desc.num_properties = ARRAY_SIZE(hfx_battery_properties);
	lane->desc.get_property = hfx_battery_get_property;
	psy = power_supply_register(&lane->hdev->dev, &lane->desc, &config);
	if (IS_ERR(psy))
		return PTR_ERR(psy);
	lane->psy = psy;
	lane->owner = context;
	return 0;
}

long hfx_battery_report(struct hfx_receiver *receiver,
			struct hfx_file_context *context,
			void __user *user_arg)
{
	struct hfx_uapi_child_battery report;
	struct hfx_battery_lane *lane;
	enum hfx_battery_check check;
	unsigned long flags;
	bool disconnecting;
	bool writer;
	long ret = 0;

	if (!context->writer)
		return -EACCES;
	if (copy_from_user(&report, user_arg, sizeof(report)))
		return -EFAULT;
	check = hfx_battery_check_report(&report);
	if (check == HFX_BATTERY_HEADER)
		return -EPROTO;
	if (check != HFX_BATTERY_VALID)
		return -EINVAL;
	lane = &receiver->battery[hfx_battery_lane_index(report.endpoint_slot)];
	if (report.receiver_generation != receiver->generation)
		return -ESTALE;

	mutex_lock(&receiver->battery_lock);
	mutex_lock(&receiver->state_lock);
	disconnecting = receiver->disconnecting;
	mutex_unlock(&receiver->state_lock);
	spin_lock_irqsave(&receiver->authorization_lock, flags);
	writer = receiver->writer_context == context;
	spin_unlock_irqrestore(&receiver->authorization_lock, flags);
	if (disconnecting) {
		ret = -ENODEV;
		goto out;
	}
	if (!writer) {
		ret = -EACCES;
		goto out;
	}

	if (report.presence == HFX_UAPI_BATTERY_PRESENCE_ABSENT) {
		hfx_battery_unregister_locked(lane);
		goto out;
	}
	if (!lane->hdev) {
		ret = -EHOSTDOWN;
		goto out;
	}
	/*
	 * get_property hands out a pointer to model_name, so a new name is
	 * published through a fresh registration instead of an in-place copy.
	 */
	if (lane->psy && memcmp(lane->model_name, report.model_name,
				sizeof(lane->model_name)))
		hfx_battery_unregister_locked(lane);

	spin_lock_irqsave(&lane->value_lock, flags);
	lane->capacity = report.capacity;
	lane->status = report.status;
	if (!lane->psy)
		memcpy(lane->model_name, report.model_name,
		       sizeof(lane->model_name));
	spin_unlock_irqrestore(&lane->value_lock, flags);

	if (lane->psy) {
		/* A writer that replaced a revoked one takes the lane over. */
		lane->owner = context;
		power_supply_changed(lane->psy);
	} else {
		ret = hfx_battery_register_locked(receiver, lane, context);
	}
out:
	mutex_unlock(&receiver->battery_lock);
	return ret;
}

#else

long hfx_battery_report(struct hfx_receiver *receiver,
			struct hfx_file_context *context,
			void __user *user_arg)
{
	(void)receiver;
	(void)context;
	(void)user_arg;
	return -EOPNOTSUPP;
}

static void hfx_battery_unregister_locked(struct hfx_battery_lane *lane)
{
	(void)lane;
}

#endif

void hfx_battery_init(struct hfx_receiver *receiver)
{
	mutex_init(&receiver->battery_lock);
	spin_lock_init(&receiver->battery[HFX_BATTERY_POINTER].value_lock);
	spin_lock_init(&receiver->battery[HFX_BATTERY_KEYBOARD].value_lock);
	receiver->battery[HFX_BATTERY_POINTER].suffix = "mouse";
	receiver->battery[HFX_BATTERY_KEYBOARD].suffix = "keyboard";
}

void hfx_battery_attach_interface(struct hfx_receiver *receiver,
				  int interface_number,
				  struct hid_device *hdev)
{
	struct hfx_battery_lane *lane =
		hfx_battery_lane_for_interface(receiver, interface_number);

	if (!lane)
		return;
	mutex_lock(&receiver->battery_lock);
	lane->hdev = hdev;
	mutex_unlock(&receiver->battery_lock);
}

void hfx_battery_detach_interface(struct hfx_receiver *receiver,
				  int interface_number)
{
	struct hfx_battery_lane *lane =
		hfx_battery_lane_for_interface(receiver, interface_number);

	if (!lane)
		return;
	mutex_lock(&receiver->battery_lock);
	hfx_battery_unregister_locked(lane);
	lane->hdev = NULL;
	mutex_unlock(&receiver->battery_lock);
}

void hfx_battery_release_owner(struct hfx_receiver *receiver,
			       struct hfx_file_context *context)
{
	size_t index;

	mutex_lock(&receiver->battery_lock);
	for (index = 0; index < HFX_BATTERY_LANES; index++) {
		if (receiver->battery[index].owner == context)
			hfx_battery_unregister_locked(&receiver->battery[index]);
	}
	mutex_unlock(&receiver->battery_lock);
}

void hfx_battery_teardown(struct hfx_receiver *receiver)
{
	size_t index;

	mutex_lock(&receiver->battery_lock);
	for (index = 0; index < HFX_BATTERY_LANES; index++) {
		hfx_battery_unregister_locked(&receiver->battery[index]);
		receiver->battery[index].hdev = NULL;
	}
	mutex_unlock(&receiver->battery_lock);
}
