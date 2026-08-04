#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "stockbuild/advanced_indicators.hpp"
#include "stockbuild/conditions.hpp"
#include "stockbuild/kbar_schema.hpp"
#include "stockbuild/indicators.hpp"
#include "stockbuild/resampler.hpp"
#include "stockbuild/sqlite_reader.hpp"
#include "stockbuild/strategy_runtime.hpp"
#include "stockbuild/version.hpp"

namespace py = pybind11;

namespace {

template <typename T>
py::buffer_info require_column(const py::array& value, const char* name) {
    py::buffer_info info = value.request(false);
    if (info.ndim != 1) {
        throw std::invalid_argument(std::string(name) + " 必須是一維 NumPy array");
    }
    // PEP 3118 對 uint32 的 format code 會依 C typedef 顯示為 I/L；用 NumPy
    // dtype identity 才能跨 Windows ABI 精確驗證，不把同寬 signed/float 放行。
    if (info.itemsize != static_cast<py::ssize_t>(sizeof(T)) ||
        !value.dtype().is(py::dtype::of<T>())) {
        throw std::invalid_argument(std::string(name) + " dtype 不符合 KBar ABI");
    }
    if (info.shape[0] > 1 && !info.strides.empty() &&
        info.strides[0] != static_cast<py::ssize_t>(sizeof(T))) {
        throw std::invalid_argument(std::string(name) + " 必須是 C-contiguous");
    }
    return info;
}

struct BatchViews final {
    py::buffer_info timestamps;
    py::buffer_info open;
    py::buffer_info high;
    py::buffer_info low;
    py::buffer_info close;
    py::buffer_info volume;
    py::buffer_info flags;
    py::ssize_t rows{0};
};

BatchViews require_batch(const py::array& timestamps,
                         const py::array& open,
                         const py::array& high,
                         const py::array& low,
                         const py::array& close,
                         const py::array& volume,
                         const py::array& flags) {
    BatchViews views{
        require_column<std::int64_t>(timestamps, "timestamps"),
        require_column<double>(open, "open"),
        require_column<double>(high, "high"),
        require_column<double>(low, "low"),
        require_column<double>(close, "close"),
        require_column<double>(volume, "volume"),
        require_column<std::uint32_t>(flags, "flags"),
        0,
    };
    views.rows = views.timestamps.shape[0];
    const std::vector<std::pair<const char*, py::ssize_t>> lengths{
        {"open", views.open.shape[0]}, {"high", views.high.shape[0]},
        {"low", views.low.shape[0]}, {"close", views.close.shape[0]},
        {"volume", views.volume.shape[0]}, {"flags", views.flags.shape[0]},
    };
    for (const auto& [name, length] : lengths) {
        if (length != views.rows) {
            throw std::invalid_argument(std::string(name) + " 列數與 timestamps 不一致");
        }
    }
    return views;
}

py::dict abi_info() {
    using stockbuild::KBarRecord;
    py::dict offsets;
    offsets["timestamp_ns"] = offsetof(KBarRecord, timestamp_ns);
    offsets["open"] = offsetof(KBarRecord, open);
    offsets["high"] = offsetof(KBarRecord, high);
    offsets["low"] = offsetof(KBarRecord, low);
    offsets["close"] = offsetof(KBarRecord, close);
    offsets["volume"] = offsetof(KBarRecord, volume);
    offsets["flags"] = offsetof(KBarRecord, flags);
    offsets["reserved"] = offsetof(KBarRecord, reserved);

    py::dict dtypes;
    dtypes["timestamp_ns"] = "int64";
    dtypes["open"] = "float64";
    dtypes["high"] = "float64";
    dtypes["low"] = "float64";
    dtypes["close"] = "float64";
    dtypes["volume"] = "float64";
    dtypes["flags"] = "uint32";
    dtypes["reserved"] = "uint32";

    py::dict result;
    result["native_version"] = stockbuild::kNativeVersion;
    result["abi_version"] = stockbuild::kAbiVersion;
    result["schema_version"] = stockbuild::kKBarSchemaVersion;
    result["struct_size"] = sizeof(KBarRecord);
    result["struct_alignment"] = alignof(KBarRecord);
    result["offsets"] = offsets;
    result["dtypes"] = dtypes;
    return result;
}

void handshake(std::uint32_t expected_abi, std::uint32_t expected_schema) {
    if (expected_abi != stockbuild::kAbiVersion) {
        throw std::runtime_error("native ABI version 不相容");
    }
    if (expected_schema != stockbuild::kKBarSchemaVersion) {
        throw std::runtime_error("KBar schema version 不相容");
    }
}

py::dict inspect_kbars(const py::array& timestamps,
                       const py::array& open,
                       const py::array& high,
                       const py::array& low,
                       const py::array& close,
                       const py::array& volume,
                       const py::array& flags) {
    const BatchViews views = require_batch(timestamps, open, high, low, close, volume, flags);
    const auto* ts = static_cast<const std::int64_t*>(views.timestamps.ptr);
    const auto* op = static_cast<const double*>(views.open.ptr);
    const auto* hi = static_cast<const double*>(views.high.ptr);
    const auto* lo = static_cast<const double*>(views.low.ptr);
    const auto* cl = static_cast<const double*>(views.close.ptr);
    const auto* vol = static_cast<const double*>(views.volume.ptr);
    const auto* fl = static_cast<const std::uint32_t*>(views.flags.ptr);

    double checksum = 0.0;
    std::uint64_t flag_checksum = 0;
    {
        py::gil_scoped_release release;
        for (py::ssize_t i = 0; i < views.rows; ++i) {
            checksum += op[i] + hi[i] + lo[i] + cl[i] + vol[i];
            flag_checksum += fl[i];
        }
    }

    py::dict addresses;
    addresses["timestamps"] = reinterpret_cast<std::uintptr_t>(views.timestamps.ptr);
    addresses["open"] = reinterpret_cast<std::uintptr_t>(views.open.ptr);
    addresses["high"] = reinterpret_cast<std::uintptr_t>(views.high.ptr);
    addresses["low"] = reinterpret_cast<std::uintptr_t>(views.low.ptr);
    addresses["close"] = reinterpret_cast<std::uintptr_t>(views.close.ptr);
    addresses["volume"] = reinterpret_cast<std::uintptr_t>(views.volume.ptr);
    addresses["flags"] = reinterpret_cast<std::uintptr_t>(views.flags.ptr);

    py::dict result;
    result["rows"] = views.rows;
    result["checksum"] = checksum;
    result["flag_checksum"] = flag_checksum;
    result["first_timestamp_ns"] = views.rows ? ts[0] : 0;
    result["last_timestamp_ns"] = views.rows ? ts[views.rows - 1] : 0;
    result["addresses"] = addresses;
    return result;
}

py::tuple echo_kbars(const py::array& timestamps,
                     const py::array& open,
                     const py::array& high,
                     const py::array& low,
                     const py::array& close,
                     const py::array& volume,
                     const py::array& flags) {
    static_cast<void>(require_batch(timestamps, open, high, low, close, volume, flags));
    return py::make_tuple(timestamps, open, high, low, close, volume, flags);
}

py::dict sqlite_probe(const std::string& database_path,
                      const std::string& sqlite_library_path,
                      const std::string& symbol,
                      const std::string& asset_type,
                      const std::string& timeframe) {
    stockbuild::SqliteProbe probe;
    {
        py::gil_scoped_release release;
        probe = stockbuild::probe_sqlite(database_path, sqlite_library_path,
                                         symbol, asset_type, timeframe);
    }
    py::dict result;
    result["readonly"] = probe.readonly;
    result["query_only"] = probe.query_only;
    result["schema_version"] = probe.schema_version;
    result["data_version"] = probe.data_version;
    result["first_ts"] = probe.first_ts;
    result["last_ts"] = probe.last_ts;
    result["count"] = probe.count;
    return result;
}

template <typename T>
py::array vector_view(std::vector<T>& values, const py::object& owner) {
    const std::vector<py::ssize_t> shape{static_cast<py::ssize_t>(values.size())};
    const std::vector<py::ssize_t> strides{static_cast<py::ssize_t>(sizeof(T))};
    return py::array(py::dtype::of<T>(), shape, strides, values.data(), owner);
}

py::dict kbar_columns_payload(std::unique_ptr<stockbuild::KBarColumns> columns) {
    stockbuild::KBarColumns* raw = columns.release();
    py::capsule owner(raw, [](void* pointer) {
        delete static_cast<stockbuild::KBarColumns*>(pointer);
    });

    py::dict result;
    result["readonly"] = raw->readonly;
    result["query_only"] = raw->query_only;
    result["schema_version"] = raw->schema_version;
    result["data_version"] = raw->data_version;
    result["rows"] = raw->timestamps.size();
    result["timestamps"] = vector_view(raw->timestamps, owner);
    result["open"] = vector_view(raw->open, owner);
    result["high"] = vector_view(raw->high, owner);
    result["low"] = vector_view(raw->low, owner);
    result["close"] = vector_view(raw->close, owner);
    result["volume"] = vector_view(raw->volume, owner);
    result["flags"] = vector_view(raw->flags, owner);
    return result;
}

py::dict sqlite_range(const std::string& database_path,
                      const std::string& sqlite_library_path,
                      const std::string& symbol,
                      const std::string& asset_type,
                      const std::string& timeframe,
                      const std::optional<std::string>& start_ts,
                      const std::optional<std::string>& end_ts) {
    std::unique_ptr<stockbuild::KBarColumns> columns;
    {
        py::gil_scoped_release release;
        columns = std::make_unique<stockbuild::KBarColumns>(stockbuild::read_kbars_range(
            database_path, sqlite_library_path, symbol, asset_type, timeframe, start_ts, end_ts));
    }
    return kbar_columns_payload(std::move(columns));
}

std::string sqlite_range_query_plan(const std::string& database_path,
                                    const std::string& sqlite_library_path,
                                    const std::string& symbol,
                                    const std::string& asset_type,
                                    const std::string& timeframe,
                                    const std::optional<std::string>& start_ts,
                                    const std::optional<std::string>& end_ts) {
    py::gil_scoped_release release;
    return stockbuild::explain_kbars_range(database_path, sqlite_library_path,
                                           symbol, asset_type, timeframe, start_ts, end_ts);
}

py::dict resample_kbars_native(const py::array& timestamps,
                               const py::array& open,
                               const py::array& high,
                               const py::array& low,
                               const py::array& close,
                               const py::array& volume,
                               const py::array& flags,
                               const std::string& mode,
                               std::int64_t interval_minutes,
                               int timezone_offset_minutes,
                               const std::string& session_basis) {
    const BatchViews views = require_batch(
        timestamps, open, high, low, close, volume, flags);
    std::unique_ptr<stockbuild::KBarColumns> columns;
    {
        py::gil_scoped_release release;
        columns = std::make_unique<stockbuild::KBarColumns>(stockbuild::resample_kbars(
            {static_cast<const std::int64_t*>(views.timestamps.ptr),
             static_cast<std::size_t>(views.rows)},
            {static_cast<const double*>(views.open.ptr), static_cast<std::size_t>(views.rows)},
            {static_cast<const double*>(views.high.ptr), static_cast<std::size_t>(views.rows)},
            {static_cast<const double*>(views.low.ptr), static_cast<std::size_t>(views.rows)},
            {static_cast<const double*>(views.close.ptr), static_cast<std::size_t>(views.rows)},
            {static_cast<const double*>(views.volume.ptr), static_cast<std::size_t>(views.rows)},
            {static_cast<const std::uint32_t*>(views.flags.ptr),
             static_cast<std::size_t>(views.rows)},
            mode, interval_minutes, timezone_offset_minutes, session_basis));
    }
    return kbar_columns_payload(std::move(columns));
}

py::dict indicator_core_native(const py::array& close,
                               int ma_period,
                               int rsi_period,
                               int macd_fast,
                               int macd_slow,
                               int macd_signal,
                               int bb_period,
                               double bb_std_up,
                               double bb_std_down,
                               const std::string& bb_ma_type) {
    const py::buffer_info info = require_column<double>(close, "close");
    std::unique_ptr<stockbuild::IndicatorColumns> columns;
    {
        py::gil_scoped_release release;
        columns = std::make_unique<stockbuild::IndicatorColumns>(
            stockbuild::calculate_indicator_core(
                {static_cast<const double*>(info.ptr), static_cast<std::size_t>(info.shape[0])},
                ma_period, rsi_period, macd_fast, macd_slow, macd_signal,
                bb_period, bb_std_up, bb_std_down, bb_ma_type));
    }
    stockbuild::IndicatorColumns* raw = columns.release();
    py::capsule owner(raw, [](void* pointer) {
        delete static_cast<stockbuild::IndicatorColumns*>(pointer);
    });
    py::dict result;
    result["rows"] = raw->sma.size();
    result["sma"] = vector_view(raw->sma, owner);
    result["ema"] = vector_view(raw->ema, owner);
    result["wma"] = vector_view(raw->wma, owner);
    result["rsi"] = vector_view(raw->rsi, owner);
    result["macd"] = vector_view(raw->macd, owner);
    result["signal"] = vector_view(raw->signal, owner);
    result["hist"] = vector_view(raw->hist, owner);
    result["bb_mid"] = vector_view(raw->bb_mid, owner);
    result["bb_std"] = vector_view(raw->bb_std, owner);
    result["bb_upper"] = vector_view(raw->bb_upper, owner);
    result["bb_lower"] = vector_view(raw->bb_lower, owner);
    result["bb_width"] = vector_view(raw->bb_width, owner);
    return result;
}

py::dict multi_ma_core_native(const py::array& close,
                              const std::vector<int>& periods,
                              const std::vector<std::string>& kinds) {
    const py::buffer_info info = require_column<double>(close, "close");
    std::unique_ptr<stockbuild::MultiMovingAverageColumns> columns;
    {
        py::gil_scoped_release release;
        columns = std::make_unique<stockbuild::MultiMovingAverageColumns>(
            stockbuild::calculate_moving_averages(
                {static_cast<const double*>(info.ptr),
                 static_cast<std::size_t>(info.shape[0])},
                periods, kinds));
    }
    stockbuild::MultiMovingAverageColumns* raw = columns.release();
    py::capsule owner(raw, [](void* pointer) {
        delete static_cast<stockbuild::MultiMovingAverageColumns*>(pointer);
    });
    py::list values;
    for (std::vector<double>& column : raw->values) {
        values.append(vector_view(column, owner));
    }
    py::dict result;
    result["rows"] = raw->rows;
    result["count"] = raw->values.size();
    result["values"] = values;
    return result;
}

py::dict condition_core_native(const py::array& open,
                               const py::array& high,
                               const py::array& low,
                               const py::array& close,
                               const py::array& volume,
                               const std::vector<std::string>& types,
                               const std::vector<std::vector<double>>& params,
                               const std::vector<std::string>& ma_kinds) {
    const py::buffer_info open_info = require_column<double>(open, "open");
    const py::buffer_info high_info = require_column<double>(high, "high");
    const py::buffer_info low_info = require_column<double>(low, "low");
    const py::buffer_info close_info = require_column<double>(close, "close");
    const py::buffer_info volume_info = require_column<double>(volume, "volume");
    const py::ssize_t rows = close_info.shape[0];
    if (open_info.shape[0] != rows || high_info.shape[0] != rows ||
        low_info.shape[0] != rows || volume_info.shape[0] != rows) {
        throw std::invalid_argument("condition OHLCV 列數不一致");
    }
    if (types.size() != params.size() || types.size() != ma_kinds.size()) {
        throw std::invalid_argument("condition types/params/ma_kinds 數量不一致");
    }
    std::vector<stockbuild::ConditionRequest> requests;
    requests.reserve(types.size());
    for (std::size_t i = 0; i < types.size(); ++i) {
        requests.push_back({types[i], params[i], ma_kinds[i]});
    }
    std::unique_ptr<stockbuild::ConditionColumns> columns;
    {
        py::gil_scoped_release release;
        columns = std::make_unique<stockbuild::ConditionColumns>(
            stockbuild::calculate_conditions(
                {static_cast<const double*>(open_info.ptr), static_cast<std::size_t>(rows)},
                {static_cast<const double*>(high_info.ptr), static_cast<std::size_t>(rows)},
                {static_cast<const double*>(low_info.ptr), static_cast<std::size_t>(rows)},
                {static_cast<const double*>(close_info.ptr), static_cast<std::size_t>(rows)},
                {static_cast<const double*>(volume_info.ptr), static_cast<std::size_t>(rows)},
                requests));
    }
    stockbuild::ConditionColumns* raw = columns.release();
    py::capsule owner(raw, [](void* pointer) {
        delete static_cast<stockbuild::ConditionColumns*>(pointer);
    });
    py::list values;
    for (std::vector<std::uint8_t>& column : raw->values) {
        values.append(vector_view(column, owner));
    }
    py::dict result;
    result["rows"] = raw->rows;
    result["count"] = raw->values.size();
    result["values"] = values;
    return result;
}

py::dict strategy_runtime_native(
        const py::array& timestamps, const py::array& session_days,
        const py::array& close, const py::list& entry_values,
        const py::list& exit_values, bool short_direction,
        std::uint32_t quantity, const std::string& entry_logic,
        const std::string& exit_logic, double stop_loss_pct,
        double take_profit_pct, double stop_loss_abs, double take_profit_abs,
        std::uint32_t max_trades_per_day, double daily_loss_limit,
        double cooldown_seconds, const py::object& execution_price_object,
        std::size_t decision_start_row, const py::object& decision_end_row_object) {
    const py::buffer_info timestamp_info = require_column<std::int64_t>(timestamps, "timestamps");
    const py::buffer_info day_info = require_column<std::int64_t>(session_days, "session_days");
    const py::buffer_info close_info = require_column<double>(close, "close");
    const std::size_t rows = static_cast<std::size_t>(close_info.shape[0]);
    if (timestamp_info.shape[0] != close_info.shape[0] || day_info.shape[0] != close_info.shape[0]) {
        throw std::invalid_argument("strategy timestamps/day/close lengths differ");
    }
    py::array execution_price = execution_price_object.is_none()
        ? close : py::cast<py::array>(execution_price_object);
    const py::buffer_info execution_info =
        require_column<double>(execution_price, "execution_price");
    if (execution_info.shape[0] != close_info.shape[0]) {
        throw std::invalid_argument("strategy execution price length differs");
    }
    const std::size_t decision_end_row = decision_end_row_object.is_none()
        ? rows : py::cast<std::size_t>(decision_end_row_object);

    std::vector<py::array> arrays;
    arrays.reserve(entry_values.size() + exit_values.size());
    std::vector<std::span<const std::uint8_t>> entry_columns;
    std::vector<std::span<const std::uint8_t>> exit_columns;
    auto append_columns = [&](const py::list& source,
                              std::vector<std::span<const std::uint8_t>>& target,
                              const char* name) {
        target.reserve(source.size());
        for (const py::handle item : source) {
            arrays.push_back(py::cast<py::array>(item));
            const py::buffer_info info = require_column<std::uint8_t>(arrays.back(), name);
            if (static_cast<std::size_t>(info.shape[0]) != rows) {
                throw std::invalid_argument(std::string(name) + " length differs");
            }
            target.emplace_back(static_cast<const std::uint8_t*>(info.ptr), rows);
        }
    };
    append_columns(entry_values, entry_columns, "entry_condition");
    append_columns(exit_values, exit_columns, "exit_condition");

    const stockbuild::StrategyRuntimeConfig config{
        short_direction, quantity, entry_logic, exit_logic,
        stop_loss_pct, take_profit_pct, stop_loss_abs, take_profit_abs,
        max_trades_per_day, daily_loss_limit, cooldown_seconds,
    };
    std::unique_ptr<stockbuild::StrategyIntentColumns> columns;
    {
        py::gil_scoped_release release;
        columns = std::make_unique<stockbuild::StrategyIntentColumns>(
            stockbuild::evaluate_strategy_runtime(
                {static_cast<const std::int64_t*>(timestamp_info.ptr), rows},
                {static_cast<const std::int64_t*>(day_info.ptr), rows},
                {static_cast<const double*>(close_info.ptr), rows},
                {static_cast<const double*>(execution_info.ptr), rows},
                entry_columns, exit_columns, config,
                decision_start_row, decision_end_row));
    }
    stockbuild::StrategyIntentColumns* raw = columns.release();
    py::capsule owner(raw, [](void* pointer) {
        delete static_cast<stockbuild::StrategyIntentColumns*>(pointer);
    });
    py::dict result;
    result["rows"] = rows;
    result["kind"] = vector_view(raw->kind, owner);
    result["action"] = vector_view(raw->action, owner);
    result["quantity"] = vector_view(raw->quantity, owner);
    result["price"] = vector_view(raw->price, owner);
    result["reason"] = vector_view(raw->reason, owner);
    result["blocked_reason"] = vector_view(raw->blocked_reason, owner);
    result["state"] = vector_view(raw->state, owner);
    result["entry_price"] = vector_view(raw->entry_price, owner);
    result["realized_pnl_today"] = vector_view(raw->realized_pnl_today, owner);
    return result;
}

py::dict advanced_indicator_core_native(const py::array& high,
                                        const py::array& low,
                                        const py::array& close,
                                        int kdj_n,
                                        int kdj_m1,
                                        int kdj_m2,
                                        int dmi_n,
                                        int jae_a_period,
                                        int jae_j_n,
                                        int jae_j_m1,
                                        int jae_j_m2,
                                        int jae_e_period) {
    const py::buffer_info high_info = require_column<double>(high, "high");
    const py::buffer_info low_info = require_column<double>(low, "low");
    const py::buffer_info close_info = require_column<double>(close, "close");
    if (high_info.shape[0] != close_info.shape[0] ||
        low_info.shape[0] != close_info.shape[0]) {
        throw std::invalid_argument("high/low/close 列數不一致");
    }
    const std::size_t rows = static_cast<std::size_t>(close_info.shape[0]);
    std::unique_ptr<stockbuild::AdvancedIndicatorColumns> columns;
    {
        py::gil_scoped_release release;
        columns = std::make_unique<stockbuild::AdvancedIndicatorColumns>(
            stockbuild::calculate_advanced_indicators(
                {static_cast<const double*>(high_info.ptr), rows},
                {static_cast<const double*>(low_info.ptr), rows},
                {static_cast<const double*>(close_info.ptr), rows},
                kdj_n, kdj_m1, kdj_m2, dmi_n,
                jae_a_period, jae_j_n, jae_j_m1, jae_j_m2, jae_e_period));
    }
    stockbuild::AdvancedIndicatorColumns* raw = columns.release();
    py::capsule owner(raw, [](void* pointer) {
        delete static_cast<stockbuild::AdvancedIndicatorColumns*>(pointer);
    });
    py::dict result;
    result["rows"] = raw->rsv.size();
    result["rsv"] = vector_view(raw->rsv, owner);
    result["k"] = vector_view(raw->k, owner);
    result["d"] = vector_view(raw->d, owner);
    result["j"] = vector_view(raw->j, owner);
    result["plus_dm"] = vector_view(raw->plus_dm, owner);
    result["minus_dm"] = vector_view(raw->minus_dm, owner);
    result["plus_di"] = vector_view(raw->plus_di, owner);
    result["minus_di"] = vector_view(raw->minus_di, owner);
    result["adx"] = vector_view(raw->adx, owner);
    result["jae_a"] = vector_view(raw->jae_a, owner);
    result["jae_j"] = vector_view(raw->jae_j, owner);
    result["jae_e"] = vector_view(raw->jae_e, owner);
    return result;
}

}  // namespace

PYBIND11_MODULE(_stockbuild_native, module) {
    module.doc() = "StockBuild ADR-154 native KBar, indicator and backtest-shadow runtime core";
    module.def("abi_info", &abi_info);
    module.def("handshake", &handshake, py::arg("expected_abi"), py::arg("expected_schema"));
    module.def("inspect_kbars", &inspect_kbars,
               py::arg("timestamps"), py::arg("open"), py::arg("high"), py::arg("low"),
               py::arg("close"), py::arg("volume"), py::arg("flags"));
    module.def("echo_kbars", &echo_kbars,
               py::arg("timestamps"), py::arg("open"), py::arg("high"), py::arg("low"),
               py::arg("close"), py::arg("volume"), py::arg("flags"));
    module.def("sqlite_probe", &sqlite_probe,
               py::arg("database_path"), py::arg("sqlite_library_path"),
               py::arg("symbol"), py::arg("asset_type"), py::arg("timeframe"));
    module.def("sqlite_range", &sqlite_range,
               py::arg("database_path"), py::arg("sqlite_library_path"),
               py::arg("symbol"), py::arg("asset_type"), py::arg("timeframe"),
               py::arg("start_ts") = py::none(), py::arg("end_ts") = py::none());
    module.def("sqlite_range_query_plan", &sqlite_range_query_plan,
               py::arg("database_path"), py::arg("sqlite_library_path"),
               py::arg("symbol"), py::arg("asset_type"), py::arg("timeframe"),
               py::arg("start_ts") = py::none(), py::arg("end_ts") = py::none());
    module.def("resample_kbars", &resample_kbars_native,
               py::arg("timestamps"), py::arg("open"), py::arg("high"), py::arg("low"),
               py::arg("close"), py::arg("volume"), py::arg("flags"), py::arg("mode"),
               py::arg("interval_minutes") = 0,
               py::arg("timezone_offset_minutes") = 0,
               py::arg("session_basis") = "all");
    module.def("indicator_core", &indicator_core_native,
               py::arg("close"), py::arg("ma_period") = 20,
               py::arg("rsi_period") = 14, py::arg("macd_fast") = 12,
               py::arg("macd_slow") = 26, py::arg("macd_signal") = 9,
               py::arg("bb_period") = 20, py::arg("bb_std_up") = 2.0,
               py::arg("bb_std_down") = 2.0, py::arg("bb_ma_type") = "SMA");
    module.def("multi_ma_core", &multi_ma_core_native,
               py::arg("close"), py::arg("periods"), py::arg("kinds"));
    module.def("condition_core", &condition_core_native,
               py::arg("open"), py::arg("high"), py::arg("low"),
               py::arg("close"), py::arg("volume"), py::arg("types"),
               py::arg("params"), py::arg("ma_kinds"));
    module.def("strategy_runtime", &strategy_runtime_native,
               py::arg("timestamps"), py::arg("session_days"), py::arg("close"),
               py::arg("entry_values"), py::arg("exit_values"),
               py::arg("short_direction") = false, py::arg("quantity") = 1,
               py::arg("entry_logic") = "AND", py::arg("exit_logic") = "OR",
               py::arg("stop_loss_pct") = 0.0, py::arg("take_profit_pct") = 0.0,
               py::arg("stop_loss_abs") = 0.0, py::arg("take_profit_abs") = 0.0,
               py::arg("max_trades_per_day") = 0,
               py::arg("daily_loss_limit") = 0.0,
               py::arg("cooldown_seconds") = 0.0,
               py::arg("execution_price") = py::none(),
               py::arg("decision_start_row") = 0,
               py::arg("decision_end_row") = py::none());
    module.def("advanced_indicator_core", &advanced_indicator_core_native,
               py::arg("high"), py::arg("low"), py::arg("close"),
               py::arg("kdj_n") = 9, py::arg("kdj_m1") = 3,
               py::arg("kdj_m2") = 3, py::arg("dmi_n") = 14,
               py::arg("jae_a_period") = 14, py::arg("jae_j_n") = 9,
               py::arg("jae_j_m1") = 3, py::arg("jae_j_m2") = 3,
               py::arg("jae_e_period") = 60);
}
