// SPDX-License-Identifier: GPL-2.0-only

use crate::{FramedIoChannel, RequestIdentitySource, SdkError, SdkMethod};
use hfx_domain::{
    ClientId, ClientName, EventBatchLimit, GenerationId, LeaseDurationMs, LeaseId, MonotonicMs,
    ProfileDigest, ProfileId, ProtocolFeatureId, ProtocolVersion, ReceiverId, RequestId,
    TransactionClass, TransactionId,
};
use hfx_protocol::{
    BridgeSnapshot, ClientHello, DeviceProfileBinding, DiagnosticSnapshot, EmptyRequest,
    EventBatch, EventCursor, IntegrationView, LeaseRequest, LeaseResult, LightingFrame,
    NegotiationRequestEnvelope, ReleaseLeaseRequest, RenewLeaseRequest, RpcRequest, RpcResponse,
    ServerHello, SessionRequestEnvelope, StableLightingIntent, SubscriptionRequest,
    TransactionLookup, TransactionRequest, TransactionResult,
};
use std::io::{Read, Write};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SdkClientConfig {
    pub client_id: ClientId,
    pub client_name: ClientName,
    pub minimum_version: ProtocolVersion,
    pub maximum_version: ProtocolVersion,
    pub required_features: Vec<ProtocolFeatureId>,
    pub optional_features: Vec<ProtocolFeatureId>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EventSubscription {
    pub subscription_id: Option<hfx_domain::SubscriptionId>,
    pub expected_cursor: Option<EventCursor>,
    pub max_events: EventBatchLimit,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TransactionSubmission {
    pub transaction_id: TransactionId,
    pub lease_id: LeaseId,
    pub receiver_id: ReceiverId,
    pub generation_id: GenerationId,
    pub receiver_profile_id: ProfileId,
    pub receiver_profile_digest: ProfileDigest,
    pub device_profiles: Vec<DeviceProfileBinding>,
    pub transaction_class: TransactionClass,
    pub stable_intents: Vec<StableLightingIntent>,
    pub deadline_ms: MonotonicMs,
    pub resources: Vec<hfx_protocol::ResourceKey>,
    pub frames: Vec<LightingFrame>,
}

#[derive(Debug)]
pub struct HyperFluxClient<S, I> {
    channel: FramedIoChannel<S>,
    identities: I,
    client_id: ClientId,
    hello: ServerHello,
}

impl<S, I> HyperFluxClient<S, I>
where
    S: Read + Write,
    I: RequestIdentitySource,
{
    /// Negotiates a new SDK connection using the stable base handshake.
    ///
    /// # Errors
    ///
    /// Returns a typed identity, framing, server, or negotiation-validation
    /// error.
    pub fn connect(
        stream: S,
        config: SdkClientConfig,
        mut identities: I,
    ) -> Result<Self, SdkError> {
        let request_id = identities.next_request_id()?;
        let request = RpcRequest::Negotiate(NegotiationRequestEnvelope {
            request_id: request_id.clone(),
            params: ClientHello {
                client_id: config.client_id.clone(),
                client_name: config.client_name,
                minimum_version: config.minimum_version,
                maximum_version: config.maximum_version,
                required_features: config.required_features.clone(),
                optional_features: config.optional_features,
            },
        });
        let mut channel = FramedIoChannel::new(stream);
        let response = channel.exchange(&request, None)?;
        let RpcResponse::NegotiateSuccess(envelope) = response else {
            return match response {
                RpcResponse::Error(envelope) => {
                    validate_request_id(envelope.request_id.as_ref(), &request_id)?;
                    Err(SdkError::Server(envelope.error))
                }
                _ => Err(SdkError::UnexpectedResponse {
                    expected: SdkMethod::Negotiate,
                }),
            };
        };
        validate_request_id(Some(&envelope.request_id), &request_id)?;
        if envelope.server_instance_id != envelope.result.server_instance_id
            || envelope.result.selected_version < config.minimum_version
            || envelope.result.selected_version > config.maximum_version
            || !config.required_features.iter().all(|required| {
                envelope
                    .result
                    .enabled_features
                    .binary_search(required)
                    .is_ok()
            })
        {
            return Err(SdkError::InvalidNegotiation);
        }

        Ok(Self {
            channel,
            identities,
            client_id: config.client_id,
            hello: envelope.result,
        })
    }

    #[must_use]
    pub const fn server_hello(&self) -> &ServerHello {
        &self.hello
    }

    #[must_use]
    pub const fn client_id(&self) -> &ClientId {
        &self.client_id
    }

    #[must_use]
    pub fn into_inner(self) -> S {
        self.channel.into_inner()
    }

    /// # Errors
    ///
    /// Returns a typed local or server error.
    pub fn snapshot(&mut self) -> Result<BridgeSnapshot, SdkError> {
        let request_id = self.next_request_id()?;
        let response = self.exchange(
            &RpcRequest::Snapshot(self.envelope(request_id.clone(), EmptyRequest {})),
            &request_id,
        )?;
        match response {
            RpcResponse::SnapshotSuccess(envelope) => Ok(envelope.result),
            _ => Err(SdkError::UnexpectedResponse {
                expected: SdkMethod::Snapshot,
            }),
        }
    }

    /// Returns the bridge's canonical, viewer-specific application projection.
    ///
    /// # Errors
    ///
    /// Returns a typed local or server error. The negotiated protocol must
    /// include the `integration-view-projection` feature.
    pub fn integration_view(&mut self) -> Result<IntegrationView, SdkError> {
        let request_id = self.next_request_id()?;
        let response = self.exchange(
            &RpcRequest::IntegrationView(self.envelope(request_id.clone(), EmptyRequest {})),
            &request_id,
        )?;
        match response {
            RpcResponse::IntegrationViewSuccess(envelope) => Ok(envelope.result),
            _ => Err(SdkError::UnexpectedResponse {
                expected: SdkMethod::IntegrationView,
            }),
        }
    }

    /// # Errors
    ///
    /// Returns a typed local or server error.
    pub fn acquire_lease(
        &mut self,
        resources: Vec<hfx_protocol::ResourceKey>,
        duration_ms: LeaseDurationMs,
    ) -> Result<LeaseResult, SdkError> {
        let request_id = self.next_request_id()?;
        let params = LeaseRequest {
            request_id: request_id.clone(),
            client_id: self.client_id.clone(),
            resources,
            duration_ms,
        };
        let response = self.exchange(
            &RpcRequest::AcquireLease(self.envelope(request_id.clone(), params)),
            &request_id,
        )?;
        match response {
            RpcResponse::AcquireLeaseSuccess(envelope) => Ok(envelope.result),
            _ => Err(SdkError::UnexpectedResponse {
                expected: SdkMethod::AcquireLease,
            }),
        }
    }

    /// # Errors
    ///
    /// Returns a typed local or server error.
    pub fn renew_lease(
        &mut self,
        lease_id: LeaseId,
        duration_ms: LeaseDurationMs,
    ) -> Result<LeaseResult, SdkError> {
        let request_id = self.next_request_id()?;
        let params = RenewLeaseRequest {
            request_id: request_id.clone(),
            client_id: self.client_id.clone(),
            lease_id,
            duration_ms,
        };
        let response = self.exchange(
            &RpcRequest::RenewLease(self.envelope(request_id.clone(), params)),
            &request_id,
        )?;
        match response {
            RpcResponse::RenewLeaseSuccess(envelope) => Ok(envelope.result),
            _ => Err(SdkError::UnexpectedResponse {
                expected: SdkMethod::RenewLease,
            }),
        }
    }

    /// # Errors
    ///
    /// Returns a typed local or server error.
    pub fn release_lease(&mut self, lease_id: LeaseId) -> Result<LeaseResult, SdkError> {
        let request_id = self.next_request_id()?;
        let params = ReleaseLeaseRequest {
            request_id: request_id.clone(),
            client_id: self.client_id.clone(),
            lease_id,
        };
        let response = self.exchange(
            &RpcRequest::ReleaseLease(self.envelope(request_id.clone(), params)),
            &request_id,
        )?;
        match response {
            RpcResponse::ReleaseLeaseSuccess(envelope) => Ok(envelope.result),
            _ => Err(SdkError::UnexpectedResponse {
                expected: SdkMethod::ReleaseLease,
            }),
        }
    }

    /// # Errors
    ///
    /// Returns a typed local or server error. Legacy protocols reject an
    /// unrepresentable transaction before any bytes are emitted.
    pub fn submit_transaction(
        &mut self,
        submission: TransactionSubmission,
    ) -> Result<TransactionResult, SdkError> {
        let request_id = self.next_request_id()?;
        let params = TransactionRequest {
            request_id: request_id.clone(),
            transaction_id: submission.transaction_id,
            client_id: self.client_id.clone(),
            lease_id: submission.lease_id,
            receiver_id: submission.receiver_id,
            generation_id: submission.generation_id,
            receiver_profile_id: submission.receiver_profile_id,
            receiver_profile_digest: submission.receiver_profile_digest,
            device_profiles: submission.device_profiles,
            transaction_class: submission.transaction_class,
            stable_intents: submission.stable_intents,
            deadline_ms: submission.deadline_ms,
            resources: submission.resources,
            frames: submission.frames,
        };
        let response = self.exchange(
            &RpcRequest::SubmitTransaction(self.envelope(request_id.clone(), params)),
            &request_id,
        )?;
        match response {
            RpcResponse::SubmitTransactionSuccess(envelope) => Ok(envelope.result),
            _ => Err(SdkError::UnexpectedResponse {
                expected: SdkMethod::SubmitTransaction,
            }),
        }
    }

    /// # Errors
    ///
    /// Returns a typed local or server error.
    pub fn transaction_outcome(
        &mut self,
        transaction_id: TransactionId,
    ) -> Result<TransactionResult, SdkError> {
        let request_id = self.next_request_id()?;
        let params = TransactionLookup {
            request_id: request_id.clone(),
            client_id: self.client_id.clone(),
            transaction_id,
        };
        let response = self.exchange(
            &RpcRequest::TransactionOutcome(self.envelope(request_id.clone(), params)),
            &request_id,
        )?;
        match response {
            RpcResponse::TransactionOutcomeSuccess(envelope) => Ok(envelope.result),
            _ => Err(SdkError::UnexpectedResponse {
                expected: SdkMethod::TransactionOutcome,
            }),
        }
    }

    /// # Errors
    ///
    /// Returns a typed local or server error.
    pub fn subscribe(&mut self, subscription: EventSubscription) -> Result<EventBatch, SdkError> {
        let request_id = self.next_request_id()?;
        let params = SubscriptionRequest {
            client_id: self.client_id.clone(),
            subscription_id: subscription.subscription_id,
            expected_cursor: subscription.expected_cursor,
            max_events: subscription.max_events,
        };
        let response = self.exchange(
            &RpcRequest::Subscribe(self.envelope(request_id.clone(), params)),
            &request_id,
        )?;
        match response {
            RpcResponse::SubscribeSuccess(envelope) => Ok(envelope.result),
            _ => Err(SdkError::UnexpectedResponse {
                expected: SdkMethod::Subscribe,
            }),
        }
    }

    /// # Errors
    ///
    /// Returns a typed local or server error.
    pub fn diagnostics(&mut self) -> Result<DiagnosticSnapshot, SdkError> {
        let request_id = self.next_request_id()?;
        let response = self.exchange(
            &RpcRequest::Diagnostics(self.envelope(request_id.clone(), EmptyRequest {})),
            &request_id,
        )?;
        match response {
            RpcResponse::DiagnosticsSuccess(envelope) => Ok(envelope.result),
            _ => Err(SdkError::UnexpectedResponse {
                expected: SdkMethod::Diagnostics,
            }),
        }
    }

    fn next_request_id(&mut self) -> Result<RequestId, SdkError> {
        self.identities.next_request_id().map_err(SdkError::from)
    }

    fn envelope<T>(&self, request_id: RequestId, params: T) -> SessionRequestEnvelope<T> {
        SessionRequestEnvelope {
            request_id,
            protocol_session_id: self.hello.protocol_session_id.clone(),
            negotiation_token: self.hello.negotiation_token.clone(),
            params,
        }
    }

    fn exchange(
        &mut self,
        request: &RpcRequest,
        request_id: &RequestId,
    ) -> Result<RpcResponse, SdkError> {
        let response = self
            .channel
            .exchange(request, Some(self.hello.selected_version))?;
        validate_request_id(response.request_id(), request_id)?;
        if response.server_instance_id() != &self.hello.server_instance_id {
            return Err(SdkError::ServerInstanceMismatch);
        }
        if let RpcResponse::Error(envelope) = response {
            return Err(SdkError::Server(envelope.error));
        }
        Ok(response)
    }
}

fn validate_request_id(actual: Option<&RequestId>, expected: &RequestId) -> Result<(), SdkError> {
    if actual == Some(expected) {
        Ok(())
    } else {
        Err(SdkError::ResponseRequestMismatch)
    }
}
