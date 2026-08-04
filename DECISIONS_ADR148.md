# ADR-148：C++ KDJ／DMI／JAE 與實際 SQLite shadow differential

- **日期**：2026-08-04
- **狀態**：已完成；shadow API，產品路徑尚未切換

## 背景

ADR-147 已完成 resampler 與 SMA／EMA／WMA／RSI／MACD／Bollinger 第一批核心。
Phase 2 下一批先處理會共同使用 High／Low／Close、rolling extrema 與帶 NaN EWM
語意的 KDJ、DMI、JAE，並把 differential 從 synthetic 擴大到本機實際 SQLite
長歷史資料。

## 決策

1. 新增獨立 advanced indicator API，不改 ADR-147 既有 close-only API 簽名。
2. C++ 輸出 KDJ 的 RSV／K／D／J、DMI 的 +DM／-DM／+DI／-DI／ADX，以及
   JAE 的 A／J／E。
3. KDJ rolling low/high 使用 O(n) monotonic deque；RSV 完整 window 前為 NaN，
   window 高低差為零時也為 NaN。
4. K、D、ADX 與 JAE E 必須仿 pandas `ewm(..., adjust=False, ignore_na=False)`；
   不可把 leading 或 internal NaN 當零。
5. DMI 沿用現行 Python：DM 方向比較、TR 三者最大值、ATR／DM／ADX 都使用
   `ewm(span=n, adjust=False)`，不改成另一種常見 Wilder 版本。
6. JAE 定義維持 ADR-134：A 是 RSI、J 是可獨立設定參數的 KDJ J、E 是 A 的
   `ewm(span=e_period, adjust=False)`；趨勢文字與交叉判斷仍留在 Python，因為不是
   百萬根計算熱點。
7. 實際 SQLite shadow runner 只做 read-only range load 與匿名化數值摘要；不得寫入
   DB，也不記錄帳號、API key 或完整策略內容。

## 驗收

- 使用測試內獨立 pandas 公式逐欄比對，不呼叫被測的 `core.indicators`／`core.jae`。
- 覆蓋 leading／internal NaN、零 range、空／短資料、非法參數、非 finite OHLC、
  readonly owner lifetime 與突變測試。
- 百萬根 synthetic benchmark 記錄 C++／pandas 時間及逐欄最大誤差。
- 對本機實際 0050 與 TXFR1 長歷史 SQLite 執行 shadow differential。
- Release、MSVC ASan 與專案完整回歸合併前後全過。

## 明確不做

- 不切換 GUI、策略、回測、選股或送單路徑，`native_indicators` 仍維持關閉。
- 型態、volume profile、fibonacci、chips features 留在後續 Phase 2 批次。
- 不把 JAE 趨勢／交叉文字判斷搬入 C++；先移植大量逐列計算部分。

## 實作與驗證結果

- `_stockbuild_native` 升至 0.4.0；advanced core 計算期間釋放 GIL，12 欄結果
  共用 C++ owner capsule 並以 contiguous、readonly NumPy views 回傳。
- 1,000,000 根 synthetic 一分 K：C++ 中位數 164.089 ms（約 6,094,265 rows/s），
  pandas 中位數 454.910 ms，提升 2.772 倍；NaN mask 與所有數值逐欄通過。
- 本機實際 SQLite shadow：0050 日 K 5,215 根（2006-08-09～2026-08-04）與
  TXFR1 日 K 2,438 根（2016-08-03～2026-08-04）均通過 KDJ／DMI／JAE、
  週 K 與月 K differential。0050 最大絕對誤差為 J 約 `2.842e-14`，TXFR1
  本次所有欄位為 0。
- 原始、匿名化 case 報告固定於
  `benchmarks/results/adr148_advanced_indicators_20260804.json`。
- Release 與 MSVC ASan native suite 各 30 項通過；完整專案測試結果記於
  `DECISIONS_ADR113.md` 追記。

## 實機尚待驗證

- 本次已使用使用者本機實際 SQLite 長歷史資料，但仍只有日 K；期貨夜盤分 K、
  休市邊界與跨交易日的實際大量分 K shadow 仍待資料累積後驗證。
- 產品路徑尚未切換，所以沒有新增 GUI 點按流程；真實券商登入、報價與下單未執行。
