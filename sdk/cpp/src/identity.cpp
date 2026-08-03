// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/sdk/identity.hpp>

#include <cerrno>
#include <cstddef>
#include <iomanip>
#include <limits>
#include <sstream>
#include <string>
#include <sys/random.h>

namespace hyperflux::sdk
{
namespace
{

Result<std::array<std::uint8_t, 16>> process_entropy()
{
    std::array<std::uint8_t, 16> entropy {};
    std::size_t received = 0;
    while(received < entropy.size())
    {
        const auto result = ::getrandom(
            entropy.data() + received,
            entropy.size() - received,
            0);
        if(result < 0)
        {
            if(errno == EINTR)
            {
                continue;
            }
            return Result<std::array<std::uint8_t, 16>>::failure({
                ErrorCode::IdentityUnavailable,
                "operating-system entropy is unavailable",
                std::nullopt,
            });
        }
        if(result == 0)
        {
            return Result<std::array<std::uint8_t, 16>>::failure({
                ErrorCode::IdentityUnavailable,
                "operating-system entropy ended unexpectedly",
                std::nullopt,
            });
        }
        received += static_cast<std::size_t>(result);
    }
    return Result<std::array<std::uint8_t, 16>>::success(entropy);
}

std::string hex(const std::array<std::uint8_t, 16>& bytes)
{
    std::ostringstream stream;
    stream << std::hex << std::setfill('0');
    for(const auto byte : bytes)
    {
        stream << std::setw(2) << static_cast<unsigned int>(byte);
    }
    return stream.str();
}

} // namespace

ProcessIdentitySource::ProcessIdentitySource(std::array<std::uint8_t, 16> entropy)
    : entropy_hex_(hex(entropy))
{
}

Result<std::unique_ptr<ProcessIdentitySource>> ProcessIdentitySource::create()
{
    auto entropy = process_entropy();
    if(!entropy)
    {
        return Result<std::unique_ptr<ProcessIdentitySource>>::failure(entropy.error());
    }
    return Result<std::unique_ptr<ProcessIdentitySource>>::success(
        std::unique_ptr<ProcessIdentitySource>(
            new ProcessIdentitySource(std::move(entropy).value())));
}

Result<std::string> ProcessIdentitySource::next_value(std::string_view kind)
{
    if(sequence_ == std::numeric_limits<std::uint64_t>::max())
    {
        return Result<std::string>::failure({
            ErrorCode::IdentityExhausted,
            "process identity sequence is exhausted",
            std::nullopt,
        });
    }
    ++sequence_;
    return Result<std::string>::success(
        "hfx-" + std::string(kind) + "-" + entropy_hex_ + "-" + std::to_string(sequence_));
}

Result<RequestId> ProcessIdentitySource::next_request_id()
{
    auto value = next_value("request");
    if(!value)
    {
        return Result<RequestId>::failure(value.error());
    }
    auto request_id = RequestId::from(std::move(value).value());
    if(!request_id.has_value())
    {
        return Result<RequestId>::failure({
            ErrorCode::IdentityUnavailable,
            "generated request identity violates its domain",
            std::nullopt,
        });
    }
    return Result<RequestId>::success(*request_id);
}

Result<TransactionId> ProcessIdentitySource::next_transaction_id()
{
    auto value = next_value("transaction");
    if(!value)
    {
        return Result<TransactionId>::failure(value.error());
    }
    auto transaction_id = TransactionId::from(std::move(value).value());
    if(!transaction_id.has_value())
    {
        return Result<TransactionId>::failure({
            ErrorCode::IdentityUnavailable,
            "generated transaction identity violates its domain",
            std::nullopt,
        });
    }
    return Result<TransactionId>::success(*transaction_id);
}

} // namespace hyperflux::sdk
