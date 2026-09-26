# git-runner-strip

- **#550**：`git_runner` seam 保留完整 stdout，避免 porcelain 輸出的前導空白遭移除；`rev-parse` 呼叫端自行清理 SHA 換行，並補上 dispatcher、autonomy 與兩份 runner 的回歸測試。
