# ADR-154：Native Intent 接入正式回測 Shadow A/B

- **日期**：2026-08-04
- **狀態**：已實作；Python 回測仍為唯一正式輸出
- **承接**：ADR-143、ADR-144、ADR-152、ADR-153

## 決策

1. `_stockbuild_native` 升級至 v0.8.0，策略 runtime 可分開接收判斷收盤價與預定成交價。
2. 回測 shadow 遵守 ADR-064：第 N 根收盤判斷，第 N+1 根開盤成交；最後一根不產生無法成交的 intent。
3. `core/native_backtest_shadow.py` 先執行正式 Python 回測，再獨立計算 native intent stream，逐筆比對判斷列、成交列、OPEN/CLOSE、買賣方向、數量與價格。
4. Shadow 不會取代 Python 的 trades、equity、markers、metrics，也不會呼叫 broker 或送單。
5. 自訂 Python 策略、買進持有／DCA、時間窗與 intrabar stop 目前明確回報 `not_applicable`，禁止縮減語意後假裝通過。

## T+1 邊界

Native condition 仍使用原始 OHLCV。Runtime 的 stop 判斷使用已收盤 Close，但持倉成本使用下一根 Open，與正式 Python 回測一致。Intent 保留在判斷列，shadow 報告則映射至下一根成交列。

## 驗收

- 新增 T+1 成交價、決策範圍、完整 Python/native intent parity 與不支援語意測試。
- C++ runtime 可用嚴格警告的 `g++ -fsyntax-only` 驗證。
- `tests/test_core.py`：821 項通過。
- `tests/test_brokers.py`：43 項通過（本機缺少三個隔離子行程條件，3 項略過）。
- `diag_crossref.py` 與 ADR-154 診斷案例通過；既有 ADR-126 診斷仍失敗，不屬本階段變更。
- 76 個 Python 檔案 byte-compile 通過。
- 本機目前缺少 MSVC Build Tools，因此 v0.8.0 `.pyd`、Release native suite、ASan 與 100,000 根 benchmark 必須在 MSVC 環境補跑，不宣稱已通過。

## 下一步（ADR-155）

1. 將 fill/cost/equity/markers/metrics 移入 native typed backtest result。
2. 補齊 intrabar stop、看 A 做 B、期末結算及成本模型 parity。
3. MSVC Release／ASan 全套通過後，才考慮 opt-in native backtest；預設仍維持 shadow。
