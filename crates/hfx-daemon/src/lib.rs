// SPDX-License-Identifier: GPL-2.0-only

#![forbid(unsafe_code)]

mod authority;
mod configuration;
mod discovery;
mod entropy;
mod identity;
mod logging;
mod manager;
mod observation;
mod production;
mod production_restoration;
mod restoration;
mod service;
mod socket;

pub use authority::{
    DAEMON_NONCE_BYTES, WriterAuthorityError, derive_capability_digest, generate_daemon_nonce,
};
pub use configuration::{ProductionConfigError, load_production_config};
pub use discovery::{EndpointCandidate, EndpointDiscovery, EndpointDiscoveryError, EndpointName};
pub use identity::{ReceiverIdentityAuthority, ReceiverIdentityError};
pub use logging::{
    StructuredEventLogger, StructuredEventLoggerError, StructuredEventLoggerExit,
    StructuredEventSink,
};
pub use manager::{LinuxRuntimeManager, RuntimeTickError, RuntimeTickReport};
pub use observation::{
    PassiveDisposition, PassiveObservationTranslator, PassiveTranslation, PassiveTranslationError,
};
pub use production::{
    ProductionBackend, ProductionBuildError, ProductionComposition, ProductionTransport,
    ProductionWriter, compose_production,
};
pub use production_restoration::ProductionRestoration;
pub use restoration::{RestorationScheduleError, RestorationScheduler, RestorationTickReport};
pub use service::{
    ProductionServiceError, ProductionServiceExit, ProductionServicePaths, run_production_service,
};
pub use socket::{BoundUnixListener, SocketBindError};
