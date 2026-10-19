# Architecture

Start here if you want to know how HyperFlux Next works or why it was built this way. A
HyperFlux V2 receiver puts a Razer keyboard and mouse behind one USB dongle, and stock Linux
applications cannot reach devices behind it. HyperFlux Next is a kernel driver, a bridge
service, and thin connectors that let OpenRazer, Polychromatic, and OpenRGB reach those
devices without learning anything about the receiver.

## The Pieces In One Paragraph

The kernel driver keeps ordinary keyboard and mouse input working and gives userspace one
narrow way to talk to the receiver. The bridge is the only program that writes to the
receiver: it decides which devices are present, which may be written, and in what order.
Connectors use the published SDK to read the bridge's decisions and pass each
application's own commands through. The served protocol versions (5 and 7) and the kernel
interface are generated from checked-in registries, so no adapter can submit arbitrary receiver
frames or invent a device.

## Words You Will Meet

- **Route**: the bridge's handle for one device on one receiver connection.
- **Generation**: one physical connection of the receiver; a replug starts a new one, so
  old state cannot leak into it.
- **Connector**: the code that lets one application use HyperFlux devices through the SDK.
- **Bridge**: the `hyperflux-next-bridge` service that owns routes and receiver writes.
- **Qualification**: matching a receiver or device to a known, tested profile before any
  write is allowed.

## Where To Read Next

| If you want to know | Read |
| --- | --- |
| The whole design, with the reasons behind it | [Design book](../architecture/design-book.md) |
| What the kernel driver does and does not do | [Kernel boundary](../architecture/kernel.md) |
| How the bridge decides what may be written | [Bridge core](../architecture/bridge-core.md) |
| What is saved between restarts | [Persistence](../architecture/persistence.md) |
| How applications connect | [SDK boundary](../architecture/sdk.md) and [SDK and connectors](sdk-connectors.md) |
| How OpenRazer and OpenRGB are integrated | [Integration boundary](../architecture/integrations.md) |
| How new devices are added | [Device knowledge](../architecture/device-knowledge.md) and [native device onboarding](../architecture/native-device-onboarding.md) |
| Why a past decision was made | The decision records in `docs/architecture/decisions/`, starting with the [route selector](../architecture/decisions/declarative-route-selector.md) |
| What went wrong for users and how it was fixed | [Investigations](../investigations/README.md) |

To change code, read [Building](building.md) and [Verification](verification.md) next.
