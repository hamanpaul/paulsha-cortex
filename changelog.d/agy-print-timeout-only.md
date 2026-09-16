# AGY print timeout

- **#824 AGY print timeout**：launcher 現在會解析 `PSC_AGY_PRINT_TIMEOUT` 或既有 gate timeout fallback，對所有 AGY headless argv 形態顯式加入 canonical `--print-timeout <Ns>`，並在 direct launch／capability probe 前 fail-closed 拒絕非法或超界設定。
