// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/generated/integration_catalog.hpp>
#include <hyperflux/generated/profile_catalog.hpp>

int main()
{
    const auto* openrgb = hyperflux::integrations::upstream_by_id("openrgb");
    const auto* mouse = hyperflux::profiles::profile_by_id(
        "child.razer.basilisk-v3-pro-35k.00cd");
    if(openrgb == nullptr || mouse == nullptr || mouse->presentation == nullptr)
    {
        return 1;
    }
    if(mouse->presentation->upstream_id != openrgb->id
       || mouse->presentation->source_commit != openrgb->commit)
    {
        return 2;
    }
    for(const auto& adapter : hyperflux::integrations::adapters)
    {
        if(adapter.sdk_protocol_versions.empty())
        {
            return 3;
        }
    }
    return 0;
}
