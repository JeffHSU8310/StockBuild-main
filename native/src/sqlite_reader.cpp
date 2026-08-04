#include "stockbuild/sqlite_reader.hpp"

#include <cstdint>
#include <cstring>
#include <filesystem>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>

#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#else
#include <dlfcn.h>
#endif

namespace stockbuild {
namespace {

struct sqlite3;
struct sqlite3_stmt;
using sqlite3_destructor_type = void (*)(void*);

inline constexpr int kSqliteOk = 0;
inline constexpr int kSqliteRow = 100;
inline constexpr int kSqliteDone = 101;
inline constexpr int kSqliteInteger = 1;
inline constexpr int kSqliteFloat = 2;
inline constexpr int kSqliteText = 3;
inline constexpr int kOpenReadonly = 0x00000001;
inline constexpr int kOpenUri = 0x00000040;
inline constexpr int kOpenNoMutex = 0x00008000;

class SqliteApi final {
public:
    explicit SqliteApi(const std::string& library_path) {
#ifdef _WIN32
        const std::filesystem::path path = library_path.empty()
            ? std::filesystem::path(L"sqlite3.dll")
            : std::filesystem::path(library_path);
        handle_ = LoadLibraryW(path.c_str());
        if (handle_ == nullptr) {
            throw std::runtime_error("無法載入 SQLite DLL: " + library_path);
        }
#else
        const char* path = library_path.empty() ? "libsqlite3.so.0" : library_path.c_str();
        handle_ = dlopen(path, RTLD_NOW | RTLD_LOCAL);
        if (handle_ == nullptr) {
            throw std::runtime_error(std::string("無法載入 SQLite library: ") + dlerror());
        }
#endif
        try {
            load(open_v2, "sqlite3_open_v2");
            load(close_v2, "sqlite3_close_v2");
            load(errmsg, "sqlite3_errmsg");
            load(prepare_v2, "sqlite3_prepare_v2");
            load(step, "sqlite3_step");
            load(finalize, "sqlite3_finalize");
            load(bind_text, "sqlite3_bind_text");
            load(column_int64, "sqlite3_column_int64");
            load(column_double, "sqlite3_column_double");
            load(column_text, "sqlite3_column_text");
            load(column_type, "sqlite3_column_type");
            load(busy_timeout, "sqlite3_busy_timeout");
            load(db_readonly, "sqlite3_db_readonly");
        } catch (...) {
            unload();
            throw;
        }
    }

    ~SqliteApi() { unload(); }
    SqliteApi(const SqliteApi&) = delete;
    SqliteApi& operator=(const SqliteApi&) = delete;

    int (*open_v2)(const char*, sqlite3**, int, const char*){nullptr};
    int (*close_v2)(sqlite3*){nullptr};
    const char* (*errmsg)(sqlite3*){nullptr};
    int (*prepare_v2)(sqlite3*, const char*, int, sqlite3_stmt**, const char**){nullptr};
    int (*step)(sqlite3_stmt*){nullptr};
    int (*finalize)(sqlite3_stmt*){nullptr};
    int (*bind_text)(sqlite3_stmt*, int, const char*, int, sqlite3_destructor_type){nullptr};
    std::int64_t (*column_int64)(sqlite3_stmt*, int){nullptr};
    double (*column_double)(sqlite3_stmt*, int){nullptr};
    const unsigned char* (*column_text)(sqlite3_stmt*, int){nullptr};
    int (*column_type)(sqlite3_stmt*, int){nullptr};
    int (*busy_timeout)(sqlite3*, int){nullptr};
    int (*db_readonly)(sqlite3*, const char*){nullptr};

private:
    template <typename T>
    void load(T& target, const char* name) {
#ifdef _WIN32
        const FARPROC symbol = GetProcAddress(handle_, name);
        if (symbol == nullptr) {
            throw std::runtime_error(std::string("SQLite DLL 缺少符號: ") + name);
        }
        target = reinterpret_cast<T>(symbol);
#else
        void* symbol = dlsym(handle_, name);
        if (symbol == nullptr) {
            throw std::runtime_error(std::string("SQLite library 缺少符號: ") + name);
        }
        static_assert(sizeof(target) == sizeof(symbol));
        std::memcpy(&target, &symbol, sizeof(target));
#endif
    }

    void unload() noexcept {
#ifdef _WIN32
        if (handle_ != nullptr) {
            FreeLibrary(handle_);
            handle_ = nullptr;
        }
#else
        if (handle_ != nullptr) {
            dlclose(handle_);
            handle_ = nullptr;
        }
#endif
    }

#ifdef _WIN32
    HMODULE handle_{nullptr};
#else
    void* handle_{nullptr};
#endif
};

class Connection final {
public:
    Connection(SqliteApi& api, const std::string& path) : api_(api) {
        const int flags = kOpenReadonly | kOpenUri | kOpenNoMutex;
        const int rc = api_.open_v2(path.c_str(), &db_, flags, nullptr);
        if (rc != kSqliteOk) {
            const std::string message = db_ != nullptr ? api_.errmsg(db_) : "unknown error";
            if (db_ != nullptr) {
                api_.close_v2(db_);
                db_ = nullptr;
            }
            throw std::runtime_error("SQLite read-only open 失敗: " + message);
        }
        if (api_.busy_timeout(db_, 2'000) != kSqliteOk) {
            api_.close_v2(db_);
            db_ = nullptr;
            throw std::runtime_error("SQLite busy_timeout 設定失敗");
        }
    }

    ~Connection() {
        if (db_ != nullptr) {
            api_.close_v2(db_);
            db_ = nullptr;
        }
    }
    Connection(const Connection&) = delete;
    Connection& operator=(const Connection&) = delete;
    sqlite3* get() const noexcept { return db_; }

private:
    SqliteApi& api_;
    sqlite3* db_{nullptr};
};

class Statement final {
public:
    Statement(SqliteApi& api, sqlite3* db, const char* sql) : api_(api), db_(db) {
        const int rc = api_.prepare_v2(db_, sql, -1, &statement_, nullptr);
        if (rc != kSqliteOk) {
            if (statement_ != nullptr) {
                api_.finalize(statement_);
                statement_ = nullptr;
            }
            throw std::runtime_error(std::string("SQLite prepare 失敗: ") + api_.errmsg(db_));
        }
    }
    ~Statement() {
        if (statement_ != nullptr) {
            api_.finalize(statement_);
            statement_ = nullptr;
        }
    }
    Statement(const Statement&) = delete;
    Statement& operator=(const Statement&) = delete;
    sqlite3_stmt* get() const noexcept { return statement_; }

    void bind(int index, const std::string& value) {
        if (api_.bind_text(statement_, index, value.c_str(), -1, nullptr) != kSqliteOk) {
            throw std::runtime_error(std::string("SQLite bind 失敗: ") + api_.errmsg(db_));
        }
    }

private:
    SqliteApi& api_;
    sqlite3* db_{nullptr};
    sqlite3_stmt* statement_{nullptr};
};

std::int64_t scalar_int(SqliteApi& api, sqlite3* db, const char* sql) {
    Statement statement(api, db, sql);
    const int rc = api.step(statement.get());
    if (rc != kSqliteRow) {
        throw std::runtime_error(std::string("SQLite scalar query 失敗: ") + api.errmsg(db));
    }
    return api.column_int64(statement.get(), 0);
}

void execute_pragma(SqliteApi& api, sqlite3* db, const char* sql) {
    Statement statement(api, db, sql);
    const int rc = api.step(statement.get());
    if (rc != kSqliteDone && rc != kSqliteRow) {
        throw std::runtime_error(std::string("SQLite PRAGMA 失敗: ") + api.errmsg(db));
    }
}

std::string column_string(SqliteApi& api, sqlite3_stmt* statement, int column) {
    const unsigned char* value = api.column_text(statement, column);
    return value == nullptr ? std::string() : std::string(reinterpret_cast<const char*>(value));
}

int parse_digits(std::string_view value, std::size_t offset, std::size_t count) {
    if (offset + count > value.size()) {
        throw std::runtime_error("SQLite timestamp 長度不足: " + std::string(value));
    }
    int result = 0;
    for (std::size_t i = 0; i < count; ++i) {
        const char ch = value[offset + i];
        if (ch < '0' || ch > '9') {
            throw std::runtime_error("SQLite timestamp 格式錯誤: " + std::string(value));
        }
        result = result * 10 + (ch - '0');
    }
    return result;
}

bool is_leap_year(int year) noexcept {
    return (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
}

int days_in_month(int year, int month) noexcept {
    static constexpr int days[]{31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    return month == 2 && is_leap_year(year) ? 29 : days[month - 1];
}

std::int64_t days_from_civil(int year, unsigned month, unsigned day) noexcept {
    year -= month <= 2U;
    const int era = (year >= 0 ? year : year - 399) / 400;
    const unsigned year_of_era = static_cast<unsigned>(year - era * 400);
    const unsigned adjusted_month = month > 2U ? month - 3U : month + 9U;
    const unsigned day_of_year =
        (153U * adjusted_month + 2U) / 5U + day - 1U;
    const unsigned day_of_era =
        year_of_era * 365U + year_of_era / 4U - year_of_era / 100U + day_of_year;
    return static_cast<std::int64_t>(era) * 146097 +
           static_cast<std::int64_t>(day_of_era) - 719468;
}

std::int64_t parse_timestamp_ns(const std::string& text) {
    const std::string_view value(text);
    if (value.size() < 19 || value[4] != '-' || value[7] != '-' ||
        (value[10] != 'T' && value[10] != ' ') || value[13] != ':' || value[16] != ':') {
        throw std::runtime_error("SQLite timestamp 格式錯誤: " + text);
    }
    const int year = parse_digits(value, 0, 4);
    const int month = parse_digits(value, 5, 2);
    const int day = parse_digits(value, 8, 2);
    const int hour = parse_digits(value, 11, 2);
    const int minute = parse_digits(value, 14, 2);
    const int second = parse_digits(value, 17, 2);
    if (month < 1 || month > 12 || day < 1 || day > days_in_month(year, month) ||
        hour > 23 || minute > 59 || second > 59) {
        throw std::runtime_error("SQLite timestamp 值超出範圍: " + text);
    }

    std::size_t cursor = 19;
    std::int64_t fractional_ns = 0;
    if (cursor < value.size() && value[cursor] == '.') {
        ++cursor;
        const std::size_t fraction_start = cursor;
        while (cursor < value.size() && value[cursor] >= '0' && value[cursor] <= '9') {
            if (cursor - fraction_start >= 9) {
                throw std::runtime_error("SQLite timestamp 小數秒超過 9 位: " + text);
            }
            fractional_ns = fractional_ns * 10 + (value[cursor] - '0');
            ++cursor;
        }
        const std::size_t fraction_digits = cursor - fraction_start;
        if (fraction_digits == 0) {
            throw std::runtime_error("SQLite timestamp 小數秒為空: " + text);
        }
        for (std::size_t i = fraction_digits; i < 9; ++i) {
            fractional_ns *= 10;
        }
    }

    int offset_seconds = 0;
    if (cursor < value.size()) {
        if ((value[cursor] == 'Z' || value[cursor] == 'z') && cursor + 1 == value.size()) {
            ++cursor;
        } else if (value[cursor] == '+' || value[cursor] == '-') {
            const int sign = value[cursor] == '+' ? 1 : -1;
            ++cursor;
            if (cursor + 5 != value.size() || value[cursor + 2] != ':') {
                throw std::runtime_error("SQLite timestamp 時區格式錯誤: " + text);
            }
            const int offset_hour = parse_digits(value, cursor, 2);
            const int offset_minute = parse_digits(value, cursor + 3, 2);
            if (offset_hour > 23 || offset_minute > 59) {
                throw std::runtime_error("SQLite timestamp 時區超出範圍: " + text);
            }
            offset_seconds = sign * (offset_hour * 3600 + offset_minute * 60);
            cursor += 5;
        } else {
            throw std::runtime_error("SQLite timestamp 尾端格式錯誤: " + text);
        }
    }
    if (cursor != value.size()) {
        throw std::runtime_error("SQLite timestamp 尾端格式錯誤: " + text);
    }

    const std::int64_t seconds =
        days_from_civil(year, static_cast<unsigned>(month), static_cast<unsigned>(day)) * 86400 +
        hour * 3600 + minute * 60 + second - offset_seconds;
    constexpr std::int64_t billion = 1'000'000'000;
    if (seconds > (std::numeric_limits<std::int64_t>::max() - fractional_ns) / billion ||
        seconds < std::numeric_limits<std::int64_t>::min() / billion) {
        throw std::runtime_error("SQLite timestamp 超出 int64 nanoseconds 範圍: " + text);
    }
    return seconds * billion + fractional_ns;
}

const char* range_sql(bool has_start, bool has_end, bool explain) noexcept {
    if (explain) {
        if (has_start && has_end) {
            return "EXPLAIN QUERY PLAN SELECT ts, open, high, low, close, volume FROM kbars "
                   "WHERE symbol=? AND asset_type=? AND timeframe=? AND ts>=? AND ts<=? ORDER BY ts";
        }
        if (has_start) {
            return "EXPLAIN QUERY PLAN SELECT ts, open, high, low, close, volume FROM kbars "
                   "WHERE symbol=? AND asset_type=? AND timeframe=? AND ts>=? ORDER BY ts";
        }
        if (has_end) {
            return "EXPLAIN QUERY PLAN SELECT ts, open, high, low, close, volume FROM kbars "
                   "WHERE symbol=? AND asset_type=? AND timeframe=? AND ts<=? ORDER BY ts";
        }
        return "EXPLAIN QUERY PLAN SELECT ts, open, high, low, close, volume FROM kbars "
               "WHERE symbol=? AND asset_type=? AND timeframe=? ORDER BY ts";
    }
    if (has_start && has_end) {
        return "SELECT ts, open, high, low, close, volume FROM kbars "
               "WHERE symbol=? AND asset_type=? AND timeframe=? AND ts>=? AND ts<=? ORDER BY ts";
    }
    if (has_start) {
        return "SELECT ts, open, high, low, close, volume FROM kbars "
               "WHERE symbol=? AND asset_type=? AND timeframe=? AND ts>=? ORDER BY ts";
    }
    if (has_end) {
        return "SELECT ts, open, high, low, close, volume FROM kbars "
               "WHERE symbol=? AND asset_type=? AND timeframe=? AND ts<=? ORDER BY ts";
    }
    return "SELECT ts, open, high, low, close, volume FROM kbars "
           "WHERE symbol=? AND asset_type=? AND timeframe=? ORDER BY ts";
}

void bind_range(Statement& statement,
                const std::string& symbol,
                const std::string& asset_type,
                const std::string& timeframe,
                const std::optional<std::string>& start_ts,
                const std::optional<std::string>& end_ts) {
    statement.bind(1, symbol);
    statement.bind(2, asset_type);
    statement.bind(3, timeframe);
    int index = 4;
    if (start_ts) {
        statement.bind(index++, *start_ts);
    }
    if (end_ts) {
        statement.bind(index, *end_ts);
    }
}

void require_numeric(SqliteApi& api, sqlite3_stmt* statement, int column,
                     const char* name) {
    const int type = api.column_type(statement, column);
    if (type != kSqliteInteger && type != kSqliteFloat) {
        throw std::runtime_error(std::string("SQLite KBar ") + name + " 必須是數值");
    }
}

}  // namespace

SqliteProbe probe_sqlite(const std::string& database_path,
                         const std::string& sqlite_library_path,
                         const std::string& symbol,
                         const std::string& asset_type,
                         const std::string& timeframe) {
    SqliteApi api(sqlite_library_path);
    Connection connection(api, database_path);
    sqlite3* db = connection.get();

    execute_pragma(api, db, "PRAGMA query_only=ON");
    SqliteProbe result;
    result.readonly = api.db_readonly(db, "main") == 1;
    result.query_only = scalar_int(api, db, "PRAGMA query_only") == 1;
    result.schema_version = scalar_int(api, db, "PRAGMA schema_version");
    result.data_version = scalar_int(api, db, "PRAGMA data_version");

    Statement statement(api, db,
        "SELECT MIN(ts), MAX(ts), COUNT(*) FROM kbars "
        "WHERE symbol=? AND asset_type=? AND timeframe=?");
    statement.bind(1, symbol);
    statement.bind(2, asset_type);
    statement.bind(3, timeframe);
    const int rc = api.step(statement.get());
    if (rc != kSqliteRow) {
        throw std::runtime_error(std::string("SQLite coverage query 失敗: ") + api.errmsg(db));
    }
    result.first_ts = column_string(api, statement.get(), 0);
    result.last_ts = column_string(api, statement.get(), 1);
    result.count = api.column_int64(statement.get(), 2);
    return result;
}

KBarColumns read_kbars_range(const std::string& database_path,
                             const std::string& sqlite_library_path,
                             const std::string& symbol,
                             const std::string& asset_type,
                             const std::string& timeframe,
                             const std::optional<std::string>& start_ts,
                             const std::optional<std::string>& end_ts) {
    SqliteApi api(sqlite_library_path);
    Connection connection(api, database_path);
    sqlite3* db = connection.get();
    execute_pragma(api, db, "PRAGMA query_only=ON");

    KBarColumns result;
    result.readonly = api.db_readonly(db, "main") == 1;
    result.query_only = scalar_int(api, db, "PRAGMA query_only") == 1;
    result.schema_version = scalar_int(api, db, "PRAGMA schema_version");
    result.data_version = scalar_int(api, db, "PRAGMA data_version");

    Statement statement(api, db, range_sql(start_ts.has_value(), end_ts.has_value(), false));
    bind_range(statement, symbol, asset_type, timeframe, start_ts, end_ts);
    constexpr std::size_t initial_reserve = 4096;
    result.timestamps.reserve(initial_reserve);
    result.open.reserve(initial_reserve);
    result.high.reserve(initial_reserve);
    result.low.reserve(initial_reserve);
    result.close.reserve(initial_reserve);
    result.volume.reserve(initial_reserve);
    result.flags.reserve(initial_reserve);

    int rc = kSqliteRow;
    while ((rc = api.step(statement.get())) == kSqliteRow) {
        if (api.column_type(statement.get(), 0) != kSqliteText) {
            throw std::runtime_error("SQLite KBar ts 必須是 ISO-8601 TEXT");
        }
        for (int column = 1; column <= 5; ++column) {
            static constexpr const char* names[]{"open", "high", "low", "close", "volume"};
            require_numeric(api, statement.get(), column, names[column - 1]);
        }
        result.timestamps.push_back(parse_timestamp_ns(column_string(api, statement.get(), 0)));
        result.open.push_back(api.column_double(statement.get(), 1));
        result.high.push_back(api.column_double(statement.get(), 2));
        result.low.push_back(api.column_double(statement.get(), 3));
        result.close.push_back(api.column_double(statement.get(), 4));
        result.volume.push_back(api.column_double(statement.get(), 5));
        result.flags.push_back(0U);
    }
    if (rc != kSqliteDone) {
        throw std::runtime_error(std::string("SQLite range query 失敗: ") + api.errmsg(db));
    }
    return result;
}

std::string explain_kbars_range(const std::string& database_path,
                                const std::string& sqlite_library_path,
                                const std::string& symbol,
                                const std::string& asset_type,
                                const std::string& timeframe,
                                const std::optional<std::string>& start_ts,
                                const std::optional<std::string>& end_ts) {
    SqliteApi api(sqlite_library_path);
    Connection connection(api, database_path);
    sqlite3* db = connection.get();
    execute_pragma(api, db, "PRAGMA query_only=ON");
    Statement statement(api, db, range_sql(start_ts.has_value(), end_ts.has_value(), true));
    bind_range(statement, symbol, asset_type, timeframe, start_ts, end_ts);

    std::ostringstream plan;
    bool first = true;
    int rc = kSqliteRow;
    while ((rc = api.step(statement.get())) == kSqliteRow) {
        if (!first) {
            plan << '\n';
        }
        first = false;
        plan << column_string(api, statement.get(), 3);
    }
    if (rc != kSqliteDone) {
        throw std::runtime_error(std::string("SQLite EXPLAIN QUERY PLAN 失敗: ") + api.errmsg(db));
    }
    return plan.str();
}

}  // namespace stockbuild
