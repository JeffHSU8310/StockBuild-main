# ADR-152：C++ 批次策略條件核心（Phase 3 第一階段）

- **日期**：2026-08-04
- **狀態**：完成第一批條件 shadow API；尚未成為 Intent／回測正式來源
- **前置 ADR**：ADR-143、ADR-144、ADR-151

## 背景

目前 `strategy_engine.evaluate_strategy()` 在每根 K 棒上重新以 pandas 計算條件。
完整回測、最佳化與全市場選股若各自重跑相同 rolling 指標，Python 逐根切片會成為
主要瓶頸。Phase 3 必須先把條件轉成可重用的逐根訊號欄，之後 runtime、回測與選股
才能共用相同 C++ 語意，而不是各寫一套捷徑。

## 決策

1. `_stockbuild_native` 升至 v0.6.0，新增 typed `condition_core` batch API；單次接受
   1～64 個條件，輸入既有 OHLCV 欄式 buffer，計算時釋放 GIL。
2. 第一批支援 24 種無籌碼條件：價格門檻／突破、SMA 交叉、價格與 SMA／EMA 的
   交叉／狀態、均線斜率／排列、成交量相對均量、連續紅黑、單根漲跌、always true、
   inside bar 與 gap up/down。
3. Python bridge 將既有 condition dict 編譯成 `type + numeric params + ma_kind`；
   未支援類型、非法週期、非有限數值與 SMA／EMA 以外類型一律明確拒絕。
4. C++ 回傳每個條件的逐根 `uint8` 序列；所有欄位共用 capsule owner、readonly，
   供下一階段 runtime／Intent／回測直接按 bar index 消費，不再逐根進 Python。
5. 本 ADR 不改 `evaluate_strategy`、不產生 authoritative OrderIntent、不接券商。
   正式交易 shadow 時仍只能採 Python intent，直到 runtime state／risk／fill 完成。

## 驗收結果

- 24 種條件在 360 根資料上逐根呼叫現行 `strategy_engine.CONDITIONS` 比對，所有
  warm-up、交叉時點與布林值完全一致。
- 空資料、未知類型、非法週期、錯誤均線型態、超過 64 欄、readonly、共享 owner
  與 mutation guard 均已覆蓋。
- 600 根、24 條件逐根同工作量：C++ `1.3169 ms`，現行 Python 每根切片重算
  `2938.2765 ms`。此數字只比較條件訊號預計算，不代表完整回測速度。
- 100,000 根、24 條件產生 2,400,000 個訊號：C++ `39.7701 ms`，約
  60.35 百萬 bar-condition／秒。
- 可重現報告：`benchmarks/results/adr152_conditions_20260804.json`。

## 下一階段

1. 把 entry／exit 的 AND／OR 組合、runtime state、DCA／buy-and-hold 與 risk check
   移入 C++，只回傳 broker-neutral OrderIntent。
2. 對每根 intent、state transition 與拒絕原因做 Python／C++ shadow differential。
3. 再承接 T+1、成本、盤中觸價、交易／equity／markers／metrics，形成完整回測。
4. 後續補上 RSI／KD／MACD／BB 與籌碼條件；未支援條件不得靜默改走不同語意。

## 實機尚待驗證

- 本階段是離線純計算核心，沒有新增 GUI 操作。
- 真實券商登入、報價與下單未執行；C++ module 不依賴任何 broker SDK。
