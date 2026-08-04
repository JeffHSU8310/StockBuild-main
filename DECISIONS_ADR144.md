# ADR-144：C++／Qt 移植 Phase 0 黃金資料與基準合約

- **日期**：2026-08-04
- **狀態**：Phase 0 smoke／reference 已實作；full 長歷史實機基準待執行

## 背景

ADR-143 已核准以 C++ 作為資料、指標、策略、回測、最佳化與選股核心，並以
Qt 分階段取代主圖；但若沒有先凍結 Python 參考輸出，後續 native 路徑即使很快，
也可能在 T+1、成本、NaN warm-up、標點或選股未來函數上悄悄改變語意。

同理，效能數字若沒有硬體、Python、套件版本、資料大小與 fixture hash，就不能
判斷改善來自 C++、快取、資料縮水，還是換了一台電腦。

## 本階段範圍

1. 建立 deterministic synthetic OHLCV、策略、最佳化與全市場選股資料。
2. 凍結回測 `trades/equity/markers/metrics`、最佳化完整排名與選股結果／錯誤。
3. 建立 SQLite 完整命中、尾端增量、全新商品三種情境；記錄 API 計數、coverage、
   載入列數與 `EXPLAIN QUERY PLAN`。
4. 每個 component 與整份 bundle 使用 SHA-256；輸出以版本化 canonical JSON 保存。
5. 建立 `smoke`、`reference`、`full` 三種 profile。CI／一般開發跑 smoke；full 才使用
   ADR-143 的 100,000 根回測、500 組最佳化、2,000 檔 × 2,600 日 K 尺度。
6. 報告記錄作業系統、CPU、Python、NumPy、pandas、資料尺度、warm-up 與重複次數。

## 明確不做

- 不改 GUI、券商、下載或送單產品路徑。
- 不在本階段建立 C++ extension、Qt 視窗或 Strategy IR compiler。
- 不把 `smoke` 毫秒數冒充 `full` 效能門檻，也不以較短資料宣稱 native 達標。
- 不把 benchmark 產生的 SQLite、WAL、log 或含本機絕對路徑的檔案提交版本庫。

## 黃金比較規則

- dict key 排序；timestamp 使用 ISO-8601；float 固定八位小數；NaN／±Inf 使用明確字串。
- list 保留順序，因為交易、equity、markers、排名與選股顯示順序都是產品語意。
- 欄位缺少、多出、型別、長度或值不同，必須回報精確 JSONPath 並使測試失敗。
- 測試必須刻意改壞交易損益與選股結果，證明差異檢查不是空殼。

## SQLite 情境合約

Phase 0 fixture 以假 downloader 計數，不接觸網路：

| 情境 | 初始 DB | 預期下載呼叫 |
|---|---|---:|
| 完整命中 | 請求區間全在 SQLite | 0 |
| 尾端增量 | 尾端少一段 | 1（只補缺口） |
| 全新商品 | SQLite 無該商品 | 1（完整請求區間） |

這是 native SQL reader 的驗收合約，不表示目前 GUI 下載路徑已經完成零呼叫切換。
Python 仍是唯一 writer；Phase 1/2 的 C++ 僅允許 read-only/query-only。

## 出口條件

1. 固定 fixture 可重建相同 bundle hash，改壞任一關鍵欄位會失敗。
2. smoke 基準可在離線環境執行並輸出完整環境與 SQLite 三情境。
3. 完整測試不增加網路、tkinter、shioaji 依賴。
4. reference/full 實機報告如實標示是否執行；未跑不得宣稱 Phase 0 完成。
5. 通過審核後，下一筆 ADR 才能進入 Phase 1 原生工具鏈與 ABI 骨架。

## 本機基準紀錄（2026-08-04）

環境：Windows 10 19045、Intel Family 6 Model 165（12 logical CPUs）、
CPython 3.14.5、NumPy 2.4.6、pandas 3.0.3。以下皆為 warm-up 後 median：

| profile | 回測 | 最佳化 | 選股 | SQLite 三情境 |
|---|---:|---:|---:|---:|
| smoke（400 bars／4 組／12×90） | 206.64 ms | 356.11 ms | 26.26 ms | 134.16 ms |
| reference（5,000 bars／100 組／300×520） | 2,604.44 ms | 20,356.22 ms | 1,340.65 ms | 135.55 ms |

- smoke bundle SHA-256：`992478ff135d19723182f1fee9f6a32cdc8f343a70af03502028fb1148024fc4`
- reference bundle SHA-256：`9b7a94bcf897a35728d4eaca013fcf3ff182dc1f5e3e9692ec497104946062b5`
- SQLite range query 使用 `idx_kbars_lookup`；完整命中／尾端增量／新商品 API
  計數分別為 `0／1／1`，三者載入後資料 hash 完全相同。
- 第一次 smoke 執行曾因唯讀 `sqlite3.Connection` 未顯式 close 而在 Windows
  觸發 `WinError 32`；已改用 `closing()`，並加入離開暫存目錄即刪 DB 的回歸測試。
- `full` 尚未執行，所以目前不宣稱達成 100,000／500／2,000×2,600 尺度的
  Python 基準或 ADR-143 native 效能目標。
- 合併前離線驗證：`test_core` 807 項、`test_brokers` 43 項（0 略過）、
  `diag_repro_issues` 63 項、`diag_crossref` 與全專案 `py_compile` 全部通過；
  未執行真實券商登入、真實下單或 Qt／長歷史 full 實機測試。
