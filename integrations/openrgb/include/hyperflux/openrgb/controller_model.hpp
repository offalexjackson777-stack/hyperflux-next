// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <hyperflux/sdk/lighting.hpp>

#include <optional>
#include <string>
#include <vector>

namespace hyperflux::openrgb
{

struct ControllerAuthorityKey
{
    ReceiverId receiver_id;
    GenerationId generation_id;
    LogicalDeviceId device_id;
    EndpointId endpoint_id;

    friend bool operator==(const ControllerAuthorityKey&, const ControllerAuthorityKey&) = default;
};

enum class ControllerOwnerState
{
    Unowned,
    OwnedByOpenRgb,
    OwnedByAnotherClient,
};

struct ControllerControlState
{
    ControllerOwnerState ownership;
    std::optional<ClientId> owner_client_id;
    std::optional<LeaseId> lease_id;
    std::optional<MonotonicMs> lease_expires_at_ms;
    v5::ControllerActions actions;

    friend bool operator==(const ControllerControlState&, const ControllerControlState&) = default;
};

struct ControllerModel
{
    std::string stable_id;
    ControllerAuthorityKey authority;
    DeviceKind device_kind;
    ProductId product_id;
    ModelName model_name;
    v5::ProfileBindingView device_profile;
    v5::PresentationView presentation;
    ControllerAvailability availability;
    v5::BatteryObservation battery;
    std::vector<CapabilityId> capabilities;
    v5::LightingTopologyView lighting;
    ControllerControlState control;
    sdk::LightingTarget lighting_target;

    friend bool operator==(const ControllerModel&, const ControllerModel&) = default;
};

enum class ReconcileKind
{
    Added,
    Removed,
    Retained,
    StateUpdated,
    PresentationReplaced,
};

struct ControllerChange
{
    ReconcileKind kind;
    std::string stable_id;
    std::optional<ControllerModel> before;
    std::optional<ControllerModel> after;
};

/// Projects the bridge's canonical integration view into OpenRGB-owned models.
///
/// Controllers assigned to another presentation authority are ignored so this
/// plugin never suppresses or replaces unrelated OpenRGB devices.
[[nodiscard]] sdk::Result<std::vector<ControllerModel>> project_controllers(
    const v5::IntegrationView& view);

/// Computes a deterministic lifecycle delta without destroying unchanged
/// OpenRGB controller objects during a routine rescan.
[[nodiscard]] std::vector<ControllerChange> reconcile_controllers(
    const std::vector<ControllerModel>& current,
    const std::vector<ControllerModel>& next);

[[nodiscard]] bool same_presentation(
    const ControllerModel& left,
    const ControllerModel& right) noexcept;

} // namespace hyperflux::openrgb
