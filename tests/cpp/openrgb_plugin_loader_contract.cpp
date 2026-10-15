// SPDX-License-Identifier: GPL-2.0-only

#include "support/openrgb_plugin_api_stub.hpp"

#include "OpenRGBPluginInterface.h"
#include "ResourceManagerCallback.h"

#include <QApplication>
#include <QJsonObject>
#include <QPluginLoader>
#include <QWidget>

#include <cstdlib>
#include <iostream>
#include <vector>

namespace
{

int failure(int line, const QString& detail = {})
{
    std::cerr << "openrgb-plugin-loader-contract failed at line " << line;
    if(!detail.isEmpty())
    {
        std::cerr << ": " << detail.toStdString();
    }
    std::cerr << '\n';
    return EXIT_FAILURE;
}

class PluginApi final : public hyperflux::test::OpenRgbPluginApiStub
{
public:
    RGBControllerInterface* CreateVirtualRGBController(RGBController_Setup* setup) override
    {
        // The container has no HyperFlux receiver, so the plugin must not
        // present any controller; a host would own the returned object.
        if(setup == nullptr)
        {
            valid_ = false;
        }
        ++creates_;
        return nullptr;
    }
    void DeleteVirtualRGBController(RGBControllerInterface*) override { valid_ = false; }
    void RegisterVirtualRGBController(RGBControllerInterface*) override { valid_ = false; }
    void RegisterVirtualRGBControllerInThread(RGBControllerInterface*) override
    {
        valid_ = false;
    }
    void UnregisterVirtualRGBController(RGBControllerInterface*) override { valid_ = false; }
    void UpdateVirtualRGBController(RGBControllerInterface*, RGBController_Setup*) override
    {
        valid_ = false;
    }
    void UnregisterVirtualRGBControllerInThread(RGBControllerInterface*) override
    {
        valid_ = false;
    }

    [[nodiscard]] bool clean() const noexcept { return valid_ && creates_ == 0; }

private:
    unsigned int creates_ = 0;
    bool valid_ = true;
};

} // namespace

int main(int argc, char** argv)
{
    if(argc != 2)
    {
        return failure(__LINE__);
    }
    QApplication application(argc, argv);
    QPluginLoader loader(QString::fromLocal8Bit(argv[1]));
    // Bind every symbol at load time so an unresolved upstream dependency
    // fails here instead of inside OpenRGB.
    loader.setLoadHints(QLibrary::ResolveAllSymbolsHint);
    const auto root = loader.metaData();
    const auto metadata = root.value("MetaData").toObject();
    if(metadata.value("Id").toString() != "org.hyperflux.next.openrgb"
       || metadata.value("Name").toString() != "HyperFlux Next"
       || metadata.value("OpenRGBPluginAPIVersion").toInt() != OPENRGB_PLUGIN_API_VERSION)
    {
        return failure(__LINE__, loader.errorString());
    }
    if(!loader.load())
    {
        return failure(__LINE__, loader.errorString());
    }
    auto* instance = loader.instance();
    auto* plugin = qobject_cast<OpenRGBPluginInterface*>(instance);
    if(plugin == nullptr || plugin->GetPluginAPIVersion() != OPENRGB_PLUGIN_API_VERSION)
    {
        return failure(__LINE__, loader.errorString());
    }
    const auto info = plugin->GetPluginInfo();
    if(info.Name != "HyperFlux Next" || info.Version != "2.1.0"
       || info.Commit.empty() || !info.URL.empty()
       || info.Location != OPENRGB_PLUGIN_LOCATION_INFORMATION
       || info.Label != "HyperFlux Next" || info.ProtocolVersion != 0)
    {
        return failure(__LINE__);
    }
    PluginApi api;
    plugin->Load(&api);
    QCoreApplication::processEvents();
    // OpenRGB reports detection completion from its worker threads.
    plugin->ResourceManagerUpdated(RESOURCEMANAGER_UPDATE_REASON_DETECTION_COMPLETE);
    QCoreApplication::processEvents();
    unsigned int packet_size = 1;
    unsigned char packet = 0;
    if(plugin->OnSDKCommand(0, &packet, &packet_size) != nullptr || packet_size != 0
       || !plugin->OnProfileSave().is_null())
    {
        return failure(__LINE__);
    }
    QWidget* widget = plugin->GetWidget();
    if(widget == nullptr || widget->objectName() != "hyperfluxNextInformation")
    {
        return failure(__LINE__);
    }
    delete widget;
    plugin->Unload();
    QCoreApplication::processEvents();
    if(!api.clean())
    {
        return failure(__LINE__);
    }
    if(!loader.unload())
    {
        return failure(__LINE__, loader.errorString());
    }
    return EXIT_SUCCESS;
}
