# ADR-145：Phase 1 原生工具鏈、KBar ABI 與 SQLite 唯讀骨架

- **日期**：2026-08-04
- **狀態**：Phase 1 基礎完成；產品路徑尚未切換，MSVC ASan 實機待可支援工具鏈

## 背景

ADR-144 已建立 Python authoritative golden bundle 與 smoke/reference 基準。進入
ADR-143 Phase 1 前，必須先證明官方 Python 3.14 可載入原生模組、跨語言欄位布局
可被機器檢查、版本不相容會拒載，而且 C++ SQLite 探測不會留下 Windows 檔案鎖。

## 本階段決策

1. 正式 Windows ABI 使用 MSVC x64；MinGW 不用來編譯官方 CPython 的 `.pyd`。
2. CMake + Ninja + pybind11 建立 `_stockbuild_native`；建置產物不進版控。
3. KBar ABI v1 固定為標準布局 `KBarRecord`：`timestamp_ns`、OHLCV、`flags`、
   `reserved`。C++ `static_assert` 與 Python offset/dtype 測試使用同一份版本握手。
4. Python→C++ 採七個一維 contiguous NumPy columns 批次傳入；拒絕 dtype、維度、
   stride、列數或 ABI 版本錯誤，不做逐根 Python object 轉換。
5. `core/native_bridge.py` 只負責載入、版本／schema 驗證與 DataFrame→column buffer；
   不放策略、成交、風控或 fallback 決策。
6. SQLite adapter 在 Phase 1 只做 read-only/query-only probe、schema/data version、
   coverage 與 RAII 關閉；Phase 2 才將 prepared range query 轉為 KBar buffer。
7. SQLite C API 以動態載入處理，Windows 明確使用 Python 發行版的 `DLLs/sqlite3.dll`；
   不新增第二個 SQLite writer 或把 Python schema migration 搬進 C++。
8. native 缺失可由尚未啟用的產品路徑保持 Python 現況；但 native 已存在而 ABI／
   schema 不符時必須明確失敗，不得靜默當成可用。

## 本階段不做

- 不移植指標、策略、回測、最佳化或選股演算法。
- 不切換 GUI、下載、量化 runner、broker 或真正送單路徑。
- 不建立 Qt 視窗，也不宣稱 C++ 已帶來產品加速。
- 不提交 `.pyd`、CMake cache、SQLite DB 或本機絕對建置路徑。

## 驗收

1. 乾淨 build directory 可 configure／compile／CTest／import。
2. ABI size、alignment、offset、dtype 與版本握手雙端一致；改錯版本測試轉紅。
3. 1,000,000 根 columns 可一次傳入 C++，回傳 view 與原陣列共享記憶體；Python
   呼叫端不存在逐根迴圈。
4. 非 contiguous、錯 dtype、列數不一致全部拒絕。
5. C++ SQLite probe 為 read-only/query-only，coverage 正確；probe 完成後 Windows
   能立刻刪除 DB／WAL／SHM，不殘留連線鎖。
6. CMake 提供 warnings-as-errors 與 ASan／UBSan（非 MSVC）或 ASan（MSVC）選項。
7. 既有完整測試與新增 native suite 全過；真實送單不屬於本階段驗證。

## 實作與驗證結果

- 已建立 MSVC x64／CMake／Ninja／pybind11 原生模組、KBar ABI v1、Python
  `native_bridge` 與 C++ SQLite read-only/query-only RAII reader。
- `tests/test_native.py` 會從乾淨目錄建置並執行 CTest，涵蓋 ABI／錯版拒載、dtype／
  stride／列數拒絕、1,000,000 根批次與 zero-copy、SQLite coverage 及 Windows 立即刪庫。
- 2026-08-04 本機 benchmark（1,000,000 根）中，DataFrame 轉欄式陣列中位數
  11.658 ms、C++ scan 2.482 ms、zero-copy echo 0.005 ms、SQLite 唯讀 probe
  2.251 ms；結果存於 `benchmarks/results/adr145_native_boundary_20260804.json`。
- CMake 已提供 `STOCKBUILD_ENABLE_SANITIZERS`。本機 MSVC 可編譯 `/fsanitize=address`，
  但連結器缺 `clang_rt.asan_dynamic_runtime_thunk-x86_64.lib`；Visual Studio Installer
  加裝 ASan component 回傳 5007（主機不符合該元件需求），因此不得宣稱 ASan 已通過。
- 本階段未切換 GUI、回測、策略、選股、下載或送單；Python 仍是正式產品路徑。
