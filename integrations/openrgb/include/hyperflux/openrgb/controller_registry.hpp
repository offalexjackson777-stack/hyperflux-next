// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "native_controller.hpp"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <vector>

namespace hyperflux::openrgb::native
{

class ControllerHost
{
public:
    virtual ~ControllerHost() = default;

    /// Synchronous host ownership transitions. Implementations must never
    /// retain a second registration for the same object.
    virtual void register_controller(NativeController& controller) noexcept = 0;
    virtual void unregister_controller(NativeController& controller) noexcept = 0;
};

class NativeControllerFactory
{
public:
    virtual ~NativeControllerFactory() = default;

    [[nodiscard]] virtual sdk::Result<std::unique_ptr<NativeController>> create(
        const ControllerModel& model) = 0;
};

class RazerNativeControllerFactory final : public NativeControllerFactory
{
public:
    RazerNativeControllerFactory(LightingCommandSink& sink,
        KeyboardLayoutVariant keyboard_layout,
        std::string component_version);

    [[nodiscard]] sdk::Result<std::unique_ptr<NativeController>> create(
        const ControllerModel& model) override;

private:
    LightingCommandSink* sink_;
    KeyboardLayoutVariant keyboard_layout_;
    std::string component_version_;
};

struct RegistryUpdate
{
    std::size_t added = 0;
    std::size_t removed = 0;
    std::size_t retained = 0;
    std::size_t state_updated = 0;
    std::size_t presentation_replaced = 0;
    std::size_t registered = 0;

    friend bool operator==(const RegistryUpdate&, const RegistryUpdate&) = default;
};

/// Serialized owner of exactly the controllers created by HyperFlux.
///
/// Every owned controller stays registered with the host until it is removed,
/// replaced or the registry shuts down. The host and factory must outlive this
/// registry.
class ControllerRegistry
{
public:
    ControllerRegistry(ControllerHost& host, NativeControllerFactory& factory) noexcept;
    ControllerRegistry(const ControllerRegistry&) = delete;
    ControllerRegistry& operator=(const ControllerRegistry&) = delete;
    ControllerRegistry(ControllerRegistry&&) = delete;
    ControllerRegistry& operator=(ControllerRegistry&&) = delete;
    ~ControllerRegistry();

    [[nodiscard]] sdk::Result<RegistryUpdate> apply(std::vector<ControllerModel> next);
    void shutdown() noexcept;

    [[nodiscard]] bool stopped() const noexcept;
    [[nodiscard]] std::size_t size() const noexcept;
    [[nodiscard]] std::size_t registered_count() const noexcept;
    [[nodiscard]] std::vector<ControllerModel> models() const;
    [[nodiscard]] std::uintptr_t controller_identity(std::string_view stable_id) const noexcept;

private:
    struct Entry
    {
        ControllerModel model;
        std::unique_ptr<NativeController> controller;
    };

    void unregister_all_locked() noexcept;
    [[nodiscard]] std::vector<ControllerModel> models_locked() const;

    ControllerHost* host_;
    NativeControllerFactory* factory_;
    std::map<std::string, Entry, std::less<>> entries_;
    mutable std::mutex mutex_;
    bool stopped_ = false;
};

} // namespace hyperflux::openrgb::native
