// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/sdk/lighting.hpp>

#include <algorithm>
#include <optional>
#include <string>
#include <utility>
#include <variant>

namespace hyperflux::sdk
{
namespace
{

Error error(ErrorCode code, std::string message, std::optional<std::string> finding = std::nullopt)
{
    return {code, std::move(message), std::move(finding)};
}

bool has_capability(const v5::ControllerView& controller, std::string_view capability)
{
    return std::any_of(
        controller.capabilities.begin(),
        controller.capabilities.end(),
        [capability](const CapabilityId& candidate) { return candidate.value() == capability; });
}

bool same_resource_set(
    const std::vector<v5::ResourceKey>& left,
    const std::vector<v5::ResourceKey>& right)
{
    return left.size() == right.size()
        && std::all_of(left.begin(), left.end(), [&right](const v5::ResourceKey& resource) {
               return std::count(right.begin(), right.end(), resource) == 1;
           });
}

Result<v5::LeaseGrant> require_grant(
    v5::LeaseResult result,
    const std::vector<v5::ResourceKey>& expected,
    const std::optional<LeaseId>& expected_lease,
    LeaseState expected_state)
{
    if(auto* granted = std::get_if<v5::LeaseResultGranted>(&result))
    {
        if(granted->detail.state != expected_state
           || !same_resource_set(granted->detail.resources, expected)
           || (expected_lease.has_value() && granted->detail.lease_id != *expected_lease))
        {
            return Result<v5::LeaseGrant>::failure(error(
                ErrorCode::LeaseRejected,
                "bridge returned a lease grant that does not match the requested resource set",
                "HFX-REQUEST-001"));
        }
        return Result<v5::LeaseGrant>::success(std::move(granted->detail));
    }
    if(std::holds_alternative<v5::LeaseResultConflict>(result))
    {
        return Result<v5::LeaseGrant>::failure(error(
            ErrorCode::OwnershipConflict,
            "another application owns a requested HyperFlux lighting resource",
            "HFX-OWNERSHIP-001"));
    }
    const auto& rejected = std::get<v5::LeaseResultRejected>(result).detail;
    return Result<v5::LeaseGrant>::failure(error(
        ErrorCode::LeaseRejected,
        "the bridge rejected the HyperFlux lighting lease",
        std::string(rejected.finding_id.value())));
}

Result<void> validate_targets(const std::vector<LightingTarget>& targets)
{
    if(targets.empty())
    {
        return Result<void>::failure(error(
            ErrorCode::InvalidArgument,
            "a lighting session requires at least one controller"));
    }
    const auto& first = targets.front();
    for(std::size_t index = 0; index < targets.size(); ++index)
    {
        const auto& target = targets[index];
        if(target.receiver_id != first.receiver_id || target.generation_id != first.generation_id)
        {
            return Result<void>::failure(error(
                ErrorCode::MixedReceiverGeneration,
                "one lighting session cannot span receiver generations",
                "HFX-GENERATION-001"));
        }
        if(target.application_slot_count.value() == 0
           || target.resource.kind != ResourceKind::Lighting
           || target.resource.receiver_id != target.receiver_id
           || target.resource.generation_id != target.generation_id
           || target.resource.device_id != target.device_id)
        {
            return Result<void>::failure(error(
                ErrorCode::InvalidController,
                "a lighting target is not bound to one exact controller resource",
                "HFX-REQUEST-001"));
        }
        if(std::count_if(
               targets.begin(),
               targets.end(),
               [&target](const LightingTarget& candidate) {
                   return candidate.resource == target.resource;
               })
           != 1)
        {
            return Result<void>::failure(error(
                ErrorCode::InvalidController,
                "a lighting session contains a duplicate controller resource",
                "HFX-REQUEST-001"));
        }
    }
    return Result<void>::success();
}

bool is_black(const v5::RgbColor& color)
{
    return color.red.value() == 0 && color.green.value() == 0 && color.blue.value() == 0;
}

const LightingTarget* owned_target(
    const std::vector<LightingTarget>& targets,
    const LightingTarget& requested)
{
    const auto found = std::find(targets.begin(), targets.end(), requested);
    return found == targets.end() ? nullptr : &*found;
}

Result<void> validate_updates(
    const std::vector<LightingTarget>& targets,
    LightingIntent intent,
    const std::vector<LightingUpdate>& updates)
{
    if(updates.empty())
    {
        return Result<void>::failure(error(
            ErrorCode::InvalidLightingFrame,
            "a lighting transaction requires at least one frame",
            "HFX-REQUEST-001"));
    }
    for(const auto& update : updates)
    {
        const auto* target = owned_target(targets, update.target);
        if(target == nullptr)
        {
            return Result<void>::failure(error(
                ErrorCode::InvalidController,
                "the lighting transaction names a controller outside its lease",
                "HFX-OWNERSHIP-002"));
        }
        if(update.colors.size() != target->application_slot_count.value())
        {
            return Result<void>::failure(error(
                ErrorCode::InvalidLightingFrame,
                "a lighting frame does not match the qualified application slot count",
                "HFX-REQUEST-001"));
        }
        if(intent == LightingIntent::Off
           && !std::all_of(update.colors.begin(), update.colors.end(), is_black))
        {
            return Result<void>::failure(error(
                ErrorCode::InvalidLightingFrame,
                "an Off intent may contain only black lighting values",
                "HFX-REQUEST-001"));
        }
        if(std::count_if(
               updates.begin(),
               updates.end(),
               [&update](const LightingUpdate& candidate) {
                   return candidate.target.resource == update.target.resource;
               })
           != 1)
        {
            return Result<void>::failure(error(
                ErrorCode::InvalidLightingFrame,
                "a lighting transaction contains duplicate frames for one controller",
                "HFX-REQUEST-001"));
        }
    }
    return Result<void>::success();
}

std::vector<v5::ResourceKey> resources(const std::vector<LightingTarget>& targets)
{
    std::vector<v5::ResourceKey> result;
    result.reserve(targets.size());
    for(const auto& target : targets)
    {
        result.push_back(target.resource);
    }
    return result;
}

TransactionClass transaction_class(LightingIntent intent)
{
    return intent == LightingIntent::EffectFrame ? TransactionClass::EffectFrame
                                                  : TransactionClass::StaticLighting;
}

std::vector<v5::StableLightingIntent> stable_intents(
    LightingIntent intent,
    const std::vector<LightingUpdate>& updates)
{
    std::vector<v5::StableLightingIntent> result;
    if(intent == LightingIntent::EffectFrame)
    {
        return result;
    }
    result.reserve(updates.size());
    const auto mode = intent == LightingIntent::Off ? StableLightingMode::Off
                                                    : StableLightingMode::Static;
    for(const auto& update : updates)
    {
        result.push_back({update.target.device_id, mode});
    }
    return result;
}

} // namespace

Result<LightingTarget> lighting_target(const v5::ControllerView& controller)
{
    if(controller.resource.receiver_id != controller.receiver_id
       || controller.resource.generation_id != controller.generation_id
       || controller.resource.device_id != controller.device_id
       || controller.resource.kind != ResourceKind::Lighting
       || controller.lighting.application_slot_count.value() == 0
       || !has_capability(controller, "lighting.direct-frame"))
    {
        return Result<LightingTarget>::failure(error(
            ErrorCode::InvalidController,
            "integration controller lacks an exact writable lighting binding",
            "HFX-REQUEST-001"));
    }
    return Result<LightingTarget>::success({
        controller.receiver_id,
        controller.generation_id,
        controller.device_id,
        controller.endpoint_id,
        controller.receiver_profile,
        controller.device_profile,
        controller.lighting.application_slot_count,
        controller.resource,
    });
}

LightingSession::LightingSession(
    LightingBridge& bridge,
    std::vector<LightingTarget> targets,
    v5::LeaseGrant grant)
    : bridge_(&bridge), targets_(std::move(targets)), grant_(std::move(grant))
{
}

Result<LightingSession> LightingSession::acquire(
    LightingBridge& bridge,
    std::vector<LightingTarget> targets,
    LeaseDurationMs duration_ms)
{
    auto validation = validate_targets(targets);
    if(!validation)
    {
        return Result<LightingSession>::failure(validation.error());
    }
    const auto requested_resources = resources(targets);
    auto result = bridge.acquire_lease(requested_resources, duration_ms);
    if(!result)
    {
        return Result<LightingSession>::failure(result.error());
    }
    auto grant = require_grant(
        std::move(result).value(),
        requested_resources,
        std::nullopt,
        LeaseState::Granted);
    if(!grant)
    {
        return Result<LightingSession>::failure(grant.error());
    }
    return Result<LightingSession>::success(
        LightingSession(bridge, std::move(targets), std::move(grant).value()));
}

bool LightingSession::active() const noexcept
{
    return bridge_ != nullptr && grant_.has_value();
}

const LeaseId* LightingSession::lease_id() const noexcept
{
    return grant_.has_value() ? &grant_->lease_id : nullptr;
}

const std::vector<LightingTarget>& LightingSession::targets() const noexcept
{
    return targets_;
}

bool LightingSession::matches(const LightingTarget& target) const noexcept
{
    return active() && owned_target(targets_, target) != nullptr;
}

Result<void> LightingSession::renew(LeaseDurationMs duration_ms)
{
    if(!active())
    {
        return Result<void>::failure(error(
            ErrorCode::SessionInactive,
            "the lighting session is no longer active",
            "HFX-OWNERSHIP-002"));
    }
    const auto expected_resources = resources(targets_);
    const auto expected_lease = grant_->lease_id;
    auto result = bridge_->renew_lease(expected_lease, duration_ms);
    if(!result)
    {
        return Result<void>::failure(result.error());
    }
    auto grant = require_grant(
        std::move(result).value(),
        expected_resources,
        expected_lease,
        LeaseState::Renewed);
    if(!grant)
    {
        abandon();
        return Result<void>::failure(grant.error());
    }
    grant_ = std::move(grant).value();
    return Result<void>::success();
}

Result<void> LightingSession::release()
{
    if(!active())
    {
        return Result<void>::success();
    }
    const auto expected_resources = resources(targets_);
    const auto expected_lease = grant_->lease_id;
    auto result = bridge_->release_lease(expected_lease);
    if(!result)
    {
        return Result<void>::failure(result.error());
    }
    auto grant = require_grant(
        std::move(result).value(),
        expected_resources,
        expected_lease,
        LeaseState::Released);
    if(!grant)
    {
        return Result<void>::failure(grant.error());
    }
    abandon();
    return Result<void>::success();
}

void LightingSession::abandon() noexcept
{
    grant_.reset();
    bridge_ = nullptr;
}

Result<v5::TransactionResult> LightingSession::submit(
    LightingIntent intent,
    std::vector<LightingUpdate> updates,
    MonotonicMs deadline_ms)
{
    if(!active())
    {
        return Result<v5::TransactionResult>::failure(error(
            ErrorCode::SessionInactive,
            "the lighting session is no longer active",
            "HFX-OWNERSHIP-002"));
    }
    auto validation = validate_updates(targets_, intent, updates);
    if(!validation)
    {
        return Result<v5::TransactionResult>::failure(validation.error());
    }
    auto transaction_id = bridge_->next_transaction_id();
    if(!transaction_id)
    {
        return Result<v5::TransactionResult>::failure(transaction_id.error());
    }

    std::vector<v5::DeviceProfileBinding> profiles;
    std::vector<v5::ResourceKey> requested_resources;
    std::vector<v5::LightingFrame> frames;
    profiles.reserve(updates.size());
    requested_resources.reserve(updates.size());
    frames.reserve(updates.size());
    auto frame_index = FrameIndex::from(0).value();
    for(auto& update : updates)
    {
        profiles.push_back({
            update.target.device_id,
            update.target.device_profile.profile_id,
            update.target.device_profile.profile_digest,
            update.target.application_slot_count,
        });
        requested_resources.push_back(update.target.resource);
        frames.push_back({update.target.device_id, frame_index, std::move(update.colors)});
        frame_index = FrameIndex::from(frame_index.value() + 1).value();
    }

    const auto& receiver = targets_.front();
    return bridge_->submit_transaction({
        std::move(transaction_id).value(),
        grant_->lease_id,
        receiver.receiver_id,
        receiver.generation_id,
        receiver.receiver_profile.profile_id,
        receiver.receiver_profile.profile_digest,
        std::move(profiles),
        transaction_class(intent),
        stable_intents(intent, updates),
        deadline_ms,
        std::move(requested_resources),
        std::move(frames),
    });
}

} // namespace hyperflux::sdk
