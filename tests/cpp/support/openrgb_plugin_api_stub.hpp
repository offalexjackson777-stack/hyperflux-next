// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "OpenRGBPluginInterface.h"

#include <string>
#include <vector>

namespace hyperflux::test
{

/// Neutral OpenRGB plugin API 5 host. Contracts override the calls they
/// observe; every other call is inert.
class OpenRgbPluginApiStub : public OpenRGBPluginAPIInterface
{
public:
    void LogEntry(const char*, int, unsigned int, const char*, ...) override {}

    RGBControllerInterface* CreateVirtualRGBController(RGBController_Setup*) override
    {
        return nullptr;
    }
    void DeleteVirtualRGBController(RGBControllerInterface*) override {}
    void RegisterVirtualRGBController(RGBControllerInterface*) override {}
    void RegisterVirtualRGBControllerInThread(RGBControllerInterface*) override {}
    void UnregisterVirtualRGBController(RGBControllerInterface*) override {}
    void UpdateVirtualRGBController(RGBControllerInterface*, RGBController_Setup*) override {}
    void UnregisterVirtualRGBControllerInThread(RGBControllerInterface*) override {}

    void ClearActiveProfile() override {}
    std::vector<std::string> GetProfileList() override { return {}; }
    bool LoadProfile(std::string) override { return false; }
    bool SaveProfileFromPlugin(std::string, std::string, nlohmann::json) override
    {
        return false;
    }

    filesystem::path GetConfigurationDirectory() override { return {}; }
    bool GetDetectionEnabled() override { return true; }
    unsigned int GetDetectionPercent() override { return 100; }
    std::string GetDetectionString() override { return {}; }
    void RescanDevices() override {}
    void WaitForDetection() override {}
    std::vector<RGBControllerInterface*> GetRGBControllers() override { return {}; }

    nlohmann::json GetDeviceDescriptionJSON(RGBControllerInterface*) override { return {}; }
    nlohmann::json GetLEDDescriptionJSON(led) override { return {}; }
    nlohmann::json GetMatrixMapDescriptionJSON(matrix_map_type) override { return {}; }
    nlohmann::json GetModeDescriptionJSON(mode) override { return {}; }
    nlohmann::json GetSegmentDescriptionJSON(segment) override { return {}; }
    nlohmann::json GetZoneDescriptionJSON(zone) override { return {}; }

    RGBControllerInterface* SetDeviceDescriptionJSON(nlohmann::json) override
    {
        return nullptr;
    }
    led SetLEDDescriptionJSON(nlohmann::json) override { return {}; }
    matrix_map_type SetMatrixMapDescriptionJSON(nlohmann::json) override { return {}; }
    mode SetModeDescriptionJSON(nlohmann::json) override { return {}; }
    segment SetSegmentDescriptionJSON(nlohmann::json) override { return {}; }
    zone SetZoneDescriptionJSON(nlohmann::json) override { return {}; }

    bool CompareControllers(RGBControllerInterface*, RGBControllerInterface*) override
    {
        return false;
    }
    std::string DeviceTypeToString(device_type) override { return {}; }
    bool SetModeValuesFromMode(mode&, mode&) override { return false; }

    nlohmann::json GetSettings(std::string) override { return {}; }
    void SaveSettings() override {}
    void SetSettings(std::string, nlohmann::json) override {}
};

} // namespace hyperflux::test
