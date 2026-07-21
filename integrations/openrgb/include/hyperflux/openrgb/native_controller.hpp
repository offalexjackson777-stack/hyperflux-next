// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "native_command.hpp"
#include "razer_registry.hpp"

#include "RGBController.h"

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

/// Thin OpenRGB API-4 controller backed exclusively by RuntimeWorker.
class NativeController final : public RGBController
{
public:
    NativeController(const NativeController&) = delete;
    NativeController& operator=(const NativeController&) = delete;
    NativeController(NativeController&&) = delete;
    NativeController& operator=(NativeController&&) = delete;
    ~NativeController() override = default;

    [[nodiscard]] static sdk::Result<std::unique_ptr<NativeController>> create(
        const ControllerModel& model,
        RazerPresentation presentation,
        LightingCommandSink& sink,
        std::string component_version);

    [[nodiscard]] const std::string& stable_id() const noexcept;
    [[nodiscard]] CommandStatus command_status() const;

    void SetupZones() override;
    void ResizeZone(int zone, int new_size) override;
    void DeviceUpdateLEDs() override;
    void UpdateZoneLEDs(int zone) override;
    void UpdateSingleLED(int led) override;
    void DeviceUpdateMode() override;

private:
    struct MatrixStorage
    {
        std::vector<unsigned int> values;
        matrix_map_type native {};
    };

    NativeController(const ControllerModel& model,
        RazerPresentation presentation,
        LightingCommandSink& sink,
        std::string component_version);

    void configure();
    [[nodiscard]] std::vector<v5::RgbColor> direct_colors(unsigned int brightness) const;
    [[nodiscard]] std::vector<v5::RgbColor> uniform_colors(
        RGBColor color, unsigned int brightness) const;
    [[nodiscard]] unsigned int active_brightness() const noexcept;
    void dispatch(ControllerMode requested_mode);
    void record(sdk::Result<EnqueueDisposition> result);

    std::string stable_id_;
    std::size_t application_slots_;
    RazerPresentation presentation_;
    LightingCommandSink* sink_;
    std::string component_version_;
    std::vector<std::unique_ptr<MatrixStorage>> matrices_;

    mutable std::mutex status_mutex_;
    CommandStatus status_ {EnqueueDisposition::Accepted, std::nullopt};
};

} // namespace hyperflux::openrgb::native
