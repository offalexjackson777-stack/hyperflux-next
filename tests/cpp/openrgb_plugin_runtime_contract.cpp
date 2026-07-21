// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/build_config.hpp>
#include <hyperflux/openrgb/plugin_runtime.hpp>

#include <cstdlib>
#include <iostream>
#include <set>
#include <string>

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
    if(config.socket_path != "/run/hyperflux-next/bridge.sock"
       || config.expected_peer_user != "hyperflux-next"
       || config.client_id != "openrgb"
       || config.client_name.empty()
       || build_config::component_version != "0.0.0-dev.1"
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
    invalid.expected_peer_user = "Invalid User";
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
