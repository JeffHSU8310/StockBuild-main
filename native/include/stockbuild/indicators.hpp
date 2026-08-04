#pragma once

#include <cstddef>
#include <span>
#include <string>
#include <vector>

namespace stockbuild {

struct IndicatorColumns final {
    std::vector<double> sma;
    std::vector<double> ema;
    std::vector<double> wma;
    std::vector<double> rsi;
    std::vector<double> macd;
    std::vector<double> signal;
    std::vector<double> hist;
    std::vector<double> bb_mid;
    std::vector<double> bb_std;
    std::vector<double> bb_upper;
    std::vector<double> bb_lower;
    std::vector<double> bb_width;
};

struct MultiMovingAverageColumns final {
    std::vector<std::vector<double>> values;
    std::size_t rows{};
};

MultiMovingAverageColumns calculate_moving_averages(
    std::span<const double> close,
    std::span<const int> periods,
    std::span<const std::string> kinds);

IndicatorColumns calculate_indicator_core(std::span<const double> close,
                                           int ma_period,
                                           int rsi_period,
                                           int macd_fast,
                                           int macd_slow,
                                           int macd_signal,
                                           int bb_period,
                                           double bb_std_up,
                                           double bb_std_down,
                                           const std::string& bb_ma_type);

}  // namespace stockbuild
