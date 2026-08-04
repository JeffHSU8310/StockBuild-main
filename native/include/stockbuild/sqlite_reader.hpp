#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace stockbuild {

struct SqliteProbe final {
    bool readonly{false};
    bool query_only{false};
    std::int64_t schema_version{0};
    std::int64_t data_version{0};
    std::string first_ts;
    std::string last_ts;
    std::int64_t count{0};
};

struct KBarColumns final {
    bool readonly{false};
    bool query_only{false};
    std::int64_t schema_version{0};
    std::int64_t data_version{0};
    std::vector<std::int64_t> timestamps;
    std::vector<double> open;
    std::vector<double> high;
    std::vector<double> low;
    std::vector<double> close;
    std::vector<double> volume;
    std::vector<std::uint32_t> flags;
};

SqliteProbe probe_sqlite(const std::string& database_path,
                         const std::string& sqlite_library_path,
                         const std::string& symbol,
                         const std::string& asset_type,
                         const std::string& timeframe);

KBarColumns read_kbars_range(const std::string& database_path,
                             const std::string& sqlite_library_path,
                             const std::string& symbol,
                             const std::string& asset_type,
                             const std::string& timeframe,
                             const std::optional<std::string>& start_ts,
                             const std::optional<std::string>& end_ts);

std::string explain_kbars_range(const std::string& database_path,
                                const std::string& sqlite_library_path,
                                const std::string& symbol,
                                const std::string& asset_type,
                                const std::string& timeframe,
                                const std::optional<std::string>& start_ts,
                                const std::optional<std::string>& end_ts);

}  // namespace stockbuild
