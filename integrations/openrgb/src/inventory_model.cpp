// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/inventory_model.hpp>

#include <algorithm>
#include <set>
#include <string>
#include <utility>

namespace hyperflux::openrgb
{
namespace
{

sdk::Error inventory_error(std::string message)
{
    return {
        sdk::ErrorCode::InvalidController,
        std::move(message),
        "HFX-INTEGRATION-001",
    };
}

std::string stable_id(const ReceiverId& receiver_id, const LogicalDeviceId& device_id)
{
    return std::string(receiver_id.value()) + "/" + std::string(device_id.value());
}

} // namespace

sdk::Result<std::vector<InventoryReceiverModel>> project_inventory(
    const v5::IntegrationView& view)
{
    std::vector<InventoryReceiverModel> result;
    result.reserve(view.receivers.size());
    std::set<std::string> receiver_ids;
    std::set<std::string> device_ids;

    for(const auto& receiver : view.receivers)
    {
        const auto receiver_key = std::string(receiver.receiver_id.value());
        if(!receiver_ids.insert(receiver_key).second)
        {
            return sdk::Result<std::vector<InventoryReceiverModel>>::failure(
                inventory_error("integration inventory contains a duplicate receiver"));
        }

        std::vector<InventoryDeviceModel> devices;
        devices.reserve(receiver.inventory.size());
        for(const auto& device : receiver.inventory)
        {
            auto identifier = stable_id(receiver.receiver_id, device.device_id);
            if(!device_ids.insert(identifier).second)
            {
                return sdk::Result<std::vector<InventoryReceiverModel>>::failure(
                    inventory_error("integration inventory contains a duplicate paired device"));
            }
            devices.push_back({
                std::move(identifier),
                receiver.receiver_id,
                receiver.generation_id,
                device.device_id,
                device.device_kind,
                device.product_id,
                device.profile,
                device.model_name,
                device.pairing,
                device.presence,
                device.availability,
                device.support_level,
                device.endpoints,
                device.battery,
                device.capabilities,
            });
        }
        std::sort(devices.begin(), devices.end(), [](const auto& left, const auto& right) {
            return left.stable_id < right.stable_id;
        });
        result.push_back({
            receiver.receiver_id,
            receiver.generation_id,
            receiver.profile,
            receiver.model_name,
            receiver.lifecycle,
            receiver.stable_restore_enabled,
            receiver.restore_state,
            std::move(devices),
        });
    }
    std::sort(result.begin(), result.end(), [](const auto& left, const auto& right) {
        return left.receiver_id.value() < right.receiver_id.value();
    });
    return sdk::Result<std::vector<InventoryReceiverModel>>::success(std::move(result));
}

} // namespace hyperflux::openrgb
