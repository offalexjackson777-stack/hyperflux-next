/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef HYPERFLUX_NEXT_INTERNAL_H
#define HYPERFLUX_NEXT_INTERNAL_H

#include <linux/bitmap.h>
#include <linux/hid.h>
#include <linux/kref.h>
#include <linux/list.h>
#include <linux/miscdevice.h>
#include <linux/mutex.h>
#include <linux/power_supply.h>
#include <linux/spinlock.h>
#include <linux/usb.h>

#include "hyperflux-next-battery.h"
#include "hyperflux-next-profile.h"
#include "uapi/hyperflux_next.h"

#define HFX_USB_INTERFACE_LIMIT 256
#define HFX_OBSERVATION_CAPACITY 256U
#define HFX_RESULT_CAPACITY 64U
#define HFX_TOMBSTONE_CAPACITY 64U
#define HFX_ACTIVITY_INTERVAL_NS 250000000ULL
#define HFX_BATTERY_NAME_BYTES 64

struct hfx_file_context;
struct hfx_pairing_inventory;

struct hfx_result_record {
	struct list_head node;
	struct hfx_uapi_transaction_result result;
	u32 frame_count;
	struct hfx_uapi_frame frames[HFX_UAPI_MAX_FRAMES];
};

struct hfx_result_tombstone {
	bool valid;
	u64 receiver_generation;
	u64 authorization_epoch;
	u64 dispatch_nonce;
	u8 request_digest[HFX_UAPI_DIGEST_BYTES];
};

struct hfx_native_result_record {
	struct list_head node;
	struct hfx_uapi_native_exchange_result result;
	u8 request[HFX_UAPI_NATIVE_REPORT_BYTES];
};

struct hfx_native_result_tombstone {
	bool valid;
	struct hfx_uapi_native_exchange_result result;
	u8 request[HFX_UAPI_NATIVE_REPORT_BYTES];
};

/*
 * One child lane's bridge-forwarded battery state.  psy, parent, hdev, owner,
 * and desc change only under battery_lock; the published values change only
 * under value_lock.  hdev is the lane's own input interface; parent is the
 * interface psy is registered under.
 */
struct hfx_battery_lane {
	struct power_supply *psy;
	struct power_supply_desc desc;
	struct hid_device *hdev;
	struct hid_device *parent;
	struct hfx_file_context *owner;
	const char *suffix;
	spinlock_t value_lock;
	u32 capacity;
	u32 status;
	char name[HFX_BATTERY_NAME_BYTES];
	char model_name[HFX_UAPI_BATTERY_MODEL_NAME_BYTES];
};

struct hfx_receiver {
	struct list_head node;
	struct usb_device *udev;
	struct kref refcount;
	struct mutex state_lock;
	struct mutex transport_lock;
	spinlock_t authorization_lock;
	spinlock_t observation_lock;
	struct miscdevice session_device;
	char *session_name;
	u64 generation;
	u32 backend_id;
	const struct hfx_receiver_profile *profile;
	DECLARE_BITMAP(bound_interface_numbers, HFX_USB_INTERFACE_LIMIT);
	unsigned int bound_interfaces;
	unsigned int suspended_interfaces;
	bool disconnecting;
	bool session_registered;
	struct hfx_file_context *writer_context;
	bool session_active;
	u64 authorization_epoch;
	u64 session_expires_boottime_ns;
	u64 session_max_dispatch_nonce;
	u8 session_profile_digest[HFX_UAPI_DIGEST_BYTES];
	u8 session_capability_digest[HFX_UAPI_DIGEST_BYTES];
	u8 session_daemon_nonce[HFX_UAPI_NONCE_BYTES];
	u32 session_revoke_reason;
	struct hfx_uapi_observation *observation_ring;
	u32 observation_head;
	u32 observation_count;
	u64 next_observation_sequence;
	u64 last_activity_boottime_ns[3];
	u16 observed_product_id[3];
	bool observed_product_id_valid[3];
	bool identity_conflict;
	struct list_head result_records;
	u32 result_count;
	u64 next_kernel_sequence;
	struct hfx_result_tombstone *result_tombstones;
	u32 tombstone_head;
	u32 tombstone_count;
	struct list_head native_result_records;
	u32 native_result_count;
	struct hfx_native_result_tombstone *native_result_tombstones;
	u32 native_tombstone_head;
	u32 native_tombstone_count;
	struct mutex battery_lock;
	struct hfx_battery_lane battery[HFX_BATTERY_LANES];
	struct hid_device *battery_parent;
	int battery_parent_interface;
};

struct hfx_interface {
	struct hid_device *hdev;
	struct hfx_receiver *receiver;
	int interface_number;
	bool started;
	bool suspended;
};

struct hfx_file_context {
	struct hfx_receiver *receiver;
	bool writer;
};

void hfx_receiver_release(struct kref *refcount);

void hfx_session_revoke_locked(struct hfx_receiver *receiver, u32 reason);
void hfx_session_revoke(struct hfx_receiver *receiver, u32 reason);
int hfx_session_check(struct hfx_receiver *receiver,
		      struct hfx_file_context *context,
		      u64 generation, u64 authorization_epoch);
bool hfx_session_nonce_not_observed(struct hfx_receiver *receiver,
				    struct hfx_file_context *context,
				    u64 authorization_epoch,
				    u64 dispatch_nonce);
int hfx_session_device_register(struct hfx_receiver *receiver);

void hfx_observation_emit(struct hfx_receiver *receiver, u32 kind,
			  u32 endpoint_slot, u32 source, u32 confidence,
			  u32 value, u32 auxiliary);
void hfx_observe_activity(struct hfx_receiver *receiver, int interface_number,
			  unsigned int report_id, int size);
void hfx_observe_passive(struct hfx_receiver *receiver, int interface_number,
			 const u8 *data, int size);
void hfx_observe_feature_battery(struct hfx_receiver *receiver,
				 u32 endpoint_slot, const u8 *request,
				 const u8 *response);
bool hfx_observation_identity_complete(struct hfx_receiver *receiver);
bool hfx_observe_pairing_inventory(struct hfx_receiver *receiver,
				   const struct hfx_pairing_inventory *inventory);
void hfx_observation_replay_cached_identities(struct hfx_receiver *receiver);
bool hfx_observation_identity_conflicted(struct hfx_receiver *receiver);
long hfx_observation_read(struct hfx_receiver *receiver, void __user *user_arg);

long hfx_transport_submit(struct hfx_receiver *receiver,
			  struct hfx_file_context *context,
			  void __user *user_arg);
long hfx_transport_get_result(struct hfx_receiver *receiver,
			      struct hfx_file_context *context,
			      void __user *user_arg);
void hfx_transport_free_journal(struct hfx_receiver *receiver);

long hfx_native_exchange_submit(struct hfx_receiver *receiver,
				struct hfx_file_context *context,
				void __user *user_arg);
long hfx_native_exchange_get_result(struct hfx_receiver *receiver,
				    struct hfx_file_context *context,
				    void __user *user_arg);
void hfx_native_exchange_free_journal(struct hfx_receiver *receiver);

void hfx_battery_init(struct hfx_receiver *receiver);
long hfx_battery_report(struct hfx_receiver *receiver,
			struct hfx_file_context *context,
			void __user *user_arg);
void hfx_battery_attach_interface(struct hfx_receiver *receiver,
				  int interface_number,
				  struct hid_device *hdev);
void hfx_battery_detach_interface(struct hfx_receiver *receiver,
				  int interface_number);
void hfx_battery_release_owner(struct hfx_receiver *receiver,
			       struct hfx_file_context *context);
void hfx_battery_teardown(struct hfx_receiver *receiver);

#endif
