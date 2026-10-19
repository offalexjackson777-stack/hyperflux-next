// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/build_config.hpp>
#include <hyperflux/openrgb/plugin_runtime.hpp>

#include <cstdlib>
#include <iostream>
#include <regex>
#include <set>
#include <string>
#include <unistd.h>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-plugin-runtime-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

} // namespace

int main()
{
    using namespace hyperflux::openrgb;
    using namespace hyperflux::openrgb::native;

    const auto config = default_production_runtime_config();
    const auto second_config = default_production_runtime_config();
    const auto uid = static_cast<std::uint32_t>(::geteuid());
    const auto expected_socket = uid == 0
        ? std::string("/run/hyperflux-next/bridge.sock")
        : std::string("/run/user/") + std::to_string(uid)
            + "/hyperflux-next-bridge.sock";
    if(config.socket_path != expected_socket
       || (uid == 0 && config.expected_peer_user != "hyperflux-next")
       || (uid != 0 && config.expected_peer_uid != 0)
       || config.client_id == second_config.client_id
       || !std::regex_match(
           config.client_id,
           std::regex("openrgb-runtime-[0-9a-f]{64}"))
       || config.client_name.empty()
       || build_config::component_version != "2.2.0"
       || build_config::source_revision.empty())
    {
        return failure(__LINE__);
    }

    const std::set<std::string> required {
        "integration-view-projection",
        "event-subscriptions",
        "ownership-leases",
        "atomic-transactions",
        "profile-bound-transactions",
        "semantic-stable-lighting",
    };
    const std::set<std::string> actual_required(
        required_runtime_features().begin(), required_runtime_features().end());
    if(actual_required != required || optional_runtime_features().size() != 1
       || optional_runtime_features().front() != "structured-diagnostics")
    {
        return failure(__LINE__);
    }

    auto runtime = create_production_runtime();
    if(!runtime || runtime.value()->connection_epoch() != 0)
    {
        return failure(__LINE__);
    }

    auto invalid = config;
    invalid.socket_path = "relative.sock";
    if(create_production_runtime(invalid))
    {
        return failure(__LINE__);
    }
    invalid = config;
    invalid.timeout_ms = 0;
    if(create_production_runtime(invalid))
    {
        return failure(__LINE__);
    }
    invalid = config;
    invalid.expected_peer_uid.reset();
    invalid.expected_peer_user = "Invalid User";
    if(create_production_runtime(invalid))
    {
        return failure(__LINE__);
    }
    invalid = config;
    invalid.expected_peer_uid = 0;
    invalid.expected_peer_user = "hyperflux-next";
    if(create_production_runtime(invalid))
    {
        return failure(__LINE__);
    }
    invalid = config;
    invalid.client_id.clear();
    if(create_production_runtime(invalid))
    {
        return failure(__LINE__);
    }

    return EXIT_SUCCESS;
}
