# StockBuild native runtime

此目錄只保留安裝說明，不提交平台／Python ABI 綁定的二進位檔。

在專案根目錄執行：

```powershell
python native/build_native.py --install
```

建置工具會把 Release `_stockbuild_native` 安裝到
`native/runtime/<Python cache tag>/`，並寫入 `manifest.json`。產品 loader 只搜尋
這個 ABI 隔離目錄、正常 Python import 路徑，以及明確設定的
`STOCKBUILD_NATIVE_DIR`；不會把 `build-test` 等測試產物當成正式 runtime。
