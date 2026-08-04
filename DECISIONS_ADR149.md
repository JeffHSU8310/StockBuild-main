# ADR-149：Native 指標路由與產品 shadow 導入閘門

- **日期**：2026-08-04
- **狀態**：已完成第一道產品整合；預設 `off`，可明確選擇 `shadow`，`native` 尚未開放
- **前置 ADR**：ADR-143、ADR-147、ADR-148

## 背景

ADR-147／148 已完成 25 個主圖共用欄位的 C++ 計算與 synthetic／實際 SQLite
differential，但 API 尚未接到產品路徑。若直接把 C++ 設成預設引擎，GUI 的真實參數
組合、重畫頻率、extension 發布與錯誤呈現都尚未驗證；若一直停留在 benchmark API，
也無法累積正式切換所需的產品證據。

## 決策

1. 新增 `core.engine_router` 作為 GUI、回測、策略與選股未來共同使用的引擎路由契約。
2. 指標旗標存於 `app_settings.json` 的 `native_indicators`：
   - `off`：只執行既有 Python，完全不載入 native，且是預設值；
   - `shadow`：Python 仍是 authoritative output，同一次執行 C++ 並逐欄比較；
   - `native`：保留名稱但目前明確拒絕，不能只改設定檔提前啟用。
3. 第一個產品接點是 `StockTradingAppPro.calculate_custom_indicators()`。支援 BB／BBW、
   第二組 BB、MACD、RSI、KDJ、DMI、JAE，共 25 欄；六組可自訂 MA 因 native API
   尚未支援多組不同週期，暫由 Python 處理且不列入 shadow 成功數。
4. shadow 必須先核對長度、NaN mask，再使用逐類型窄 tolerance 比對數值。任何 ABI、
   載入、參數或 parity 問題都讓當次工作明確失敗並寫入 GUI 日誌；不得靜默改拿另一套
   結果冒充成功。沒有啟用可支援欄位則是明確 no-op，不誤報錯誤。
5. telemetry 只記模式、狀態、欄數、最大絕對誤差、native 版本與耗時；不記 symbol、
   帳號、行情值、策略內容或憑證。

## 正式切換時點

`native` opt-in 要在下一個 ADR 同時滿足以下條件後才開放：

1. extension 有正式安裝／打包路徑，不依賴開發目錄裡的 build artifact；
2. 主圖以實際日 K、分 K、期貨日夜盤及常用參數組合完成 shadow 驗收；
3. shadow 的錯誤與效能不阻塞 GUI，且失敗訊息能由使用者辨識；
4. supported／unsupported 欄位邊界固定，native 模式不會為了補欄位反而重算整套 Python；
5. Release、ASan、完整回歸與至少一輪使用者實機點按全過。

之後採兩步：先讓主圖 `native` 成為明確 opt-in；穩定一個發布／模擬週期後才考慮預設。
回測／策略／選股要等各自的 C++ event/condition/screener core 完成 golden differential，
分別走相同 `off → shadow → native opt-in → default`，不因主圖通過就宣稱全部已切換。

## 驗收

- off 不可呼叫 native provider，並保留原 DataFrame identity。
- shadow happy path、數值突變、NaN 突變、未開放 native 模式、設定 round-trip 都有單元測試。
- 真實 `_stockbuild_native` Release build 對 480 根資料、25 欄與現行 Python 實作比對。
- 完整專案測試、native Release／ASan、診斷與 `py_compile` 合併前後全過。

## 實機尚待驗證

- tkinter 主圖實際切成 shadow 後的點按、縮放、參數切換、錯誤日誌與 UI 延遲。
- 正式 extension 安裝／打包；目前自動測試會建置並以明確 module handle 驗證。
- 期貨夜盤大量分 K 的產品 shadow，以及回測／策略／選股正式 native 路由。
