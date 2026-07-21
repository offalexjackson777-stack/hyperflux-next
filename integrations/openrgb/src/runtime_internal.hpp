// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <hyperflux/openrgb/runtime_core.hpp>

#include <algorithm>
#include <cstdint>
#include <limits>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace hyperflux::openrgb::runtime_detail
{

inline sdk::Error error(
    sdk::ErrorCode code,
    std::string message,
    std::string finding = "HFX-RUNTIME-001")
{
    return {code, std::move(message), std::move(finding)};
}

inline std::string receiver_key(const ReceiverId& receiver_id)
{
    return std::string(receiver_id.value());
}

inline std::uint64_t saturating_add(std::uint64_t left, std::uint64_t right) noexcept
{
    return left > std::numeric_limits<std::uint64_t>::max() - right
        ? std::numeric_limits<std::uint64_t>::max()
        : left + right;
}

inline const ControllerModel* find_controller(
    const std::vector<ControllerModel>& controllers,
    std::string_view stable_id)
{
    const auto found = std::find_if(
        controllers.begin(),
        controllers.end(),
        [stable_id](const ControllerModel& controller) {
            return controller.stable_id == stable_id;
        });
    return found == controllers.end() ? nullptr : &*found;
}

inline std::vector<sdk::LightingTarget> ready_targets(
    const std::vector<ControllerModel>& controllers,
    const ReceiverId& receiver_id,
    const GenerationId& generation_id)
{
    std::vector<sdk::LightingTarget> result;
    for(const auto& controller : controllers)
    {
        if(controller.authority.receiver_id == receiver_id
           && controller.authority.generation_id == generation_id
           && controller.availability == ControllerAvailability::Ready)
        {
            result.push_back(controller.lighting_target);
        }
    }
    return result;
}

inline bool refresh_event(EventKind kind) noexcept
{
    return kind != EventKind::TransactionCompleted
        && kind != EventKind::DiagnosticRaised;
}

inline DispatchOutcomeState terminal_state(
    const v5::TransactionTerminal& terminal,
    std::uint16_t expected_frames) noexcept
{
    const auto complete = terminal.state == TransactionState::Succeeded
        && terminal.declared_frames.value() == expected_frames
        && terminal.delivered_frames.value() == expected_frames
        && terminal.side_effect_certainty == SideEffectCertainty::Committed
        && terminal.live_write_executed;
    if(complete)
    {
        return DispatchOutcomeState::Succeeded;
    }
    if(terminal.state == TransactionState::Revoked)
    {
        return DispatchOutcomeState::Revoked;
    }
    if(terminal.state == TransactionState::Superseded)
    {
        return DispatchOutcomeState::Superseded;
    }
    return DispatchOutcomeState::Failed;
}

inline bool same_targets(
    const std::vector<sdk::LightingTarget>& left,
    const std::vector<sdk::LightingTarget>& right)
{
    return left == right;
}

} // namespace hyperflux::openrgb::runtime_detail
