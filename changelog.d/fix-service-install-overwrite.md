### Fixed

- **Issue #148：service install 不可覆寫既有 manager env 的 Python 指向**：`cortex install service` 當既有 runtime env 中的 `PY` 指向不同有效 venv 時，改為直接中止並回報清楚錯誤；同時修正既有相對/無效 `PSC_AGENTS_ROOT` 的覆寫行為，避免因既有參數異常而中斷或誤覆蓋。
