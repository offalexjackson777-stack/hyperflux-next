/* SPDX-License-Identifier: GPL-2.0-only */

#include <stddef.h>

#include "hyperflux_next.h"

_Static_assert(HFX_UAPI_ABI_VERSION == 1U, "unexpected ABI version");
_Static_assert(HFX_UAPI_NATIVE_ABI_VERSION == 2U, "unexpected native ABI version");
_Static_assert(HFX_UAPI_POWER_SUPPLY_ABI_VERSION == 3U, "unexpected power-supply ABI version");
_Static_assert(HFX_UAPI_IOCTL_MAGIC == 0xb7, "unexpected ioctl family");

_Static_assert(sizeof(struct hfx_uapi_info) == 40, "info layout drift");
_Static_assert(sizeof(struct hfx_uapi_begin_session) == 128, "begin layout drift");
_Static_assert(sizeof(struct hfx_uapi_end_session) == 32, "end layout drift");
_Static_assert(sizeof(struct hfx_uapi_frame) == 112, "frame layout drift");
_Static_assert(sizeof(struct hfx_uapi_submit) == 1872, "submit layout drift");
_Static_assert(sizeof(struct hfx_uapi_transaction_result) == 104, "result layout drift");
_Static_assert(sizeof(struct hfx_uapi_observation) == 40, "observation layout drift");
_Static_assert(sizeof(struct hfx_uapi_read_observations) == 1328, "observation batch layout drift");
_Static_assert(sizeof(struct hfx_uapi_native_exchange_submit) == 216, "native submit layout drift");
_Static_assert(sizeof(struct hfx_uapi_native_exchange_result) == 216, "native result layout drift");
_Static_assert(sizeof(struct hfx_uapi_child_battery) == 64, "child battery layout drift");

_Static_assert(offsetof(struct hfx_uapi_info, version) == 0, "version must lead info");
_Static_assert(offsetof(struct hfx_uapi_info, size) == 4, "size must follow version");
_Static_assert(offsetof(struct hfx_uapi_submit, frames) == 72, "frame array offset drift");
_Static_assert(offsetof(struct hfx_uapi_native_exchange_submit, report) == 112, "native report offset drift");
_Static_assert(offsetof(struct hfx_uapi_native_exchange_result, response) == 120, "native response offset drift");
_Static_assert(offsetof(struct hfx_uapi_child_battery, receiver_generation) == 8, "child battery generation offset drift");
_Static_assert(offsetof(struct hfx_uapi_child_battery, capacity) == 24, "child battery capacity offset drift");
_Static_assert(offsetof(struct hfx_uapi_child_battery, model_name) == 32, "child battery model name offset drift");
_Static_assert(HFX_UAPI_BATTERY_MODEL_NAME_BYTES == 32U, "child battery model name bound drift");
_Static_assert(HFX_UAPI_BATTERY_MAX_CAPACITY == 100U, "child battery capacity bound drift");
_Static_assert(HFX_UAPI_BATTERY_STATUS_UNKNOWN == 0U, "unknown battery status must be the zero value");

_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_GET_INFO) == sizeof(struct hfx_uapi_info), "info ioctl size drift");
_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_BEGIN_SESSION) == sizeof(struct hfx_uapi_begin_session), "begin ioctl size drift");
_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_END_SESSION) == sizeof(struct hfx_uapi_end_session), "end ioctl size drift");
_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_SUBMIT) == sizeof(struct hfx_uapi_submit), "submit ioctl size drift");
_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_GET_TRANSACTION_RESULT) == sizeof(struct hfx_uapi_transaction_result), "result ioctl size drift");
_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_READ_OBSERVATIONS) == sizeof(struct hfx_uapi_read_observations), "observation ioctl size drift");
_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_SUBMIT_NATIVE_EXCHANGE) == sizeof(struct hfx_uapi_native_exchange_submit), "native submit ioctl size drift");
_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_GET_NATIVE_EXCHANGE_RESULT) == sizeof(struct hfx_uapi_native_exchange_result), "native result ioctl size drift");
_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_REPORT_CHILD_BATTERY) == sizeof(struct hfx_uapi_child_battery), "child battery ioctl size drift");
_Static_assert(_IOC_DIR(HFX_UAPI_IOCTL_REPORT_CHILD_BATTERY) == _IOC_WRITE, "child battery forwarding must be write-only");
_Static_assert(_IOC_NR(HFX_UAPI_IOCTL_REPORT_CHILD_BATTERY) == 0x08, "child battery ioctl number drift");

int main(void)
{
    return 0;
}
