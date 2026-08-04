# ADR-151：C++ 批次多均線核心與主圖 31 欄 shadow

- **日期**：2026-08-04
- **狀態**：已完成主圖 shadow；`native` 正式輸出仍未開放
- **前置 ADR**：ADR-147、ADR-149、ADR-150

## 背景

ADR-150 已讓產品主圖能使用正式安裝的 C++ runtime 驗證 25 欄指標，但六組使用者
自訂均線仍只由 Python 計算。若直接為每條均線呼叫既有 `indicator_core`，會重算
RSI、MACD、Bollinger 等無關欄位，既浪費 CPU，也讓未來正式 native 輸出仍不完整。

## 決策

1. `_stockbuild_native` 升至 v0.5.0，新增 `multi_ma_core(close, periods, kinds)`；一次
   接受 1～64 組 SMA／EMA／WMA，單次驗證 close，計算期間釋放 GIL。
2. 多欄結果由同一個 capsule 管理，Python bridge 逐欄驗證 float64、連續記憶體與
   列數後設為 readonly。這是 ABI v1 的加法 API，KBar layout 未變，因此 ABI/schema
   版本維持不變。
3. WMA 使用 O(n) 滾動加權和，並用 compensated summation 抑制百萬根長序列的
   浮點漂移；不得退回 O(n × period) 的逐窗重算。
4. `engine_router` 只收集已啟用的 MA，批次呼叫一次，再依原索引寫回
   `MA_CUSTOM_0`～`MA_CUSTOM_5`。主圖 route settings 必須傳入 flags、types、periods。
5. shadow 支援欄位由 25 增至最多 31；Python 仍是畫圖權威，逐欄 parity 失敗必須
   明確失敗。`native` 正式輸出仍由程式拒絕，不能因本 ADR 自動解鎖。

## 驗收結果

- 六組 `[5, 10, 20, 60, 120, 240]`、`[SMA, EMA, WMA, SMA, EMA, WMA]` 的
  600 根獨立 pandas differential 通過；空資料、短資料、錯誤型別／週期／欄數、
  共享 owner、readonly 與 mutation guard 都已覆蓋。
- 1,000,000 根 synthetic 六組 MA：C++ `36.8362 ms`，pandas rolling／ewm 加
  NumPy convolution 參考 `158.5882 ms`，快 `4.305` 倍；NaN mask 完全相同，
  在報告精度下最大誤差為 0。
- 實際 SQLite 產品 shadow：
  - 0050 日 K 5,215 根，31 欄、最大誤差 0，native `5.0836 ms`；
  - TXFR1 日 K 2,438 根，31 欄、最大誤差 0，native `2.5901 ms`。
- 匿名化結果固定於 `benchmarks/results/adr151_multi_ma_20260804.json`。

## 正式 native 下一道門檻

1. 使用者在 GUI 實際選擇六組不同 MA 類型／週期，完成股票／期貨、日 K／分 K、
   縮放／重畫、保存／重啟的操作驗證。
2. 補足大量期貨日夜盤分 K，涵蓋 13:45／15:00、跨交易日與休市邊界。
3. 評估 shadow 同步計算對 GUI 回應時間的影響，必要時另做背景化或 cache。
4. 達成上述門檻後另開 ADR，只先開放主圖 `native` opt-in；回測、策略與選股仍須
   各自的 C++ 核心、golden differential 與 rollout，不隨主圖自動切換。

## 實機尚待驗證

- 自動測試沒有操作 tkinter 視窗，GUI 點按、視覺結果與實際互動延遲仍待使用者驗證。
- 本機長歷史仍只有日 K，尚無大量夜盤分 K 資料。
- 真實券商登入、報價與下單未執行；本階段不改任何送單路徑。
