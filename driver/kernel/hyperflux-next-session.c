// SPDX-License-Identifier: GPL-2.0-only

#include <linux/compat.h>
#include <linux/fs.h>
#include <linux/ktime.h>
#include <linux/module.h>
#include <linux/slab.h>
#include <linux/uaccess.h>

#include "hyperflux-next-internal.h"

static void hfx_session_clear_secrets(struct hfx_receiver *receiver)
{
	memzero_explicit(receiver->session_profile_digest,
			 sizeof(receiver->session_profile_digest));
	memzero_explicit(receiver->session_capability_digest,
			 sizeof(receiver->session_capability_digest));
	memzero_explicit(receiver->session_daemon_nonce,
			 sizeof(receiver->session_daemon_nonce));
}

void hfx_session_revoke_locked(struct hfx_receiver *receiver, u32 reason)
{
	if (receiver->session_active) {
		receiver->session_active = false;
		receiver->session_expires_boottime_ns = 0;
		receiver->session_max_dispatch_nonce = 0;
		receiver->authorization_epoch++;
		hfx_session_clear_secrets(receiver);
	}
	receiver->session_revoke_reason = reason;
}

void hfx_session_revoke(struct hfx_receiver *receiver, u32 reason)
{
	unsigned long flags;

	spin_lock_irqsave(&receiver->authorization_lock, flags);
	hfx_session_revoke_locked(receiver, reason);
	spin_unlock_irqrestore(&receiver->authorization_lock, flags);
}

static void hfx_session_expire_locked(struct hfx_receiver *receiver, u64 now_ns)
{
	if (receiver->session_active &&
	    now_ns >= receiver->session_expires_boottime_ns)
		hfx_session_revoke_locked(receiver,
					  HFX_UAPI_REVOKE_REASON_TIMEOUT);
}

static int hfx_session_check_locked(struct hfx_receiver *receiver,
				    struct hfx_file_context *context,
				    u64 generation,
				    u64 authorization_epoch)
{
	hfx_session_expire_locked(receiver, ktime_get_boottime_ns());
	if (!context->writer || receiver->writer_context != context)
		return -EACCES;
	if (!receiver->session_active)
		return receiver->session_revoke_reason ==
			       HFX_UAPI_REVOKE_REASON_TIMEOUT ?
		       -ETIME :
		       -EKEYREVOKED;
	if (generation != receiver->generation ||
	    authorization_epoch != receiver->authorization_epoch) {
		hfx_session_revoke_locked(
			receiver, HFX_UAPI_REVOKE_REASON_GENERATION_CHANGE);
		return -ESTALE;
	}
	return 0;
}

int hfx_session_check(struct hfx_receiver *receiver,
			      struct hfx_file_context *context,
			      u64 generation, u64 authorization_epoch)
{
	unsigned long flags;
	int ret;

	spin_lock_irqsave(&receiver->authorization_lock, flags);
	ret = hfx_session_check_locked(receiver, context, generation,
				       authorization_epoch);
	spin_unlock_irqrestore(&receiver->authorization_lock, flags);
	return ret;
}

bool hfx_session_nonce_not_observed(struct hfx_receiver *receiver,
				    struct hfx_file_context *context,
				    u64 authorization_epoch,
				    u64 dispatch_nonce)
{
	unsigned long flags;
	bool not_observed;

	spin_lock_irqsave(&receiver->authorization_lock, flags);
	hfx_session_expire_locked(receiver, ktime_get_boottime_ns());
	not_observed = receiver->writer_context == context &&
		receiver->session_active &&
		receiver->authorization_epoch == authorization_epoch &&
		dispatch_nonce > receiver->session_max_dispatch_nonce;
	spin_unlock_irqrestore(&receiver->authorization_lock, flags);
	return not_observed;
}

static int hfx_session_open(struct inode *inode, struct file *file)
{
	struct miscdevice *misc = file->private_data;
	struct hfx_receiver *receiver =
		container_of(misc, struct hfx_receiver, session_device);
	struct hfx_file_context *context;
	unsigned long flags;
	bool disconnecting;
	int ret;

	context = kzalloc(sizeof(*context), GFP_KERNEL);
	if (!context)
		return -ENOMEM;
	context->receiver = receiver;
	context->writer = (file->f_mode & FMODE_WRITE) != 0;
	if (context->writer && (file->f_mode & FMODE_READ) == 0) {
		ret = -EACCES;
		goto err_context;
	}

	if (!kref_get_unless_zero(&receiver->refcount)) {
		ret = -ENODEV;
		goto err_context;
	}
	mutex_lock(&receiver->state_lock);
	disconnecting = receiver->disconnecting;
	mutex_unlock(&receiver->state_lock);
	if (disconnecting) {
		ret = -ENODEV;
		goto err_put;
	}

	if (context->writer) {
		spin_lock_irqsave(&receiver->authorization_lock, flags);
		if (receiver->writer_context) {
			ret = -EBUSY;
		} else {
			receiver->writer_context = context;
			receiver->session_revoke_reason =
				HFX_UAPI_REVOKE_REASON_NONE;
			ret = 0;
		}
		spin_unlock_irqrestore(&receiver->authorization_lock, flags);
		if (ret)
			goto err_put;
	}

	file->private_data = context;
	ret = nonseekable_open(inode, file);
	if (!ret)
		return 0;

	if (context->writer) {
		spin_lock_irqsave(&receiver->authorization_lock, flags);
		if (receiver->writer_context == context)
			receiver->writer_context = NULL;
		spin_unlock_irqrestore(&receiver->authorization_lock, flags);
	}
err_put:
	kref_put(&receiver->refcount, hfx_receiver_release);
err_context:
	kfree(context);
	return ret;
}

static int hfx_session_release_file(struct inode *inode, struct file *file)
{
	struct hfx_file_context *context = file->private_data;
	struct hfx_receiver *receiver = context->receiver;
	unsigned long flags;

	(void)inode;
	if (context->writer) {
		mutex_lock(&receiver->transport_lock);
		spin_lock_irqsave(&receiver->authorization_lock, flags);
		if (receiver->writer_context == context) {
			hfx_session_revoke_locked(receiver,
						 HFX_UAPI_REVOKE_REASON_CLOSE);
			receiver->writer_context = NULL;
		}
		spin_unlock_irqrestore(&receiver->authorization_lock, flags);
		mutex_unlock(&receiver->transport_lock);
	}
	kref_put(&receiver->refcount, hfx_receiver_release);
	kfree(context);
	return 0;
}

static long hfx_session_get_info(struct hfx_receiver *receiver,
				 void __user *user_arg)
{
	struct hfx_uapi_info info = {
		.version = HFX_UAPI_ABI_VERSION,
		.size = sizeof(info),
		.receiver_generation = receiver->generation,
		.vendor_id = le16_to_cpu(receiver->udev->descriptor.idVendor),
		.product_id = le16_to_cpu(receiver->udev->descriptor.idProduct),
	};
	unsigned long flags;

	mutex_lock(&receiver->state_lock);
	if (receiver->disconnecting)
		info.flags |= HFX_UAPI_INFO_FLAG_DISCONNECTING;
	if (receiver->suspended_interfaces)
		info.flags |= HFX_UAPI_INFO_FLAG_SUSPENDED;
	info.bound_interfaces = min_t(unsigned int, receiver->bound_interfaces,
				      U16_MAX);
	mutex_unlock(&receiver->state_lock);

	spin_lock_irqsave(&receiver->authorization_lock, flags);
	hfx_session_expire_locked(receiver, ktime_get_boottime_ns());
	info.authorization_epoch = receiver->authorization_epoch;
	info.revoke_reason = receiver->session_revoke_reason;
	if (receiver->writer_context)
		info.flags |= HFX_UAPI_INFO_FLAG_WRITER_OPEN;
	if (receiver->session_active)
		info.flags |= HFX_UAPI_INFO_FLAG_SESSION_ACTIVE;
	spin_unlock_irqrestore(&receiver->authorization_lock, flags);

	return copy_to_user(user_arg, &info, sizeof(info)) ? -EFAULT : 0;
}

static long hfx_session_begin(struct hfx_receiver *receiver,
			      struct hfx_file_context *context,
			      void __user *user_arg)
{
	struct hfx_uapi_begin_session begin;
	unsigned long flags;
	u64 now_ns = ktime_get_boottime_ns();
	bool unavailable;
	long ret = 0;

	if (!context->writer)
		return -EACCES;
	if (copy_from_user(&begin, user_arg, sizeof(begin)))
		return -EFAULT;
	if (begin.version != HFX_UAPI_ABI_VERSION ||
	    begin.size != sizeof(begin))
		return -EPROTO;
	if (begin.authorization_epoch != 0)
		return -EINVAL;
	if (begin.receiver_generation != receiver->generation)
		return -ESTALE;
	if (begin.expires_boottime_ns <= now_ns ||
	    begin.expires_boottime_ns - now_ns > HFX_UAPI_MAX_SESSION_NS)
		return -ETIME;
	if (!memchr_inv(begin.profile_digest, 0, sizeof(begin.profile_digest)) ||
	    !memchr_inv(begin.capability_digest, 0,
			sizeof(begin.capability_digest)) ||
	    !memchr_inv(begin.daemon_nonce, 0, sizeof(begin.daemon_nonce)))
		return -EINVAL;

	mutex_lock(&receiver->transport_lock);
	mutex_lock(&receiver->state_lock);
	unavailable = receiver->disconnecting || receiver->suspended_interfaces ||
		!test_bit(0, receiver->bound_interface_numbers);
	mutex_unlock(&receiver->state_lock);
	if (unavailable) {
		ret = -EHOSTDOWN;
		goto out_unlock;
	}

	spin_lock_irqsave(&receiver->authorization_lock, flags);
	hfx_session_expire_locked(receiver, now_ns);
	if (receiver->writer_context != context) {
		ret = -EACCES;
	} else if (receiver->session_active) {
		ret = -EALREADY;
	} else if (hfx_observation_identity_conflicted(receiver)) {
		ret = -EKEYREJECTED;
	} else {
		receiver->authorization_epoch++;
		receiver->session_active = true;
		receiver->session_expires_boottime_ns =
			begin.expires_boottime_ns;
		receiver->session_max_dispatch_nonce = 0;
		receiver->session_revoke_reason = HFX_UAPI_REVOKE_REASON_NONE;
		memcpy(receiver->session_profile_digest, begin.profile_digest,
		       sizeof(receiver->session_profile_digest));
		memcpy(receiver->session_capability_digest,
		       begin.capability_digest,
		       sizeof(receiver->session_capability_digest));
		memcpy(receiver->session_daemon_nonce, begin.daemon_nonce,
		       sizeof(receiver->session_daemon_nonce));
		begin.authorization_epoch = receiver->authorization_epoch;
	}
	spin_unlock_irqrestore(&receiver->authorization_lock, flags);
	if (!ret && copy_to_user(user_arg, &begin, sizeof(begin))) {
		hfx_session_revoke(receiver,
				   HFX_UAPI_REVOKE_REASON_SERVICE_LOSS);
		ret = -EFAULT;
	}
out_unlock:
	mutex_unlock(&receiver->transport_lock);
	return ret;
}

static long hfx_session_end(struct hfx_receiver *receiver,
			    struct hfx_file_context *context,
			    void __user *user_arg)
{
	struct hfx_uapi_end_session end;
	unsigned long flags;
	long ret;

	if (!context->writer)
		return -EACCES;
	if (copy_from_user(&end, user_arg, sizeof(end)))
		return -EFAULT;
	if (end.version != HFX_UAPI_ABI_VERSION || end.size != sizeof(end))
		return -EPROTO;
	if (end.reserved ||
	    (end.reason != HFX_UAPI_REVOKE_REASON_EXPLICIT &&
	     end.reason != HFX_UAPI_REVOKE_REASON_SERVICE_LOSS))
		return -EINVAL;

	mutex_lock(&receiver->transport_lock);
	spin_lock_irqsave(&receiver->authorization_lock, flags);
	ret = hfx_session_check_locked(receiver, context,
				       end.receiver_generation,
				       end.authorization_epoch);
	if (!ret)
		hfx_session_revoke_locked(receiver, end.reason);
	spin_unlock_irqrestore(&receiver->authorization_lock, flags);
	mutex_unlock(&receiver->transport_lock);
	return ret;
}

static long hfx_session_ioctl(struct file *file, unsigned int command,
			      unsigned long argument)
{
	struct hfx_file_context *context = file->private_data;
	struct hfx_receiver *receiver = context->receiver;
	void __user *user_arg = (void __user *)argument;

	switch (command) {
	case HFX_UAPI_IOCTL_GET_INFO:
		return hfx_session_get_info(receiver, user_arg);
	case HFX_UAPI_IOCTL_BEGIN_SESSION:
		return hfx_session_begin(receiver, context, user_arg);
	case HFX_UAPI_IOCTL_END_SESSION:
		return hfx_session_end(receiver, context, user_arg);
	case HFX_UAPI_IOCTL_SUBMIT:
		return hfx_transport_submit(receiver, context, user_arg);
	case HFX_UAPI_IOCTL_GET_TRANSACTION_RESULT:
		return hfx_transport_get_result(receiver, context, user_arg);
	case HFX_UAPI_IOCTL_READ_OBSERVATIONS:
		return hfx_observation_read(receiver, user_arg);
	default:
		return -ENOTTY;
	}
}

#ifdef CONFIG_COMPAT
static long hfx_session_compat_ioctl(struct file *file, unsigned int command,
				     unsigned long argument)
{
	return hfx_session_ioctl(file, command,
				 (unsigned long)compat_ptr(argument));
}
#endif

static const struct file_operations hfx_session_fops = {
	.owner = THIS_MODULE,
	.open = hfx_session_open,
	.release = hfx_session_release_file,
	.unlocked_ioctl = hfx_session_ioctl,
#ifdef CONFIG_COMPAT
	.compat_ioctl = hfx_session_compat_ioctl,
#endif
};

int hfx_session_device_register(struct hfx_receiver *receiver)
{
	int ret;

	receiver->session_name = kasprintf(
		GFP_KERNEL, "hyperflux-next-%03u-%03u-g%llu",
		receiver->udev->bus->busnum, receiver->udev->devnum,
		receiver->generation);
	if (!receiver->session_name)
		return -ENOMEM;
	receiver->session_device.minor = MISC_DYNAMIC_MINOR;
	receiver->session_device.name = receiver->session_name;
	receiver->session_device.fops = &hfx_session_fops;
	receiver->session_device.parent = &receiver->udev->dev;
	receiver->session_device.mode = 0600;
	ret = misc_register(&receiver->session_device);
	if (ret) {
		kfree(receiver->session_name);
		receiver->session_name = NULL;
		return ret;
	}
	receiver->session_registered = true;
	return 0;
}
