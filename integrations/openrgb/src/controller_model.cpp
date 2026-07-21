// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/controller_model.hpp>

#include <hyperflux/generated/integration_catalog.hpp>

#include <algorithm>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <variant>

namespace hyperflux::openrgb
{
namespace
{

sdk::Error model_error(std::string message)
{
    return {
        sdk::ErrorCode::InvalidController,
        std::move(message),
        "HFX-INTEGRATION-001",
    };
}

const integrations::UpstreamRecord& openrgb_upstream()
{
    const auto* upstream = integrations::upstream_by_id("openrgb");
    if(upstream == nullptr)
    {
        throw std::logic_error("generated integration catalog has no OpenRGB authority");
    }
    return *upstream;
}

bool uses_openrgb_presentation(const v5::ControllerView& controller)
{
    return controller.presentation.upstream_id.value() == "openrgb";
}

bool exact_presentation_pin(const v5::ControllerView& controller)
{
    const auto& upstream = openrgb_upstream();
    return controller.presentation.owner.value() == upstream.name
        && controller.presentation.project_version.value() == upstream.version
        && controller.presentation.source_revision.value() == upstream.commit;
}

std::string stable_id(const v5::ControllerView& controller)
{
    std::string value;
    value.reserve(
        controller.receiver_id.value().size() + controller.device_id.value().size()
        + controller.device_profile.profile_id.value().size() + 3);
    value.append(controller.receiver_id.value());
    value.push_back('/');
    value.append(controller.device_id.value());
    value.push_back('/');
    value.append(controller.device_profile.profile_id.value());
    return value;
}

bool supported_kind(DeviceKind kind)
{
    return kind == DeviceKind::Mouse || kind == DeviceKind::Keyboard;
}

const ControllerModel* find_model(
    const std::vector<ControllerModel>& models,
    std::string_view requested)
{
    const auto found = std::find_if(
        models.begin(),
        models.end(),
        [requested](const ControllerModel& model) { return model.stable_id == requested; });
    return found == models.end() ? nullptr : &*found;
}

ControllerChange change(
    ReconcileKind kind,
    std::string stable_id_value,
    std::optional<ControllerModel> before,
    std::optional<ControllerModel> after)
{
    return {kind, std::move(stable_id_value), std::move(before), std::move(after)};
}

ControllerControlState control_state(const v5::ControllerView& controller)
{
    if(const auto* viewer =
           std::get_if<v5::ControllerOwnershipOwnedByViewer>(&controller.ownership))
    {
        return {
            ControllerOwnerState::OwnedByOpenRgb,
            std::nullopt,
            viewer->detail.lease_id,
            viewer->detail.expires_at_ms,
            controller.actions,
        };
    }
    if(const auto* other =
           std::get_if<v5::ControllerOwnershipOwnedByOther>(&controller.ownership))
    {
        return {
            ControllerOwnerState::OwnedByAnotherClient,
            other->detail.client_id,
            other->detail.lease_id,
            other->detail.expires_at_ms,
            controller.actions,
        };
    }
    return {
        ControllerOwnerState::Unowned,
        std::nullopt,
        std::nullopt,
        std::nullopt,
        controller.actions,
    };
}

} // namespace

sdk::Result<std::vector<ControllerModel>> project_controllers(const v5::IntegrationView& view)
{
    std::vector<ControllerModel> result;
    for(const auto& receiver : view.receivers)
    {
        for(const auto& controller : receiver.controllers)
        {
            if(!uses_openrgb_presentation(controller))
            {
                continue;
            }
            if(!exact_presentation_pin(controller))
            {
                return sdk::Result<std::vector<ControllerModel>>::failure(model_error(
                    "controller presentation does not match the pinned OpenRGB source"));
            }
            if(!supported_kind(controller.device_kind))
            {
                return sdk::Result<std::vector<ControllerModel>>::failure(model_error(
                    "OpenRGB adapter received an unsupported controller kind"));
            }
            auto target = sdk::lighting_target(controller);
            if(!target)
            {
                return sdk::Result<std::vector<ControllerModel>>::failure(target.error());
            }
            auto identifier = stable_id(controller);
            if(find_model(result, identifier) != nullptr)
            {
                return sdk::Result<std::vector<ControllerModel>>::failure(model_error(
                    "OpenRGB projection contains duplicate stable controller identities"));
            }
            result.push_back({
                std::move(identifier),
                {
                    controller.receiver_id,
                    controller.generation_id,
                    controller.device_id,
                    controller.endpoint_id,
                },
                controller.device_kind,
                controller.product_id,
                controller.model_name,
                controller.device_profile,
                controller.presentation,
                controller.availability,
                controller.battery,
                controller.capabilities,
                controller.lighting,
                control_state(controller),
                std::move(target).value(),
            });
        }
    }
    std::sort(result.begin(), result.end(), [](const ControllerModel& left, const ControllerModel& right) {
        return left.stable_id < right.stable_id;
    });
    return sdk::Result<std::vector<ControllerModel>>::success(std::move(result));
}

bool same_presentation(const ControllerModel& left, const ControllerModel& right) noexcept
{
    return left.stable_id == right.stable_id
        && left.device_kind == right.device_kind
        && left.product_id == right.product_id
        && left.device_profile == right.device_profile
        && left.model_name == right.model_name
        && left.presentation == right.presentation
        && left.lighting == right.lighting;
}

std::vector<ControllerChange> reconcile_controllers(
    const std::vector<ControllerModel>& current,
    const std::vector<ControllerModel>& next)
{
    std::vector<ControllerChange> result;
    result.reserve(current.size() + next.size());
    for(const auto& before : current)
    {
        const auto* after = find_model(next, before.stable_id);
        if(after == nullptr)
        {
            result.push_back(change(
                ReconcileKind::Removed,
                before.stable_id,
                before,
                std::nullopt));
            continue;
        }
        if(!same_presentation(before, *after))
        {
            result.push_back(change(
                ReconcileKind::PresentationReplaced,
                before.stable_id,
                before,
                *after));
        }
        else if(before == *after)
        {
            result.push_back(change(
                ReconcileKind::Retained,
                before.stable_id,
                before,
                *after));
        }
        else
        {
            result.push_back(change(
                ReconcileKind::StateUpdated,
                before.stable_id,
                before,
                *after));
        }
    }
    for(const auto& after : next)
    {
        if(find_model(current, after.stable_id) == nullptr)
        {
            result.push_back(change(
                ReconcileKind::Added,
                after.stable_id,
                std::nullopt,
                after));
        }
    }
    std::sort(result.begin(), result.end(), [](const ControllerChange& left, const ControllerChange& right) {
        if(left.stable_id != right.stable_id)
        {
            return left.stable_id < right.stable_id;
        }
        return static_cast<int>(left.kind) < static_cast<int>(right.kind);
    });
    return result;
}

} // namespace hyperflux::openrgb
