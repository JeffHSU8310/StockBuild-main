#include "stockbuild/indicators.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace stockbuild {
namespace {

double quiet_nan() noexcept {
    return std::numeric_limits<double>::quiet_NaN();
}

void require_period(int period, const char* name, int minimum = 1) {
    if (period < minimum) {
        throw std::invalid_argument(std::string(name) + " 期間不合法");
    }
}

void validate_close(std::span<const double> close) {
    for (std::size_t i = 0; i < close.size(); ++i) {
        if (!std::isfinite(close[i])) {
            throw std::invalid_argument(
                "indicator close 不可包含 NaN 或 Infinity，錯誤列=" + std::to_string(i));
        }
    }
}

std::vector<double> simple_moving_average(std::span<const double> values, int period) {
    std::vector<double> result(values.size(), quiet_nan());
    if (values.size() < static_cast<std::size_t>(period)) {
        return result;
    }
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

std::vector<double> exponential_moving_average(std::span<const double> values,
                                               double alpha) {
    std::vector<double> result(values.size(), quiet_nan());
    if (values.empty()) {
        return result;
    }
    result[0] = values[0];
    for (std::size_t i = 1; i < values.size(); ++i) {
        result[i] = alpha * values[i] + (1.0 - alpha) * result[i - 1];
    }
    return result;
}

std::vector<double> weighted_moving_average(std::span<const double> values, int period) {
    std::vector<double> result(values.size(), quiet_nan());
    if (values.size() < static_cast<std::size_t>(period)) {
        return result;
    }
    const double denominator = static_cast<double>(period) * (period + 1) / 2.0;
    for (std::size_t end = static_cast<std::size_t>(period - 1);
         end < values.size(); ++end) {
        const std::size_t start = end + 1 - static_cast<std::size_t>(period);
        double weighted_sum = 0.0;
        for (int weight = 1; weight <= period; ++weight) {
            weighted_sum += weight * values[start + static_cast<std::size_t>(weight - 1)];
        }
        result[end] = weighted_sum / denominator;
    }
    return result;
}

std::vector<double> rolling_sample_std(std::span<const double> values, int period) {
    std::vector<double> result(values.size(), quiet_nan());
    if (values.size() < static_cast<std::size_t>(period)) {
        return result;
    }
    for (std::size_t end = static_cast<std::size_t>(period - 1);
         end < values.size(); ++end) {
        const std::size_t start = end + 1 - static_cast<std::size_t>(period);
        double mean = 0.0;
        double m2 = 0.0;
        for (int count = 1; count <= period; ++count) {
            const double value = values[start + static_cast<std::size_t>(count - 1)];
            const double delta = value - mean;
            mean += delta / count;
            m2 += delta * (value - mean);
        }
        result[end] = std::sqrt(std::max(0.0, m2 / (period - 1)));
    }
    return result;
}

std::vector<double> relative_strength_index(std::span<const double> close, int period) {
    std::vector<double> result(close.size(), quiet_nan());
    if (close.size() < 2) {
        return result;
    }
    const double alpha = 1.0 / period;
    double delta = close[1] - close[0];
    double average_gain = std::max(delta, 0.0);
    double average_loss = std::max(-delta, 0.0);
    for (std::size_t i = 1; i < close.size(); ++i) {
        if (i > 1) {
            delta = close[i] - close[i - 1];
            const double gain = std::max(delta, 0.0);
            const double loss = std::max(-delta, 0.0);
            average_gain = alpha * gain + (1.0 - alpha) * average_gain;
            average_loss = alpha * loss + (1.0 - alpha) * average_loss;
        }
        if (average_loss == 0.0) {
            result[i] = average_gain == 0.0 ? quiet_nan() : 100.0;
        } else {
            const double strength = average_gain / average_loss;
            result[i] = 100.0 - 100.0 / (1.0 + strength);
        }
    }
    return result;
}

std::vector<double> moving_average_by_kind(std::span<const double> values,
                                           int period,
                                           const std::string& kind) {
    if (kind == "SMA") {
        return simple_moving_average(values, period);
    }
    if (kind == "EMA") {
        return exponential_moving_average(values, 2.0 / (period + 1.0));
    }
    if (kind == "WMA") {
        return weighted_moving_average(values, period);
    }
    throw std::invalid_argument("bb_ma_type 只接受 SMA、EMA 或 WMA");
}

}  // namespace

IndicatorColumns calculate_indicator_core(std::span<const double> close,
                                           int ma_period,
                                           int rsi_period,
                                           int macd_fast,
                                           int macd_slow,
                                           int macd_signal,
                                           int bb_period,
                                           double bb_std_up,
                                           double bb_std_down,
                                           const std::string& bb_ma_type) {
    require_period(ma_period, "ma_period");
    require_period(rsi_period, "rsi_period");
    require_period(macd_fast, "macd_fast");
    require_period(macd_slow, "macd_slow");
    require_period(macd_signal, "macd_signal");
    require_period(bb_period, "bb_period", 2);
    if (!std::isfinite(bb_std_up) || bb_std_up <= 0.0 ||
        !std::isfinite(bb_std_down) || bb_std_down <= 0.0) {
        throw std::invalid_argument("Bollinger 上下 sigma 必須是 finite 正數");
    }
    validate_close(close);

    IndicatorColumns output;
    output.sma = simple_moving_average(close, ma_period);
    output.ema = exponential_moving_average(close, 2.0 / (ma_period + 1.0));
    output.wma = weighted_moving_average(close, ma_period);
    output.rsi = relative_strength_index(close, rsi_period);

    const std::vector<double> fast =
        exponential_moving_average(close, 2.0 / (macd_fast + 1.0));
    const std::vector<double> slow =
        exponential_moving_average(close, 2.0 / (macd_slow + 1.0));
    output.macd.resize(close.size());
    for (std::size_t i = 0; i < close.size(); ++i) {
        output.macd[i] = fast[i] - slow[i];
    }
    output.signal = exponential_moving_average(
        output.macd, 2.0 / (macd_signal + 1.0));
    output.hist.resize(close.size());
    for (std::size_t i = 0; i < close.size(); ++i) {
        output.hist[i] = output.macd[i] - output.signal[i];
    }

    if (bb_period == ma_period && bb_ma_type == "SMA") {
        output.bb_mid = output.sma;
    } else if (bb_period == ma_period && bb_ma_type == "EMA") {
        output.bb_mid = output.ema;
    } else if (bb_period == ma_period && bb_ma_type == "WMA") {
        output.bb_mid = output.wma;
    } else {
        output.bb_mid = moving_average_by_kind(close, bb_period, bb_ma_type);
    }
    output.bb_std = rolling_sample_std(close, bb_period);
    output.bb_upper.resize(close.size());
    output.bb_lower.resize(close.size());
    output.bb_width.resize(close.size());
    for (std::size_t i = 0; i < close.size(); ++i) {
        output.bb_upper[i] = output.bb_mid[i] + bb_std_up * output.bb_std[i];
        output.bb_lower[i] = output.bb_mid[i] - bb_std_down * output.bb_std[i];
        output.bb_width[i] =
            (output.bb_upper[i] - output.bb_lower[i]) / output.bb_mid[i] * 100.0;
    }
    return output;
}

}  // namespace stockbuild
