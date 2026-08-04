#pragma once

#include <cstddef>
#include <cstdint>
#include <type_traits>
#include <vector>

#include "stockbuild/version.hpp"

namespace stockbuild {

struct KBarRecord final {
    std::int64_t timestamp_ns;
    double open;
    double high;
    double low;
    double close;
    double volume;
    std::uint32_t flags;
    std::uint32_t reserved;
};

struct KBarColumns final {
    bool readonly{false};
    bool query_only{false};
    std::int64_t schema_version{0};
    std::int64_t data_version{0};
    std::vector<std::int64_t> timestamps;
    std::vector<double> open;
    std::vector<double> high;
    std::vector<double> low;
    std::vector<double> close;
    std::vector<double> volume;
    std::vector<std::uint32_t> flags;
};

static_assert(std::is_standard_layout_v<KBarRecord>);
static_assert(std::is_trivially_copyable_v<KBarRecord>);
static_assert(sizeof(KBarRecord) == 56);
static_assert(alignof(KBarRecord) == 8);
static_assert(offsetof(KBarRecord, timestamp_ns) == 0);
static_assert(offsetof(KBarRecord, open) == 8);
static_assert(offsetof(KBarRecord, high) == 16);
static_assert(offsetof(KBarRecord, low) == 24);
static_assert(offsetof(KBarRecord, close) == 32);
static_assert(offsetof(KBarRecord, volume) == 40);
static_assert(offsetof(KBarRecord, flags) == 48);
static_assert(offsetof(KBarRecord, reserved) == 52);

}  // namespace stockbuild
