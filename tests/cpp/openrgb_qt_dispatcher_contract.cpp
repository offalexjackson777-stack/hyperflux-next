// SPDX-License-Identifier: GPL-2.0-only

#include "qt_dispatcher.hpp"

#include <QCoreApplication>
#include <QEventLoop>

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <thread>
#include <vector>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-qt-dispatcher-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

template<typename Predicate> bool wait_until(Predicate predicate)
{
    for(std::size_t attempt = 0; attempt < 2'000; ++attempt)
    {
        QCoreApplication::processEvents(QEventLoop::AllEvents, 5);
        if(predicate())
        {
            return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    return predicate();
}

} // namespace

int main(int argc, char** argv)
{
    QCoreApplication application(argc, argv);
    hyperflux::openrgb::native::QtApplicationDispatcher dispatcher;
    const auto application_thread = std::this_thread::get_id();
    constexpr std::size_t producer_count = 4;
    constexpr std::size_t tasks_per_producer = 250;
    std::atomic_size_t executed {0};
    std::atomic_bool accepted {true};
    std::atomic_bool wrong_thread {false};
    std::vector<std::thread> producers;
    producers.reserve(producer_count);

    for(std::size_t producer = 0; producer < producer_count; ++producer)
    {
        producers.emplace_back([&]
        {
            for(std::size_t task_index = 0; task_index < tasks_per_producer; ++task_index)
            {
                if(!dispatcher.post([&]
                   {
                       if(std::this_thread::get_id() != application_thread)
                       {
                           wrong_thread.store(true, std::memory_order_release);
                       }
                       executed.fetch_add(1, std::memory_order_release);
                   }))
                {
                    accepted.store(false, std::memory_order_release);
                }
            }
        });
    }
    for(auto& producer : producers)
    {
        producer.join();
    }

    if(!accepted.load(std::memory_order_acquire)
       || executed.load(std::memory_order_acquire) != 0
       || !wait_until([&]
          {
              return executed.load(std::memory_order_acquire)
                  == producer_count * tasks_per_producer;
          })
       || wrong_thread.load(std::memory_order_acquire))
    {
        return failure(__LINE__);
    }

    std::atomic_bool cancelled_task_ran {false};
    if(!dispatcher.post([&]
       {
           cancelled_task_ran.store(true, std::memory_order_release);
       }))
    {
        return failure(__LINE__);
    }
    dispatcher.stop();
    QCoreApplication::processEvents(QEventLoop::AllEvents, 10);
    if(cancelled_task_ran.load(std::memory_order_acquire)
       || dispatcher.post([] {}))
    {
        return failure(__LINE__);
    }
    return EXIT_SUCCESS;
}
