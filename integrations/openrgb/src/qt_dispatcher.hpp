// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <hyperflux/openrgb/plugin_coordinator.hpp>

#include <QObject>

#include <deque>
#include <mutex>

class QSocketNotifier;

namespace hyperflux::openrgb::native
{

class QtApplicationDispatcher final : public QObject, public ApplicationDispatcher
{
public:
    explicit QtApplicationDispatcher(QObject* parent = nullptr);
    ~QtApplicationDispatcher() override;

    [[nodiscard]] bool post(std::function<void()> task) noexcept override;
    void stop() noexcept;

private:
    void drain() noexcept;

    std::mutex queue_mutex_;
    std::deque<std::function<void()>> tasks_;
    QSocketNotifier* notifier_ = nullptr;
    int event_fd_ = -1;
    bool accepting_ = true;
};

} // namespace hyperflux::openrgb::native
