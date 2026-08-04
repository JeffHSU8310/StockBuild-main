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
