// SPDX-License-Identifier: GPL-2.0-only

#include "OpenRGBPluginInterface.h"

#include <QApplication>
#include <QJsonObject>
#include <QPluginLoader>
#include <QWidget>

#include <algorithm>
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

class ResourceManager final : public ResourceManagerInterface
{
public:
    std::vector<i2c_smbus_interface*>& GetI2CBusses() override { return busses_; }

    void RegisterRGBController(RGBController* controller) override
    {
        if(controller == nullptr
           || std::find(controllers_.begin(), controllers_.end(), controller)
               != controllers_.end())
        {
            valid_ = false;
            return;
        }
        controllers_.push_back(controller);
    }

    void UnregisterRGBController(RGBController* controller) override
    {
        const auto entry = std::find(controllers_.begin(), controllers_.end(), controller);
        if(entry == controllers_.end())
        {
            valid_ = false;
            return;
        }
        controllers_.erase(entry);
    }

    void RegisterDeviceListChangeCallback(DeviceListChangeCallback, void*) override {}
    void RegisterDetectionProgressCallback(DetectionProgressCallback, void*) override {}
    void RegisterDetectionStartCallback(
        DetectionStartCallback callback, void* context) override
    {
        start_ = callback;
        start_context_ = context;
    }
    void RegisterDetectionEndCallback(
        DetectionEndCallback callback, void* context) override
    {
        end_ = callback;
        end_context_ = context;
    }
    void RegisterI2CBusListChangeCallback(I2CBusListChangeCallback, void*) override {}

    void UnregisterDeviceListChangeCallback(DeviceListChangeCallback, void*) override {}
    void UnregisterDetectionProgressCallback(DetectionProgressCallback, void*) override {}
    void UnregisterDetectionStartCallback(
        DetectionStartCallback callback, void* context) override
    {
        if(callback != start_ || context != start_context_)
        {
            valid_ = false;
        }
        start_ = nullptr;
        start_context_ = nullptr;
    }
    void UnregisterDetectionEndCallback(
        DetectionEndCallback callback, void* context) override
    {
        if(callback != end_ || context != end_context_)
        {
            valid_ = false;
        }
        end_ = nullptr;
        end_context_ = nullptr;
    }
    void UnregisterI2CBusListChangeCallback(I2CBusListChangeCallback, void*) override {}

    std::vector<RGBController*>& GetRGBControllers() override { return controllers_; }
    unsigned int GetDetectionPercent() override { return 100; }
    filesystem::path GetConfigurationDirectory() override { return {}; }
    std::vector<NetworkClient*>& GetClients() override { return clients_; }
    NetworkServer* GetServer() override { return nullptr; }
    ProfileManager* GetProfileManager() override { return nullptr; }
    SettingsManager* GetSettingsManager() override { return nullptr; }
    void UpdateDeviceList() override {}
    void WaitForDeviceDetection() override { ++waits_; }

    [[nodiscard]] bool clean() const noexcept
    {
        return valid_ && controllers_.empty() && start_ == nullptr && end_ == nullptr
            && waits_ == 1;
    }

private:
    std::vector<i2c_smbus_interface*> busses_;
    std::vector<RGBController*> controllers_;
    std::vector<NetworkClient*> clients_;
    DetectionStartCallback start_ = nullptr;
    DetectionEndCallback end_ = nullptr;
    void* start_context_ = nullptr;
    void* end_context_ = nullptr;
    unsigned int waits_ = 0;
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
    const auto root = loader.metaData();
    const auto metadata = root.value("MetaData").toObject();
    if(metadata.value("Id").toString() != "org.hyperflux.next.openrgb"
       || metadata.value("Name").toString() != "HyperFlux Next"
       || metadata.value("OpenRGBPluginAPIVersion").toInt() != 4)
    {
        return failure(__LINE__, loader.errorString());
    }
    if(!loader.load())
    {
        return failure(__LINE__, loader.errorString());
    }
    auto* instance = loader.instance();
    auto* plugin = qobject_cast<OpenRGBPluginInterface*>(instance);
    if(plugin == nullptr || plugin->GetPluginAPIVersion() != 4)
    {
        return failure(__LINE__, loader.errorString());
    }
    const auto info = plugin->GetPluginInfo();
    if(info.Name != "HyperFlux Next" || info.Version != "0.0.0-dev.1"
       || info.Commit.empty() || !info.URL.empty()
       || info.Location != OPENRGB_PLUGIN_LOCATION_INFORMATION
       || info.Label != "HyperFlux Next")
    {
        return failure(__LINE__);
    }
    ResourceManager manager;
    plugin->Load(&manager);
    QCoreApplication::processEvents();
    QWidget* widget = plugin->GetWidget();
    if(widget == nullptr || widget->objectName() != "hyperfluxNextInformation")
    {
        return failure(__LINE__);
    }
    delete widget;
    plugin->Unload();
    QCoreApplication::processEvents();
    if(!manager.clean())
    {
        return failure(__LINE__);
    }
    if(!loader.unload())
    {
        return failure(__LINE__, loader.errorString());
    }
    return EXIT_SUCCESS;
}
