// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "result.hpp"

#include <hyperflux/generated/domain_types.hpp>

#include <array>
#include <cstdint>
#include <memory>
#include <string>

namespace hyperflux::sdk
{

class IdentitySource
{
public:
    virtual ~IdentitySource() = default;

    [[nodiscard]] virtual Result<RequestId> next_request_id() = 0;
    [[nodiscard]] virtual Result<TransactionId> next_transaction_id() = 0;
};

class ProcessIdentitySource final : public IdentitySource
{
public:
    [[nodiscard]] static Result<std::unique_ptr<ProcessIdentitySource>> create();

    [[nodiscard]] Result<RequestId> next_request_id() override;
    [[nodiscard]] Result<TransactionId> next_transaction_id() override;

private:
    explicit ProcessIdentitySource(std::array<std::uint8_t, 16> entropy);

    [[nodiscard]] Result<std::string> next_value(std::string_view kind);

    std::string entropy_hex_;
    std::uint64_t sequence_ = 0;
};

} // namespace hyperflux::sdk
