#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

#include "stockbuild/kbar_schema.hpp"
#include "stockbuild/sqlite_reader.hpp"
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
    if (!info.strides.empty() && info.strides[0] != static_cast<py::ssize_t>(sizeof(T))) {
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

}  // namespace

PYBIND11_MODULE(_stockbuild_native, module) {
    module.doc() = "StockBuild ADR-145 native ABI and read-only SQLite foundation";
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
}
