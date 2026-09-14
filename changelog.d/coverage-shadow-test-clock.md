# coverage shadow reader 測試改用動態時鐘

`tests/test_coverage_shadow_reader_591.py` 把 `NOW` 固定在 2026-08-16，而 reader 以真實時鐘做 30 天 TTL 清掃；2026-09-14T12:00Z 起所有合成記錄都被判過期清掃，兩個 CLI 測試在任何分支都必紅，連帶所有 work item 的 ship preflight 與 CI pytest 矩陣全紅。改為測試執行當下的 UTC 時間。
