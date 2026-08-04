# StockBuild native foundation

這個目錄是 ADR-145～148 的 Windows 原生核心。提供 KBar ABI／批次邊界、SQLite
唯讀 RAII reader、prepared range query、C++ 欄式 KBar buffers、OHLCV resampler，
第一批 SMA／EMA／WMA／RSI／MACD／Bollinger，以及第二批 KDJ／DMI／JAE
指標核心；尚未接管正式回測、策略、選股、GUI 或送單。

## 建置與測試

在專案根目錄使用官方 Python 3.14：

```powershell
python -m pip install -r requirements-native.txt
python native/build_native.py
python tests/test_native.py
```

`build_native.py` 會尋找 Visual Studio Build Tools 的 `VsDevCmd.bat`，以 MSVC x64、
CMake 與 Ninja 建置，再執行 CTest。`tests/test_native.py` 每次使用乾淨的
`native/build-test`，並測試 Python import、ABI、100 萬根 zero-copy、SQLite range
differential／索引／snapshot／Windows 解鎖、時間分組語意、指標 differential、
唯讀輸出與 owner lifetime。

百萬根 SQLite → C++ buffer 的同機比較可執行：

```powershell
python benchmarks/adr146_sqlite_range.py --rows 1000000
python benchmarks/adr147_resampler_indicators.py --rows 1000000
python benchmarks/adr148_advanced_indicators.py --rows 1000000
```

若要對既有 SQLite 實際資料做 read-only shadow differential，可另外提供資料庫與
一個以上的 `SYMBOL|ASSET_TYPE|TIMEFRAME` case；輸出只保存雜湊 case ID：

```powershell
python benchmarks/adr148_advanced_indicators.py --database data/kbars.sqlite3 `
  --case '0050|stock|日K' --case 'TXFR1|future|日K'
```

可用 `python native/build_native.py --sanitizers` 啟用 sanitizer 設定；MSVC 主機還必須
安裝與 compiler 相符的 AddressSanitizer runtime，否則會在連結階段失敗。完整 native
邊界的 sanitizer 驗證使用：

```powershell
python tests/test_native.py --sanitizers
```

Windows 10 會由建置工具透過 `vswhere` 選擇含 ASan 元件的 Visual Studio 2022，並把
測試所需 runtime 複製到被忽略的 build 目錄；一般 Release build 選最新可啟動 MSVC。
建置目錄、runtime、`.pyd` 與本機 SQLite 檔案都不提交 Git。
