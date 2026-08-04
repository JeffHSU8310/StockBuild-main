#pragma once

#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <vector>

namespace stockbuild {

struct ConditionRequest final {
    std::string type;
    std::vector<double> params;
    std::string ma_kind;
};

struct ConditionColumns final {
    std::vector<std::vector<std::uint8_t>> values;
    std::size_t rows{};
};

ConditionColumns calculate_conditions(
    std::span<const double> open,
    std::span<const double> high,
    std::span<const double> low,
    std::span<const double> close,
    std::span<const double> volume,
    std::span<const ConditionRequest> requests);

}  // namespace stockbuild
