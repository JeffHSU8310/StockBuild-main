# ADR-146：SQLite prepared range query 與 C++ 欄式 KBar buffer

- **日期**：2026-08-04
- **狀態**：range reader 與 buffer 已完成；產品路徑與 feature flag 尚未切換

## 背景

ADR-145 已驗證 MSVC x64、pybind11、KBar ABI v1、唯讀 SQLite probe 與
RAII 關閉。ADR-143 Phase 2 的下一個必要出口，是讓原生核心直接從 SQLite
指定範圍建立欄式 KBar buffer，避免 `sqlite3 rows → pandas → NumPy → C++`
的逐層複製與 Python 逐列物件成本。

## 本階段決策

1. Python `data/kbars_store.py` 繼續是唯一 writer 與 schema owner；C++ 每次讀取
   建立獨立的 read-only/query-only 短連線，不執行 migration、upsert 或 checkpoint。
2. 原生查詢只選 `ts, open, high, low, close, volume`，並依有無起訖邊界使用四個
   prepared SQL 版本；起訖皆為 inclusive，固定 `ORDER BY ts`，不可用 `SELECT *`。
3. 查詢條件固定以 `symbol, asset_type, timeframe` 為複合索引前綴，再接 `ts`
   range；另暴露只供診斷的 `EXPLAIN QUERY PLAN`，測試必須證明使用索引。
4. C++ 將 SQLite ISO-8601 timestamp 直接解析成 UTC `int64` nanoseconds，OHLCV
   直接填入五個 `double` vectors，flags 建立為 `uint32` 零值；不建立逐根 Python object。
5. vectors 由一個 capsule owner 管理，NumPy arrays 只建立 view；七個欄位共享同一
   owner，結果離開 Python wrapper 後仍依陣列 reference count 正確釋放。
6. Python bridge 先做 ABI/schema 握手、邊界正規化與欄位驗證，再回傳 typed
   `SqliteRangeResult`。錯 schema、錯 timestamp、缺檔或反向範圍必須明確失敗，
   不得用空資料冒充成功。
7. 讀取期間釋放 GIL。任何成功、空結果或例外路徑都必須 finalize statement、close
   connection，Windows 測試需能立即刪除 DB/WAL/SHM。

## 時間與範圍契約

- DB `ts` 仍沿用現有 ISO-8601 TEXT schema；range 比較與現行 Python reader 相同，
  使用 canonical `Timestamp.isoformat()` 字串。
- C++ parser 接受 `T` 或空白分隔、0～9 位小數秒、`Z` 或 `±HH:MM`；無時區值
  按現行 pandas `DatetimeIndex.asi8` 語意視為 UTC-like epoch。
- 混用不同 offset 表示同一時間時，TEXT 的字典序不保證等於時間序；資料寫入端仍
  應維持同商品／週期一致時區。若日後要徹底消除此限制，需另做整數 timestamp
  schema migration，不在本階段偷偷改庫。

## 驗收

1. 無界、單邊、雙邊與空範圍結果逐欄等於 Python authoritative load，邊界包含。
2. 結果 dtype、contiguous、列數與時間排序符合 KBar ABI；陣列不擁有資料但共享
   capsule owner，能再直接交給 native `inspect_kbars`。
3. `EXPLAIN QUERY PLAN` 命中 `idx_kbars_lookup` 或等價複合主鍵索引，不全表掃描。
4. writer 未提交資料不可被 reader 看見；讀取結束或錯誤後 Windows 可立即刪庫。
5. 1,000,000 根實機 benchmark 記錄吞吐與 Python 參考路徑；若未達 ADR-143 的
   3 倍門檻，照實保留為待優化，不因此切換產品路徑。
6. 一般 native suite、MSVC ASan suite 與既有完整驗證全過。

## 本階段不做

- 不切換 GUI、下載規劃、回測、策略、選股或送單正式路徑。
- 不新增 native cache/LRU；`data_version` 先隨結果回傳，cache invalidation 留待後續。
- 不在這張 ADR 同時實作 resampler、指標或 Qt renderer。

## 實作與驗證結果

- `_stockbuild_native` 0.2.0 已加入四種 prepared range query、原生 ISO-8601→UTC
  nanoseconds parser、七欄 SoA vectors、共享 capsule 的唯讀 NumPy views，以及獨立
  `EXPLAIN QUERY PLAN` 診斷。
- differential 涵蓋 inclusive boundaries、無界／單邊／空結果、商品 metadata 隔離、
  時區與 9 位小數秒、未提交 writer snapshot、錯 timestamp／型別／schema、缺檔、
  Windows 立即刪庫與突變檢查。
- 百萬根測試首次發現 pandas 3 對秒級 ISO 字串保留 microseconds resolution，直接
  `.asi8` 會違反 `timestamp_ns` 契約並相差 1,000 倍；Python bridge 已先固定
  `as_unit('ns')`，再與 C++ 全欄逐值相等。
- 2026-08-04 本機（Windows 10、CPython 3.14.5、MSVC 2022）百萬根 warm benchmark：
  native 中位數 625.802 ms、Python `sqlite3→pandas→NumPy` 2727.919 ms，提升
  4.359 倍、約 1,597,951 根／秒，超過 ADR-143 的 3 倍門檻；查詢計畫命中
  `idx_kbars_lookup`。完整報告存於
  `benchmarks/results/adr146_sqlite_range_20260804.json`。
- 一般與 MSVC ASan native suite 各 19 項全數通過。正式 GUI／下載／回測／策略／
  選股與送單仍使用 Python，因此上述結果不代表產品路徑已切換。
