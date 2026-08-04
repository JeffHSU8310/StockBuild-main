#pragma once

#include <cstdint>
#include <string>

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

SqliteProbe probe_sqlite(const std::string& database_path,
                         const std::string& sqlite_library_path,
                         const std::string& symbol,
                         const std::string& asset_type,
                         const std::string& timeframe);

}  // namespace stockbuild
