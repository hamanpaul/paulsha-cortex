# unified-outcome-taxonomy

- **`#499`／`#500`／`#487`／`#485`：三套 outcome 分類器收編成單一 taxonomy 模組**——
  「executor 失敗該歸哪一類」在 planning／build／review 三個 lane 各自實作了一次，各自
  漂移，於是同一型缺陷被踩了六次。根因不是關鍵字表寫錯，是**餵給關鍵字表的東西一開始
  就不該進來**（nested tool result、init metadata、CLI banner），或反過來**該當證據的
  結構化終局記錄被整個忽略**。新模組 `coordinator/outcome_taxonomy.py` 定義四大類
  outcome family（transient-service／content／environment／auth）＋共用 markers 表，
  三個 lane 共同消費；`#533` 的 planning 先行實作一併收編（其測試涵蓋原樣保留）。
  分類器拆成兩層，順序不可互換：
  - **證據分層**（`parse_stream_evidence`）：結構化終局記錄、CLI 原生 stderr、error 記錄
    屬 provider 證據；模型自己的話只拿來判 content；nested tool result 與 init metadata
    兩者皆不是，直接丟棄。64 KiB tail 開頭的殘行視為截斷產物，不當證據。
  - **共用 markers 表**：結構化證據優先於文字關鍵字；文字判定順序沿用 `#369`/`#370`
    （rate limit 必須先於 auth，限流訊息常同時帶 "authenticate" 字樣）。
- **`#499`：Claude review 429 被投影成 `foreign-review-absent`、`provider_outcome` null**
  ——stream-json 早就帶了機器可讀的限流證據（`rate_limit_event.status = rejected`、
  `rateLimitType = five_hour`、`resetsAt`，終局 `api_error_status = 429`），review lane
  卻完全不看，一律壓平成「沒有 review 結論」，operator 得自己翻 raw JSONL 才知道要等到
  什麼時候才值得 retry-review。修法：結構化限流證據以 `STRUCTURED` authority 落
  `rate_limited`、保留權威重置時刻（新增可選欄位 `provider_outcome.reset_at`，epoch 秒，
  舊四鍵 payload 原樣合法）；review lane 補上 build lane 早已有的分類投影線，
  `gate_reason` 改為 `foreign-review-provider-<outcome>`。**後續處置不變**：仍是
  needs_human、仍只提供既有的手動 retry-review 出口。
- **`#500`：tool parser 的 `timeout` 文字被誤判成 network failure**——`_TRANSIENT_RE` 的
  無界 `timeout` token 命中了 nested tool-result 裡的
  `Parser aborted (timeout, resource limit, or over-length)`，於是一個被 controller
  SIGTERM 停掉 no-progress 迴圈的 job 被判成 `transient`／`retryable: true`，recovery
  policy 據此排了不該排的重試，真正的 no-progress/tool-use 缺陷被 provider-degraded
  標籤蓋掉。修法在證據分層：tool result 不是 provider 診斷，根本不會走到關鍵字表；
  終局 `aborted_streaming` 以 `STRUCTURED` authority 落 `unknown`（維持既有不自動重試）。
- **`#487`：`doc-coauthoring` 被誤判成 OAuth authentication failure**——
  `github_rate_limit._AUTH_PATTERN` 的 `oauth` 是無界的，命中了 Claude init 正常技能名
  `doc-coauthoring` 裡的 `coauthoring` 子字串，把一次無關的工具失敗轉成不可重試的
  `auth`、封死正確的復原路徑。雙重修法：訊號收緊為 `\boauth\b`（`oauth token`／
  `OAuth-2.0` 仍命中，`coauthoring` 不再命中），且 init metadata 本來就不該是分類證據
  ——證據分層已將其排除。真正的 OAuth 失敗維持正向命中。
- **`#485`：Codex JSONL stdin banner 讓每次 foreign review 都成 `invalid-process-output`**
  ——Codex CLI 0.147.0 的 `codex exec ... --json` 會先把
  `Reading additional input from stdin...` 印進同一份 evidence log，
  `_review_log_has_only_json_lines()` 對每行做 `json.loads()`，於是 process exit 0、
  `.psc-review-verdict.json` 也寫好了的成功 review 永遠到不了 verdict 驗證。採 issue 列
  的第二條路：只在 parse 前剝離**精確、adapter 自有、且位於串流開頭**的已知 banner
  （`KNOWN_PROCESS_BANNERS`）。JSONL 純度檢查本身一格未放寬——不在該表上的任何非 JSON
  文字、以及出現在串流中段的同一句話，仍舊 fail closed。
