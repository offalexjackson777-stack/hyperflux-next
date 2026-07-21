// SPDX-License-Identifier: GPL-2.0-only

use hfx_core::{ReceiverTransport, TransportDispatch, TransportReconciliation};
use hfx_domain::{
    AuthorizationEpoch, ColorChannel, DeviceApplicationState, DispatchNonce, FrameIndex,
    GenerationId, LedCount, LogicalDeviceId, ProfileDigest, ReceiverId, RequestDigest, SessionId,
    SideEffectCertainty, TransactionId,
};
use hfx_kernel_transport::{
    AlignedU64, HFX_UAPI_ABI_VERSION, HFX_UAPI_INFO_FLAG_SESSION_ACTIVE,
    HFX_UAPI_INFO_FLAG_SUSPENDED, HFX_UAPI_INFO_FLAG_WRITER_OPEN, HFX_UAPI_MAX_OBSERVATIONS,
    HFX_UAPI_RESULT_FLAG_AUTOMATIC_RETRY_SAFE, HFX_UAPI_RESULT_FLAG_WRITE_STARTED,
    HFX_UAPI_TRANSPORT_STATUS_FAILED, HFX_UAPI_TRANSPORT_STATUS_NOT_OBSERVED,
    HFX_UAPI_TRANSPORT_STATUS_SUCCEEDED, HFX_UAPI_TRANSPORT_STATUS_UNAVAILABLE,
    HfxUapiBeginSession, HfxUapiEndSession, HfxUapiInfo, HfxUapiObservation,
    HfxUapiReadObservations, HfxUapiSubmit, HfxUapiTransactionResult, KernelIo, KernelIoError,
    KernelReceiverTransport, KernelSessionMaterial, KernelTransportErrorKind,
    ReceiverFrameEncoderRegistry,
};
use hfx_profiles::RuntimeProfileCatalog;
use hfx_protocol::{DeviceProfileBinding, LightingFrame, RgbColor};
use std::collections::BTreeMap;
use std::mem::size_of;
use std::sync::{Arc, Mutex};
use std::time::Duration;

const GENERATION: u64 = 7;
const VENDOR_ID: u16 = 0x1532;
const PRODUCT_ID: u16 = 0x00cf;

#[derive(Clone, Copy, Debug)]
struct FailurePlan {
    physical_frames_completed: u32,
    write_started: bool,
    retry_safe: bool,
}

#[derive(Clone, Debug)]
struct FakeKernel {
    state: Arc<Mutex<FakeKernelState>>,
}

#[derive(Clone, Debug)]
struct FakeKernelState {
    now_ns: u64,
    generation: u64,
    authorization_epoch: u64,
    session_active: bool,
    suspended: bool,
    expires_ns: u64,
    maximum_nonce: u64,
    next_sequence: u64,
    begin_calls: usize,
    end_calls: usize,
    submit_calls: usize,
    last_submit: Option<HfxUapiSubmit>,
    results: BTreeMap<(u64, u64), HfxUapiTransactionResult>,
    failure_plan: Option<FailurePlan>,
    observations: Vec<HfxUapiObservation>,
}

impl FakeKernel {
    fn new() -> Self {
        Self {
            state: Arc::new(Mutex::new(FakeKernelState {
                now_ns: 1_000_000_000,
                generation: GENERATION,
                authorization_epoch: 1,
                session_active: false,
                suspended: false,
                expires_ns: 0,
                maximum_nonce: 0,
                next_sequence: 0,
                begin_calls: 0,
                end_calls: 0,
                submit_calls: 0,
                last_submit: None,
                results: BTreeMap::new(),
                failure_plan: None,
                observations: Vec::new(),
            })),
        }
    }

    fn snapshot(&self) -> FakeKernelState {
        self.state
            .lock()
            .expect("fake kernel lock is healthy")
            .clone()
    }

    fn fail_next(&self, plan: FailurePlan) {
        self.state
            .lock()
            .expect("fake kernel lock is healthy")
            .failure_plan = Some(plan);
    }

    fn advance_to_renewal_window(&self) {
        let mut state = self.state.lock().expect("fake kernel lock is healthy");
        state.now_ns = state.expires_ns.saturating_sub(4_000_000_000);
    }

    fn add_observation(&self, observation: HfxUapiObservation) {
        self.state
            .lock()
            .expect("fake kernel lock is healthy")
            .observations
            .push(observation);
    }

    fn set_suspended(&self, suspended: bool) {
        self.state
            .lock()
            .expect("fake kernel lock is healthy")
            .suspended = suspended;
    }
}

impl KernelIo for FakeKernel {
    fn boottime_ns(&self) -> Result<u64, KernelIoError> {
        Ok(self.state.lock().map_err(lock_error)?.now_ns)
    }

    fn get_info(&self) -> Result<HfxUapiInfo, KernelIoError> {
        let mut state = self.state.lock().map_err(lock_error)?;
        if state.session_active && state.now_ns >= state.expires_ns {
            state.session_active = false;
            state.authorization_epoch = state.authorization_epoch.saturating_add(1);
        }
        Ok(HfxUapiInfo {
            version: HFX_UAPI_ABI_VERSION,
            size: u32::try_from(size_of::<HfxUapiInfo>()).map_err(|_| fake_error())?,
            receiver_generation: AlignedU64(state.generation),
            authorization_epoch: AlignedU64(state.authorization_epoch),
            flags: HFX_UAPI_INFO_FLAG_WRITER_OPEN
                | if state.session_active {
                    HFX_UAPI_INFO_FLAG_SESSION_ACTIVE
                } else {
                    0
                }
                | if state.suspended {
                    HFX_UAPI_INFO_FLAG_SUSPENDED
                } else {
                    0
                },
            revoke_reason: 0,
            vendor_id: VENDOR_ID,
            product_id: PRODUCT_ID,
            bound_interfaces: 6,
            reserved: 0,
        })
    }

    fn begin_session(&self, request: &mut HfxUapiBeginSession) -> Result<(), KernelIoError> {
        let mut state = self.state.lock().map_err(lock_error)?;
        if state.session_active || request.receiver_generation.0 != state.generation {
            return Err(fake_error());
        }
        state.authorization_epoch = state.authorization_epoch.saturating_add(1);
        state.session_active = true;
        state.expires_ns = request.expires_boottime_ns.0;
        state.maximum_nonce = 0;
        state.begin_calls += 1;
        request.authorization_epoch = AlignedU64(state.authorization_epoch);
        Ok(())
    }

    fn end_session(&self, request: HfxUapiEndSession) -> Result<(), KernelIoError> {
        let mut state = self.state.lock().map_err(lock_error)?;
        if !state.session_active
            || request.authorization_epoch.0 != state.authorization_epoch
            || request.receiver_generation.0 != state.generation
        {
            return Err(fake_error());
        }
        state.session_active = false;
        state.authorization_epoch = state.authorization_epoch.saturating_add(1);
        state.end_calls += 1;
        Ok(())
    }

    fn submit(&self, request: &mut HfxUapiSubmit) -> Result<(), KernelIoError> {
        let mut state = self.state.lock().map_err(lock_error)?;
        if let Some(result) = state
            .results
            .values()
            .find(|result| {
                result.receiver_generation == request.receiver_generation
                    && result.dispatch_nonce == request.dispatch_nonce
                    && result.request_digest == request.request_digest
            })
            .copied()
        {
            request.kernel_sequence = result.kernel_sequence;
            return if result.status == HFX_UAPI_TRANSPORT_STATUS_SUCCEEDED {
                Ok(())
            } else {
                Err(fake_error())
            };
        }
        if !state.session_active
            || request.authorization_epoch.0 != state.authorization_epoch
            || request.receiver_generation.0 != state.generation
            || request.dispatch_nonce.0 <= state.maximum_nonce
        {
            return Err(fake_error());
        }
        state.maximum_nonce = request.dispatch_nonce.0;
        state.next_sequence = state.next_sequence.saturating_add(1);
        state.submit_calls += 1;
        request.kernel_sequence = AlignedU64(state.next_sequence);
        state.last_submit = Some(*request);
        let plan = state.failure_plan.take();
        let flags = plan.map_or(HFX_UAPI_RESULT_FLAG_WRITE_STARTED, |failure| {
            (u32::from(failure.write_started) * HFX_UAPI_RESULT_FLAG_WRITE_STARTED)
                | (u32::from(failure.retry_safe) * HFX_UAPI_RESULT_FLAG_AUTOMATIC_RETRY_SAFE)
        });
        let result = HfxUapiTransactionResult {
            version: HFX_UAPI_ABI_VERSION,
            size: u32::try_from(size_of::<HfxUapiTransactionResult>()).map_err(|_| fake_error())?,
            receiver_generation: request.receiver_generation,
            authorization_epoch: request.authorization_epoch,
            kernel_sequence: request.kernel_sequence,
            dispatch_nonce: request.dispatch_nonce,
            request_digest: request.request_digest,
            status: plan.map_or(HFX_UAPI_TRANSPORT_STATUS_SUCCEEDED, |_| {
                HFX_UAPI_TRANSPORT_STATUS_FAILED
            }),
            frames_planned: request.frame_count,
            frames_completed: plan.map_or(request.frame_count, |failure| {
                failure.physical_frames_completed
            }),
            failed_frame: plan.map_or(0, |failure| {
                failure.physical_frames_completed.saturating_add(1)
            }),
            transport_errno: plan.map_or(0, |_| -5),
            revoke_reason: 0,
            flags,
        };
        state.results.insert(
            (request.authorization_epoch.0, request.dispatch_nonce.0),
            result,
        );
        if plan.is_some() {
            Err(fake_error())
        } else {
            Ok(())
        }
    }

    fn get_transaction_result(
        &self,
        request: &mut HfxUapiTransactionResult,
    ) -> Result<(), KernelIoError> {
        let state = self.state.lock().map_err(lock_error)?;
        if let Some(result) = state.results.values().find(|result| {
            result.receiver_generation == request.receiver_generation
                && result.dispatch_nonce == request.dispatch_nonce
                && result.request_digest == request.request_digest
        }) {
            *request = *result;
            return Ok(());
        }
        if let Some(result) = state
            .results
            .get(&(request.authorization_epoch.0, request.dispatch_nonce.0))
        {
            *request = *result;
            return Ok(());
        }
        request.kernel_sequence = AlignedU64(0);
        request.frames_planned = 0;
        request.frames_completed = 0;
        request.failed_frame = 0;
        request.transport_errno = 0;
        request.revoke_reason = 0;
        if state.session_active
            && request.authorization_epoch.0 == state.authorization_epoch
            && request.dispatch_nonce.0 > state.maximum_nonce
        {
            request.status = HFX_UAPI_TRANSPORT_STATUS_NOT_OBSERVED;
            request.flags = HFX_UAPI_RESULT_FLAG_AUTOMATIC_RETRY_SAFE;
        } else {
            request.status = HFX_UAPI_TRANSPORT_STATUS_UNAVAILABLE;
            request.flags = 0;
        }
        Ok(())
    }

    fn read_observations(
        &self,
        request: &mut HfxUapiReadObservations,
    ) -> Result<(), KernelIoError> {
        let state = self.state.lock().map_err(lock_error)?;
        let selected = state
            .observations
            .iter()
            .filter(|item| item.sequence.0 > request.after_sequence.0)
            .take(HFX_UAPI_MAX_OBSERVATIONS)
            .copied()
            .collect::<Vec<_>>();
        request.oldest_sequence =
            AlignedU64(state.observations.first().map_or(0, |item| item.sequence.0));
        request.latest_sequence =
            AlignedU64(state.observations.last().map_or(0, |item| item.sequence.0));
        request.count = u32::try_from(selected.len()).map_err(|_| fake_error())?;
        for (destination, source) in request.observations.iter_mut().zip(selected) {
            *destination = source;
        }
        Ok(())
    }
}

#[test]
fn profile_encoder_preserves_qualified_order_maps_and_timing() {
    let catalog = RuntimeProfileCatalog::load().expect("catalog is valid");
    let dispatch = dispatch(&catalog, 1, true, true);
    let receiver = catalog
        .profile(&dispatch.receiver_profile_id)
        .expect("receiver profile resolves");
    let encoded = ReceiverFrameEncoderRegistry
        .encode(
            &catalog,
            receiver.protocol_family.expect("receiver protocol exists"),
            receiver
                .transport_backend_id
                .expect("receiver backend exists"),
            &dispatch,
        )
        .expect("qualified dispatch encodes");

    assert_eq!(encoded.frames().len(), 14);
    assert_eq!(encoded.frames()[0].payload[5], 0x2c);
    assert_eq!(encoded.frames()[1].payload[5], 0x38);
    assert_eq!(encoded.frames()[6].payload[10], 5);
    assert_eq!(encoded.frames()[7].payload[5], 0x2c);
    assert!(
        encoded.frames()[..6]
            .iter()
            .all(|frame| frame.delay_after_us == 2_500)
    );
    assert_eq!(encoded.frames()[6].delay_after_us, 50_000);
    assert!(
        encoded.frames()[7..13]
            .iter()
            .all(|frame| frame.delay_after_us == 2_500)
    );
    assert_eq!(encoded.frames()[13].delay_after_us, 0);

    let mouse = &encoded.frames()[0].payload;
    assert_eq!(&mouse[13..16], &[0, 255, 0]);
    assert_eq!(&mouse[16..19], &[255, 0, 0]);
    assert!(encoded.frames().iter().all(checksum_is_valid));
    assert_eq!(encoded.semantic_frames_completed(7), 0);
    assert_eq!(encoded.semantic_frames_completed(8), 1);
    assert_eq!(encoded.semantic_frames_completed(14), 2);
}

#[test]
fn encoder_composes_mouse_and_keyboard_independently() {
    let catalog = RuntimeProfileCatalog::load().expect("catalog is valid");
    let receiver = catalog
        .receiver(
            VENDOR_ID.try_into().expect("vendor id is valid"),
            PRODUCT_ID.try_into().expect("product id is valid"),
        )
        .expect("receiver resolves");
    for (mouse, keyboard, expected) in [(true, false, 2), (false, true, 12)] {
        let encoded = ReceiverFrameEncoderRegistry
            .encode(
                &catalog,
                receiver.protocol_family.expect("protocol exists"),
                receiver.transport_backend_id.expect("backend exists"),
                &dispatch(&catalog, 1, mouse, keyboard),
            )
            .expect("independent child dispatch encodes");
        assert_eq!(encoded.frames().len(), expected);
    }
}

#[test]
fn adapter_reconciles_exact_success_without_a_second_submit() {
    let fake = FakeKernel::new();
    let mut transport = transport(fake.clone());
    let dispatch = dispatch(
        &RuntimeProfileCatalog::load().expect("catalog is valid"),
        1,
        true,
        true,
    );

    assert_eq!(
        transport.reconcile(&dispatch),
        TransportReconciliation::NotObserved
    );
    let receipt = transport.dispatch(&dispatch).expect("dispatch succeeds");
    assert_eq!(receipt.delivered_frames.get(), 2);
    assert_eq!(
        receipt.side_effect_certainty,
        SideEffectCertainty::Committed
    );
    assert_eq!(
        receipt.device_application,
        DeviceApplicationState::Unverified
    );
    assert_eq!(transport.dispatch(&dispatch), Ok(receipt));
    let state = fake.snapshot();
    assert_eq!(state.submit_calls, 1);
    assert_eq!(
        state.last_submit.expect("submit was captured").frame_count,
        14
    );
}

#[test]
fn retained_result_survives_writer_session_rotation() {
    let fake = FakeKernel::new();
    let catalog = RuntimeProfileCatalog::load().expect("catalog is valid");
    let dispatch = dispatch(&catalog, 1, true, true);
    {
        let mut first = transport(fake.clone());
        first.dispatch(&dispatch).expect("first dispatch succeeds");
    }
    let mut replacement = transport(fake.clone());
    let retained = replacement.reconcile(&dispatch);
    assert!(matches!(retained, TransportReconciliation::Retained(_)));
    replacement
        .dispatch(&dispatch)
        .expect("replacement returns retained success");
    assert_eq!(fake.snapshot().submit_calls, 1);
}

#[test]
fn adapter_preserves_partial_physical_side_effects_as_semantic_facts() {
    let fake = FakeKernel::new();
    fake.fail_next(FailurePlan {
        physical_frames_completed: 8,
        write_started: true,
        retry_safe: false,
    });
    let mut transport = transport(fake.clone());
    let dispatch = dispatch(
        &RuntimeProfileCatalog::load().expect("catalog is valid"),
        1,
        true,
        true,
    );
    let error = transport
        .dispatch(&dispatch)
        .expect_err("dispatch must fail");
    let facts = error.failure_facts();
    assert_eq!(
        error.kind(),
        KernelTransportErrorKind::OutcomeRetainedFailure
    );
    assert_eq!(facts.delivered_frames.get(), 1);
    assert_eq!(facts.side_effect_certainty, SideEffectCertainty::Partial);
    assert!(facts.live_write_executed);
    assert!(!facts.automatic_retry_safe);
    assert_eq!(
        transport.reconcile(&dispatch),
        TransportReconciliation::RetainedFailure(facts)
    );
    assert_eq!(fake.snapshot().submit_calls, 1);
}

#[test]
fn adapter_reports_a_prewrite_kernel_rejection_as_retry_safe() {
    let fake = FakeKernel::new();
    fake.fail_next(FailurePlan {
        physical_frames_completed: 0,
        write_started: false,
        retry_safe: true,
    });
    let mut transport = transport(fake);
    let dispatch = dispatch(
        &RuntimeProfileCatalog::load().expect("catalog is valid"),
        1,
        true,
        false,
    );
    let error = transport
        .dispatch(&dispatch)
        .expect_err("dispatch must fail");
    let facts = error.failure_facts();
    assert_eq!(facts.delivered_frames.get(), 0);
    assert_eq!(facts.side_effect_certainty, SideEffectCertainty::None);
    assert!(!facts.live_write_executed);
    assert!(facts.automatic_retry_safe);
}

#[test]
fn adapter_renews_the_kernel_session_without_reusing_a_dispatch() {
    let fake = FakeKernel::new();
    let mut transport = transport(fake.clone());
    let catalog = RuntimeProfileCatalog::load().expect("catalog is valid");
    transport
        .dispatch(&dispatch(&catalog, 1, true, false))
        .expect("first dispatch succeeds");
    fake.advance_to_renewal_window();
    transport
        .dispatch(&dispatch(&catalog, 2, false, true))
        .expect("dispatch after renewal succeeds");
    let state = fake.snapshot();
    assert_eq!(state.begin_calls, 2);
    assert_eq!(state.end_calls, 1);
    assert_eq!(state.submit_calls, 2);
}

#[test]
fn suspended_receiver_retains_generation_identity_but_rejects_writes() {
    let fake = FakeKernel::new();
    let mut transport = transport(fake.clone());
    let receiver_id = ReceiverId::try_from("receiver-1").expect("receiver id is valid");
    let catalog = RuntimeProfileCatalog::load().expect("catalog is valid");

    fake.set_suspended(true);

    assert_eq!(
        transport.current_generation(&receiver_id),
        Some(GenerationId::try_from(GENERATION).expect("generation is valid"))
    );
    let error = transport
        .dispatch(&dispatch(&catalog, 1, true, true))
        .expect_err("suspended receiver cannot admit a write");
    assert_eq!(error.kind(), KernelTransportErrorKind::OutcomeUnavailable);
    assert_eq!(fake.snapshot().submit_calls, 0);
}

#[test]
fn passive_observations_remain_raw_bounded_and_generation_bound() {
    let fake = FakeKernel::new();
    fake.add_observation(HfxUapiObservation {
        sequence: AlignedU64(1),
        observed_boottime_ns: AlignedU64(5_000),
        kind: 8,
        endpoint_slot: 1,
        source: 2,
        confidence: 2,
        value: 1,
        auxiliary: 0,
    });
    let transport = transport(fake);
    let batch = transport
        .read_observations(0)
        .expect("passive observations decode");
    assert_eq!(batch.generation_id.get(), GENERATION);
    assert_eq!(batch.observations.len(), 1);
    assert_eq!(batch.observations[0].kind, 8);
    assert_eq!(batch.observations[0].value, 1);
}

#[test]
fn invalid_profile_digest_fails_before_session_admission() {
    let fake = FakeKernel::new();
    let catalog = RuntimeProfileCatalog::load().expect("catalog is valid");
    let mut material = session_material(&catalog);
    material.receiver_profile_digest =
        ProfileDigest::try_from("z".repeat(64)).expect("length-only domain value is accepted");
    let error = KernelReceiverTransport::new(fake.clone(), catalog, material)
        .err()
        .expect("invalid digest must fail");
    assert_eq!(error.kind(), KernelTransportErrorKind::ProfileMismatch);
    assert_eq!(fake.snapshot().begin_calls, 0);
}

fn transport(fake: FakeKernel) -> KernelReceiverTransport<FakeKernel> {
    let catalog = RuntimeProfileCatalog::load().expect("catalog is valid");
    let material = session_material(&catalog);
    KernelReceiverTransport::new(fake, catalog, material).expect("fake kernel transport starts")
}

fn session_material(catalog: &RuntimeProfileCatalog) -> KernelSessionMaterial {
    let receiver = catalog
        .receiver(
            VENDOR_ID.try_into().expect("vendor id is valid"),
            PRODUCT_ID.try_into().expect("product id is valid"),
        )
        .expect("receiver resolves");
    KernelSessionMaterial {
        receiver_id: ReceiverId::try_from("receiver-1").expect("receiver id is valid"),
        generation_id: GenerationId::try_from(GENERATION).expect("generation is valid"),
        receiver_profile_id: receiver.profile_id.clone(),
        receiver_profile_digest: receiver.runtime_digest.clone(),
        capability_digest: [0x22; 32],
        daemon_nonce: [0x33; 32],
        session_duration: Duration::from_mins(1),
    }
}

fn dispatch(
    catalog: &RuntimeProfileCatalog,
    nonce: u64,
    include_mouse: bool,
    include_keyboard: bool,
) -> TransportDispatch {
    let receiver = catalog
        .receiver(
            VENDOR_ID.try_into().expect("vendor id is valid"),
            PRODUCT_ID.try_into().expect("product id is valid"),
        )
        .expect("receiver resolves");
    let mouse = catalog
        .child(0x00cd_u16.try_into().expect("mouse product id is valid"))
        .expect("mouse resolves");
    let keyboard = catalog
        .child(0x0296_u16.try_into().expect("keyboard product id is valid"))
        .expect("keyboard resolves");
    let mut device_profiles = Vec::new();
    let mut frames = Vec::new();
    if include_keyboard {
        push_device(
            &mut device_profiles,
            &mut frames,
            "keyboard-1",
            keyboard,
            keyboard_colors(),
        );
    }
    if include_mouse {
        push_device(
            &mut device_profiles,
            &mut frames,
            "mouse-1",
            mouse,
            mouse_colors(),
        );
    }
    TransportDispatch {
        session_id: SessionId::try_from("session-1").expect("session id is valid"),
        authorization_epoch: AuthorizationEpoch::try_from(99_u64)
            .expect("authorization epoch is valid"),
        dispatch_nonce: DispatchNonce::try_from(nonce).expect("dispatch nonce is valid"),
        receiver_id: ReceiverId::try_from("receiver-1").expect("receiver id is valid"),
        generation_id: GenerationId::try_from(GENERATION).expect("generation is valid"),
        transaction_id: TransactionId::try_from(format!("transaction-{nonce}"))
            .expect("transaction id is valid"),
        request_digest: RequestDigest::try_from(format!("{nonce:064x}"))
            .expect("request digest is valid"),
        receiver_profile_id: receiver.profile_id.clone(),
        receiver_profile_digest: receiver.runtime_digest.clone(),
        device_profiles,
        frames,
    }
}

fn push_device(
    bindings: &mut Vec<DeviceProfileBinding>,
    frames: &mut Vec<LightingFrame>,
    device_id: &str,
    profile: &hfx_profiles::RuntimeProfile,
    colors: Vec<RgbColor>,
) {
    let device_id = LogicalDeviceId::try_from(device_id).expect("device id is valid");
    let slots = LedCount::try_from(u16::try_from(colors.len()).expect("color count fits"))
        .expect("LED count is valid");
    bindings.push(DeviceProfileBinding {
        device_id: device_id.clone(),
        profile_id: profile.profile_id.clone(),
        profile_digest: profile.runtime_digest.clone(),
        application_slot_count: slots,
    });
    frames.push(LightingFrame {
        device_id,
        frame_index: FrameIndex::try_from(u32::try_from(frames.len()).expect("frame index fits"))
            .expect("frame index is valid"),
        colors,
    });
}

fn mouse_colors() -> Vec<RgbColor> {
    let mut colors = vec![color(0, 0, 255); 13];
    colors[0] = color(255, 0, 0);
    colors[1] = color(0, 255, 0);
    colors
}

fn keyboard_colors() -> Vec<RgbColor> {
    (0..102)
        .map(|index| {
            if index < 17 {
                color(255, 255, 0)
            } else {
                color(0, 0, 255)
            }
        })
        .collect()
}

fn color(red: u8, green: u8, blue: u8) -> RgbColor {
    RgbColor {
        red: ColorChannel::try_from(red).expect("red is valid"),
        green: ColorChannel::try_from(green).expect("green is valid"),
        blue: ColorChannel::try_from(blue).expect("blue is valid"),
    }
}

fn checksum_is_valid(frame: &hfx_kernel_transport::HfxUapiFrame) -> bool {
    frame.payload[88]
        == frame.payload[2..88]
            .iter()
            .fold(0_u8, |checksum, value| checksum ^ value)
        && frame.payload[89] == 0
}

fn lock_error<T>(_: std::sync::PoisonError<T>) -> KernelIoError {
    fake_error()
}

const fn fake_error() -> KernelIoError {
    KernelIoError::from_raw_os_error(5)
}
