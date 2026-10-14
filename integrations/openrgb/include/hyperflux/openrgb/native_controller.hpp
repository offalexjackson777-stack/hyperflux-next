// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "device_thread_barrier.hpp"
#include "native_command.hpp"
#include "razer_registry.hpp"

#include "RGBController.h"

#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace hyperflux::openrgb::native
{

enum class ControllerMode : int
{
    Direct = 0,
    Off = 1,
    Static = 2,
};

struct CommandStatus
{
    EnqueueDisposition disposition;
    std::optional<sdk::Error> error;

    friend bool operator==(const CommandStatus&, const CommandStatus&) = default;
};

/// Thin OpenRGB API-5 controller backed exclusively by RuntimeWorker.
class NativeController final : public RGBController
{
public:
    NativeController(const NativeController&) = delete;
    NativeController& operator=(const NativeController&) = delete;
    NativeController(NativeController&&) = delete;
    NativeController& operator=(NativeController&&) = delete;
    ~NativeController() override;

    [[nodiscard]] static sdk::Result<std::unique_ptr<NativeController>> create(
        const ControllerModel& model,
        RazerPresentation presentation,
        LightingCommandSink& sink,
        std::string component_version);

    [[nodiscard]] const std::string& stable_id() const noexcept;
    [[nodiscard]] CommandStatus command_status() const;
    void update_generation(const GenerationId& generation_id) noexcept;

    void DeviceUpdateLEDs() override;
    void DeviceUpdateZoneLEDs(int zone) override;
    void DeviceUpdateSingleLED(int led) override;
    void DeviceUpdateMode() override;

private:
    NativeController(const ControllerModel& model,
        RazerPresentation presentation,
        LightingCommandSink& sink,
        std::string component_version);

    void configure();
    [[nodiscard]] std::vector<v5::RgbColor> direct_colors(unsigned int brightness) const;
    [[nodiscard]] std::vector<v5::RgbColor> uniform_colors(
        RGBColor color, unsigned int brightness) const;
    [[nodiscard]] unsigned int active_brightness() const noexcept;
    void apply_active_mode();
    void dispatch(ControllerMode requested_mode);
    void record(sdk::Result<EnqueueDisposition> result);

    ReceiverId receiver_id_;
    std::atomic_uint64_t generation_id_;
    std::string stable_id_;
    std::size_t application_slots_;
    RazerPresentation presentation_;
    LightingCommandSink* sink_;
    std::string component_version_;

    mutable std::mutex status_mutex_;
    CommandStatus status_ {EnqueueDisposition::Accepted, std::nullopt};
    DeviceThreadBarrier barrier_;
};

} // namespace hyperflux::openrgb::native
