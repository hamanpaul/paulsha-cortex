- **#851：AGY probe argv 建構 containment**：將 `build_agy_argv(...)` 納入既有
  smoke 失敗邊界；建構失敗只回傳 `smoke-failed` 的 AGY not-ready 結果，保留
  原有例外診斷與 safe probe 參數，不中斷非 AGY primary 的 runtime 建構。
