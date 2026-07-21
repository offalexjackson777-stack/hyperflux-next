// SPDX-License-Identifier: GPL-2.0-only

#include "plugin_widget.hpp"

#include <QApplication>
#include <QLabel>
#include <QTableWidget>

#include <cstdlib>
#include <iostream>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-plugin-widget-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

} // namespace

int main(int argc, char** argv)
{
    QApplication application(argc, argv);
    using namespace hyperflux::openrgb::native;

    PluginInformationViewModel model;
    model.tone = PluginHealthTone::Positive;
    model.headline = "Ready";
    model.summary = "1 controller is available in OpenRGB.";
    model.controllers.push_back({
        "receiver-1/mouse-1/profile",
        "Test Mouse",
        "Mouse",
        "Ready",
        "76%",
        "13 LEDs",
        "Available",
    });
    model.lighting_transport = "Lighting transport";
    model.effects_authority = "Effects authority";
    model.build_identity = "Build identity";

    PluginInformationWidget widget([&model] { return model; });
    auto* health = widget.findChild<QLabel*>("hyperfluxHealthTitle");
    auto* table = widget.findChild<QTableWidget*>("hyperfluxControllerTable");
    auto* effects = widget.findChild<QLabel*>("hyperfluxEffectsAuthority");
    if(health == nullptr || health->text() != "Ready" || table == nullptr
       || table->isHidden() || table->rowCount() != 1 || table->columnCount() != 6
       || table->item(0, 0)->text() != "Test Mouse"
       || table->item(0, 2)->text() != "Ready"
       || effects == nullptr || effects->text() != "Effects authority")
    {
        return failure(__LINE__);
    }

    model.tone = PluginHealthTone::Warning;
    model.headline = "Connecting";
    model.summary = "Waiting for the local HyperFlux bridge.";
    model.controllers.clear();
    widget.refresh();
    auto* empty = widget.findChild<QLabel*>("hyperfluxControllerEmptyState");
    if(health->text() != "Connecting" || !table->isHidden()
       || empty == nullptr || empty->text().isEmpty())
    {
        return failure(__LINE__);
    }
    return EXIT_SUCCESS;
}
