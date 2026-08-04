#include "stockbuild/conditions.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace stockbuild {
namespace {

double nan() noexcept { return std::numeric_limits<double>::quiet_NaN(); }

void require_finite(std::span<const double> values, const char* name) {
    for (std::size_t i = 0; i < values.size(); ++i) {
        if (!std::isfinite(values[i])) {
            throw std::invalid_argument(
                std::string(name) + " 包含非有限值，index=" + std::to_string(i));
        }
    }
}

int positive_int(const ConditionRequest& request, std::size_t index, int fallback) {
    const double raw = index < request.params.size() ? request.params[index] : fallback;
    if (!std::isfinite(raw) || raw < 1.0 || std::floor(raw) != raw) {
        throw std::invalid_argument(request.type + " 期間參數必須是正整數");
    }
    return static_cast<int>(raw);
}

double number(const ConditionRequest& request, std::size_t index, double fallback) {
    const double value = index < request.params.size() ? request.params[index] : fallback;
    if (!std::isfinite(value)) {
        throw std::invalid_argument(request.type + " 數值參數必須有限");
    }
    return value;
}

std::vector<double> sma(std::span<const double> values, int period) {
    std::vector<double> result(values.size(), nan());
    double sum = 0.0;
    for (std::size_t i = 0; i < values.size(); ++i) {
        sum += values[i];
        if (i >= static_cast<std::size_t>(period)) {
            sum -= values[i - static_cast<std::size_t>(period)];
        }
        if (i + 1 >= static_cast<std::size_t>(period)) {
            result[i] = sum / period;
        }
    }
    return result;
}

std::vector<double> ema(std::span<const double> values, int period) {
    std::vector<double> result(values.size(), nan());
    if (values.empty()) {
        return result;
    }
    const double alpha = 2.0 / (period + 1.0);
    result[0] = values[0];
    for (std::size_t i = 1; i < values.size(); ++i) {
        result[i] = alpha * values[i] + (1.0 - alpha) * result[i - 1];
    }
    return result;
}

std::vector<double> moving_average(std::span<const double> values, int period,
                                   const std::string& kind) {
    if (kind == "EMA") {
        return ema(values, period);
    }
    if (kind == "SMA" || kind.empty()) {
        return sma(values, period);
    }
    throw std::invalid_argument("策略均線型態只接受 SMA 或 EMA");
}

bool cross_up(double previous_a, double previous_b, double current_a, double current_b) {
    return std::isfinite(previous_a) && std::isfinite(previous_b) &&
           std::isfinite(current_a) && std::isfinite(current_b) &&
           previous_a <= previous_b && current_a > current_b;
}

bool cross_down(double previous_a, double previous_b, double current_a, double current_b) {
    return std::isfinite(previous_a) && std::isfinite(previous_b) &&
           std::isfinite(current_a) && std::isfinite(current_b) &&
           previous_a >= previous_b && current_a < current_b;
}

std::vector<std::uint8_t> evaluate(std::span<const double> open,
                                   std::span<const double> high,
                                   std::span<const double> low,
                                   std::span<const double> close,
                                   std::span<const double> volume,
                                   const ConditionRequest& request) {
    std::vector<std::uint8_t> out(close.size(), 0U);
    const std::string& type = request.type;
    if (type == "always_true") {
        std::fill(out.begin(), out.end(), static_cast<std::uint8_t>(1));
        return out;
    }
    if (type == "price_above" || type == "price_below") {
        const double value = number(request, 0U, 0.0);
        for (std::size_t i = 0; i < close.size(); ++i) {
            out[i] = static_cast<std::uint8_t>(
                type == "price_above" ? close[i] > value : value > 0.0 && close[i] < value);
        }
        return out;
    }
    if (type == "ma_cross_up" || type == "ma_cross_down") {
        const auto fast = sma(close, positive_int(request, 0U, 5));
        const auto slow = sma(close, positive_int(request, 1U, 20));
        for (std::size_t i = 1; i < close.size(); ++i) {
            out[i] = static_cast<std::uint8_t>(type == "ma_cross_up"
                ? cross_up(fast[i - 1], slow[i - 1], fast[i], slow[i])
                : cross_down(fast[i - 1], slow[i - 1], fast[i], slow[i]));
        }
        return out;
    }
    if (type == "price_break_high" || type == "price_break_low") {
        const int period = positive_int(request, 0U, 20);
        for (std::size_t i = static_cast<std::size_t>(period); i < close.size(); ++i) {
            const auto begin = (type == "price_break_high" ? high : low).begin() +
                               static_cast<std::ptrdiff_t>(i - period);
            const auto end = (type == "price_break_high" ? high : low).begin() +
                             static_cast<std::ptrdiff_t>(i);
            const double level = type == "price_break_high"
                ? *std::max_element(begin, end) : *std::min_element(begin, end);
            out[i] = static_cast<std::uint8_t>(type == "price_break_high"
                ? close[i] > level : close[i] < level);
        }
        return out;
    }
    if (type == "price_cross_up_ma" || type == "price_cross_down_ma" ||
        type == "price_above_ma" || type == "price_below_ma") {
        const auto ma = moving_average(close, positive_int(request, 0U, 20), request.ma_kind);
        for (std::size_t i = 0; i < close.size(); ++i) {
            if (!std::isfinite(ma[i])) {
                continue;
            }
            if (type == "price_above_ma") {
                out[i] = static_cast<std::uint8_t>(close[i] > ma[i]);
            } else if (type == "price_below_ma") {
                out[i] = static_cast<std::uint8_t>(close[i] < ma[i]);
            }
            else if (i > 0) out[i] = static_cast<std::uint8_t>(type == "price_cross_up_ma"
                ? cross_up(close[i - 1], ma[i - 1], close[i], ma[i])
                : cross_down(close[i - 1], ma[i - 1], close[i], ma[i]));
        }
        return out;
    }
    if (type == "ma_slope_up" || type == "ma_slope_down") {
        const int look = positive_int(request, 1U, 3);
        const auto ma = moving_average(close, positive_int(request, 0U, 20), request.ma_kind);
        for (std::size_t i = static_cast<std::size_t>(look); i < close.size(); ++i) {
            if (std::isfinite(ma[i]) && std::isfinite(ma[i - look])) {
                out[i] = static_cast<std::uint8_t>(type == "ma_slope_up"
                    ? ma[i] > ma[i - look] : ma[i] < ma[i - look]);
            }
        }
        return out;
    }
    if (type == "ma_align_bull" || type == "ma_align_bear") {
        const auto short_ma = moving_average(close, positive_int(request, 0U, 5), request.ma_kind);
        const auto mid_ma = moving_average(close, positive_int(request, 1U, 20), request.ma_kind);
        const auto long_ma = moving_average(close, positive_int(request, 2U, 60), request.ma_kind);
        for (std::size_t i = 0; i < close.size(); ++i) {
            if (std::isfinite(short_ma[i]) && std::isfinite(mid_ma[i]) && std::isfinite(long_ma[i])) {
                out[i] = static_cast<std::uint8_t>(type == "ma_align_bull"
                    ? short_ma[i] > mid_ma[i] && mid_ma[i] > long_ma[i]
                    : short_ma[i] < mid_ma[i] && mid_ma[i] < long_ma[i]);
            }
        }
        return out;
    }
    if (type == "volume_above_ma" || type == "volume_below_ma") {
        const int period = positive_int(request, 0U, 20);
        const double multiplier = number(request, 1U, type == "volume_above_ma" ? 1.5 : 0.7);
        const auto average = sma(volume, period);
        for (std::size_t i = static_cast<std::size_t>(period); i < close.size(); ++i) {
            if (std::isfinite(average[i]) && average[i] > 0.0) {
                out[i] = static_cast<std::uint8_t>(type == "volume_above_ma"
                    ? volume[i] > average[i] * multiplier : volume[i] < average[i] * multiplier);
            }
        }
        return out;
    }
    if (type == "consecutive_up" || type == "consecutive_down") {
        const int period = positive_int(request, 0U, 3);
        int streak = 0;
        for (std::size_t i = 0; i < close.size(); ++i) {
            const bool matches = type == "consecutive_up" ? close[i] > open[i] : close[i] < open[i];
            streak = matches ? streak + 1 : 0;
            out[i] = static_cast<std::uint8_t>(streak >= period);
        }
        return out;
    }
    if (type == "pct_change_above" || type == "pct_change_below") {
        const double value = number(request, 0U, type == "pct_change_above" ? 3.0 : -3.0);
        for (std::size_t i = 1; i < close.size(); ++i) {
            if (close[i - 1] != 0.0) {
                const double change = (close[i] / close[i - 1] - 1.0) * 100.0;
                out[i] = static_cast<std::uint8_t>(type == "pct_change_above"
                    ? change >= value : change <= value);
            }
        }
        return out;
    }
    if (type == "inside_bar" || type == "gap_up" || type == "gap_down") {
        const double threshold = number(request, 0U, 0.0) / 100.0;
        for (std::size_t i = 1; i < close.size(); ++i) {
            if (type == "inside_bar") {
                out[i] = static_cast<std::uint8_t>(
                    high[i] <= high[i - 1] && low[i] >= low[i - 1]);
            } else if (type == "gap_up" && high[i - 1] != 0.0) {
                out[i] = static_cast<std::uint8_t>(
                    open[i] > high[i - 1] * (1.0 + threshold));
            } else if (type == "gap_down" && low[i - 1] != 0.0) {
                out[i] = static_cast<std::uint8_t>(
                    open[i] < low[i - 1] * (1.0 - threshold));
            }
        }
        return out;
    }
    throw std::invalid_argument("不支援的 native 策略條件: " + type);
}

}  // namespace

ConditionColumns calculate_conditions(
        std::span<const double> open,
        std::span<const double> high,
        std::span<const double> low,
        std::span<const double> close,
        std::span<const double> volume,
        std::span<const ConditionRequest> requests) {
    if (open.size() != close.size() || high.size() != close.size() ||
        low.size() != close.size() || volume.size() != close.size()) {
        throw std::invalid_argument("condition OHLCV 列數不一致");
    }
    if (requests.empty() || requests.size() > 64U) {
        throw std::invalid_argument("condition 請求數量必須介於 1 與 64");
    }
    require_finite(open, "open"); require_finite(high, "high");
    require_finite(low, "low"); require_finite(close, "close");
    require_finite(volume, "volume");
    ConditionColumns result;
    result.rows = close.size();
    result.values.reserve(requests.size());
    for (const ConditionRequest& request : requests) {
        result.values.push_back(evaluate(open, high, low, close, volume, request));
    }
    return result;
}

}  // namespace stockbuild
