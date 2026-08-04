# StockBuild-main
StockBuild-main

## C++ native runtime（目前供主圖 shadow 驗證）

```powershell
python native/build_native.py --install
```

完成後在「主圖指標參數設定」按「檢查 Native」，再把 C++ 指標引擎從 `off`
切為 `shadow`。shadow 仍使用 Python 結果畫圖，同時逐欄驗證 C++；正式 `native`
輸出尚未開放。ADR-151 已把六組自訂 SMA／EMA／WMA 納入同一次 C++ 批次計算，
產品 shadow 現可核對最多 31 欄；詳細規則見 `DECISIONS_ADR151.md`。

ADR-152 另建立 24 種價格／均線／成交量／K 棒策略條件的 C++ 批次訊號核心，
目前只供 differential 與後續 runtime 使用，尚未取代 Python Intent 或正式回測。
ADR-153 已新增 C++ AND／OR、LONG／SHORT 狀態、停損停利及每日／冷卻風控，輸出
broker-neutral typed intent stream；下一步先接正式回測的 shadow A/B，不直接送單。

ADR-154 已把 native intent stream 接入正式 Python 回測的 T+1 shadow A/B；Python
trades／equity／metrics 仍是唯一正式結果。自訂 Python、DCA、時間窗與 intrabar stop
尚未納入 native parity，詳細邊界見 `DECISIONS_ADR154.md`。
