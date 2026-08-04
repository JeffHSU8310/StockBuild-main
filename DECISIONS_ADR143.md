# ADR-143：SmartStock 經驗導入 StockBuild 的 C++ 核心與 Qt 圖表分階段移植

- **日期**：2026-08-04
- **狀態**：已接受，規劃中（尚未開始產品程式移植）
- **範圍**：主圖渲染、指標／重採樣、策略與即時量化 runner、單標的回測、
  參數最佳化、選股與選股回測
- **前置 ADR**：ADR-039、040、057、062、075、101、106、122、140、141、142

## 一、背景與問題

ADR-140 已把「指標計算／Matplotlib 建圖／畫布點陣化」分開量測，ADR-141
要求長週期至少保留十年，ADR-142 再以 SQLite 增量保存完整歷史。資料不再靠
刪除換速度後，現行 tkinter + Matplotlib／mplfinance 在大量 K 棒的建圖、縮放、
平移，以及 Python 逐根回測／重複參數掃描上，將逐漸成為可預期的瓶頸。

本次唯讀檢查 `JeffHSU8310/SmartStock` 的 `main` commit
`67f09a7a99cb45c8b8a0cb8db687786f79213ccd`，確認兩個值得採用的方向：

1. Qt6 + pyqtgraph 將 K 棒先錄製為 `QPicture`，畫面重繪不必反覆建立大量
   Matplotlib artist。
2. C++ 以單次線性掃描計算指標與回測，純核心運算可遠快於 Python 逐根切片。

但 SmartStock 現況**不可直接複製**：主圖仍由 Python／pandas 計算，資料預設
只抓約 1,000 天；C++ 回測只有 MA 交叉，尚未涵蓋 StockBuild 的完整策略語意；
而且 C++ `KBar` 與 Python ctypes 宣告的大小／欄位不一致。StockBuild 必須從
自己的已驗證行為建立原生核心，不能把「跑得快」誤當成「算得對」。

另一個不能忽略的成本是資料邊界：ADR-142 雖已避免每天重抓十年歷史，但若流程
仍是「SQLite → Python rows → pandas DataFrame → NumPy → C++」，大量歷史會在
Python 重複配置與複製。原生核心必須能在安全的一致性規則下直接讀取 SQLite，
並以 SQL coverage／range query 決定真正缺少的區間，讓網路只下載資料庫沒有的部分。

## 二、目標

1. **C++ 成為所有運算功能的共同核心**：
   - 多週期 K 棒重採樣與技術指標；
   - 內建條件策略、買進持有／累積加碼／定期定額；
   - 看 A 做 B、股票／零股／期貨、日夜盤口徑；
   - 停損停利、盤中觸價、成本／稅／滑價與 T+1 成交；
   - 終極波段雙時間週期狀態機；
   - 自訂策略的事件重放、部位、成交、風控與績效統計；
   - 網格搜尋、隨機搜尋、樣本內外檢定；
   - 技術面／籌碼面／基本面選股與選股投資組合回測。
2. **SQLite 成為歷史資料的共同來源**：Python 負責下載、schema migration 與
   唯一寫入；C++ 以唯讀連線直接做 coverage、區間／增量查詢與欄式載入，
   避免多餘下載和 Python 中轉複製。
3. **Qt 成為下一代主圖渲染技術**，完整歷史留在資料層，畫面只依 viewport
   建立可見幾何，平移／縮放不得重新建立全資料集圖元。
4. **結果等價優先於速度**：Python 現行測試與既有 ADR 所定義的語意是基準。
   C++ 未逐項通過差異測試前，不得成為預設路徑。
5. **漸進導入且可立即回退**：每個子系統都有獨立 feature flag、Python
   參考路徑與 shadow mode，不做一次性全面重寫。
6. **券商與交易安全邊界不變**：C++ 只輸出券商中立 `OrderIntent`；登入、
   憑證、確認視窗、帳號路由與真正 `place_order()` 仍由 Python broker／GUI
   邊界負責，C++ 永遠不能直接送單。

## 三、非目標

- 不直接複製 SmartStock 的 DLL、ctypes 結構或 MA-only 回測。
- 不把 shioaji／kgisuperpy、API Key、憑證或網路重試搬進 C++。
- 不因 Qt viewport 而刪除 SQLite／DataFrame 中的十年歷史。
- 不讓 C++ 與 Python 同時成為 SQLite writer；初期 native SQL 功能嚴格唯讀。
- 不承諾把任意 Python 原始碼自動翻譯成等價 C++。既有自訂 Python 策略必須
  有相容路徑；可靜態轉換的受限語法才進入原生 Strategy IR。
- 不在沒有基準數據時宣稱固定倍數加速；所有數字都由同機 A/B benchmark 產生。

## 四、目標架構與責任邊界

```text
券商 SDK / 設定 / 金鑰             SQLite WAL              Qt 主視圖
       Python I/O / 唯一 writer ───────┤                     │
              │                        │ read-only snapshot   │ viewport arrays
              │ immutable arrays       ▼                      ▼
              └──────────────► core/native_bridge.py ─► chart_qt/ renderer
                                      │ pybind11 / SQL reader │
                                      ▼                       │
┌──────────────────────── stockbuild_core (C++20) ────────────────────────┐
│ KBarStore / Resampler / Indicators / Pattern & Chips Features           │
│ ConditionEngine / StrategyRuntime / Custom Strategy IR                  │
│ Risk & Cost / FillEngine / Backtest / Optimizer                         │
│ Screener / ScreenerPortfolioBacktest / Metrics / Audit                  │
└─────────────────────────────────────────────────────────────────────────┘
              │ OrderIntent / Result / Diagnostics
              ▼
 Python 安全閘門 → 確認視窗／帳號路由／broker adapter → place_order
```

| 功能 | C++ 責任 | Python 保留責任 |
|---|---|---|
| K 線歷史 | SQLite 唯讀 coverage／range query、欄式陣列、聚合、缺值／排序檢查 | SQLite schema 與單一 writer、券商／官方資料下載 |
| 指標與型態 | MA/EMA/BB/MACD/RSI/KDJ/DMI/JAE、量價、型態、費波南希 | 設定編輯與文字呈現 |
| 量化策略 | 條件、狀態機、風控、Intent、部位狀態 | runner 排程、資料取得、錯誤通知、下單確認 |
| 回測 | 事件時序、成交、成本、交易／權益／標記／稽核 | 期間選擇、下載、報告視窗 |
| 最佳化 | 組合批次、平行執行、排名 | 使用者輸入、取消、結果套用 |
| 選股 | 基本面／技術面／籌碼面批次條件 | 原始檔下載與欄位解析、結果視窗 |
| 選股回測 | 調倉、等權配置、成本、績效與未來函數旗標 | 歷史資料載入與報告呈現 |
| 主圖 | 提供連續數值陣列與 LOD／viewport | PySide6/pyqtgraph 事件迴圈與互動 UI |
| 券商 | **不直接碰觸** | SDK、帳號、憑證、callback、真正送單 |

## 五、完整功能等價清單

以下每一列都要有 Python↔C++ golden/differential test；未通過的列不得切換預設引擎。

| 現有能力 | C++ 核心交付要求 |
|---|---|
| `strategy_engine.CONDITIONS` 全條件庫 | 價格、均線、量能、KD、MACD、RSI、BB、K 棒型態及所有籌碼條件，AND／OR 與錯誤原因完全一致 |
| 多空與三種商品 | 股票張、零股股、TXF/MXF/TMF 乘數、合法 tick／滑價與方向一致 |
| T+1 成交模型 | 訊號只看已收盤資料，下一根開盤成交，不得偷看當根收盤 |
| 看 A 做 B | 不同商品／不同週期時間戳對齊、A 產生訊號、B 決定成交與損益 |
| 風控與時段 | 停損、停利、絕對點位、盤中高低觸價、交易窗口與特殊策略排除規則 |
| 買進持有 | single／accumulate／DCA，投入週期、結餘、加權成本與期末結算 |
| 自訂策略 | 標準 Python Strategy Language 編譯成 IR 由 C++ 執行；舊動態 Python 僅作明示 callback 相容模式。兩者的事件迴圈、成交、風控與績效都由 C++ 掌管 |
| 終極波段 | 日 K 訊號、12:00 二次確認、延遲執行、X/C/F/Y/Z 出場、日盤限制與跨日重試狀態 |
| 成本與稽核 | 手續費、稅、滑價、毛／淨損益、MDD、Sharpe、獲利因子、連勝敗、持有根數及 `audit_result` |
| 最佳化 | 網格、隨機、樣本內外、取消、min trades、六種目標排序與錯誤彙總 |
| 全市場選股 | 產業→基本面→技術／籌碼兩階段篩選、進度／取消、缺資料理由 |
| 選股回測 | 下一交易日開盤、定期調倉、等權、成本、停牌缺價、基準報酬與未來函數警告 |
| 模擬帳戶 | fill、部位加權成本、已／未實現損益與帳戶隔離的共同計算核心 |
| 圖表 | 十年日／週／月歷史可瀏覽；大量分 K 只限制單幀幾何，不刪資料；hover 與紅漲綠跌一致 |

### 自訂策略的語言結論

**使用者寫 Python 語法，系統用 C++ 執行。**標準自訂策略不是要求使用者直接寫
C++，而是受限、可驗證的 Python Strategy Language；儲存為 Python 原始碼，先經
AST 驗證與編譯成 Strategy IR，再由 C++ 批次執行。任意舊 Python `on_bar(ctx)`
無法轉換時才進入明示的 Python 相容模式。完整規則見第八節。

## 六、原生介面決策

1. 採 **CMake + C++20 + pybind11 buffer protocol**；Python 以連續 NumPy
   陣列批次傳入，不使用逐根 ctypes 物件組裝。
2. 唯一 KBar schema：`timestamp_ns:int64`、OHLCV `float64/int64`，另帶
   symbol／timeframe metadata；C++ `static_assert` 與 Python dtype/stride
   測試同時驗證欄位、offset、大小、endianness。
3. 輸入預設唯讀；輸出用 typed result，包含狀態碼、錯誤位置與診斷文字，
   不以例外吞掉或回一份看似成功的空結果。
4. 每個 native 模組暴露 `api_version`、`schema_version`、`build_id`；版本不合
   立即拒絕載入並回 Python，不允許 ABI 猜測。
5. Windows 發布預編譯 x64 wheel／extension，runtime 不要求使用者安裝編譯器；
   所有相依 DLL 必須一併打包並在乾淨 VM 驗證。
6. C++ 熱路徑不持有 GIL；Python callback 相容模式才在明確邊界取得 GIL。
7. 平行最佳化固定 seed、穩定排序與每工作單元獨立 runtime，結果必須可重現。

## 七、SQLite 與 C++ 直接讀取決策

1. **Python 是唯一 writer**：券商／官方資料下載、schema migration、upsert、
   checkpoint 與資料修復仍走 `data/kbars_store.py`。C++ 第一階段只以
   `SQLITE_OPEN_READONLY` 開庫並設定 `PRAGMA query_only=ON`，不做任何 DDL/DML。
2. **WAL snapshot read**：沿用 ADR-142 WAL；每次工作使用自己的 read connection
   與短交易快照，不跨執行緒共用 `sqlite3*`。statement、transaction、connection
   全部用 RAII 保證 finalize／rollback／close，並設 bounded busy timeout。
3. **SQL 先縮小資料**：以 `(symbol, asset_type, timeframe, ts)` 複合索引執行
   `MIN/MAX/COUNT` coverage、`ts BETWEEN ? AND ?`、`ts > last_ts` 與只選必要欄位；
   不允許 `SELECT *` 後再交給 Python 裁切。
4. **下載規劃使用 DB 真實 coverage**：先由 SQL 查現有最早／最晚時間與列數，
   Python downloader 只請求前缺口、尾端增量或可證明的中間缺口；完整命中時
   網路呼叫必須是 0。API 回傳後仍由 Python writer upsert，C++ 下一個 snapshot
   才看見新資料。
5. **直接填入欄式 native buffer**：C++ prepared statement 逐列寫入預先配置的
   timestamp/OHLCV vectors，後續指標、回測、選股與 Qt viewport 共用，不繞回
   pandas。小資料或 native 關閉時仍保留既有 Python load 路徑。
6. **coverage metadata 是最佳化，不是真相替代品**：可新增每商品／週期的
   `min_ts/max_ts/row_count/updated_at` 摘要表加速規劃，但任何摘要都能由 kbars
   主表重建；異常中止不得讓摘要誤導成「資料完整」。
7. **cache invalidation**：native LRU key 至少包含 DB path、schema version、
   symbol、asset_type、timeframe、range 與 SQLite `PRAGMA data_version`／更新世代；
   Python upsert 後舊 native buffer 不得永久沿用。
8. **查詢計畫納入測試**：以 `EXPLAIN QUERY PLAN` 證明 range／coverage 使用索引；
   另測同時讀寫、取消、例外、statement 未取完、程式關閉與 WAL/SHM 生命週期，
   防止再次出現 Windows SQLite 檔案被鎖住或連線關不掉。
9. 選股所需的全市場日 K、籌碼與基本面後續可統一進 SQLite，但要另做 schema
   migration；C++ reader 只讀已正式版本化的 schema，不直接猜 CSV 欄位。

## 八、自訂策略語言與執行模式

### 標準答案：Python 前端語法，C++ 執行後端

- 使用者繼續撰寫熟悉的 `on_bar(ctx)` Python 形式，不必安裝編譯器、不必處理
  pointer／記憶體與 ABI，也不會因一個 C++ bug 讓整個交易程式直接崩潰。
- 儲存時先走 AST validator，再編譯成與語言無關的 typed Strategy IR；回測、
  最佳化與即時策略都載入同一份 IR，由 C++ Condition/StrategyEngine 執行。
- 第一版標準語法包含：數值／布林運算、`if/elif/else`、受控 state、`ctx.param`、
  OHLCV、已核准指標／交叉／籌碼函式，以及 `BUY/SELL/CLOSE/HOLD` decision。
- 標準語法禁止：`import`、檔案／網路／process、`eval/exec`、反射、任意物件存取、
  無界 `while`、非決定性時間／亂數，以及直接呼叫 broker／GUI。新增能力必須先
  定義 IR opcode、資源上限與 Python↔C++ 等價測試。
- 編譯結果帶 `language_version`、`ir_version`、source hash 與 diagnostics；source
  有改動、引擎版本不合或編譯失敗時不得沿用舊 IR。

### 舊策略相容模式

- 無法轉成 IR 的既有動態 Python 可以繼續執行，但 UI／報告必須明確標示
  **「Python 訊號相容模式（較慢）」**。
- C++ 仍掌管資料時間軸、部位、風控、成交、成本、績效與稽核；Python subprocess
  只收到受限 context 並回 decision，不能直接送單。
- 相容模式不得用於宣稱 native 效能；最佳化大量參數時要提示 callback 成本，
  並允許使用者先把語法改成可編譯的標準形式。

### C++ 策略原始碼不是一般使用者格式

直接載入使用者 C++ DLL 具有記憶體破壞、ABI、編譯器版本與繞過交易邊界風險，
因此**不列入 ADR-143 的標準自訂策略功能**。未來若要支援專家級 native plugin，
必須另開 ADR，採版本化 C ABI、簽章／信任提示與 out-of-process 隔離；不能與本次
Python Strategy Language 混稱同一功能。

## 九、Qt 圖表決策

1. 先採 PySide6 + pyqtgraph，借用 SmartStock 的 `QPicture` 批次繪圖概念，
   但改為**分塊 picture cache + viewport LOD**，避免每次更新重建完整十年圖形。
2. tkinter 與 Qt 不在同一主執行緒混跑兩套 event loop。先建立可獨立啟動的
   Qt chart shell／sidecar，在資料、互動與功能等價後才遷移主視窗。
3. 完整資料與完整指標留在 C++／SQLite；viewport 只決定當幀送出的 OHLC／線段，
   沿用 P-119「單幀渲染量不等於資料保存量」。
4. 主圖、副圖共用 X 軸；Y 軸只依可見區間重算。hover 用索引二分搜尋，
   不逐 artist hit-test。
5. 紅漲綠跌、面板比例、指標設定、交易標記、盤勢判斷、費波南希、量價線、
   回測標點與現有快捷操作全部列入 Qt parity gate。
6. 是否再把 renderer 本身下沉 C++，只看 ADR-140/本 ADR benchmark；
   若 Qt scene 已達標，不為「C++ 比較快」而增加不必要的跨語言 GUI 複雜度。

## 十、分階段實作計畫

### Phase 0：基準、清冊與黃金資料（不改產品行為）

- 凍結本 ADR 第五節功能清冊，補齊每項 Python golden fixture。
- 記錄 ADR-140 指標／建圖／畫布時間，以及回測、500 組最佳化、全市場選股基準。
- 建立效能測試硬體／Python／資料範圍紀錄，禁止跨機直接比較毫秒數。
- 記錄 SQLite 完整命中／尾端增量／全新商品三種情境的 API 次數與資料載入時間。
- **出口**：相同輸入可重現交易、equity、markers、metrics、選股名單與錯誤原因。

### Phase 1：原生骨架與零拷貝邊界

- 建立 `native/`、CMake、pybind11 extension、版本握手與 KBar schema。
- 加入乾淨 Windows VM build/load test、ABI offset test、ASan/UBSan 測試組態。
- `core/native_bridge.py` 只負責驗證陣列與轉送，不包含交易規則。
- 建立 SQLite read-only adapter、RAII 連線與 schema/data version 握手。
- **出口**：1,000,000 根 K 棒批次往返不逐根 Python 迴圈，錯誤版本必定拒載。

### Phase 2：資料重採樣與指標／特徵

- C++ 直接用 prepared range query 從 SQLite 建立欄式 KBar buffer；coverage
  完整命中不得再呼叫網路，尾端只讀／下載增量。
- 移植股票自然日、期貨交易日、分／時／日／週／月聚合。
- 移植 indicators、JAE、型態、volume profile、fibonacci、chips features。
- 先以 shadow mode 同時計算，逐欄比較 NaN warm-up、精度與時間索引。
- **出口**：所有指標差異在明定 tolerance 內；OHLCV 聚合與交易日完全相同。

### Phase 3：條件、runtime、風控與 Intent

- 移植 `CONDITIONS`、`evaluate_strategy`、`risk_check`、`apply_fill`、時段過濾、
  買進持有／DCA、看 A 做 B 與模擬帳戶純計算。
- C++ 只回 `OrderIntent`，用 mutation test 證明無 broker SDK／無送單符號依賴。
- **出口**：每根 K 棒的 intent、runtime state 與 Python 參考引擎逐步一致。

### Phase 4：完整單標的回測與稽核

- C++ 事件迴圈承接 T+1、盤中觸價、成本、期末結算、交易／equity／markers／metrics。
- 保留 Python 與 C++ 雙引擎，先 shadow、後 opt-in；任何差異自動保存最小重現輸入。
- **出口**：所有 `tests/test_core.py` 回測案例與新增 differential suite 全過，
  報告每個欄位均有來源，禁止硬編固定績效值。

### Phase 5：特殊與自訂策略

- 移植終極波段雙時間狀態機，建立跨日、12:00 失敗重試與日盤邊界測試。
- 依第八節實作 Python Strategy Language、Strategy IR 與安全 AST→IR 編譯器；
  既有任意 Python 先走 C++ orchestration + Python subprocess signal callback。
- **出口**：終極波段加入回測／最佳化；自訂策略清楚顯示 Native IR 或 Python
  signal compatibility mode，兩者的成交與風控仍由相同 C++ 核心處理。

### Phase 6：最佳化、選股與選股回測

- 把單次回測資料／指標快取跨參數組合重用，平行執行網格／隨機／walk-forward。
- 批次移植產業、基本面、技術、籌碼篩選與投資組合調倉回測。
- 未來函數旗標是 typed metadata，C++ 不得因資料存在就自動放寬。
- **出口**：排名、eligible、錯誤彙總、選股名單與 portfolio metrics 等價；
  相同 seed 在不同執行緒數仍產生相同結果。

### Phase 7：Qt 主圖 sidecar 與功能等價

- 建立 Qt chart shell，從 SQLite／native arrays 顯示主副圖、十字線、hover、
  viewport、指標設定、回測標點與多面板同步。
- 以真實 SQLite 快取測十年日 K 與大量分 K；不得用縮短歷史通過效能門檻。
- **出口**：Qt parity checklist 全過，切商品／週期與視窗操作無資料或狀態殘留。

### Phase 8：Qt 主視窗遷移與預設切換

- 逐頁搬移 GUI；Python broker workers 維持安全邊界，UI 更新統一由 Qt signal/slot。
- native engine 依子系統逐一設為預設；保留至少一個發布週期的 Python fallback。
- **出口**：真實長歷史、模擬帳戶、模擬券商連線實機驗證完成，才移除 tkinter／
  Matplotlib 主圖與舊回測熱路徑。

## 十一、效能與正確性門檻

效能數字以 Phase 0 同一台參考機為準，且**必須同時通過等價測試**：

- 100,000 根內建策略回測：native 核心 warm run 目標 ≤100 ms，且至少比 Python
  參考路徑快 10 倍；若兩者衝突，以結果正確為先。
- 500 組參數最佳化：至少比單執行緒 Python 快 8 倍；取消要求 ≤250 ms 回應。
- 2,000 檔 × 2,600 日 K 技術選股：warm data 目標 ≤3 秒。
- Qt 平移／縮放：p95 frame ≤33 ms；資料已在記憶體時，十年日 K 換圖 ≤350 ms。
- 1,000,000 根分 K 可保存在資料層並瀏覽；畫面透過 viewport／LOD 達標，
  不要求同時把一百萬根實體畫成一百萬個可互動物件。
- Python→C++ 轉換時間不得超過整體回測時間 20%；超過即視為邊界設計失敗。
- SQLite 完整 coverage 命中時 API 下載呼叫必須為 0；native range load 至少比
  `sqlite3 rows → pandas → NumPy → native` 參考路徑快 3 倍，且不得逐根進 Python。
- SQL coverage/range query 必須命中複合索引；任何全表掃描要有明確理由與基準。

## 十二、驗證策略

1. **Golden differential**：同一份輸入同跑 Python/C++，逐筆比較 intent、成交、
   費用、狀態、交易、equity、markers、metrics、選股結果。
2. **反向／突變測試**：刻意移除 T+1、防未來函數、成本、確認邊界或改錯乘數，
   測試必須轉紅，避免只有空殼等式。
3. **性質測試**：OHLC 合法性、時間排序、upsert 後冪等、成本不增加績效、
   不同帳戶狀態隔離、平行結果可重現。
4. **SQLite differential/concurrency**：同範圍 Python load 與 native read 逐欄一致；
   writer upsert 與 native snapshot 同時進行時不讀半筆、不長期 busy、不遺留鎖；
   coverage 命中、尾端缺口、中間缺口對應的 API 次數必須正確。
5. **原生安全**：編譯警告視為錯誤、ASan/UBSan、邊界／空值／超大資料 fuzz、
   ABI 版本與相依 DLL 檢查。
6. **Shadow telemetry**：正式功能仍使用 Python 結果時，背景抽樣跑 C++ 並只記
   匿名化差異摘要；不得把 API Key、帳號或完整策略原始碼寫進日誌。
7. **實機門檻**：真實 SQLite 長歷史、Qt 顏色／hover／縮放、模擬券商登入、
   runner 節拍與中止反應只能由實機確認；任何真實下單不屬於效能驗證。

## 十三、feature flags 與回退

- `native_sql_reader`
- `native_indicators`
- `native_strategy_engine`
- `native_backtest`
- `native_optimizer`
- `native_screener`
- `qt_chart`

旗標預設關閉，依 Phase 逐項開啟。native 載入失敗可回 Python，但必須顯示明確
警告與原因；執行中產生結果差異時該次工作直接標記失敗，不可靜默拿另一套結果
冒充成功。真正送單路徑在 shadow 階段只採用 Python authoritative intent；等價與
模擬實機門檻完成後才允許 native intent 成為來源。

## 十四、目錄規劃

```text
native/
├─ CMakeLists.txt
├─ include/stockbuild/     types、schema、storage、indicators、strategy、backtest、screener
├─ src/                    C++ 實作
└─ python/                 pybind11 module
core/
├─ native_bridge.py        版本／dtype 驗證與批次轉送
└─ engine_router.py        feature flag、shadow、fallback 與差異報告
strategy_lang/             Python AST validator、IR compiler、版本與 diagnostics
chart_qt/                  Qt shell、viewport、picture cache、互動與面板
tests/native/              differential、ABI、效能與 sanitizer 測試
```

## 十五、主要風險與處置

| 風險 | 處置 |
|---|---|
| C++ 快但語意縮水 | 第五節逐項 parity gate；未通過不切預設 |
| Python/C++ ABI 錯位 | 單一 schema、buffer protocol、offset/stride 雙端測試、版本握手 |
| SQLite 多 writer／鎖庫 | Python 唯一 writer；C++ read-only snapshot、RAII、busy timeout 與並行測試 |
| DB 有資料仍重複下載 | SQL coverage/missing-range 是下載計畫輸入；完整命中斷言 API 0 次 |
| 自訂 Python 無法原生化 | Python 標準語法編譯 IR；舊動態語法明示 callback 相容模式 |
| 使用者 C++ plugin 崩潰／繞過安全 | 不列為標準格式；若未來支援必須另開 ADR 並隔離行程 |
| 平行最佳化不重現 | 固定 seed、穩定排序、runtime 隔離、跨執行緒數測試 |
| Qt 與 tkinter event loop 衝突 | sidecar／獨立 shell，功能等價後才切主視窗 |
| C++ 繞過交易安全 | native 無 broker 依賴，只能回 OrderIntent；送單仍經 Python 唯一入口 |
| 發布機缺 runtime DLL | 預編譯 wheel、相依 DLL 打包、乾淨 VM smoke test |
| 長歷史又被效能修正裁掉 | 資料保存、指標計算、單幀 viewport 三層型別與測試分離 |

## 十六、完成定義

只有以下全部成立，ADR-143 才能改為「已完成」：

1. 第五節所有功能都有 C++ 核心路徑與差異測試，沒有硬編績效或靜默降級。
2. 終極波段可回測／最佳化；既有自訂策略至少由 C++ 掌管事件、風控、成交與績效。
3. 標準自訂策略以 Python Strategy Language 撰寫、可穩定編譯 IR 並由 C++ 執行；
   不可轉換的舊策略會明確顯示相容模式，不存在靜默換語意。
4. 選股、選股回測、最佳化與即時量化 runner 都使用同一份 native 條件／狀態語意。
5. C++ 直接唯讀 SQLite 已成為 native 預設資料路徑，完整 coverage 不再下載，
   且 Python 唯一 writer／關閉連線／WAL 並行測試全過。
6. Qt 主圖完整取代 Matplotlib 主圖，十年歷史與大量分 K 實機效能達標。
7. Python 仍是唯一券商／金鑰／真正送單邊界，所有交易安全診斷保持通過。
8. Windows 乾淨機安裝、升級、native 載入失敗回退、版本不相容拒載均完成驗證。
9. `test_core`、`test_brokers`、全部 diag、crossref、py_compile、native suite、
   sanitizer、效能基準與實機 checklist 全部通過並記錄版本。

## 十七、立即下一步

本 ADR 只批准架構與分階段路線，**不批准一次性重寫**。下一張實作 ADR 應只做
Phase 0：建立可重現基準、功能清冊與 golden differential fixtures；完成並審核後，
才開 Phase 1 的工具鏈與原生骨架。
