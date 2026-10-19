// SPDX-License-Identifier: GPL-2.0-only

#include "plugin_widget.hpp"

#include <QApplication>
#include <QFrame>
#include <QLabel>
#include <QProgressBar>
#include <QTimer>
#include <QToolButton>

#include <cstdlib>
#include <iostream>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-plugin-widget-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

QString label_text(const QWidget* parent, const char* name)
{
    const auto* label = parent->findChild<QLabel*>(name);
    return label == nullptr ? QString("<missing>") : label->text();
}

} // namespace

int main(int argc, char** argv)
{
    QApplication application(argc, argv);
    using namespace hyperflux::openrgb::native;

    PluginInformationViewModel model;
    model.tone = PluginHealthTone::Positive;
    model.headline = "Ready";
    model.summary = "2 paired devices; 2 controllers are exposed in OpenRGB.";
    PluginDeviceRow mouse {
        "receiver-1/mouse-1/profile",
        "Test Mouse",
        "Mouse",
        "Paired",
        "Available",
        "76%",
        "Full support",
        "Controlled by OpenRGB",
    };
    mouse.connection = "Wireless";
    mouse.battery_percent = 76;
    mouse.charge = "Charging";
    model.devices.push_back(mouse);
    PluginDeviceRow keyboard {
        "receiver-1/keyboard-1/profile",
        "Test Keyboard TKL",
        "Keyboard",
        "Paired",
        "Available",
        "Waiting for report",
        "Full support",
        "Controlled by OpenRGB",
    };
    keyboard.connection = "Wireless";
    keyboard.battery_note = "Waiting for report";
    model.devices.push_back(keyboard);
    model.lighting_transport = "Lighting transport";
    model.effects_authority = "Effects authority";
    model.build_identity = "Build identity";

    PluginInformationWidget widget([&model] { return model; });
    auto* health = widget.findChild<QLabel*>("hyperfluxHealthTitle");
    auto* cards = widget.findChild<QWidget*>("hyperfluxDeviceCards");
    auto* attention = widget.findChild<QFrame*>("hyperfluxAttention");
    auto* repository = widget.findChild<QToolButton*>("hyperfluxRepository");
    const auto card_frames = widget.findChildren<QFrame*>("hyperfluxDeviceCard");
    if(health == nullptr || health->text() != "Ready" || cards == nullptr || cards->isHidden()
       || card_frames.size() != 2 || attention == nullptr || !attention->isHidden()
       || repository == nullptr || !repository->text().startsWith("GitHub")
       || !repository->toolTip().contains("github.com/offalexjackson777-stack/hyperflux-next")
       || widget.findChild<QTimer*>() != nullptr
       || label_text(&widget, "hyperfluxEffectsAuthority") != "Effects authority")
    {
        return failure(__LINE__);
    }
    const QFrame* mouse_card = nullptr;
    const QFrame* keyboard_card = nullptr;
    for(const auto* card : card_frames)
    {
        const auto stable_id = card->property("stableId").toString();
        if(stable_id == "receiver-1/mouse-1/profile")
        {
            mouse_card = card;
        }
        else if(stable_id == "receiver-1/keyboard-1/profile")
        {
            keyboard_card = card;
        }
    }
    if(mouse_card == nullptr || keyboard_card == nullptr
       || label_text(mouse_card, "hyperfluxDeviceName") != "Test Mouse"
       || label_text(mouse_card, "hyperfluxDeviceConnection") != "Wireless"
       || label_text(mouse_card, "hyperfluxBatteryPercent") != "76%"
       || label_text(mouse_card, "hyperfluxBatteryCharge") != "Charging"
       || mouse_card->findChild<QProgressBar*>("hyperfluxBatteryBar") == nullptr
       || mouse_card->findChild<QProgressBar*>("hyperfluxBatteryBar")->value() != 76
       || label_text(keyboard_card, "hyperfluxDeviceName") != "Test Keyboard TKL"
       || keyboard_card->findChild<QLabel*>("hyperfluxBatteryCharge") != nullptr
       || label_text(keyboard_card, "hyperfluxBatteryNote") != "Waiting for report"
       || !label_text(keyboard_card, "hyperfluxDeviceControl").contains("Full support"))
    {
        return failure(__LINE__);
    }

    model.tone = PluginHealthTone::Warning;
    model.headline = "Connecting";
    model.summary = "Waiting for the local HyperFlux native tunnel.";
    model.attention = "Run hyperfluxctl doctor --explain in a terminal for the fix.";
    model.devices.clear();
    widget.refresh();
    auto* empty = widget.findChild<QLabel*>("hyperfluxControllerEmptyState");
    if(health->text() != "Connecting" || !cards->isHidden()
       || !widget.findChildren<QFrame*>("hyperfluxDeviceCard").isEmpty()
       || empty == nullptr || empty->isHidden() || empty->text().isEmpty()
       || attention->isHidden()
       || !label_text(&widget, "hyperfluxAttentionText").contains("hyperfluxctl doctor --explain"))
    {
        return failure(__LINE__);
    }
    return EXIT_SUCCESS;
}
