#pragma once

#include <span>
#include <vector>

namespace stockbuild {

struct AdvancedIndicatorColumns final {
    std::vector<double> rsv;
    std::vector<double> k;
    std::vector<double> d;
    std::vector<double> j;
    std::vector<double> plus_dm;
    std::vector<double> minus_dm;
    std::vector<double> plus_di;
    std::vector<double> minus_di;
    std::vector<double> adx;
    std::vector<double> jae_a;
    std::vector<double> jae_j;
    std::vector<double> jae_e;
};

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
    int jae_e_period);

}  // namespace stockbuild
