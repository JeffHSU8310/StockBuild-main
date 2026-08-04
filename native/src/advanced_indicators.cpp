#include "stockbuild/advanced_indicators.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <deque>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace stockbuild {
namespace {

double quiet_nan() noexcept {
    return std::numeric_limits<double>::quiet_NaN();
}

void require_period(int period, const char* name) {
    if (period < 1) {
        throw std::invalid_argument(std::string(name) + " must be positive");
    }
}

void validate_ohlc(std::span<const double> high,
                   std::span<const double> low,
                   std::span<const double> close) {
    if (high.size() != low.size() || high.size() != close.size()) {
        throw std::invalid_argument("high/low/close row counts differ");
    }
    for (std::size_t i = 0; i < close.size(); ++i) {
        if (!std::isfinite(high[i]) || !std::isfinite(low[i]) ||
            !std::isfinite(close[i])) {
            throw std::invalid_argument(
                "advanced indicator OHLC contains NaN or Infinity at row " +
                std::to_string(i));
        }
    }
}

template <bool Minimum>
std::vector<double> rolling_extreme(std::span<const double> values, int period) {
    std::vector<double> result(values.size(), quiet_nan());
    std::deque<std::size_t> queue;
    const std::size_t window = static_cast<std::size_t>(period);
    for (std::size_t i = 0; i < values.size(); ++i) {
        while (!queue.empty() &&
               (Minimum ? values[queue.back()] >= values[i]
                        : values[queue.back()] <= values[i])) {
            queue.pop_back();
        }
        queue.push_back(i);
        if (i >= window) {
            const std::size_t expired = i - window;
            if (queue.front() == expired) {
                queue.pop_front();
            }
        }
        if (i + 1 >= window) {
            result[i] = values[queue.front()];
        }
    }
    return result;
}

std::vector<double> ewm_adjust_false(std::span<const double> values,
                                     double alpha) {
    std::vector<double> result(values.size(), quiet_nan());
    bool initialized = false;
    double average = quiet_nan();
    double old_weight = 1.0;
    const double old_weight_factor = 1.0 - alpha;
    for (std::size_t i = 0; i < values.size(); ++i) {
        const bool observed = std::isfinite(values[i]);
        if (initialized) {
            old_weight *= old_weight_factor;
        }
        if (observed) {
            if (!initialized) {
                average = values[i];
                initialized = true;
            } else {
                average = (old_weight * average + alpha * values[i]) /
                          (old_weight + alpha);
            }
            old_weight = 1.0;
        }
        if (initialized) {
            result[i] = average;
        }
    }
    return result;
}

std::vector<double> relative_strength_index(std::span<const double> close,
                                            int period) {
    std::vector<double> result(close.size(), quiet_nan());
    if (close.size() < 2) {
        return result;
    }
    const double alpha = 1.0 / period;
    double average_gain = std::max(close[1] - close[0], 0.0);
    double average_loss = std::max(close[0] - close[1], 0.0);
    for (std::size_t i = 1; i < close.size(); ++i) {
        if (i > 1) {
            const double delta = close[i] - close[i - 1];
            average_gain = alpha * std::max(delta, 0.0) +
                           (1.0 - alpha) * average_gain;
            average_loss = alpha * std::max(-delta, 0.0) +
                           (1.0 - alpha) * average_loss;
        }
        if (average_loss == 0.0) {
            result[i] = average_gain == 0.0 ? quiet_nan() : 100.0;
        } else {
            result[i] = 100.0 - 100.0 / (1.0 + average_gain / average_loss);
        }
    }
    return result;
}

struct KdjColumns final {
    std::vector<double> rsv;
    std::vector<double> k;
    std::vector<double> d;
    std::vector<double> j;
};

KdjColumns calculate_kdj(std::span<const double> high,
                         std::span<const double> low,
                         std::span<const double> close,
                         int n,
                         int m1,
                         int m2) {
    const std::vector<double> low_min = rolling_extreme<true>(low, n);
    const std::vector<double> high_max = rolling_extreme<false>(high, n);
    KdjColumns result;
    result.rsv.resize(close.size(), quiet_nan());
    for (std::size_t i = 0; i < close.size(); ++i) {
        const double range = high_max[i] - low_min[i];
        if (std::isfinite(range) && range != 0.0) {
            result.rsv[i] = 100.0 * (close[i] - low_min[i]) / range;
        }
    }
    result.k = ewm_adjust_false(result.rsv, 1.0 / m1);
    result.d = ewm_adjust_false(result.k, 1.0 / m2);
    result.j.resize(close.size(), quiet_nan());
    for (std::size_t i = 0; i < close.size(); ++i) {
        if (std::isfinite(result.k[i]) && std::isfinite(result.d[i])) {
            result.j[i] = 3.0 * result.k[i] - 2.0 * result.d[i];
        }
    }
    return result;
}

}  // namespace

AdvancedIndicatorColumns calculate_advanced_indicators(
        std::span<const double> high,
        std::span<const double> low,
        std::span<const double> close,
        int kdj_n,
        int kdj_m1,
        int kdj_m2,
        int dmi_n,
        int jae_a_period,
        int jae_j_n,
        int jae_j_m1,
        int jae_j_m2,
        int jae_e_period) {
    require_period(kdj_n, "kdj_n");
    require_period(kdj_m1, "kdj_m1");
    require_period(kdj_m2, "kdj_m2");
    require_period(dmi_n, "dmi_n");
    require_period(jae_a_period, "jae_a_period");
    require_period(jae_j_n, "jae_j_n");
    require_period(jae_j_m1, "jae_j_m1");
    require_period(jae_j_m2, "jae_j_m2");
    require_period(jae_e_period, "jae_e_period");
    validate_ohlc(high, low, close);

    AdvancedIndicatorColumns output;
    KdjColumns kdj = calculate_kdj(high, low, close, kdj_n, kdj_m1, kdj_m2);
    output.rsv = std::move(kdj.rsv);
    output.k = std::move(kdj.k);
    output.d = std::move(kdj.d);
    output.j = std::move(kdj.j);

    output.plus_dm.resize(close.size(), 0.0);
    output.minus_dm.resize(close.size(), 0.0);
    std::vector<double> true_range(close.size(), quiet_nan());
    for (std::size_t i = 0; i < close.size(); ++i) {
        if (i > 0) {
            const double up_move = high[i] - high[i - 1];
            const double down_move = low[i - 1] - low[i];
            output.plus_dm[i] = up_move > down_move && up_move > 0.0 ? up_move : 0.0;
            output.minus_dm[i] = down_move > up_move && down_move > 0.0 ? down_move : 0.0;
            true_range[i] = std::max({high[i] - low[i],
                                      std::abs(high[i] - close[i - 1]),
                                      std::abs(low[i] - close[i - 1])});
        } else if (!close.empty()) {
            true_range[i] = high[i] - low[i];
        }
    }
    const double dmi_alpha = 2.0 / (dmi_n + 1.0);
    const std::vector<double> atr = ewm_adjust_false(true_range, dmi_alpha);
    const std::vector<double> plus_dm_smooth =
        ewm_adjust_false(output.plus_dm, dmi_alpha);
    const std::vector<double> minus_dm_smooth =
        ewm_adjust_false(output.minus_dm, dmi_alpha);
    output.plus_di.resize(close.size(), quiet_nan());
    output.minus_di.resize(close.size(), quiet_nan());
    std::vector<double> dx(close.size(), quiet_nan());
    for (std::size_t i = 0; i < close.size(); ++i) {
        if (atr[i] != 0.0) {
            output.plus_di[i] = 100.0 * plus_dm_smooth[i] / atr[i];
            output.minus_di[i] = 100.0 * minus_dm_smooth[i] / atr[i];
            const double sum = output.plus_di[i] + output.minus_di[i];
            if (sum != 0.0) {
                dx[i] = 100.0 * std::abs(output.plus_di[i] - output.minus_di[i]) / sum;
            }
        }
    }
    output.adx = ewm_adjust_false(dx, dmi_alpha);

    output.jae_a = relative_strength_index(close, jae_a_period);
    KdjColumns jae_kdj = calculate_kdj(
        high, low, close, jae_j_n, jae_j_m1, jae_j_m2);
    output.jae_j = std::move(jae_kdj.j);
    output.jae_e = ewm_adjust_false(output.jae_a, 2.0 / (jae_e_period + 1.0));
    return output;
}

}  // namespace stockbuild
