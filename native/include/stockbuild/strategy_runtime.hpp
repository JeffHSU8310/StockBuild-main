#pragma once

#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <vector>

namespace stockbuild {

struct StrategyRuntimeConfig final {
    bool short_direction{false};
    std::uint32_t quantity{1};
    std::string entry_logic{"AND"};
    std::string exit_logic{"OR"};
    double stop_loss_pct{0.0};
    double take_profit_pct{0.0};
    double stop_loss_abs{0.0};
    double take_profit_abs{0.0};
    std::uint32_t max_trades_per_day{0};
    double daily_loss_limit{0.0};
    double cooldown_seconds{0.0};
};

struct StrategyIntentColumns final {
    std::vector<std::uint8_t> kind;
    std::vector<std::int8_t> action;
    std::vector<std::uint32_t> quantity;
    std::vector<double> price;
    std::vector<std::uint8_t> reason;
    std::vector<std::uint8_t> blocked_reason;
    std::vector<std::int8_t> state;
    std::vector<double> entry_price;
    std::vector<double> realized_pnl_today;
};

StrategyIntentColumns evaluate_strategy_runtime(
    std::span<const std::int64_t> timestamps_ns,
    std::span<const std::int64_t> session_days,
    std::span<const double> close,
    std::span<const std::span<const std::uint8_t>> entry_conditions,
    std::span<const std::span<const std::uint8_t>> exit_conditions,
    const StrategyRuntimeConfig& config);

}  // namespace stockbuild
