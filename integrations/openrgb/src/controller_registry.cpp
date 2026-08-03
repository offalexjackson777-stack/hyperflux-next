// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/controller_registry.hpp>

#include <algorithm>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <utility>

namespace hyperflux::openrgb::native
{
namespace
{

sdk::Error registry_error(std::string message)
{
    return {
        sdk::ErrorCode::InvalidController,
        std::move(message),
        "HFX-INTEGRATION-001",
    };
}

sdk::Result<void> validate_models(const std::vector<ControllerModel>& models)
{
    std::optional<std::string_view> previous;
    for(const auto& model : models)
    {
        if(model.stable_id.empty() || (previous.has_value() && *previous >= model.stable_id))
        {
            return sdk::Result<void>::failure(registry_error(
                "OpenRGB controller registry input is not canonical and duplicate-free"));
        }
        previous = model.stable_id;
    }
    return sdk::Result<void>::success();
}

} // namespace

RazerNativeControllerFactory::RazerNativeControllerFactory(
    LightingCommandSink& sink, KeyboardLayoutVariant keyboard_layout, std::string component_version)
    : sink_(&sink),
      keyboard_layout_(keyboard_layout),
      component_version_(std::move(component_version))
{
}

sdk::Result<std::unique_ptr<NativeController>> RazerNativeControllerFactory::create(
    const ControllerModel& model)
{
    auto presentation = resolve_razer_presentation(model, keyboard_layout_);
    if(!presentation)
    {
        return sdk::Result<std::unique_ptr<NativeController>>::failure(presentation.error());
    }
    return NativeController::create(
        model, std::move(presentation).value(), *sink_, component_version_);
}

ControllerRegistry::ControllerRegistry(
    ControllerHost& host, NativeControllerFactory& factory) noexcept
    : host_(&host),
      factory_(&factory)
{
}

ControllerRegistry::~ControllerRegistry()
{
    shutdown();
}

sdk::Result<RegistryUpdate> ControllerRegistry::apply(std::vector<ControllerModel> next)
{
    std::lock_guard lock(mutex_);
    if(stopped_)
    {
        return sdk::Result<RegistryUpdate>::failure(
            registry_error("OpenRGB controller registry is already stopped"));
    }
    auto validated = validate_models(next);
    if(!validated)
    {
        return sdk::Result<RegistryUpdate>::failure(validated.error());
    }

    const auto changes = reconcile_controllers(models_locked(), next);
    std::map<std::string, std::unique_ptr<NativeController>> prepared;
    for(const auto& change : changes)
    {
        if(change.kind != ReconcileKind::Added
            && change.kind != ReconcileKind::PresentationReplaced)
        {
            continue;
        }
        if(!change.after.has_value())
        {
            return sdk::Result<RegistryUpdate>::failure(
                registry_error("OpenRGB controller addition has no projected model"));
        }
        auto created = factory_->create(*change.after);
        if(!created)
        {
            return sdk::Result<RegistryUpdate>::failure(created.error());
        }
        prepared.emplace(change.stable_id, std::move(created).value());
    }

    RegistryUpdate update;
    for(const auto& change : changes)
    {
        switch(change.kind)
        {
            case ReconcileKind::Added:
            {
                Entry entry {
                    *change.after,
                    std::move(prepared.at(change.stable_id)),
                    false,
                };
                auto inserted = entries_.emplace(change.stable_id, std::move(entry)).first;
                if(!suspended_)
                {
                    host_->register_controller(*inserted->second.controller);
                    inserted->second.registered = true;
                }
                ++update.added;
                break;
            }
            case ReconcileKind::Removed:
            {
                auto existing = entries_.find(change.stable_id);
                if(existing->second.registered)
                {
                    host_->unregister_controller(*existing->second.controller);
                }
                entries_.erase(existing);
                ++update.removed;
                break;
            }
            case ReconcileKind::Retained:
                ++update.retained;
                break;
            case ReconcileKind::StateUpdated:
                entries_.at(change.stable_id).controller->update_generation(
                    change.after->authority.generation_id);
                entries_.at(change.stable_id).model = *change.after;
                ++update.state_updated;
                break;
            case ReconcileKind::PresentationReplaced:
            {
                auto existing = entries_.find(change.stable_id);
                if(existing->second.registered)
                {
                    host_->unregister_controller(*existing->second.controller);
                }
                entries_.erase(existing);
                Entry entry {
                    *change.after,
                    std::move(prepared.at(change.stable_id)),
                    false,
                };
                auto inserted = entries_.emplace(change.stable_id, std::move(entry)).first;
                if(!suspended_)
                {
                    host_->register_controller(*inserted->second.controller);
                    inserted->second.registered = true;
                }
                ++update.presentation_replaced;
                break;
            }
        }
    }
    update.registered = registered_count_locked();
    return sdk::Result<RegistryUpdate>::success(update);
}

void ControllerRegistry::register_all_locked() noexcept
{
    for(auto& [stable_id, entry] : entries_)
    {
        (void)stable_id;
        if(!entry.registered)
        {
            host_->register_controller(*entry.controller);
            entry.registered = true;
        }
    }
}

void ControllerRegistry::unregister_all_locked() noexcept
{
    for(auto& [stable_id, entry] : entries_)
    {
        (void)stable_id;
        if(entry.registered)
        {
            host_->unregister_controller(*entry.controller);
            entry.registered = false;
        }
    }
}

void ControllerRegistry::suspend_for_detection() noexcept
{
    std::lock_guard lock(mutex_);
    if(stopped_ || suspended_)
    {
        return;
    }
    unregister_all_locked();
    suspended_ = true;
}

void ControllerRegistry::resume_after_detection() noexcept
{
    std::lock_guard lock(mutex_);
    if(stopped_ || !suspended_)
    {
        return;
    }
    suspended_ = false;
    register_all_locked();
}

void ControllerRegistry::shutdown() noexcept
{
    std::lock_guard lock(mutex_);
    if(stopped_)
    {
        return;
    }
    unregister_all_locked();
    entries_.clear();
    suspended_ = false;
    stopped_ = true;
}

bool ControllerRegistry::suspended() const noexcept
{
    std::lock_guard lock(mutex_);
    return suspended_;
}

bool ControllerRegistry::stopped() const noexcept
{
    std::lock_guard lock(mutex_);
    return stopped_;
}

std::size_t ControllerRegistry::size() const noexcept
{
    std::lock_guard lock(mutex_);
    return entries_.size();
}

std::size_t ControllerRegistry::registered_count() const noexcept
{
    std::lock_guard lock(mutex_);
    return registered_count_locked();
}

std::size_t ControllerRegistry::registered_count_locked() const noexcept
{
    return static_cast<std::size_t>(std::count_if(entries_.begin(),
        entries_.end(),
        [](const auto& item)
        {
            return item.second.registered;
        }));
}

std::vector<ControllerModel> ControllerRegistry::models() const
{
    std::lock_guard lock(mutex_);
    return models_locked();
}

std::vector<ControllerModel> ControllerRegistry::models_locked() const
{
    std::vector<ControllerModel> result;
    result.reserve(entries_.size());
    for(const auto& [stable_id, entry] : entries_)
    {
        (void)stable_id;
        result.push_back(entry.model);
    }
    return result;
}

std::uintptr_t ControllerRegistry::controller_identity(std::string_view stable_id) const noexcept
{
    std::lock_guard lock(mutex_);
    const auto found = entries_.find(stable_id);
    return found == entries_.end()
               ? 0
               : reinterpret_cast<std::uintptr_t>(found->second.controller.get());
}

} // namespace hyperflux::openrgb::native
