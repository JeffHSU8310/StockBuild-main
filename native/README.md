# StockBuild native foundation

這個目錄是 ADR-145 的 Windows 原生核心骨架。目前只提供 KBar ABI／批次邊界與
SQLite 唯讀 RAII reader，尚未接管正式回測、策略、選股、GUI 或送單。

## 建置與測試

在專案根目錄使用官方 Python 3.14：

```powershell
python -m pip install -r requirements-native.txt
python native/build_native.py
python tests/test_native.py
```

`build_native.py` 會尋找 Visual Studio Build Tools 的 `VsDevCmd.bat`，以 MSVC x64、
CMake 與 Ninja 建置，再執行 CTest。`tests/test_native.py` 每次使用乾淨的
`native/build-test`，並測試 Python import、ABI、100 萬根 zero-copy 與 SQLite 解鎖。

可用 `python native/build_native.py --sanitizers` 啟用 sanitizer 設定；MSVC 主機還必須
安裝與 compiler 相符的 AddressSanitizer runtime，否則會在連結階段失敗。建置目錄、
`.pyd` 與本機 SQLite 檔案都不提交 Git。
