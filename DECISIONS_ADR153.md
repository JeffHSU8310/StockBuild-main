# ADR-153：C++ 策略組合、狀態機與風控 Intent 核心

- **日期**：2026-08-04
- **狀態**：已實作；shadow API，尚未接管正式回測、選股、GUI 或券商下單
- **承接**：ADR-143、ADR-144、ADR-152

## 決策

1. `_stockbuild_native` 升級至 v0.7.0，新增 `strategy_runtime`。
2. 輸入為 ADR-152 的唯讀 `uint8` 條件欄位；進場與出場各自支援 AND／OR。
3. C++ 狀態機支援 FLAT／LONG／SHORT、固定數量、百分比及絕對值停損停利。
4. 風控支援每日交易次數、每日已實現虧損與秒級冷卻；每日狀態由明確的 session-day key 重設。
5. 輸出為 broker-neutral SoA 欄位：kind、action、quantity、price、reason、blocked_reason、state、entry_price 與 realized_pnl_today。
6. action 使用 `1=BUY`、`-1=SELL`，kind 使用 `1=OPEN`、`2=CLOSE`；C++ 不認識券商 SDK、帳號、委託 API 或 GUI。

## 理由

條件計算移入 C++ 後，如果 Python 仍逐根執行條件組合、持倉狀態與風控，大範圍回測仍會受 Python 迴圈限制。先建立純決策核心，才能在不碰真實下單的前提下，對 Python 現行語意做逐根差分與安全 rollout。

## 驗證與效能

- 96 根跨日資料逐欄比對獨立 Python 狀態機，包含 OR 進場、AND 出場、停損停利、每日上限、每日虧損與冷卻。
- 驗證 SHORT、空資料、非法方向／數量／邏輯／風控值及輸出唯讀。
- 100,000 根 K 棒、2 個進場條件、2 個出場條件，加上狀態與風控：`20.5514 ms`，約 `4.87M bars/s`。這是本機 shadow benchmark，不等同完整含成本模型與報表的回測速度。
- 效能證據：`benchmarks/results/adr153_strategy_runtime_20260804.json`。
- 完整回歸：`test_core` 820、`test_brokers` 43、Release native 38、MSVC ASan
  native 38、`diag_repro_issues` 68、`diag_crossref` 與 74 個 Python 檔案編譯全數通過。

## 安全邊界

- 本階段不呼叫 `place_order`，不載入 shioaji／kgisuperpy，也不改動正式 broker route。
- 目前成交價以同根 close 做決策狀態模擬；正式回測仍保留既有 T+1、滑價、成本、intrabar 與 exec_df 語意。
- 每日風控的 session-day key 由 Python bridge 明確傳入；後續接台期夜盤時必須改由既有 session 規則產生，不可直接假設 UTC 日界。

## 下一階段（ADR-154）

1. 將 native intent stream 接入回測 shadow A/B，不改正式輸出。
2. 對 T+1 成交、成本、滑價、intrabar stop、equity、markers、metrics 做逐筆差分。
3. 差分穩定後，先讓標準內建策略回測可選 native；自訂 Python 策略仍保留 Python DSL。
4. 選股批次共用同一條 native 訊號路徑；GUI 與真實交易最後切換。
