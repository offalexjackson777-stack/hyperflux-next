// SPDX-License-Identifier: GPL-2.0-only

#include "qt_dispatcher.hpp"

#include <QMetaObject>
#include <QPointer>

#include <utility>

namespace hyperflux::openrgb::native
{

QtApplicationDispatcher::QtApplicationDispatcher(QObject* parent) noexcept
    : QObject(parent)
{
}

QtApplicationDispatcher::~QtApplicationDispatcher()
{
    stop();
}

bool QtApplicationDispatcher::post(std::function<void()> task) noexcept
{
    if(!accepting_.load(std::memory_order_acquire) || !task)
    {
        return false;
    }
    try
    {
        const QPointer<QtApplicationDispatcher> owner(this);
        return QMetaObject::invokeMethod(
            this,
            [owner, task = std::move(task)]() mutable
            {
                if(owner != nullptr
                   && owner->accepting_.load(std::memory_order_acquire))
                {
                    task();
                }
            },
            Qt::QueuedConnection);
    }
    catch(...)
    {
        return false;
    }
}

void QtApplicationDispatcher::stop() noexcept
{
    accepting_.store(false, std::memory_order_release);
}

} // namespace hyperflux::openrgb::native
