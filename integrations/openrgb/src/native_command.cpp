// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/native_command.hpp>

#include <utility>

namespace hyperflux::openrgb::native
{

WorkerLightingCommandSink::WorkerLightingCommandSink(RuntimeWorker& worker) noexcept
    : worker_(&worker)
{
}

sdk::Result<EnqueueDisposition> WorkerLightingCommandSink::enqueue_effect(QueuedLightingFrame frame)
{
    return worker_->enqueue_effect(std::move(frame));
}

sdk::Result<EnqueueDisposition> WorkerLightingCommandSink::enqueue_stable(
    sdk::LightingIntent intent, std::vector<QueuedLightingFrame> frames)
{
    return sdk::Result<EnqueueDisposition>::success(
        worker_->enqueue_stable(intent, std::move(frames)));
}

} // namespace hyperflux::openrgb::native
