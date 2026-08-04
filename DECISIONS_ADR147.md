# ADR-147：C++ KBar Resampler 與第一批指標核心

- **日期**：2026-08-04
- **狀態**：已完成；shadow API，產品路徑尚未切換

## 背景

ADR-146 已讓 C++ 從 SQLite 直接建立欄式 KBar buffers。下一步要證明同一批 buffers
能在不回到 pandas 的情況下完成多週期 OHLCV 聚合與常用指標，且時間分組、warm-up、
NaN 與浮點語意仍能逐欄對上現行 Python authoritative implementation。

## 本階段範圍

1. `KBarColumns` 從 SQLite reader 專屬型別提升為 native 共用資料型別。
2. C++ resampler 支援：
   - 固定 N 分鐘／小時，bucket 為左閉右開並以 bucket 起點標記；
   - 股票／指數自然日、週一為左界的週、月初為左界的月；
   - 期貨近全交易日與只取日盤兩種口徑，再聚合為日／週／月；
   - Open=first、High=max、Low=min、Close=last、Volume=sum、flags=bitwise OR。
3. 第一批 C++ indicator core：SMA、EMA、WMA、RSI、MACD/Signal/Hist、
   Bollinger MID/STD/UPPER/LOWER/WIDTH。
4. pybind11 熱路徑只接收既有 contiguous arrays，計算期間釋放 GIL；輸出 vectors
   由 capsule 管理並以唯讀 NumPy views 回 Python。
5. `core/native_bridge.py` 只做 ABI、參數、dtype、stride 與結果驗證，不放交易規則，
   不把 native 結果靜默當成正式產品輸出。

## 時間契約

- `timestamp_ns` 是 ABI 的 int64 nanoseconds。resampler 另收明確
  `timezone_offset_minutes`，用於把絕對時間轉成商品所在市場的 wall clock。
- 目前 StockBuild 的 timezone-naive DataFrame 以 raw wall-clock nanoseconds 傳入時，
  offset 必須是 `0`；真正 UTC timestamp 要聚合台灣時段時傳 `480`。不得猜測時區。
- 固定週期與 calendar bucket 都採左閉右開；期貨 `all` 依既有 ADR-007：當地時間
  分鐘值 >13:45 歸下一交易日，`day` 只保留 08:45～13:45（含邊界）。
- 輸入 timestamp 必須嚴格遞增；重複或倒序明確失敗，避免 first/last 語意不確定。

## 指標語意

- SMA/WMA 與 Bollinger STD 在完整 window 前輸出 quiet NaN。
- EMA 使用 pandas `ewm(span=p, adjust=False)` seed 與遞迴；MACD 由兩條 EMA 相減，
  Signal 再用相同規則遞迴。
- RSI 使用現行 Wilder `ewm(com=p-1, adjust=False)`；第一根 NaN，零 loss 為 100，
  零 gain 為 0，同時為零則 NaN。
- Bollinger STD 使用 pandas rolling sample standard deviation（`ddof=1`）；中線可選
  SMA／EMA／WMA，上下 σ 分開，WIDTH 公式沿用現行 Python。
- 跨語言比對要求 NaN mask 完全一致；SMA／EMA／WMA／RSI／MACD 採
  `rtol=2e-10, atol=2e-10`，以逐視窗 Welford 計算的 STD 與其衍生 Bollinger
  欄位採 `rtol=1e-8, atol=1e-9`。後者避免長序列滑動累積誤差，容許的僅是
  pandas 與 C++ 浮點歸約順序差異，不容許 warm-up 或公式語意差異。
- KBar storage schema 不允許 NULL；第一批 native API 對非 finite OHLCV 明確拒絕，
  不用一份看似成功但不確定的缺值傳播冒充等價。

## 驗收

1. 固定分鐘、自然日／週／月、期貨 all/day 日／週／月逐欄等於 pandas／
   `core.futures_session`，包含 13:45 邊界、夜盤、缺 bucket 與 flags。
2. 指標使用測試內獨立公式作 differential，不以被測的 `core.indicators` 自己當基準；
   刻意改壞一個 native 值時 assertion 必須轉紅。
3. 空資料、短資料、非法期間、倒序／重複 timestamp、非 finite KBar 全部可預期處理。
4. 所有輸出 dtype、contiguous、readonly、owner lifetime 與 ABI 列數一致。
5. 百萬根 resample 與 indicator benchmark 記錄同機 Python 比較；效能未達標照實記錄。
6. Release、MSVC ASan、既有完整測試及合併後 main 回歸全過。

## 明確不做

- 本 ADR 不切換 GUI、下載、策略、回測、選股或真正送單路徑。
- KDJ、DMI、JAE、型態、volume profile、fibonacci、chips features 留給後續 Phase 2
  批次；未完成前 `native_indicators` feature flag 維持關閉。
- 不加入 cache／平行執行；先鎖定單執行緒數值等價與資料所有權。

## 實作與驗證結果

- `_stockbuild_native` 升至 0.3.0；resampler 與 indicator 計算期間釋放 GIL，
  回傳由 C++ owner capsule 維持生命週期的 contiguous、readonly NumPy views。
- 1,000,000 根一分 K 同機測試：5 分 resample 中位數 39.469 ms（約
  25,336,404 input rows/s、為 pandas 1.202 倍）；第一批指標中位數
  136.637 ms（約 7,318,689 rows/s、為 pandas 25.674 倍）。原始報告固定於
  `benchmarks/results/adr147_resampler_indicators_20260804.json`。
- 百萬筆 NaN mask 完全一致；最大絕對誤差為 Bollinger WIDTH 約 `1.429e-9`、
  上線約 `1.301e-9`、STD 約 `6.492e-10`，均在本 ADR 明定容許誤差內。
- Release 與 MSVC ASan native suite 各 27 項通過；完整 Python core 811 項、
  brokers 43 項、診斷 63 項、crossref 與 68 個 Python 檔案 `py_compile` 全過。

## 實機尚待驗證

- 目前是 shadow API，沒有 GUI 操作被替換，因此本階段沒有可要求使用者點按的新增
  畫面；仍須在後續正式切換前，以實際長歷史股票／期貨資料涵蓋休市、夜盤、跨月，
  跑一次 Python/C++ shadow differential。
- 真實券商登入、報價、回測產品路徑與下單未執行；這些不屬於本 ADR，且不得因
  native 單元測試通過而視為已驗證。
