// SPDX-License-Identifier: GPL-2.0-only

#include "qt_dispatcher.hpp"

#include <QSocketNotifier>
#include <QThread>

#include <cerrno>
#include <cstdint>
#include <system_error>
#include <utility>

#include <sys/eventfd.h>
#include <unistd.h>

namespace hyperflux::openrgb::native
{

QtApplicationDispatcher::QtApplicationDispatcher(QObject* parent)
    : QObject(parent)
{
    event_fd_ = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
    if(event_fd_ < 0)
    {
        throw std::system_error(errno, std::generic_category(), "create OpenRGB dispatcher");
    }
    try
    {
        notifier_ = new QSocketNotifier(event_fd_, QSocketNotifier::Read, this);
        connect(notifier_, &QSocketNotifier::activated, this,
            [this](QSocketDescriptor, QSocketNotifier::Type) { drain(); });
    }
    catch(...)
    {
        close(event_fd_);
        event_fd_ = -1;
        throw;
    }
}

QtApplicationDispatcher::~QtApplicationDispatcher()
{
    stop();
    delete notifier_;
    notifier_ = nullptr;
    if(event_fd_ >= 0)
    {
        close(event_fd_);
        event_fd_ = -1;
    }
}

bool QtApplicationDispatcher::post(std::function<void()> task) noexcept
{
    if(!task)
    {
        return false;
    }
    try
    {
        std::lock_guard lock(queue_mutex_);
        if(!accepting_ || event_fd_ < 0)
        {
            return false;
        }
        tasks_.push_back(std::move(task));
        const std::uint64_t signal = 1;
        for(;;)
        {
            const auto written = write(event_fd_, &signal, sizeof(signal));
            if(written == static_cast<ssize_t>(sizeof(signal)))
            {
                return true;
            }
            if(written < 0 && errno == EINTR)
            {
                continue;
            }
            if(written < 0 && errno == EAGAIN)
            {
                return true;
            }
            tasks_.pop_back();
            return false;
        }
    }
    catch(...)
    {
        return false;
    }
}

void QtApplicationDispatcher::stop() noexcept
{
    {
        std::lock_guard lock(queue_mutex_);
        accepting_ = false;
        tasks_.clear();
    }
    if(notifier_ != nullptr && QThread::currentThread() == thread())
    {
        notifier_->setEnabled(false);
    }
}

void QtApplicationDispatcher::drain() noexcept
{
    std::uint64_t pending_count = 0;
    for(;;)
    {
        const auto received = read(event_fd_, &pending_count, sizeof(pending_count));
        if(received == static_cast<ssize_t>(sizeof(pending_count)))
        {
            break;
        }
        if(received < 0 && errno == EINTR)
        {
            continue;
        }
        break;
    }

    std::deque<std::function<void()>> ready;
    {
        std::lock_guard lock(queue_mutex_);
        if(!accepting_)
        {
            tasks_.clear();
            return;
        }
        ready.swap(tasks_);
    }
    for(auto& task : ready)
    {
        try
        {
            task();
        }
        catch(...)
        {
            // Application callbacks are isolated from Qt's event loop.
        }
    }
}

} // namespace hyperflux::openrgb::native
