# OpenRazer Patch Removal Routes

**Status:** Proposed

## Context

The native OpenRazer route depends on one downstream driver change,
[`0001-Add-trusted-HyperFlux-native-HID-transport.patch`](../../../integrations/openrazer/native-route/patches/0001-Add-trusted-HyperFlux-native-HID-transport.patch),
which applies to OpenRazer commit `1ef8a91712906a89fd332a2ed62bbd105315d6d1`
(driver `3.12.4`). The patch has five effects:

1. It binds `razerkbd` and `razermouse` to `BUS_VIRTUAL` UHID devices. A device
   qualifies only when its phys starts with `hyperflux-next/native/v1/`, its
   vendor is `0x1532`, its product is already in the driver's USB table, and its
   report descriptor is exactly the feature-only descriptor
   `0600ff0901a101150026ff007508955ab102c0`, a single 90-byte vendor feature
   report.
2. It replaces `usb_control_msg` with `hid_hw_raw_request` feature SET and GET
   on report 0 for those devices only.
3. It decodes the relay's in-band `HFXNERR1` error envelope into
   `-EAGAIN` (`not-sent`) and `-ENOTRECOVERABLE` (`may-have-been-sent`). Any
   short or mismatched response also becomes `may-have-been-sent`.
4. Through the `razer_send_payload` macros, it makes the enclosing sysfs
   handler return the first transport, device-status, or correlation error. A
   multi-command store stops at the first failure, and userspace receives an
   errno instead of a silent success.
5. It adds UHID udev bind rules, moves the access group from `plugdev` to
   `openrazer`, and publishes the
   `MODULE_INFO(hyperflux_native_transport, "razer-feature-report-v1")`
   capability that [`deployment.json`](../../../integrations/openrazer/native-route/deployment.json)
   requires before the route is offered.

The patch is why HyperFlux ships its own DKMS `openrazer-driver` that conflicts
with and replaces the distribution package
([integrations](../integrations.md)). Removing it would remove a rebase burden
on every OpenRazer release and a package conflict for every user.

The certainty contract that any replacement must respect is already stated in
the [kernel](../kernel.md), [bridge core](../bridge-core.md), and
[design book](../design-book.md):

- `not-sent` means SET did not begin; an exact retry may be considered;
- `may-have-been-sent` means SET may have begun and no layer may repeat it;
- an uncertain SET is never automatically repeated.

This note compares two removal routes against today's design:

- **Route A:** present each qualified child as a virtual USB device through
  USB/IP and `vhci-hcd`, so stock drivers bind unmodified;
- **Route B:** upstream a generic HID feature-report transport for non-USB Razer
  devices, so the patch is no longer needed.

## Stock OpenRazer Error Behavior

Both routes are judged against what unmodified OpenRazer does with transport
outcomes. The facts below come from the pinned commit.

- `razer_get_usb_response` in `driver/razercommon.c` stores the SET result in
  `retval`, never reads it, and always issues the GET.
- A GET whose length is not 90 returns positive `1`. A response whose packet
  count, class, or command does not match returns `-EIO`. `RAZER_CMD_BUSY`
  counts as success.
- The driver contains no retry loops.
- Of the 88 `razer_send_payload` call sites in `razerkbd_driver.c` and 66 in
  `razermouse_driver.c`, none checks the return value. Store handlers return
  `count` and show handlers format whatever is in the response buffer, so a
  failed read yields zero-valued data rather than an error.
- The daemon writes sysfs with `open(..., 'w')` and does not retry. Effect
  methods update persistence before they write, and device initialization
  replays persisted state: `restore_dpi_poll_rate` and `restore_brightness`
  always run, and `restore_effect` runs when `restore_persistence` is enabled.

## Route A: USB/IP Virtual Device

### Shape

A USB/IP server, either a front end in `hfx-uhid-relay` or a sibling crate,
holds one end of an `AF_UNIX` `SOCK_STREAM` socketpair. It writes
`port sockfd devid speed` to `/sys/devices/platform/vhci_hcd.0/attach`.
`vhci-hcd` then enumerates a virtual USB device, and stock `razerkbd` or
`razermouse` binds through the existing USB id tables and `99-razer.rules`. The
server translates only class `SET_REPORT` and `GET_REPORT` control transfers
into protocol v7 native exchanges through the existing
[`FeatureEndpoint`](../../../crates/hfx-uhid-relay/src/endpoint.rs). It answers
everything else itself.

### Enumeration To Emulate

The two cataloged children set the constraints:

- **DeathStalker V2 Pro TKL:** `razerkbd`, wireless PID `0x0296`,
  `report_index 0x02`, 4900–5000 µs wait after SET;
- **Basilisk V3 Pro 35K:** `razermouse`, wireless PID `0x00CD`,
  `report_index 0x00`, 31000–31100 µs wait after SET.

`razer_get_report_params` sends SET and GET to `wIndex = report_index`
regardless of the interface the driver bound to. The emulated device therefore
needs:

- a device descriptor with `bcdUSB 0x0200`, vendor `0x1532`, the child's
  wireless PID, and manufacturer, product, and serial strings;
- one configuration whose HID interface number equals the PID's
  `report_index`, with any lower-numbered interfaces declared as vendor class
  `0xFF` with no endpoints so that nothing binds to them;
- HID class, subclass `0x00` (`0x01` only for the DeathAdder V2 family that
  `razermouse` filters on), and `bInterfaceProtocol 0x02`, because `razerkbd`
  creates its sysfs attributes only on the mouse-protocol interface;
- a HID class descriptor that returns the same feature-only report descriptor
  used today, so `hid-input` creates no input nodes;
- one interrupt-IN endpoint. `usbhid_probe` returns `-ENODEV` without one.
  The server must hold its URBs pending indefinitely and must never complete
  them with an error, because `hid_io_error` escalates to
  `usb_queue_reset_device` after about one second.

The server answers these requests locally: standard `GET_DESCRIPTOR`,
`SET_CONFIGURATION`, `GET_STATUS`, `SET_IDLE` (from `usbhid_parse`), and
`USBIP_CMD_UNLINK`. `vhci-hcd` already handles `SET_ADDRESS` and the first
device-descriptor read. Probe is otherwise harmless: `razerkbd` sends
`razer_set_device_mode(dev, 0, 0)` on `BUS_USB`, and the relay already
virtualizes device mode. The driver's `usleep_range` waits add 5 ms or 31 ms of
latency to every exchange.

`razer_send_payload_no_response` issues a SET with no GET. Today's
`FeatureEndpoint` rejects a SET while a response is pending, so the front end
would have to discard an unclaimed response when the next SET arrives.

### Duplicate Devices And Second Writers

The feature-only descriptor prevents duplicate keyboard and pointer input nodes,
exactly as UHID does today. The device is still a real `BUS_USB` hidraw node,
and that changes who can reach it:

- hidapi's hidraw backend enumerates it. Wherever a distribution or OpenRGB
  udev rule grants seat access to the child PID, stock OpenRGB can detect a
  second controller beside the HyperFlux OpenRGB plugin controller and drive the
  same child as an independent writer.
- Control-endpoint SET and GET pairing cannot tell two writers apart, so a
  second writer can consume or replace the first writer's response.
- Today's `BUS_VIRTUAL` device does not match USB-bus hidapi enumeration.

Mitigations would need USB authorization (`authorized_default=0` on the vhci
root hubs with per-device approval), hidraw udev restrictions that override
distribution rules, and an OpenRGB detector test for every child PID. The wired
direct-route withdrawal becomes a vhci detach. Macro-key event paths are
unchanged from today because no input interface exists in either design.

### Privilege And Attach Flow

- `vhci-hcd` must be loaded through a packaged `modules-load.d` entry, because
  the relay unit sets `ProtectKernelModules=yes`.
- `attach` and `detach` are `DEVICE_ATTR_WO` (mode `0200`). `attach_store`
  performs no `capable()` check and only requires a `SOCK_STREAM` socket, so an
  `AF_UNIX` socketpair should qualify. This must be validated on a disposable
  kernel before anything depends on it.
- The relay unit currently grants only `/dev/uhid` under `ProtectSystem=strict`
  and `ProtectKernelTunables=yes`. It would need write access to the vhci
  `attach` and `detach` attributes, or a separate root helper that receives the
  socket.
- The server must choose a free port from `status` and races with any manual
  `usbip attach` on the same host.
- Closing the socket or crashing the relay makes `vhci-hcd` shut the port down,
  which fails closed.

### Security Surface

UHID lets its holder impersonate HID devices only. A vhci attach lets its holder
impersonate any USB device class, and modalias autoloading can then bind
storage, network, or other drivers to attacker-chosen descriptors. `vhci-hcd`
and its USB/IP receive path have a long fix history (`CVE-2016-3955`,
`CVE-2017-16911`, `CVE-2021-29265`, and continuing syzbot reports), and most
distributions do not load it by default. A compromised relay would gain a much
larger kernel attack surface than `/dev/uhid` gives it today. A descriptor
allow-list and USB authorization reduce this, but neither removes it.

### Can Stock OpenRazer Double-Send?

Neither stock OpenRazer, usbcore, `usbhid` on the control endpoint, nor
`vhci-hcd` automatically repeats a control SET. `USB_CTRL_SET_TIMEOUT` (5 s)
unlinks a pending SET and the driver still issues its GET, which the server must
answer with a STALL. The relay begins each translated SET at most once. **At the
transport level, route A does not double-send.**

Route A still violates the contract, because it erases certainty instead of
downgrading it:

- The SET result is discarded, a GET STALL becomes an ignored positive `1`, a
  mismatch becomes an ignored `-EIO`, and every store returns success.
- Multi-command stores, such as custom-frame rows and keyswitch pairs, continue
  after an uncertain step.
- The daemon has already updated persistence and reports success over D-Bus.
- Read attributes such as battery and firmware return zero-valued data instead
  of an error.

These are the repeat paths that remain:

1. **Re-enumeration replay.** Every relay restart, socket loss, or reattach
   re-enumerates the device, and the daemon replays persisted DPI, poll rate,
   brightness, and optionally the last effect. Commands whose outcome was
   `may-have-been-sent` are sent again. This is safe today only because the
   native command policy admits absolute-state commands alone; a relative or
   counter-advancing command would not be safe.
2. **Human retry.** The UI reports success after an uncertain write. A user who
   sees no change retries by hand without knowing the first write may have
   applied.
3. **Second writers.** A hidraw co-writer, as described above, can issue its
   own SET for the same state.

The front end cannot repair this inside the protocol. Answering GET with a
fabricated `RAZER_CMD_FAILURE` would convert transport uncertainty into a device
status, which the bridge forbids, and the stock driver would still ignore it.
The best route A can do is STALL the GET and record exact certainty in relay
evidence that no application reads.

## Route B: Upstream Non-USB Feature Transport

### Proposed Change

Upstream a transport that is generic, unbranded, and selected by bus and report
descriptor rather than by HyperFlux phys:

```diff
--- a/driver/razercommon.h
+++ b/driver/razercommon.h
+/* Feature report 0, 90 bytes, used when the device is not reached over USB. */
+bool razer_hid_feature_transport(struct hid_device *hdev);
+int razer_hid_feature_exchange(struct hid_device *hdev,
+                               struct razer_report *request,
+                               struct razer_report *response);
--- a/driver/razercommon.c
+++ b/driver/razercommon.c
+bool razer_hid_feature_transport(struct hid_device *hdev)
+{
+    struct hid_report *report;
+
+    if (hdev->bus == BUS_USB)
+        return false;
+    report = hdev->report_enum[HID_FEATURE_REPORT].report_id_hash[0];
+    return report && hid_report_len(report) == RAZER_USB_REPORT_LEN;
+}
+
+int razer_hid_feature_exchange(struct hid_device *hdev,
+                               struct razer_report *request,
+                               struct razer_report *response)
+{
+    /* One SET, then one GET; never repeated. Any failure is returned. */
+    ret = hid_hw_raw_request(hdev, 0, buf, RAZER_USB_REPORT_LEN,
+                             HID_FEATURE_REPORT, HID_REQ_SET_REPORT);
+    if (ret < 0)
+        return ret;
+    if (ret != RAZER_USB_REPORT_LEN)
+        return -EIO;
+    ret = hid_hw_raw_request(hdev, 0, buf, RAZER_USB_REPORT_LEN,
+                             HID_FEATURE_REPORT, HID_REQ_GET_REPORT);
+    ...
+}
--- a/driver/razerkbd_driver.c
+++ b/driver/razerkbd_driver.c
 static const struct hid_device_id razer_devices[] = {
     ...
+    { HID_DEVICE(BUS_VIRTUAL, HID_GROUP_ANY, USB_VENDOR_ID_RAZER, HID_ANY_ID) },
     { 0 }
 };
```

The change also needs:

- `razer_get_report_params` keyed by a `u16` product, so non-USB devices reuse
  the existing per-PID table;
- probe and init that never dereference `usb_dev` or the USB interface for a
  non-USB device, and skip `razer_set_device_mode` and
  `usb_disable_autosuspend` there;
- a probe gate that accepts a non-USB device only when its product is already in
  the driver's USB id table;
- for non-USB devices only, return-value propagation from `razer_send_payload`
  that stops multi-command stores and returns the errno to userspace. USB
  behavior stays byte-for-byte unchanged. On USB, `report_index` can differ from
  the bound interface, so the control transfer must stay as it is.

### What HyperFlux Gives Up

Upstream will not accept a HyperFlux phys prefix, the `HFXNERR1` envelope, or a
`hyperflux_native_transport` module field. Because `uhid.c` turns any nonzero
reply error into `-EIO`, the kernel driver can no longer tell `not-sent` from
`may-have-been-sent`. The proposed contract is conservative:

- the relay answers `not-sent` with an error in `UHID_SET_REPORT_REPLY`;
- it answers `may-have-been-sent` with SET success and an error in
  `UHID_GET_REPORT_REPLY`;
- the driver returns `-EIO` for either, and every consumer treats a non-USB
  transport failure as possibly applied.

No current consumer uses the `not-sent` retry permission: the daemon and
Polychromatic never retry, and the bridge keeps exact certainty in its own
status and evidence. The conservative mapping only removes an unused
permission. It never permits a repeat that today's design forbids.

Trust does not weaken. Creating a UHID device already requires write access to
`/dev/uhid`, which is root-only, so whoever can present a matching
`BUS_VIRTUAL` device can already present an equivalent USB-bus HID device
through UHID.

### Residue After Upstreaming

- **Capability detection.** `deployment.json` would read a released OpenRazer
  version, or a generic upstream `MODULE_INFO` field if maintainers accept one,
  instead of `hyperflux_native_transport`.
- **Access group.** Upstream `99-razer.rules` uses `plugdev`. HyperFlux would
  ship a separate rules file for `0006:1532:*` bind events and its group policy,
  or adopt `plugdev`, instead of rewriting upstream files.
- **Bind rules.** Upstream would carry only the `HID_DEVICE` id-table entries.
  A distribution-neutral udev bind rule for virtual devices may stay in HyperFlux.
- **Transition.** Until a release that contains the change reaches the supported
  distributions, today's patch and DKMS ownership stay in place.

### Draft Pitch (Not Posted)

> **Title:** Support Razer devices reached over a non-USB HID transport
>
> OpenRazer assumes every device sits behind a `usb_device`: it reads the PID
> from the USB descriptor and sends commands with `usb_control_msg`. Devices
> reached through another HID transport, such as `uhid` (`BUS_VIRTUAL`), have
> the same 90-byte feature-report protocol but cannot bind.
>
> This series adds a transport that uses `hid_hw_raw_request` feature SET and
> GET on report 0. It is selected only when the device is not on `BUS_USB` and
> its report descriptor declares a 90-byte feature report. USB devices are
> unchanged. Non-USB devices reuse the existing per-PID parameter table, skip
> USB-only probe steps, and return transport errors to userspace instead of
> reporting success.
>
> The immediate user is a userspace receiver bridge that exposes paired
> wireless children through `uhid`. The same code is a first step toward
> Bluetooth-connected devices, which users report as undetected in #2167,
> #2407, #2090, #2653, and #1339. Bluetooth support is not claimed here:
> those devices use other vendor IDs (for example `068E:00CE`) and their
> descriptors have not been checked.

## Comparison

| Concern | Today's patch | Route A: USB/IP | Route B: upstream |
| --- | --- | --- | --- |
| Downstream kernel change | One patch, DKMS owned | None | None after release |
| Errno reaches userspace | Yes | No | Yes |
| Certainty classes | Exact | Erased | Conservative |
| Multi-command stop on error | Yes | No | Yes |
| Automatic repeat by stack | None | None | None |
| Replay on re-enumeration | Absolute-state only | Absolute-state only | Absolute-state only |
| Kernel surface | `/dev/uhid` | `/dev/uhid` and `vhci-hcd` | `/dev/uhid` |
| Second hidraw writers | Not enumerated | Enumerated | Not enumerated |

## Decision

1. **Pursue route B** as the path to removing the patch. Prepare the
   upstream-shaped series against the pinned OpenRazer commit and keep it local
   until maintainers are approached deliberately.
2. **Keep today's patch** and DKMS ownership unchanged until a released
   OpenRazer containing the transport reaches the supported distributions.
3. **Reject route A** as the default route. It removes the patch, but it erases
   certainty, hides failures from users, adds `vhci-hcd` attack surface and
   privilege, and exposes the child to second writers. It may be kept only as an
   isolated experiment on a disposable virtual machine to confirm whether a
   stock-only fallback is technically possible.

## Evidence

- OpenRazer commit `1ef8a91712906a89fd332a2ed62bbd105315d6d1`:
  `driver/razercommon.c`, `driver/razerkbd_driver.c`,
  `driver/razermouse_driver.c`, `install_files/udev/99-razer.rules`, and the
  daemon's `device_base.py` and `chroma_keyboard.py`.
- Linux `v6.18`: `drivers/usb/usbip/vhci_sysfs.c` (attach checks),
  `drivers/usb/usbip/vhci_hcd.c` (local `SET_ADDRESS` and descriptor handling,
  unlink without resubmission), `drivers/hid/usbhid/hid-core.c` (interrupt-IN
  requirement, `SET_IDLE`, `hid_io_error` reset), and `drivers/hid/uhid.c`
  (reply errors become `-EIO`).
- HyperFlux: `crates/hfx-uhid-relay/src/endpoint.rs`,
  `crates/hfx-uhid-relay/src/uhid.rs`, the native route
  [`catalog.json`](../../../integrations/openrazer/native-route/catalog.json),
  and the generated relay service unit.
- No upstream OpenRazer pull request for a `uhid`, `hid_hw_raw_request`, or
  Bluetooth transport was found when this note was written.

Nothing in this note was run against hardware, a loaded `vhci-hcd`, or the
OpenRazer maintainers.

## Consequences

- No runtime behavior changes now.
- The upstream series becomes the tracked removal path. Its acceptance, not a
  local redesign, decides when DKMS ownership ends.
- If route B is accepted with the conservative certainty mapping, HyperFlux
  loses only the unused kernel-level `not-sent` retry permission. Bridge
  evidence keeps exact certainty.
- The native command policy's absolute-state-only rule remains load-bearing on
  every route, because every route replays persisted state on re-enumeration.

## Alternatives Rejected

- **Route A as the default:** rejected for certainty erasure, privilege, kernel
  surface, and second writers, as described above.
- **Upstreaming today's patch unchanged:** its HyperFlux phys prefix, branded
  envelope, and module field are product-specific and would not be accepted.
- **Expressing `not-sent` through UHID reply errors alone:** `uhid.c` maps every
  nonzero reply error to `-EIO`, so the kernel cannot see the distinction
  without an in-band envelope.
