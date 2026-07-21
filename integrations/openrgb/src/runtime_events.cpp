// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/runtime_core.hpp>

#include "runtime_internal.hpp"

#include <algorithm>
#include <iterator>
#include <utility>

namespace hyperflux::openrgb
{

sdk::Result<void> RuntimeCore::refresh_controllers(
    RuntimeStep& output,
    bool cursor_gap)
{
    auto view = bridge_->integration_view();
    if(!view)
    {
        return sdk::Result<void>::failure(view.error());
    }
    auto projected = project_controllers(view.value());
    if(!projected)
    {
        return sdk::Result<void>::failure(projected.error());
    }
    auto changes = reconcile_controllers(controllers_, projected.value());
    controllers_ = std::move(projected).value();
    cursor_ = view.value().cursor;
    if(cursor_gap)
    {
        subscription_id_.reset();
        output.cursor_gap_recovered = true;
    }
    output.full_refresh = true;
    output.controller_changes.insert(
        output.controller_changes.end(),
        std::make_move_iterator(changes.begin()),
        std::make_move_iterator(changes.end()));
    invalidate_changed_sessions();
    return sdk::Result<void>::success();
}

sdk::Result<void> RuntimeCore::poll_events(RuntimeStep& output)
{
    bool requires_refresh = false;
    for(std::size_t batch_index = 0;
        batch_index < config_.max_event_batches_per_step;
        ++batch_index)
    {
        auto batch = bridge_->subscribe({
            subscription_id_,
            cursor_,
            EventBatchLimit::from(config_.event_batch_limit).value(),
        });
        if(!batch)
        {
            return sdk::Result<void>::failure(batch.error());
        }
        subscription_id_ = batch.value().subscription_id;
        cursor_ = batch.value().next_cursor;
        if(batch.value().cursor_gap)
        {
            return refresh_controllers(output, true);
        }
        requires_refresh = requires_refresh
            || std::any_of(
                batch.value().events.begin(),
                batch.value().events.end(),
                [](const v5::BridgeEvent& event) {
                    return runtime_detail::refresh_event(event.kind);
                });
        if(!batch.value().has_more)
        {
            break;
        }
    }
    return requires_refresh ? refresh_controllers(output, false)
                            : sdk::Result<void>::success();
}

} // namespace hyperflux::openrgb
