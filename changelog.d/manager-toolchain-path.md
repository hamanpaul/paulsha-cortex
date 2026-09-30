### Added

- **#716 Manager 的 toolchain PATH**：installer 產生的 Manager EnvironmentFile 帶 `PATH`，形狀與 job 相同（toolchain 排最前、系統尾段、不含 sbin）。system 部署的 ship lane 以相對名呼叫 `openspec validate`／`openspec archive`，不再因 openspec 只裝在 toolchain 而 `FileNotFoundError`（#716）。
