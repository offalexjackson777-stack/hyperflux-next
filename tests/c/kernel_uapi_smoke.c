/* SPDX-License-Identifier: GPL-2.0-only */

#include <stddef.h>

#include "hyperflux_next.h"

_Static_assert(HFX_UAPI_ABI_VERSION == 1U, "unexpected ABI version");
_Static_assert(HFX_UAPI_IOCTL_MAGIC == 0xb7, "unexpected ioctl family");

_Static_assert(sizeof(struct hfx_uapi_info) == 40, "info layout drift");
_Static_assert(sizeof(struct hfx_uapi_begin_session) == 128, "begin layout drift");
_Static_assert(sizeof(struct hfx_uapi_end_session) == 32, "end layout drift");
_Static_assert(sizeof(struct hfx_uapi_frame) == 112, "frame layout drift");
_Static_assert(sizeof(struct hfx_uapi_submit) == 1872, "submit layout drift");
_Static_assert(sizeof(struct hfx_uapi_transaction_result) == 104, "result layout drift");
_Static_assert(sizeof(struct hfx_uapi_observation) == 40, "observation layout drift");
_Static_assert(sizeof(struct hfx_uapi_read_observations) == 1328, "observation batch layout drift");

_Static_assert(offsetof(struct hfx_uapi_info, version) == 0, "version must lead info");
_Static_assert(offsetof(struct hfx_uapi_info, size) == 4, "size must follow version");
_Static_assert(offsetof(struct hfx_uapi_submit, frames) == 72, "frame array offset drift");

_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_GET_INFO) == sizeof(struct hfx_uapi_info), "info ioctl size drift");
_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_BEGIN_SESSION) == sizeof(struct hfx_uapi_begin_session), "begin ioctl size drift");
_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_END_SESSION) == sizeof(struct hfx_uapi_end_session), "end ioctl size drift");
_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_SUBMIT) == sizeof(struct hfx_uapi_submit), "submit ioctl size drift");
_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_GET_TRANSACTION_RESULT) == sizeof(struct hfx_uapi_transaction_result), "result ioctl size drift");
_Static_assert(_IOC_SIZE(HFX_UAPI_IOCTL_READ_OBSERVATIONS) == sizeof(struct hfx_uapi_read_observations), "observation ioctl size drift");

int main(void)
{
    return 0;
}
