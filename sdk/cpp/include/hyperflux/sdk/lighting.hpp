// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "client.hpp"

#include <hyperflux/generated/protocol_v5_types.hpp>

#include <optional>
#include <vector>

namespace hyperflux::sdk
{

enum class LightingIntent
{
    EffectFrame,
    Static,
    Off,
};

struct LightingTarget
{
    ReceiverId receiver_id;
    GenerationId generation_id;
    LogicalDeviceId device_id;
    EndpointId endpoint_id;
    v5::ProfileBindingView receiver_profile;
    v5::ProfileBindingView device_profile;
    LedCount application_slot_count;
    v5::ResourceKey resource;

    friend bool operator==(const LightingTarget&, const LightingTarget&) = default;
};

struct LightingUpdate
{
    LightingTarget target;
    std::vector<v5::RgbColor> colors;
};

/// Converts one canonical integration controller into an immutable write target.
///
/// The returned target contains every profile and generation binding required
/// by a protocol-v5 lighting transaction. Presentation metadata is deliberately
/// excluded because it never authorizes a hardware operation.
[[nodiscard]] Result<LightingTarget> lighting_target(const v5::ControllerView& controller);

/// Owns one atomic set of lighting resources across active receiver generations.
///
/// The bridge also revokes these resources when the underlying SDK connection
/// closes. Call `release` for a deliberate hand-off; call `abandon` after a
/// generation replacement or an unrecoverable connection failure. Each
/// submitted hardware transaction remains scoped to exactly one receiver
/// generation even when the lease covers several receivers.
class LightingSession
{
public:
    LightingSession(const LightingSession&) = delete;
    LightingSession& operator=(const LightingSession&) = delete;
    LightingSession(LightingSession&&) noexcept = default;
    LightingSession& operator=(LightingSession&&) noexcept = default;

    [[nodiscard]] static Result<LightingSession> acquire(
        LightingBridge& bridge,
        std::vector<LightingTarget> targets,
        LeaseDurationMs duration_ms);

    [[nodiscard]] bool active() const noexcept;
    [[nodiscard]] const LeaseId* lease_id() const noexcept;
    [[nodiscard]] const MonotonicMs* expires_at_ms() const noexcept;
    [[nodiscard]] const std::vector<LightingTarget>& targets() const noexcept;
    [[nodiscard]] bool matches(const LightingTarget& target) const noexcept;

    [[nodiscard]] Result<void> renew(LeaseDurationMs duration_ms);
    [[nodiscard]] Result<void> release();
    void abandon() noexcept;

    [[nodiscard]] Result<v5::TransactionResult> submit(
        LightingIntent intent,
        std::vector<LightingUpdate> updates,
        MonotonicMs deadline_ms);

private:
    LightingSession(
        LightingBridge& bridge,
        std::vector<LightingTarget> targets,
        v5::LeaseGrant grant);

    LightingBridge* bridge_;
    std::vector<LightingTarget> targets_;
    std::optional<v5::LeaseGrant> grant_;
};

} // namespace hyperflux::sdk
