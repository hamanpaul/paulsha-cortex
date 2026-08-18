# 670-probe-fence

- **`#670` `probe_agy_capability()` 不再因為模型加了 markdown code fence 就偽失敗**——
  probe 問的是**語言模型**，卻直接 `json.loads(smoke_stdout.strip())`。實測（票上表格）
  6 次有 1 次模型把**完全正確**的 JSON 包進 ```` ```json ```` fence，於是 `JSONDecodeError`
  ⇒ `_failed_agy("malformed-output")` ⇒ probe not ready ⇒ `select_secondary_planner()` 回
  `no-heterogeneous-planner` ⇒ 整個 run 進 `needs_human`。約 17% 的 define 階段憑空死掉，
  而 blocking_reason 指向「沒有異質 planner」——把**格式解析問題**誤報成**拓撲問題**。
  - 新增可測的 `strip_code_fence()`：支援 ```` ``` ```` 與 ```` ```json ```` 兩種開頭、
    有無尾隨 fence、CRLF、fence 前後空白、以及單行 ```` ```json {...}``` ````。
  - 刻意**只**處理「整串剛好是單一 fenced block」，與 `planning_runtime._find_json_object`
    的頂層嚴格語意一致：帶前言散文的輸出原樣落 `malformed-output`，剝 fence 是**純結構**
    動作，不負責從散文撈 JSON，**更不會把「內容真的不對」順手救成 ready**——本體 JSON
    合法但欄位不符時仍是 `identity-mismatch`（有測試釘住這一格）。
  - prompt 同步補上與 `planning_runtime._JSON_OUTPUT_CONTRACT` 同款的顯式輸出契約
    （`no code fences`／`MUST start with '{'`），在源頭壓低 fence 機率；`strip_code_fence()`
    是模型仍不從時的保底。
- **`#670` probe 失敗時帶出實際 stdout 節錄，不再只留一個沒有線索的 reason**——
  `malformed-output` 過去 `diagnostic=None`，現場零線索，票上的成因是靠人工重跑六遍才看見。
  新增 `stdout_excerpt()`：前 200 字元、連續空白壓成單一空格（不污染單行 log）、空輸出標
  `<empty>`，`malformed-output` 與 `identity-mismatch` 兩路都帶上。節錄內容是模型對一段
  **寫死在本模組的** probe prompt 的回應，argv 不帶憑證、env 不回顯，沒有把 token 帶進
  log／evidence 的路徑。
- **`#670` 附帶修復：`agy models` 改成兩欄 tab 輸出後，probe 100% 死在 `model-not-listed`**
  ——2026-08-18 實機驗證 fence 修復時撞到。`agy models` 現在輸出
  ``gemini-3.1-pro-high\tGemini 3.1 Pro (High)``，整行比對時字面與正規化雙雙落空（整行
  正規化成 `gemini-3-1-pro-high-gemini-3-1-pro-high`），probe 連 smoke 階段都到不了。
  這比 #670 的 fence 偽失敗**更早、更絕對**，且同樣是「格式漂移偽裝成能力／拓撲問題」。
  `_resolve_agy_cli_token()` 改為：比對可用整行或任一欄，但**回傳一律是 id 欄**——
  `--model` 只吃得下 kebab id，`Gemini 3.1 Pro (High)` 這種顯示名不是合法 CLI 值。
  單欄（舊格式）行為逐字不變。
