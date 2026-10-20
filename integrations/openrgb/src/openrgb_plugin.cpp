// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/build_config.hpp>
#if HFX_OPENRGB_NATIVE_ROUTE_ENABLED
#include <hyperflux/openrgb/native_plugin_application.hpp>
#else
#include <hyperflux/openrgb/plugin_application.hpp>
#endif
#include <hyperflux/openrgb/plugin_view_model.hpp>

#include "plugin_widget.hpp"

#include "OpenRGBPluginInterface.h"
#include "ResourceManagerCallback.h"

#include <QDebug>
#include <QWidget>

#include <string>

static_assert(OPENRGB_PLUGIN_API_VERSION == 5);

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
    HyperFluxNextOpenRgbPlugin()
        : application_([this]
          {
              QMetaObject::invokeMethod(
                  this,
                  [this] { emit status_changed(); },
                  Qt::QueuedConnection);
          })
    {
    }

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
        info.URL = std::string(repository_url);
        info.Location = OPENRGB_PLUGIN_LOCATION_INFORMATION;
        info.Label = "HyperFlux Next";
        info.TabIconString = "";
        // The plugin handles no SDK commands.
        info.ProtocolVersion = 0;
        return info;
    }

    unsigned int GetPluginAPIVersion() override
    {
        return OPENRGB_PLUGIN_API_VERSION;
    }

    void Load(OpenRGBPluginAPIInterface* api) override
    {
        const auto loaded = application_.load(api);
        if(!loaded)
        {
            qWarning().noquote()
                << "[HyperFlux Next] Plugin load deferred:" 
                << QString::fromStdString(loaded.error().message);
        }
    }

    QWidget* GetWidget() override
    {
        auto* widget = new PluginInformationWidget([this]
        {
#if HFX_OPENRGB_NATIVE_ROUTE_ENABLED
            return make_plugin_information_view_model(
                application_.status(),
                application_.controllers(),
                monotonic_now_ms());
#else
            return make_plugin_information_view_model(
                application_.status(),
                application_.inventory(),
                application_.controllers(),
                monotonic_now_ms());
#endif
        });
        connect(
            this,
            &HyperFluxNextOpenRgbPlugin::status_changed,
            widget,
            &PluginInformationWidget::refresh,
            Qt::QueuedConnection);
        return widget;
    }

    QMenu* GetTrayMenu() override
    {
        return nullptr;
    }

    void Unload() override
    {
        application_.unload();
    }

    void OnProfileAboutToLoad() override
    {
    }

    void OnProfileLoad(nlohmann::json /*profile_data*/) override
    {
    }

    nlohmann::json OnProfileSave() override
    {
        return {};
    }

    unsigned char* OnSDKCommand(
        unsigned int /*pkt_id*/,
        unsigned char* /*pkt_data*/,
        unsigned int* pkt_size) override
    {
        if(pkt_size != nullptr)
        {
            *pkt_size = 0;
        }
        return nullptr;
    }

    void ProfileManagerUpdated(unsigned int /*update_reason*/) override
    {
    }

    void ResourceManagerUpdated(unsigned int update_reason) override
    {
        // Called from OpenRGB worker threads.
        if(update_reason == RESOURCEMANAGER_UPDATE_REASON_DETECTION_COMPLETE)
        {
            application_.detection_completed();
        }
    }

    void SettingsManagerUpdated(unsigned int /*update_reason*/) override
    {
    }

signals:
    void status_changed();

private:
#if HFX_OPENRGB_NATIVE_ROUTE_ENABLED
    OpenRgbNativePluginApplication application_;
#else
    OpenRgbPluginApplication application_;
#endif
};

} // namespace hyperflux::openrgb::native

#include "openrgb_plugin.moc"
