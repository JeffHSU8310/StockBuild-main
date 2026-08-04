# ADR-150：Native extension 產品安裝、GUI shadow 控制與實際資料驗收

- **日期**：2026-08-04
- **狀態**：已完成產品 shadow 的安裝與操作入口；`native` 正式輸出仍未開放
- **前置 ADR**：ADR-145、ADR-149

## 背景

ADR-149 已把主圖接到 `off／shadow` router，但 `_stockbuild_native` 仍只存在於
`build-test` 等開發產物。產品 loader 只做一般 import，GUI 沒有可見的模式設定，
因此當時的 shadow 是「程式接線已完成、產品部署尚未完成」，不能實際交給使用者操作。

## 決策

1. `python native/build_native.py --install` 是 source checkout 的正式建置／安裝命令：
   完成 Release configure、compile、CTest 後，安裝到
   `native/runtime/<sys.implementation.cache_tag>/` 並產生 manifest。
2. runtime 依 Python ABI 分目錄；`.pyd`／`.so` 與 manifest 是本機產物，不提交 Git。
   `native/runtime/README.md` 保留安裝契約。ASan build 明確禁止安裝成產品 runtime。
3. loader 搜尋順序為正常 Python import、明確 `STOCKBUILD_NATIVE_DIR`、專案 ABI runtime，
   frozen app 再加執行檔旁的 ABI runtime；不掃描 cwd、`build-test` 或其他任意 build。
4. 每次載入仍必須通過 ADR-145 ABI/schema handshake；成功資訊附實際 module path，
   供診斷證明載入的是產品 runtime，不是測試產物。
5. 主圖指標設定視窗新增 `off／shadow` 下拉與「檢查 Native」。選 shadow 並套用前
   必須實際載入及握手；失敗則不保存、不重畫，並顯示操作指引。
6. `native` 模式繼續由 `engine_router` 拒絕。本 ADR 只解除「無法部署與無法選擇
   shadow」兩個門檻，沒有取得使用者 GUI 點按與大量夜盤分 K 證據前不提前切正式輸出。

## 驗收結果

- Windows／CPython 3.14 安裝到 `native/runtime/cpython-314`；全新 Python 行程從該
  目錄載入 v0.4.0 並通過 ABI 握手。
- 實際 SQLite 產品 router shadow：
  - 0050 日 K 5,215 根（2006-08-09～2026-08-04），25 欄、最大誤差 0，native 5.1481 ms；
  - TXFR1 日 K 2,438 根（2016-08-03～2026-08-04），25 欄、最大誤差 0，native 3.7128 ms。
- 匿名化報告固定於 `benchmarks/results/adr150_product_shadow_20260804.json`。
- 測試覆蓋缺檔搜尋訊息、明確 runtime 目錄、ABI 分層安裝、manifest、全新行程載入、
  ASan 禁止安裝，以及 GUI 設定／保存／啟用前 probe 的接線診斷。

## 正式 native 下一道門檻

1. 使用者實際開啟 GUI，確認「檢查 Native」與 off／shadow 保存、重啟還原。
2. 主圖切換股票／期貨、日 K／分 K、全部 25 欄參數並操作縮放／重畫，無 parity 失敗。
3. 補足大量期貨日夜盤分 K，涵蓋 13:45／15:00、跨交易日與休市邊界。
4. 量測 shadow 對 GUI 回應時間的影響，決定同步、背景化或 cache 策略。
5. 完成多組自訂 MA native API，避免 native 模式仍重算整套 Python。

達成後另開 ADR，先開放主圖 `native` opt-in；回測、策略、選股仍須各自的 C++ 核心
與 differential，不會隨主圖一起自動切換。

## 實機尚待驗證

- 本次自動測試已在使用者本機完成正式安裝與實際 SQLite shadow，但未自動操作
  tkinter 視窗，仍需使用者點按設定與觀察畫面／日誌。
- 本機資料仍只有日 K 長歷史，未涵蓋足量夜盤分 K。
- 真實券商登入、報價與下單未執行；本階段不改送單路徑。
