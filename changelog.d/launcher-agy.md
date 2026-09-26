#813／#945：補上 AGY commit-required 不得啟用 `--dangerously-skip-permissions` 的回歸斷言，並要求 builder 將測試與長命令留在前景同步執行、等待完成，不使用背景任務；README 另記錄 AGY `--allow-unsafe` 會啟用全工具核可，且該 executor 的權限剖面尚未依 #716 逐 executor 量測。
