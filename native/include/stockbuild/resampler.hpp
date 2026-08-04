#pragma once

#include <cstdint>
#include <span>
#include <string>

#include "stockbuild/kbar_schema.hpp"

namespace stockbuild {

KBarColumns resample_kbars(std::span<const std::int64_t> timestamps,
                           std::span<const double> open,
                           std::span<const double> high,
                           std::span<const double> low,
                           std::span<const double> close,
                           std::span<const double> volume,
                           std::span<const std::uint32_t> flags,
                           const std::string& mode,
                           std::int64_t interval_minutes,
                           int timezone_offset_minutes,
                           const std::string& session_basis);

}  // namespace stockbuild
