#include "stockbuild/sqlite_reader.hpp"

#include <cstdint>
#include <cstring>
#include <filesystem>
#include <stdexcept>
#include <string>
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
            load(column_text, "sqlite3_column_text");
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
    const unsigned char* (*column_text)(sqlite3_stmt*, int){nullptr};
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

}  // namespace stockbuild
