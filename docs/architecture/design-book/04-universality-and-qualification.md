# Design Book Chapter IV: Universality And Qualification

This chapter is part of the [HyperFlux Next Design Book](../design-book.md).

## 15. Support Levels

Support must distinguish upstream model support from HyperFlux transport
qualification. A device may be a reviewed candidate or have extensive OpenRazer
and OpenRGB knowledge while its receiver route remains non-writable. A native
route becomes qualified only when exact receiver-observed identity, source-bound
upstream records, a matching fixed report family, response correlation, and
physical transport evidence agree. Upstream applications decide which model
capabilities to expose after that route exists.

## 16. Unknown Device Behavior

When the receiver reports an unfamiliar child PID, HyperFlux may expose bounded
identity and passive observations. It sends no command copied from a similar
model and infers no route authority from a support list. A same-protocol device
advances only through source-bound command semantics, a declared receiver
selector translation when required, matched responses, and reviewed profile
data.

## 17. Declarative Transport Profiles

Applications keep their own model capabilities and command logic. HyperFlux
profiles declare transport facts such as exact identity, protocol family, fixed
report shape, route qualification, and response correlation; they never grant
support from a PID list alone. This is the key to universality because another
same-protocol device can gain a qualified native route through profile data and
upstream bindings without adding model branches to the kernel, bridge, SDK,
relay, or application transport adapters.

---

- Previous:
  [Chapter III: Devices, Routes And Truthful State](03-devices-routes-and-truthful-state.md)
- [Design Book index](../design-book.md)
- Next: [Chapter V: Application Integrations](05-application-integrations.md)
