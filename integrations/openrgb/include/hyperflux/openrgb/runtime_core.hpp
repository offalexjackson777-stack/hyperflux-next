// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "controller_model.hpp"
#include "dispatch_queue.hpp"
#include "inventory_model.hpp"
#include "runtime_bridge.hpp"

#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <vector>

namespace hyperflux::openrgb
{

struct RuntimeConfig
{
    DispatchQueueConfig dispatch_queue {};
    std::uint32_t lease_duration_ms = 30'000;
    std::uint32_t lease_renew_margin_ms = 10'000;
    std::uint64_t ownership_idle_ms = 1'000;
    std::uint64_t transaction_timeout_ms = 2'000;
    std::uint16_t event_batch_limit = 128;
    std::size_t max_event_batches_per_step = 4;
    std::size_t max_outcomes_per_step = 4;
    std::size_t max_dispatches_per_step = 4;
};

enum class DispatchOutcomeState
{
    Succeeded,
    Failed,
    Revoked,
    Superseded,
    Unavailable,
    Rejected,
};

struct DispatchOutcome
{
    std::uint64_t sequence;
    sdk::LightingIntent intent;
    std::optional<ReceiverId> receiver_id;
    std::optional<GenerationId> generation_id;
    std::optional<TransactionId> transaction_id;
    DispatchOutcomeState state;
    std::uint16_t declared_frames;
    std::uint16_t delivered_frames;
    SideEffectCertainty side_effect_certainty;
    bool live_write_executed;
    std::optional<ProtocolErrorKind> protocol_error;
    std::optional<sdk::Error> local_error;

    friend bool operator==(const DispatchOutcome&, const DispatchOutcome&) = default;
};

struct LogicalDispatchOutcome
{
    std::uint64_t sequence;
    sdk::LightingIntent intent;
    DispatchOutcomeState state;
    std::uint16_t expected_receivers;
    std::uint16_t terminal_receivers;
    std::uint16_t declared_frames;
    std::uint16_t delivered_frames;
    SideEffectCertainty side_effect_certainty;
    bool live_write_executed;
    std::optional<ProtocolErrorKind> protocol_error;
    std::optional<sdk::Error> local_error;

    friend bool operator==(const LogicalDispatchOutcome&, const LogicalDispatchOutcome&) = default;
};

struct RuntimeStep
{
    bool full_refresh = false;
    bool cursor_gap_recovered = false;
    std::vector<ControllerChange> controller_changes;
    std::vector<DispatchOutcome> dispatch_outcomes;
    std::vector<LogicalDispatchOutcome> logical_outcomes;
    std::vector<sdk::Error> notices;
};

/// Deterministic single-threaded OpenRGB adapter state machine.
///
/// A worker owns this object in production. Tests drive it directly so no
/// generation, lease, event-cursor, or terminal-outcome behavior depends on
/// wall-clock sleeps or UI timing.
class RuntimeCore
{
public:
    RuntimeCore(const RuntimeCore&) = delete;
    RuntimeCore& operator=(const RuntimeCore&) = delete;
    RuntimeCore(RuntimeCore&&) noexcept = default;
    RuntimeCore& operator=(RuntimeCore&&) noexcept = default;

    [[nodiscard]] static sdk::Result<RuntimeCore> create(
        RuntimeBridge& bridge,
        RuntimeConfig config = {});

    [[nodiscard]] sdk::Result<RuntimeStep> initialize();
    [[nodiscard]] sdk::Result<RuntimeStep> rescan();
    [[nodiscard]] sdk::Result<RuntimeStep> step(std::uint64_t now_ms);
    [[nodiscard]] RuntimeStep shutdown();

    [[nodiscard]] EnqueueDisposition enqueue_effect(
        QueuedLightingFrame frame,
        std::uint64_t now_ms);
    [[nodiscard]] EnqueueDisposition enqueue_stable(
        sdk::LightingIntent intent,
        std::vector<QueuedLightingFrame> frames);

    [[nodiscard]] bool initialized() const noexcept;
    [[nodiscard]] const std::vector<ControllerModel>& controllers() const noexcept;
    [[nodiscard]] const std::vector<InventoryReceiverModel>& inventory() const noexcept;
    [[nodiscard]] std::size_t pending_transaction_count() const noexcept;
    [[nodiscard]] std::size_t queued_stable_count() const noexcept;
    [[nodiscard]] std::set<std::string> queued_effect_targets() const;

private:
    struct OwnershipSession
    {
        sdk::LightingSession lighting;
        std::size_t active_requests = 0;
        std::uint64_t last_used_ms = 0;
    };

    struct ActiveRequest
    {
        std::uint64_t session_id;
        sdk::LightingIntent intent;
        std::uint16_t expected_receivers;
        std::uint16_t expected_frames;
        std::vector<DispatchOutcome> outcomes;
    };

    enum class RequestBindState
    {
        Ready,
        Deferred,
        Rejected,
    };

    struct RequestBindResult
    {
        RequestBindState state;
        std::optional<sdk::Error> error;
    };

    struct PendingTransaction
    {
        std::uint64_t sequence;
        sdk::LightingIntent intent;
        std::uint16_t expected_frames;
        ReceiverId receiver_id;
        GenerationId generation_id;
        TransactionId transaction_id;
    };

    RuntimeCore(RuntimeBridge& bridge, RuntimeConfig config);

    [[nodiscard]] sdk::Result<void> refresh_controllers(
        RuntimeStep& output,
        bool cursor_gap);
    [[nodiscard]] sdk::Result<void> refresh_if_required(RuntimeStep& output);
    [[nodiscard]] sdk::Result<void> poll_events(RuntimeStep& output);
    [[nodiscard]] sdk::Result<void> poll_outcomes(RuntimeStep& output);
    void renew_sessions(std::uint64_t now_ms, RuntimeStep& output);
    void retire_completed_requests(std::uint64_t now_ms, RuntimeStep& output);
    void dispatch_ready(std::uint64_t now_ms, RuntimeStep& output);
    void discard_queued_request(
        std::uint64_t sequence,
        DispatchOutcomeState state,
        std::optional<ProtocolErrorKind> protocol_error,
        const sdk::Error& error,
        RuntimeStep& output);
    void record_dispatch_outcome(DispatchOutcome outcome, RuntimeStep& output);
    void emit_logical_outcome(
        std::uint64_t sequence,
        sdk::LightingIntent intent,
        std::uint16_t expected_receivers,
        std::uint16_t expected_frames,
        const std::vector<DispatchOutcome>& outcomes,
        RuntimeStep& output);

    [[nodiscard]] RequestBindResult bind_request(
        const DispatchBatch& request,
        std::uint64_t now_ms,
        RuntimeStep& output);
    [[nodiscard]] OwnershipSession* session_for_request(std::uint64_t sequence);
    void invalidate_changed_sessions(RuntimeStep& output);
    [[nodiscard]] bool synchronize_connection(RuntimeStep& output);
    void consume_transaction_result(
        std::uint64_t sequence,
        sdk::LightingIntent intent,
        std::uint16_t expected_frames,
        const ReceiverId& receiver_id,
        const GenerationId& generation_id,
        const v5::TransactionResult& result,
        RuntimeStep& output);

    RuntimeBridge* bridge_;
    RuntimeConfig config_;
    DispatchQueue queue_;
    bool initialized_ = false;
    std::uint64_t connection_epoch_ = 0;
    bool refresh_required_ = false;
    std::vector<ControllerModel> controllers_;
    std::vector<InventoryReceiverModel> inventory_;
    std::optional<SubscriptionId> subscription_id_;
    std::optional<v5::EventCursor> cursor_;
    std::map<std::uint64_t, OwnershipSession> sessions_;
    std::map<std::uint64_t, ActiveRequest> active_requests_;
    std::uint64_t next_ownership_session_id_ = 1;
    std::map<std::string, PendingTransaction> pending_;
    std::optional<std::string> outcome_poll_cursor_;
};

} // namespace hyperflux::openrgb
