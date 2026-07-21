// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/native_controller.hpp>

#include <algorithm>
#include <limits>
#include <set>
#include <string>
#include <utility>

namespace hyperflux::openrgb::native
{
namespace
{

constexpr unsigned int kBrightnessMaximum = 100;

sdk::Error controller_error(std::string message)
{
    return {
        sdk::ErrorCode::InvalidController,
        std::move(message),
        "HFX-INTEGRATION-001",
    };
}

zone_type native_zone_kind(LayoutZoneKind kind)
{
    switch(kind)
    {
        case LayoutZoneKind::Single:
            return ZONE_TYPE_SINGLE;
        case LayoutZoneKind::Linear:
            return ZONE_TYPE_LINEAR;
        case LayoutZoneKind::Matrix:
            return ZONE_TYPE_MATRIX;
    }
    return ZONE_TYPE_SINGLE;
}

std::uint8_t scaled_channel(unsigned int value, unsigned int brightness) noexcept
{
    const auto bounded = std::min(brightness, kBrightnessMaximum);
    return static_cast<std::uint8_t>((value * bounded + 50U) / kBrightnessMaximum);
}

v5::RgbColor stable_color(RGBColor color, unsigned int brightness)
{
    return {
        *ColorChannel::from(scaled_channel(RGBGetRValue(color), brightness)),
        *ColorChannel::from(scaled_channel(RGBGetGValue(color), brightness)),
        *ColorChannel::from(scaled_channel(RGBGetBValue(color), brightness)),
    };
}

sdk::Result<void> validate_presentation(
    const ControllerModel& model, const RazerPresentation& presentation)
{
    const auto slots = model.lighting.application_slot_count.value();
    if(presentation.device_kind != model.device_kind || presentation.product_id != model.product_id
        || presentation.model_name.empty() || presentation.zones.empty()
        || presentation.leds.size() != slots)
    {
        return sdk::Result<void>::failure(
            controller_error("OpenRGB presentation does not match the controller identity"));
    }

    std::uint32_t next_slot = 0;
    for(const auto& zone : presentation.zones)
    {
        if(zone.slot_count == 0 || zone.first_slot != next_slot
            || zone.first_slot + zone.slot_count > slots)
        {
            return sdk::Result<void>::failure(
                controller_error("OpenRGB presentation zones are not contiguous"));
        }
        if(zone.kind == LayoutZoneKind::Matrix && zone.matrix_map.size() != zone.slot_count)
        {
            return sdk::Result<void>::failure(
                controller_error("OpenRGB matrix dimensions do not match its zone"));
        }
        next_slot += zone.slot_count;
    }
    if(next_slot != slots)
    {
        return sdk::Result<void>::failure(
            controller_error("OpenRGB presentation does not cover every application slot"));
    }

    std::set<std::uint32_t> seen;
    std::size_t physical = 0;
    for(const auto& led : presentation.leds)
    {
        if(led.application_slot >= slots || !seen.insert(led.application_slot).second)
        {
            return sdk::Result<void>::failure(
                controller_error("OpenRGB presentation contains an invalid LED slot"));
        }
        physical += led.physically_present ? 1U : 0U;
    }
    if(physical != model.lighting.physical_led_count.value())
    {
        return sdk::Result<void>::failure(
            controller_error("OpenRGB presentation physical LED count drifted"));
    }
    return sdk::Result<void>::success();
}

} // namespace

sdk::Result<std::unique_ptr<NativeController>> NativeController::create(
    const ControllerModel& model,
    RazerPresentation presentation,
    LightingCommandSink& sink,
    std::string component_version)
{
    auto validated = validate_presentation(model, presentation);
    if(!validated)
    {
        return sdk::Result<std::unique_ptr<NativeController>>::failure(validated.error());
    }
    if(component_version.empty())
    {
        return sdk::Result<std::unique_ptr<NativeController>>::failure(
            controller_error("OpenRGB controller requires a component version"));
    }
    return sdk::Result<std::unique_ptr<NativeController>>::success(
        std::unique_ptr<NativeController>(new NativeController(
            model, std::move(presentation), sink, std::move(component_version))));
}

NativeController::NativeController(const ControllerModel& model,
    RazerPresentation presentation,
    LightingCommandSink& sink,
    std::string component_version)
    : stable_id_(model.stable_id),
      application_slots_(model.lighting.application_slot_count.value()),
      presentation_(std::move(presentation)),
      sink_(&sink),
      component_version_(std::move(component_version))
{
    configure();
}

const std::string& NativeController::stable_id() const noexcept
{
    return stable_id_;
}

CommandStatus NativeController::command_status() const
{
    std::lock_guard lock(status_mutex_);
    return status_;
}

void NativeController::configure()
{
    vendor = "Razer";
    name = presentation_.model_name;
    description = "Receiver-backed controller through HyperFlux Next";
    version = component_version_;
    serial.clear();
    location = "hyperflux-next:" + stable_id_;
    type = presentation_.device_kind == DeviceKind::Keyboard ? DEVICE_TYPE_KEYBOARD
                                                             : DEVICE_TYPE_MOUSE;
    flags = CONTROLLER_FLAG_LOCAL;
    active_mode = static_cast<int>(ControllerMode::Direct);

    mode direct;
    direct.name = "Direct";
    direct.value = static_cast<int>(ControllerMode::Direct);
    direct.flags = MODE_FLAG_HAS_PER_LED_COLOR | MODE_FLAG_HAS_BRIGHTNESS;
    direct.color_mode = MODE_COLORS_PER_LED;
    direct.brightness_min = 0;
    direct.brightness_max = kBrightnessMaximum;
    direct.brightness = kBrightnessMaximum;
    modes.push_back(std::move(direct));

    mode off;
    off.name = "Off";
    off.value = static_cast<int>(ControllerMode::Off);
    off.color_mode = MODE_COLORS_NONE;
    modes.push_back(std::move(off));

    mode static_mode;
    static_mode.name = "Static";
    static_mode.value = static_cast<int>(ControllerMode::Static);
    static_mode.flags = MODE_FLAG_HAS_MODE_SPECIFIC_COLOR | MODE_FLAG_HAS_BRIGHTNESS;
    static_mode.color_mode = MODE_COLORS_MODE_SPECIFIC;
    static_mode.colors_min = 1;
    static_mode.colors_max = 1;
    static_mode.colors = {ToRGBColor(255, 255, 255)};
    static_mode.brightness_min = 0;
    static_mode.brightness_max = kBrightnessMaximum;
    static_mode.brightness = kBrightnessMaximum;
    modes.push_back(std::move(static_mode));

    matrices_.reserve(presentation_.zones.size());
    zones.reserve(presentation_.zones.size());
    for(const auto& source : presentation_.zones)
    {
        zone target;
        target.name = source.name;
        target.type = native_zone_kind(source.kind);
        target.start_idx = source.first_slot;
        target.leds_count = source.slot_count;
        target.leds_min = source.slot_count;
        target.leds_max = source.slot_count;
        target.flags = 0;
        if(source.kind == LayoutZoneKind::Matrix)
        {
            auto storage = std::make_unique<MatrixStorage>();
            storage->values = source.matrix_map;
            storage->native.height = source.rows;
            storage->native.width = source.columns;
            storage->native.map = storage->values.data();
            target.matrix_map = &storage->native;
            matrices_.push_back(std::move(storage));
        }
        else
        {
            target.matrix_map = nullptr;
        }
        zones.push_back(std::move(target));
    }

    leds.reserve(presentation_.leds.size());
    for(const auto& source : presentation_.leds)
    {
        led target;
        target.name = source.name;
        target.value = source.application_slot;
        leds.push_back(std::move(target));
    }
    SetupColors();
}

void NativeController::SetupZones()
{
}

void NativeController::ResizeZone(int, int)
{
}

std::vector<v5::RgbColor> NativeController::direct_colors(unsigned int brightness) const
{
    std::vector<v5::RgbColor> result;
    result.reserve(colors.size());
    for(const auto color : colors)
    {
        result.push_back(stable_color(color, brightness));
    }
    return result;
}

std::vector<v5::RgbColor> NativeController::uniform_colors(
    RGBColor color, unsigned int brightness) const
{
    return std::vector<v5::RgbColor>(application_slots_, stable_color(color, brightness));
}

unsigned int NativeController::active_brightness() const noexcept
{
    if(active_mode < 0 || static_cast<std::size_t>(active_mode) >= modes.size())
    {
        return kBrightnessMaximum;
    }
    const auto& selected = modes[static_cast<std::size_t>(active_mode)];
    return (selected.flags & MODE_FLAG_HAS_BRIGHTNESS) == 0 ? kBrightnessMaximum
                                                            : selected.brightness;
}

void NativeController::dispatch(ControllerMode requested_mode)
{
    sdk::LightingIntent intent = sdk::LightingIntent::EffectFrame;
    std::vector<v5::RgbColor> requested;
    switch(requested_mode)
    {
        case ControllerMode::Direct:
            requested = direct_colors(active_brightness());
            record(sink_->enqueue_effect({stable_id_, application_slots_, std::move(requested)}));
            return;
        case ControllerMode::Off:
            intent = sdk::LightingIntent::Off;
            requested = uniform_colors(ToRGBColor(0, 0, 0), kBrightnessMaximum);
            break;
        case ControllerMode::Static:
        {
            intent = sdk::LightingIntent::Static;
            const auto& selected = modes[static_cast<std::size_t>(ControllerMode::Static)];
            const auto color =
                selected.colors.empty() ? ToRGBColor(0, 0, 0) : selected.colors.front();
            requested = uniform_colors(color, selected.brightness);
            break;
        }
    }
    record(sink_->enqueue_stable(intent, {{stable_id_, application_slots_, std::move(requested)}}));
}

void NativeController::record(sdk::Result<EnqueueDisposition> result)
{
    std::lock_guard lock(status_mutex_);
    if(!result)
    {
        status_ = {EnqueueDisposition::RejectedInvalid, result.error()};
        return;
    }
    status_ = {result.value(), std::nullopt};
}

void NativeController::DeviceUpdateLEDs()
{
    DeviceUpdateMode();
}

void NativeController::UpdateZoneLEDs(int)
{
    DeviceUpdateLEDs();
}

void NativeController::UpdateSingleLED(int)
{
    DeviceUpdateLEDs();
}

void NativeController::DeviceUpdateMode()
{
    if(active_mode < static_cast<int>(ControllerMode::Direct)
        || active_mode > static_cast<int>(ControllerMode::Static))
    {
        record(sdk::Result<EnqueueDisposition>::failure(
            controller_error("OpenRGB selected an unsupported controller mode")));
        return;
    }
    dispatch(static_cast<ControllerMode>(active_mode));
}

} // namespace hyperflux::openrgb::native
