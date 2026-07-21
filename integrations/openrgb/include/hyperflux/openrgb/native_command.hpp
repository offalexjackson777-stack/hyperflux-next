// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "runtime_worker.hpp"

namespace hyperflux::openrgb::native
{

/// Narrow command boundary used by OpenRGB controller callbacks.
class LightingCommandSink
{
public:
    virtual ~LightingCommandSink() = default;

    [[nodiscard]] virtual sdk::Result<EnqueueDisposition> enqueue_effect(
        QueuedLightingFrame frame) = 0;
    [[nodiscard]] virtual sdk::Result<EnqueueDisposition> enqueue_stable(
        sdk::LightingIntent intent, std::vector<QueuedLightingFrame> frames) = 0;
};

class WorkerLightingCommandSink final : public LightingCommandSink
{
public:
    explicit WorkerLightingCommandSink(RuntimeWorker& worker) noexcept;

    [[nodiscard]] sdk::Result<EnqueueDisposition> enqueue_effect(
        QueuedLightingFrame frame) override;
    [[nodiscard]] sdk::Result<EnqueueDisposition> enqueue_stable(
        sdk::LightingIntent intent, std::vector<QueuedLightingFrame> frames) override;

private:
    RuntimeWorker* worker_;
};

} // namespace hyperflux::openrgb::native
