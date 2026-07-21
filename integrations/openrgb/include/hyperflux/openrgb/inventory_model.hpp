// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <hyperflux/generated/protocol_v5_types.hpp>
#include <hyperflux/sdk/result.hpp>

#include <optional>
#include <string>
#include <vector>

namespace hyperflux::openrgb
{

/// Application-neutral paired-device state retained for the information page.
///
/// This is intentionally independent from ControllerModel: an inventory device
/// can be paired, asleep, unsupported, or temporarily unavailable without
/// becoming an OpenRGB controller.
struct InventoryDeviceModel
{
    std::string stable_id;
    ReceiverId receiver_id;
    GenerationId generation_id;
    LogicalDeviceId device_id;
    DeviceKind device_kind;
    ProductId product_id;
    std::optional<v5::ProfileBindingView> profile;
    std::optional<ModelName> model_name;
    PairingState pairing;
    PresenceState presence;
    InventoryAvailability availability;
    SupportLevel support_level;
    std::vector<v5::EndpointSnapshot> endpoints;
    v5::BatteryObservation battery;
    std::vector<CapabilityId> capabilities;

    friend bool operator==(const InventoryDeviceModel&, const InventoryDeviceModel&) = default;
};

struct InventoryReceiverModel
{
    ReceiverId receiver_id;
    GenerationId generation_id;
    std::optional<v5::ProfileBindingView> profile;
    std::optional<ModelName> model_name;
    ReceiverLifecycleState lifecycle;
    bool stable_restore_enabled;
    RestoreState restore_state;
    std::vector<InventoryDeviceModel> devices;

    friend bool operator==(
        const InventoryReceiverModel&,
        const InventoryReceiverModel&) = default;
};

/// Retains the complete canonical inventory without importing OpenRGB policy.
[[nodiscard]] sdk::Result<std::vector<InventoryReceiverModel>> project_inventory(
    const v5::IntegrationView& view);

} // namespace hyperflux::openrgb
