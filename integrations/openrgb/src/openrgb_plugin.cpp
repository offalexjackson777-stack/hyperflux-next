// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/build_config.hpp>
#include <hyperflux/openrgb/plugin_application.hpp>
#include <hyperflux/openrgb/plugin_view_model.hpp>

#include "plugin_widget.hpp"

#include "OpenRGBPluginInterface.h"

#include <QDebug>
#include <QWidget>

#include <string>

static_assert(OPENRGB_PLUGIN_API_VERSION == 4);

namespace hyperflux::openrgb::native
{

class HyperFluxNextOpenRgbPlugin final : public QObject, public OpenRGBPluginInterface
{
    Q_OBJECT
    Q_PLUGIN_METADATA(
        IID OpenRGBPluginInterface_IID
        FILE "hyperflux-next-openrgb.json")
    Q_INTERFACES(OpenRGBPluginInterface)

public:
    ~HyperFluxNextOpenRgbPlugin() override
    {
        Unload();
    }

    OpenRGBPluginInfo GetPluginInfo() override
    {
        OpenRGBPluginInfo info;
        info.Name = "HyperFlux Next";
        info.Description =
            "Native OpenRGB presentation for devices paired through HyperFlux";
        info.Version = std::string(build_config::component_version);
        info.Commit = std::string(build_config::source_revision);
        info.URL = "";
        info.Location = OPENRGB_PLUGIN_LOCATION_INFORMATION;
        info.Label = "HyperFlux Next";
        info.TabIconString = "";
        return info;
    }

    unsigned int GetPluginAPIVersion() override
    {
        return OPENRGB_PLUGIN_API_VERSION;
    }

    void Load(ResourceManagerInterface* manager) override
    {
        const auto loaded = application_.load(manager);
        if(!loaded)
        {
            qWarning().noquote()
                << "[HyperFlux Next] Plugin load deferred:" 
                << QString::fromStdString(loaded.error().message);
        }
    }

    QWidget* GetWidget() override
    {
        return new PluginInformationWidget([this]
        {
            return make_plugin_information_view_model(
                application_.status(), application_.controllers());
        });
    }

    QMenu* GetTrayMenu() override
    {
        return nullptr;
    }

    void Unload() override
    {
        application_.unload();
    }

private:
    OpenRgbPluginApplication application_;
};

} // namespace hyperflux::openrgb::native

#include "openrgb_plugin.moc"
