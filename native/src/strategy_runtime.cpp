#include "stockbuild/strategy_runtime.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace stockbuild {
namespace {

constexpr std::uint8_t kEntrySignal = 1;
constexpr std::uint8_t kExitSignal = 2;
constexpr std::uint8_t kStopLossPct = 3;
constexpr std::uint8_t kTakeProfitPct = 4;
constexpr std::uint8_t kStopLossAbs = 5;
constexpr std::uint8_t kTakeProfitAbs = 6;
constexpr std::uint8_t kMaxTrades = 7;
constexpr std::uint8_t kDailyLoss = 8;
constexpr std::uint8_t kCooldown = 9;

bool combine(std::span<const std::span<const std::uint8_t>> columns,
             std::size_t row, const std::string& logic, bool empty_value) {
    if (columns.empty()) return empty_value;
    if (logic == "AND") {
        for (const auto column : columns) if (column[row] == 0) return false;
        return true;
    }
    for (const auto column : columns) if (column[row] != 0) return true;
    return false;
}

void require_nonnegative(double value, const char* name) {
    if (!std::isfinite(value) || value < 0.0) {
        throw std::invalid_argument(std::string(name) + " must be finite and non-negative");
    }
}

}  // namespace

StrategyIntentColumns evaluate_strategy_runtime(
    std::span<const std::int64_t> timestamps_ns,
    std::span<const std::int64_t> session_days,
    std::span<const double> close,
    std::span<const double> execution_price,
    std::span<const std::span<const std::uint8_t>> entry_conditions,
    std::span<const std::span<const std::uint8_t>> exit_conditions,
    const StrategyRuntimeConfig& config,
    std::size_t decision_start_row,
    std::size_t decision_end_row) {
    const std::size_t rows = close.size();
    if (timestamps_ns.size() != rows || session_days.size() != rows ||
        execution_price.size() != rows) {
        throw std::invalid_argument("strategy timestamps/day/close/execution lengths differ");
    }
    if (entry_conditions.empty() || entry_conditions.size() > 64 || exit_conditions.size() > 64) {
        throw std::invalid_argument("strategy requires 1..64 entry and 0..64 exit columns");
    }
    if ((config.entry_logic != "AND" && config.entry_logic != "OR") ||
        (config.exit_logic != "AND" && config.exit_logic != "OR")) {
        throw std::invalid_argument("strategy logic must be AND or OR");
    }
    if (config.quantity == 0) throw std::invalid_argument("strategy quantity must be positive");
    require_nonnegative(config.stop_loss_pct, "stop_loss_pct");
    require_nonnegative(config.take_profit_pct, "take_profit_pct");
    require_nonnegative(config.stop_loss_abs, "stop_loss_abs");
    require_nonnegative(config.take_profit_abs, "take_profit_abs");
    require_nonnegative(config.daily_loss_limit, "daily_loss_limit");
    require_nonnegative(config.cooldown_seconds, "cooldown_seconds");
    for (const auto column : entry_conditions) {
        if (column.size() != rows) throw std::invalid_argument("entry condition length differs");
    }
    for (const auto column : exit_conditions) {
        if (column.size() != rows) throw std::invalid_argument("exit condition length differs");
    }
    decision_end_row = std::min(decision_end_row, rows);
    if (decision_start_row > decision_end_row) {
        throw std::invalid_argument("strategy decision row range is invalid");
    }

    StrategyIntentColumns out;
    out.kind.assign(rows, 0);
    out.action.assign(rows, 0);
    out.quantity.assign(rows, 0);
    out.price.assign(rows, std::numeric_limits<double>::quiet_NaN());
    out.reason.assign(rows, 0);
    out.blocked_reason.assign(rows, 0);
    out.state.assign(rows, 0);
    out.entry_price.assign(rows, 0.0);
    out.realized_pnl_today.assign(rows, 0.0);

    std::int8_t state = 0;
    double entry_price = 0.0;
    double realized = 0.0;
    std::uint32_t trades_today = 0;
    std::int64_t current_day = rows == 0 ? 0 : session_days[0];
    std::int64_t last_order_ns = std::numeric_limits<std::int64_t>::min();
    const std::int8_t open_action = config.short_direction ? -1 : 1;

    for (std::size_t row = 0; row < rows; ++row) {
        if (!std::isfinite(close[row]) || !std::isfinite(execution_price[row])) {
            throw std::invalid_argument("strategy close/execution price is non-finite");
        }
        if (row > 0 && timestamps_ns[row] <= timestamps_ns[row - 1]) {
            throw std::invalid_argument("strategy timestamps must be strictly increasing");
        }
        if (session_days[row] != current_day) {
            current_day = session_days[row];
            trades_today = 0;
            realized = 0.0;
        }

        const bool decision_enabled = row >= decision_start_row && row < decision_end_row;
        if (decision_enabled && state == 0 &&
            combine(entry_conditions, row, config.entry_logic, false)) {
            std::uint8_t blocked = 0;
            if (config.max_trades_per_day > 0 && trades_today >= config.max_trades_per_day) {
                blocked = kMaxTrades;
            } else if (config.daily_loss_limit > 0.0 && realized <= -config.daily_loss_limit) {
                blocked = kDailyLoss;
            } else if (config.cooldown_seconds > 0.0 &&
                       last_order_ns != std::numeric_limits<std::int64_t>::min() &&
                       static_cast<double>(timestamps_ns[row] - last_order_ns) <
                           config.cooldown_seconds * 1.0e9) {
                blocked = kCooldown;
            }
            out.blocked_reason[row] = blocked;
            if (blocked == 0) {
                out.kind[row] = 1;
                out.action[row] = open_action;
                out.quantity[row] = config.quantity;
                out.price[row] = execution_price[row];
                out.reason[row] = kEntrySignal;
                state = config.short_direction ? -1 : 1;
                entry_price = execution_price[row];
                ++trades_today;
                last_order_ns = timestamps_ns[row];
            }
        } else if (decision_enabled && state != 0) {
            const double favorable = (close[row] - entry_price) * static_cast<double>(state);
            std::uint8_t close_reason = 0;
            if (config.stop_loss_pct > 0.0 &&
                favorable <= -(entry_price * config.stop_loss_pct / 100.0)) {
                close_reason = kStopLossPct;
            } else if (config.take_profit_pct > 0.0 &&
                       favorable >= entry_price * config.take_profit_pct / 100.0) {
                close_reason = kTakeProfitPct;
            } else if (config.stop_loss_abs > 0.0 && favorable <= -config.stop_loss_abs) {
                close_reason = kStopLossAbs;
            } else if (config.take_profit_abs > 0.0 && favorable >= config.take_profit_abs) {
                close_reason = kTakeProfitAbs;
            } else if (combine(exit_conditions, row, config.exit_logic, false)) {
                close_reason = kExitSignal;
            }
            if (close_reason != 0) {
                out.kind[row] = 2;
                out.action[row] = static_cast<std::int8_t>(-state);
                out.quantity[row] = config.quantity;
                out.price[row] = execution_price[row];
                out.reason[row] = close_reason;
                const double executed_favorable =
                    (execution_price[row] - entry_price) * static_cast<double>(state);
                realized += executed_favorable * static_cast<double>(config.quantity);
                state = 0;
                entry_price = 0.0;
            }
        }
        out.state[row] = state;
        out.entry_price[row] = entry_price;
        out.realized_pnl_today[row] = realized;
    }
    return out;
}

}  // namespace stockbuild
