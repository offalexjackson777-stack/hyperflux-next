// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/client_identity.hpp>

#include <algorithm>
#include <array>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <regex>
#include <set>
#include <string>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace {

std::mutex entropy_mutex;
std::condition_variable entropy_condition;
bool entropy_block_entered = false;
bool entropy_block_released = false;
thread_local bool block_entropy = false;

int failure(int line) {
  std::cerr << "openrgb-client-identity-contract failed at line " << line
            << '\n';
  return EXIT_FAILURE;
}

} // namespace

extern "C" ssize_t __real_getrandom(void *buffer, std::size_t length,
                                    unsigned int flags);

extern "C" ssize_t __wrap_getrandom(void *buffer, std::size_t length,
                                    unsigned int flags) {
  if (block_entropy) {
    std::unique_lock lock(entropy_mutex);
    entropy_block_entered = true;
    entropy_condition.notify_all();
    entropy_condition.wait(lock, [] { return entropy_block_released; });
  }
  return __real_getrandom(buffer, length, flags);
}

int main() {
  using namespace hyperflux::openrgb::native;

  constexpr std::size_t thread_count = 12;
  constexpr std::size_t identities_per_thread = 64;
  std::mutex identities_mutex;
  std::vector<std::string> identities;
  identities.reserve(thread_count * identities_per_thread);
  std::vector<std::thread> threads;
  for (std::size_t thread_index = 0; thread_index < thread_count;
       ++thread_index) {
    threads.emplace_back([&, thread_index] {
      const auto role = static_cast<OpenRgbClientRole>(thread_index % 3);
      auto factory = process_openrgb_client_id_factory(role);
      for (std::size_t identity_index = 0;
           identity_index < identities_per_thread; ++identity_index) {
        auto identity = factory();
        if (!identity) {
          std::abort();
        }
        std::lock_guard lock(identities_mutex);
        identities.emplace_back(identity.value().value());
      }
    });
  }
  for (auto &thread : threads) {
    thread.join();
  }

  const std::set<std::string, std::less<>> unique(identities.begin(),
                                                  identities.end());
  const std::regex format("openrgb-(runtime|discovery|feature)-[0-9a-f]{64}");
  if (unique.size() != identities.size() ||
      !std::all_of(identities.begin(), identities.end(),
                   [&](const auto &identity) {
                     return std::regex_match(identity, format);
                   })) {
    return failure(__LINE__);
  }

  auto before_fork = next_process_openrgb_client_id(OpenRgbClientRole::Feature);
  if (!before_fork) {
    return failure(__LINE__);
  }
  std::array<int, 2> descriptors{};
  if (::pipe(descriptors.data()) != 0) {
    return failure(__LINE__);
  }
  bool blocked_worker_succeeded = false;
  std::thread blocked_worker([&] {
    block_entropy = true;
    blocked_worker_succeeded = static_cast<bool>(
        next_process_openrgb_client_id(OpenRgbClientRole::Feature));
  });
  {
    std::unique_lock lock(entropy_mutex);
    if (!entropy_condition.wait_for(lock, std::chrono::seconds(2),
                                    [] { return entropy_block_entered; })) {
      entropy_block_released = true;
      lock.unlock();
      entropy_condition.notify_all();
      blocked_worker.join();
      static_cast<void>(::close(descriptors[0]));
      static_cast<void>(::close(descriptors[1]));
      return failure(__LINE__);
    }
  }
  const auto child = ::fork();
  if (child < 0) {
    {
      std::lock_guard lock(entropy_mutex);
      entropy_block_released = true;
    }
    entropy_condition.notify_all();
    blocked_worker.join();
    static_cast<void>(::close(descriptors[0]));
    static_cast<void>(::close(descriptors[1]));
    return failure(__LINE__);
  }
  if (child == 0) {
    ::alarm(2);
    static_cast<void>(::close(descriptors[0]));
    auto identity = next_process_openrgb_client_id(OpenRgbClientRole::Feature);
    if (!identity) {
      _exit(2);
    }
    const std::string value(identity.value().value());
    const auto written = ::write(descriptors[1], value.data(), value.size());
    _exit(written == static_cast<ssize_t>(value.size()) ? 0 : 3);
  }
  {
    std::lock_guard lock(entropy_mutex);
    entropy_block_released = true;
  }
  entropy_condition.notify_all();
  blocked_worker.join();
  static_cast<void>(::close(descriptors[1]));
  std::array<char, 256> child_buffer{};
  const auto child_bytes =
      ::read(descriptors[0], child_buffer.data(), child_buffer.size());
  static_cast<void>(::close(descriptors[0]));
  int status = 0;
  if (::waitpid(child, &status, 0) != child || !WIFEXITED(status) ||
      WEXITSTATUS(status) != 0 || child_bytes <= 0) {
    return failure(__LINE__);
  }
  const std::string child_identity(child_buffer.data(),
                                   static_cast<std::size_t>(child_bytes));
  auto parent_after_fork =
      next_process_openrgb_client_id(OpenRgbClientRole::Feature);
  if (!blocked_worker_succeeded || !parent_after_fork ||
      child_identity == before_fork.value().value() ||
      child_identity == parent_after_fork.value().value() ||
      !std::regex_match(child_identity, format)) {
    return failure(__LINE__);
  }

  return EXIT_SUCCESS;
}
