// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "error.hpp"

#include <optional>
#include <stdexcept>
#include <utility>
#include <variant>

namespace hyperflux::sdk
{

template<typename T>
class Result
{
public:
    [[nodiscard]] static Result success(T value)
    {
        return Result(std::move(value));
    }

    [[nodiscard]] static Result failure(Error error)
    {
        return Result(std::move(error));
    }

    [[nodiscard]] bool has_value() const noexcept
    {
        return std::holds_alternative<T>(storage_);
    }

    explicit operator bool() const noexcept
    {
        return has_value();
    }

    [[nodiscard]] T& value() &
    {
        if(!has_value())
        {
            throw std::logic_error("HyperFlux result does not contain a value");
        }
        return std::get<T>(storage_);
    }

    [[nodiscard]] const T& value() const&
    {
        if(!has_value())
        {
            throw std::logic_error("HyperFlux result does not contain a value");
        }
        return std::get<T>(storage_);
    }

    [[nodiscard]] T&& value() &&
    {
        if(!has_value())
        {
            throw std::logic_error("HyperFlux result does not contain a value");
        }
        return std::get<T>(std::move(storage_));
    }

    [[nodiscard]] Error& error() &
    {
        if(has_value())
        {
            throw std::logic_error("HyperFlux result does not contain an error");
        }
        return std::get<Error>(storage_);
    }

    [[nodiscard]] const Error& error() const&
    {
        if(has_value())
        {
            throw std::logic_error("HyperFlux result does not contain an error");
        }
        return std::get<Error>(storage_);
    }

private:
    explicit Result(T value) : storage_(std::in_place_type<T>, std::move(value)) {}
    explicit Result(Error error) : storage_(std::in_place_type<Error>, std::move(error)) {}

    std::variant<T, Error> storage_;
};

template<>
class Result<void>
{
public:
    [[nodiscard]] static Result success()
    {
        return Result(std::nullopt);
    }

    [[nodiscard]] static Result failure(Error error)
    {
        return Result(std::move(error));
    }

    [[nodiscard]] bool has_value() const noexcept
    {
        return !error_.has_value();
    }

    explicit operator bool() const noexcept
    {
        return has_value();
    }

    [[nodiscard]] Error& error() &
    {
        if(!error_.has_value())
        {
            throw std::logic_error("HyperFlux result does not contain an error");
        }
        return *error_;
    }

    [[nodiscard]] const Error& error() const&
    {
        if(!error_.has_value())
        {
            throw std::logic_error("HyperFlux result does not contain an error");
        }
        return *error_;
    }

private:
    explicit Result(std::optional<Error> error) : error_(std::move(error)) {}
    explicit Result(Error error) : error_(std::move(error)) {}

    std::optional<Error> error_;
};

} // namespace hyperflux::sdk
