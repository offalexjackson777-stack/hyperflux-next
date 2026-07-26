// SPDX-License-Identifier: GPL-2.0-only

//! Feature-only UHID transport for qualified `HyperFlux` native routes.
//!
//! This crate deliberately creates no keyboard, pointer, consumer-control, or
//! other input collections. `OpenRazer` continues to consume the existing
//! physical input devices; UHID carries only its 90-byte vendor Feature
//! transactions through the `HyperFlux` bridge.

mod bridge;
mod catalog;
mod direct;
mod endpoint;
mod qualification;
mod qualification_runtime;
mod runtime;
mod serial;
mod supervisor;
mod uhid;

pub use bridge::{
    BridgeExchange, BridgeExchangeResult, NativeBridge, NativeBridgeError, NativeRouteMonitor,
};
pub use catalog::{
    NativeQualificationProbe, NativeRouteCatalog, NativeRouteCatalogError, NativeRouteRecord,
};
pub use direct::{
    DirectRouteScanError, DirectRouteScanner, DirectRouteSnapshot, DirectRouteTransition,
};
pub use endpoint::{EndpointError, FeatureEndpoint, FeatureEndpointState};
pub use qualification_runtime::{
    NATIVE_QUALIFICATION_APPROVAL, QualificationRunConfig, QualificationRunPaths,
    QualificationRuntimeError, run_qualification,
};
pub use runtime::{RelayPaths, RelayRuntimeError, run_relay};
pub use serial::{SerialFingerprint, SerialQueryError, SerialQuerySpec, fingerprint_serial_bytes};
pub use supervisor::{RouteAction, RouteIdentity, RouteSupervisor, RouteSupervisorError};
pub use uhid::{
    FEATURE_REPORT_BYTES, HYPERFLUX_PHYS_PREFIX, TRANSPORT_ENVELOPE_MAGIC, TransportEnvelopeKind,
    UHID_EVENT_BYTES, UhidCodecError, UhidEvent, UhidReply, VirtualDeviceSpec,
    decode_transport_envelope, encode_transport_envelope,
};
