---
status: accepted
work_item: fix-agy-reviewer-json-schema
---

# Tasks

- [x] RED：新增 `tests/test_agy_reviewer_json_schema_880.py`，覆蓋 R6 的 AGY reviewer schema 共用、參數驗證、probe／builder 不變、launcher forwarding，以及 verification `details` 字串／空字串／object 邊界；focused pytest 對現行程式碼產生預期 RED。
- [x] launcher：依 D1／D2 實作 `build_agy_argv` 的 `review_terminal_kind` 與 `--json-schema`，並讓 `SubprocessLauncher` 對 AGY reviewer 傳入 contract kind。
- [x] manager：依 D4 在 verification terminal 對非空字串 `details` 正規化為 `{"text": ...}` 並留下 warning；空字串維持拒收。
- [x] GREEN／交付：focused 四檔全綠，補 changelog fragment，並完成 pinned planning 文件與 candidate 驗證。
- [x] repair（pre-archive）：收緊 AGY reviewer 的 terminal kind 必填契約，補上省略參數回歸測試，並重新驗證 focused／full pytest。
