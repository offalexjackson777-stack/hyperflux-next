// SPDX-License-Identifier: GPL-2.0-only

#include "NetworkServer.h"
#include "NetworkClient.h"
#include "PluginManager.h"
#include "RGBController.h"
#include "ResourceManager.h"

#include <QApplication>
#include <QSocketNotifier>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <unistd.h>
#include <vector>

namespace
{

#ifdef OPENRGB_SYSTEM_PLUGIN_DIRECTORY
constexpr std::string_view compiled_system_plugin_directory =
    OPENRGB_SYSTEM_PLUGIN_DIRECTORY;
#else
constexpr std::string_view compiled_system_plugin_directory;
#endif

std::string escape_json(const std::string& value)
{
    std::string escaped;
    escaped.reserve(value.size());
    for(const unsigned char character : value)
    {
        switch(character)
        {
            case '\"': escaped += "\\\""; break;
            case '\\': escaped += "\\\\"; break;
            case '\b': escaped += "\\b"; break;
            case '\f': escaped += "\\f"; break;
            case '\n': escaped += "\\n"; break;
            case '\r': escaped += "\\r"; break;
            case '\t': escaped += "\\t"; break;
            default:
                if(character < 0x20)
                {
                    constexpr char hex[] = "0123456789abcdef";
                    escaped += "\\u00";
                    escaped += hex[(character >> 4) & 0x0f];
                    escaped += hex[character & 0x0f];
                }
                else
                {
                    escaped += static_cast<char>(character);
                }
        }
    }
    return escaped;
}

void write_snapshot(
    const char* event,
    const std::vector<RGBController*>& controllers)
{
    std::cout << "{\"event\":\"" << event << "\",\"controllers\":[";
    for(std::size_t index = 0; index < controllers.size(); ++index)
    {
        auto* controller = controllers[index];
        if(index != 0)
        {
            std::cout << ',';
        }
        std::cout << "{\"name\":\"" << escape_json(controller->GetName())
                  << "\",\"vendor\":\"" << escape_json(controller->GetVendor())
                  << "\",\"description\":\""
                  << escape_json(controller->GetDescription())
                  << "\",\"serial\":\"" << escape_json(controller->GetSerial())
                  << "\",\"location\":\"" << escape_json(controller->GetLocation())
                  << "\",\"type\":" << static_cast<unsigned int>(controller->type)
                  << '}';
    }
    std::cout << "]}" << std::endl;
}

void snapshot(ResourceManager& resources)
{
    write_snapshot("snapshot", resources.GetRGBControllers());
}

void sdk_snapshot(unsigned short port)
{
    std::vector<RGBController*> remote_controllers;
    NetworkClient client(remote_controllers);
    client.SetIP("127.0.0.1");
    client.SetPort(port);
    client.SetName("HyperFlux isolated SDK qualifier");
    client.StartClient();

    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(8);
    while(!client.GetOnline() && std::chrono::steady_clock::now() < deadline)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    if(!client.GetOnline())
    {
        std::cout << "{\"event\":\"sdk-error\",\"reason\":\"timeout\"}"
                  << std::endl;
        client.StopClient();
        return;
    }

    {
        std::lock_guard lock(client.ControllerListMutex);
        write_snapshot("sdk-snapshot", remote_controllers);
    }
    client.StopClient();
}

class RegistryTrace
{
public:
    explicit RegistryTrace(ResourceManager& resources)
        : resources_(&resources)
    {
        record();
        resources_->RegisterDeviceListChangeCallback(&RegistryTrace::changed, this);
    }

    ~RegistryTrace()
    {
        resources_->UnregisterDeviceListChangeCallback(&RegistryTrace::changed, this);
    }

    void write() const
    {
        std::lock_guard lock(mutex_);
        std::cout << "{\"event\":\"registry-trace\",\"counts\":[";
        for(std::size_t index = 0; index < records_.size(); ++index)
        {
            if(index != 0)
            {
                std::cout << ',';
            }
            std::cout << records_[index].count;
        }
        std::cout << "],\"times_ms\":[";
        for(std::size_t index = 0; index < records_.size(); ++index)
        {
            if(index != 0)
            {
                std::cout << ',';
            }
            std::cout << records_[index].elapsed_ms;
        }
        std::cout << "],\"overflow\":" << (overflow_ ? "true" : "false")
                  << '}' << std::endl;
    }

private:
    // Keep the complete JSON trace below the qualifier's 4096-byte line bound
    // even when elapsed millisecond values approach uint64_t width.
    static constexpr std::size_t maximum_records = 128;

    struct Record
    {
        std::size_t count;
        std::uint64_t elapsed_ms;
    };

    static void changed(void* context)
    {
        static_cast<RegistryTrace*>(context)->record();
    }

    void record()
    {
        std::lock_guard lock(mutex_);
        if(records_.size() == maximum_records)
        {
            overflow_ = true;
            return;
        }
        const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - started_);
        records_.push_back(
            {resources_->GetRGBControllers().size(),
             static_cast<std::uint64_t>(elapsed.count())});
    }

    ResourceManager* resources_;
    mutable std::mutex mutex_;
    const std::chrono::steady_clock::time_point started_ =
        std::chrono::steady_clock::now();
    std::vector<Record> records_;
    bool overflow_ = false;
};

unsigned short parse_port(const char* value)
{
    if(value == nullptr || *value == '\0')
    {
        return 0;
    }
    char* end = nullptr;
    const auto parsed = std::strtoul(value, &end, 10);
    if(end == value || *end != '\0' || parsed < 1024 || parsed > 65535)
    {
        return 0;
    }
    return static_cast<unsigned short>(parsed);
}

} // namespace

int main(int argc, char** argv)
{
    if(argc != 3)
    {
        std::cerr << "usage: openrgb-plugin-manager-sdk-harness PORT SYSTEM_PLUGIN_DIR\n";
        return EXIT_FAILURE;
    }
    const auto port = parse_port(argv[1]);
    if(port == 0)
    {
        std::cerr << "invalid SDK port\n";
        return EXIT_FAILURE;
    }
    if(compiled_system_plugin_directory.empty())
    {
        std::cerr << "OpenRGB system plugin directory is not compiled in\n";
        return EXIT_FAILURE;
    }
    if(std::string_view(argv[2]) != compiled_system_plugin_directory)
    {
        std::cerr << "OpenRGB system plugin directory differs from the isolation contract\n";
        return EXIT_FAILURE;
    }

    QApplication application(argc, argv);
    auto* resources = ResourceManager::get();
    resources->GetServer()->SetHost("127.0.0.1");
    resources->GetServer()->SetPort(port);
    resources->Initialize(false, false, true, false);
    resources->WaitForInitialization();
    if(!resources->GetServer()->GetOnline())
    {
        std::cerr << "OpenRGB SDK server failed to start\n";
        return EXIT_FAILURE;
    }

    RegistryTrace registry_trace(*resources);
    PluginManager plugins;
    plugins.ScanAndLoadPlugins();
    std::cout << "{\"event\":\"ready\",\"port\":" << port
              << ",\"system_plugin_directory\":\""
              << escape_json(std::string(compiled_system_plugin_directory)) << "\"}"
              << std::endl;

    QSocketNotifier input(STDIN_FILENO, QSocketNotifier::Read, &application);
    QObject::connect(
        &input,
        &QSocketNotifier::activated,
        &application,
        [&](int)
        {
            std::string command;
            if(!std::getline(std::cin, command))
            {
                input.setEnabled(false);
                return;
            }
            if(command == "snapshot")
            {
                snapshot(*resources);
            }
            else if(command == "sdk-snapshot")
            {
                sdk_snapshot(port);
            }
            else if(command == "registry-trace")
            {
                registry_trace.write();
            }
            else if(command == "rescan")
            {
                resources->RescanDevices();
                resources->WaitForDeviceDetection();
                std::cout << "{\"event\":\"rescan-complete\"}" << std::endl;
            }
            else if(command == "unload")
            {
                plugins.UnloadPlugins();
                std::cout << "{\"event\":\"unloaded\"}" << std::endl;
            }
            else if(command == "load")
            {
                plugins.LoadPlugins();
                std::cout << "{\"event\":\"loaded\"}" << std::endl;
            }
            else if(command == "quit")
            {
                plugins.UnloadPlugins();
                resources->GetServer()->StopServer();
                std::cout << "{\"event\":\"stopped\"}" << std::endl;
                application.quit();
            }
            else
            {
                std::cout << "{\"event\":\"invalid-command\",\"command\":\""
                          << escape_json(command) << "\"}" << std::endl;
            }
        });

    const auto result = application.exec();
    plugins.UnloadPlugins();
    resources->GetServer()->StopServer();
    return result;
}
