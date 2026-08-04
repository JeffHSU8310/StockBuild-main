#include "stockbuild/resampler.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>

namespace stockbuild {
namespace {

constexpr std::int64_t kNanosecondsPerSecond = 1'000'000'000;
constexpr std::int64_t kNanosecondsPerMinute = 60 * kNanosecondsPerSecond;
constexpr std::int64_t kNanosecondsPerDay = 86'400 * kNanosecondsPerSecond;
constexpr int kDaySessionOpenMinute = 8 * 60 + 45;
constexpr int kDaySessionCloseMinute = 13 * 60 + 45;

struct CivilDate final {
    int year;
    unsigned month;
    unsigned day;
};

std::int64_t floor_div(std::int64_t value, std::int64_t divisor) {
    const std::int64_t quotient = value / divisor;
    const std::int64_t remainder = value % divisor;
    return remainder < 0 ? quotient - 1 : quotient;
}

std::int64_t floor_mod(std::int64_t value, std::int64_t divisor) {
    const std::int64_t remainder = value % divisor;
    return remainder < 0 ? remainder + divisor : remainder;
}

std::int64_t days_from_civil(int year, unsigned month, unsigned day) noexcept {
    year -= month <= 2U;
    const int era = (year >= 0 ? year : year - 399) / 400;
    const unsigned year_of_era = static_cast<unsigned>(year - era * 400);
    const unsigned adjusted_month = month > 2U ? month - 3U : month + 9U;
    const unsigned day_of_year = (153U * adjusted_month + 2U) / 5U + day - 1U;
    const unsigned day_of_era =
        year_of_era * 365U + year_of_era / 4U - year_of_era / 100U + day_of_year;
    return static_cast<std::int64_t>(era) * 146097 +
           static_cast<std::int64_t>(day_of_era) - 719468;
}

CivilDate civil_from_days(std::int64_t days) noexcept {
    days += 719468;
    const std::int64_t era = (days >= 0 ? days : days - 146096) / 146097;
    const unsigned day_of_era = static_cast<unsigned>(days - era * 146097);
    const unsigned year_of_era =
        (day_of_era - day_of_era / 1460U + day_of_era / 36524U -
         day_of_era / 146096U) / 365U;
    int year = static_cast<int>(year_of_era) + static_cast<int>(era) * 400;
    const unsigned day_of_year =
        day_of_era - (365U * year_of_era + year_of_era / 4U - year_of_era / 100U);
    const unsigned month_part = (5U * day_of_year + 2U) / 153U;
    const unsigned day = day_of_year - (153U * month_part + 2U) / 5U + 1U;
    const unsigned month = month_part < 10U ? month_part + 3U : month_part - 9U;
    year += month <= 2U;
    return {year, month, day};
}

std::int64_t checked_local_ns(std::int64_t timestamp_ns, int offset_minutes) {
    if (offset_minutes < -24 * 60 || offset_minutes > 24 * 60) {
        throw std::invalid_argument("timezone_offset_minutes 必須介於 -1440 與 1440");
    }
    const std::int64_t offset_ns =
        static_cast<std::int64_t>(offset_minutes) * kNanosecondsPerMinute;
    if ((offset_ns > 0 && timestamp_ns > std::numeric_limits<std::int64_t>::max() - offset_ns) ||
        (offset_ns < 0 && timestamp_ns < std::numeric_limits<std::int64_t>::min() - offset_ns)) {
        throw std::invalid_argument("timestamp 加上市場時區後超出 int64 範圍");
    }
    return timestamp_ns + offset_ns;
}

std::int64_t utc_label(std::int64_t local_day, int offset_minutes) {
    return local_day * kNanosecondsPerDay -
           static_cast<std::int64_t>(offset_minutes) * kNanosecondsPerMinute;
}

enum class BucketMode { Fixed, Day, Week, Month, FutureDay };

std::int64_t bucket_label(std::int64_t timestamp_ns,
                          BucketMode mode,
                          std::int64_t interval_minutes,
                          int offset_minutes,
                          const std::string& session_basis,
                          bool& keep) {
    keep = true;
    const std::int64_t local_ns = checked_local_ns(timestamp_ns, offset_minutes);
    if (mode == BucketMode::Fixed) {
        if (interval_minutes <= 0 ||
            interval_minutes > std::numeric_limits<std::int64_t>::max() /
                                   kNanosecondsPerMinute) {
            throw std::invalid_argument("fixed resample 的 interval_minutes 必須是正整數");
        }
        const std::int64_t interval_ns = interval_minutes * kNanosecondsPerMinute;
        const std::int64_t local_bucket = floor_div(local_ns, interval_ns) * interval_ns;
        return local_bucket -
               static_cast<std::int64_t>(offset_minutes) * kNanosecondsPerMinute;
    }

    std::int64_t local_day = floor_div(local_ns, kNanosecondsPerDay);
    if (mode == BucketMode::FutureDay) {
        const std::int64_t within_day = floor_mod(local_ns, kNanosecondsPerDay);
        const int minute = static_cast<int>(within_day / kNanosecondsPerMinute);
        if (session_basis == "day") {
            keep = minute >= kDaySessionOpenMinute && minute <= kDaySessionCloseMinute;
        } else if (session_basis == "all") {
            if (minute > kDaySessionCloseMinute) {
                ++local_day;
            }
        } else {
            throw std::invalid_argument("session_basis 只接受 all 或 day");
        }
        return utc_label(local_day, offset_minutes);
    }
    if (mode == BucketMode::Week) {
        const std::int64_t monday_offset = floor_mod(local_day + 3, 7);
        local_day -= monday_offset;
    } else if (mode == BucketMode::Month) {
        const CivilDate date = civil_from_days(local_day);
        local_day = days_from_civil(date.year, date.month, 1U);
    }
    return utc_label(local_day, offset_minutes);
}

void validate_inputs(std::span<const std::int64_t> timestamps,
                     std::span<const double> open,
                     std::span<const double> high,
                     std::span<const double> low,
                     std::span<const double> close,
                     std::span<const double> volume,
                     std::span<const std::uint32_t> flags) {
    const std::size_t rows = timestamps.size();
    if (open.size() != rows || high.size() != rows || low.size() != rows ||
        close.size() != rows || volume.size() != rows || flags.size() != rows) {
        throw std::invalid_argument("KBar 欄位列數不一致");
    }
    for (std::size_t i = 0; i < rows; ++i) {
        if (i > 0 && timestamps[i] <= timestamps[i - 1]) {
            throw std::invalid_argument("KBar timestamp 必須嚴格遞增且不可重複");
        }
        if (!std::isfinite(open[i]) || !std::isfinite(high[i]) ||
            !std::isfinite(low[i]) || !std::isfinite(close[i]) ||
            !std::isfinite(volume[i])) {
            throw std::invalid_argument("KBar OHLCV 不可包含 NaN 或 Infinity");
        }
    }
}

KBarColumns aggregate(std::span<const std::int64_t> timestamps,
                      std::span<const double> open,
                      std::span<const double> high,
                      std::span<const double> low,
                      std::span<const double> close,
                      std::span<const double> volume,
                      std::span<const std::uint32_t> flags,
                      BucketMode mode,
                      std::int64_t interval_minutes,
                      int offset_minutes,
                      const std::string& session_basis) {
    KBarColumns result;
    const std::size_t reserve_hint = std::min<std::size_t>(timestamps.size(), 4096);
    result.timestamps.reserve(reserve_hint);
    result.open.reserve(reserve_hint);
    result.high.reserve(reserve_hint);
    result.low.reserve(reserve_hint);
    result.close.reserve(reserve_hint);
    result.volume.reserve(reserve_hint);
    result.flags.reserve(reserve_hint);

    bool has_bucket = false;
    std::int64_t current_label = 0;
    for (std::size_t i = 0; i < timestamps.size(); ++i) {
        bool keep = true;
        const std::int64_t label = bucket_label(
            timestamps[i], mode, interval_minutes, offset_minutes, session_basis, keep);
        if (!keep) {
            continue;
        }
        if (!has_bucket || label != current_label) {
            if (has_bucket && !std::isfinite(result.volume.back())) {
                throw std::overflow_error("resample Volume 加總超出浮點範圍");
            }
            has_bucket = true;
            current_label = label;
            result.timestamps.push_back(label);
            result.open.push_back(open[i]);
            result.high.push_back(high[i]);
            result.low.push_back(low[i]);
            result.close.push_back(close[i]);
            result.volume.push_back(volume[i]);
            result.flags.push_back(flags[i]);
        } else {
            result.high.back() = std::max(result.high.back(), high[i]);
            result.low.back() = std::min(result.low.back(), low[i]);
            result.close.back() = close[i];
            result.volume.back() += volume[i];
            result.flags.back() |= flags[i];
        }
    }
    if (has_bucket && !std::isfinite(result.volume.back())) {
        throw std::overflow_error("resample Volume 加總超出浮點範圍");
    }
    return result;
}

}  // namespace

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
                           const std::string& session_basis) {
    validate_inputs(timestamps, open, high, low, close, volume, flags);
    if ((mode == "future_day" || mode == "future_week" ||
         mode == "future_month") &&
        session_basis != "all" && session_basis != "day") {
        throw std::invalid_argument("session_basis must be all or day");
    }
    if (mode == "fixed") {
        return aggregate(timestamps, open, high, low, close, volume, flags,
                         BucketMode::Fixed, interval_minutes, timezone_offset_minutes,
                         session_basis);
    }
    if (mode == "day" || mode == "week" || mode == "month") {
        const BucketMode bucket = mode == "day" ? BucketMode::Day :
                                  mode == "week" ? BucketMode::Week : BucketMode::Month;
        return aggregate(timestamps, open, high, low, close, volume, flags,
                         bucket, 0, timezone_offset_minutes, session_basis);
    }
    if (mode == "future_day" || mode == "future_week" || mode == "future_month") {
        KBarColumns daily = aggregate(timestamps, open, high, low, close, volume, flags,
                                      BucketMode::FutureDay, 0, timezone_offset_minutes,
                                      session_basis);
        if (mode == "future_day" || daily.timestamps.empty()) {
            return daily;
        }
        const BucketMode bucket = mode == "future_week" ? BucketMode::Week : BucketMode::Month;
        return aggregate(daily.timestamps, daily.open, daily.high, daily.low, daily.close,
                         daily.volume, daily.flags, bucket, 0, timezone_offset_minutes,
                         session_basis);
    }
    throw std::invalid_argument(
        "resample mode 只接受 fixed/day/week/month/future_day/future_week/future_month");
}

}  // namespace stockbuild
