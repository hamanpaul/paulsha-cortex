---
status: accepted
work_item: pr-create-adopt-provenance
issue: 982
parent: 963
---

# GitHub PR Create/Adopt Provenance API Specification (#982)

## Requirements

### R1 — Return a structured create/adopt outcome

Add an opt-in `create_or_get_pull_request_with_outcome()` result with exactly classified `created | adopted | ambiguous` status, available typed PR facts, machine-readable reason, and successful POST witness when confirmed. Preserve the existing `create_or_get_pull_request()` integer return and current caller behavior until #980 explicitly migrates the Manager call site; this child does not modify `work_bridge.py`. The structured API MUST NOT return a number-only result as publication evidence.

Return `created` only when this authenticated POST call completed successfully and its response includes canonical exact repository, positive PR number, positive REST `id`, and nonempty immutable `node_id`. Preserve the successful response tuple `{repository, number, id, node_id}` exactly for #980 to commit through #983 before #994 GET. This is only a creator witness; it is not a receipt. The structured API returns the outcome before any post-create metadata synchronization, preserving the exact successful POST witness for #980 to persist before #994 GET.

### R2 — Use #992/#994's canonical marker and request rendering

For a new PR, place the marker supplied by the durable Manager intent into the initial create body. Consume #992/#994's exact canonical request metadata and final body rules; do not introduce another canonical JSON encoder, digest formula, marker parser, or GET validator. The marker is exactly `<!-- cortex-self-publication-intent:v1:<publication_id> -->` as a standalone line. Marker-free input containing any case-insensitive `cortex-self-publication-intent:` sentinel is invalid. Title/labels NFC, body CRLF/CR → LF only, label sorting/deduplication, separator LF handling, and digest-before-marker follow the frozen A/C contract.

A matching marker is public correlation data only. The API must send the exact canonical body provided by Manager in its original POST; it cannot claim origin from body similarity.

### R3 — Never retrofit a marker onto an adopted PR

A PR found before this operation's successful create response is `adopted`, even if repo/branch/head/body/marker match. The structured API returns `adopted` before any metadata sync or write; do not mutate an adopted PR on this branch. Do not add, replace, or rewrite its intent marker. `ensure_pr_metadata()` may be called only after #980's valid-receipt/provenance guard; then it may preserve an exact existing marker and repair unrelated fields, but MUST reject marker deletion/replacement and MUST NOT add a marker to an unmarked PR. Metadata repair cannot turn `adopted` into `created`.

### R4 — Keep a lost POST response ambiguous

A timeout, lost response, uncertain HTTP result, or incomplete/malformed successful response is `ambiguous`. Diagnostic lookup may return remote facts, but even an exact marker/head/body match cannot reconstruct the successful POST witness. Never automatically issue a replacement create to escape uncertainty. #980 must stop without receipt unless it possesses and durably committed the exact successful POST response tuple.

### R5 — Limit ownership to the GitHub client API

#982 changes only `paulsha_cortex/coordinator/github_delivery.py` and focused tests/docs. It does not perform authenticated read-back (owned by #994), Manager journal writes or immutable sidecar retention (owned by #980 using #983), receipt serialization/IDs (owned by #992), registry persistence/append (owned by #993), or consumer/status/canary work. Its safe API consumes the exact #992/#994 marker and request contract.

### R6 — Preserve current caller until #980 migration

The current Manager `_ship_action` consumes the legacy integer. #982 keeps that method/caller contract intact and adds the opt-in structured API for later Manager migration. Test both contracts. Legacy integers never constitute a confirmed-create witness and #980 MUST NOT use them to mint receipts.

## Acceptance

- [ ] Successful new PR POST returns `created` only with exact repo, positive number, positive REST ID, and nonempty node ID; witness fields are preserved for #980/#983. Structured outcome never collapses to number-only evidence.
- [ ] New PR's first POST body includes exactly one #992 marker rendered under the #992/#994 canonicalization; marker-free sentinel, wrong line endings, extra marker-like lines, or malformed metadata are rejected according to shared contract.
- [ ] Existing unmarked, foreign-marker, or exact matching-marker PR returns `adopted` or `ambiguous`, never `created`; the structured API returns before metadata sync and performs no write/PATCH on the adopted branch.
- [ ] Lost/timeout response and incomplete witness remain `ambiguous` even if a later lookup finds matching marker/body/head. No replacement PR is created.
- [ ] The structured API performs no metadata PATCH after a confirmed create and before returning its POST witness or #994 read-back. Separately invoked metadata synchronization after #980's receipt guard preserves an exact marker and fails closed on removing/replacing it; it cannot add a marker to an adopted PR or upgrade its outcome.
- [ ] Tests cover POST response tuple success, missing/malformed/mismatched response, unmarked/foreign/exact-marker adoption, matching-marker timeout ambiguity, duplicate matches, source/head/base drift, adopted-branch no-write, no post-create metadata PATCH before witness return, and marker-preserving synchronization. A transition test pins the legacy method's integer result/current caller behavior.
- [ ] No `work_bridge.py`, journal, sidecar, registry, GET validator, receipt, consumer, or live canary implementation is included. Full #847 AC01–AC10 and #965 AC10 gate remain unchanged.
## Parent #847 AC01–AC10 trace

The complete parent gate is reproduced below unchanged. #982 supplies a narrow GitHub API prerequisite that can contribute provenance evidence to AC01/AC04; it does not implement or pass those parent criteria by itself.

- [ ] AC01：建立明確且版本化的「自身 frozen publication metadata 等價／真實 authority 變更／證據不足」分類。只有 exact repo/work/run/claim-era、已接受 frozen artifact、內容 hash、受治理 source owner／發布 provenance 全部可驗證，且其他需求、權限與 authority 成員均不變，才允許 metadata 等價；不得只按 source prefix、檔名、同 bytes 或 caller 自述決定。

- [ ] AC02：在正常 monitor ingestion → confirmed authority／claim → automatic action／Manager tick 的 production 路徑，注入自身已 frozen 內容延後被納入 source membership 的情境；原 needs_human 與理由、已接受 gates／evidence、candidate／verified head、attempt 計數、job／model invocation 數、既有 execution model binding 均不變，零額外 spawn。不得只測私有 helper、預造分類成功或把清 facet 當成正確接續。

- [ ] AC03：metadata 等價可由既有正式 resume／reconciliation consumer 安全消費，保留原 frozen authority／accepted evidence 的綁定語意與可重算 provenance。不能原地改寫舊 job、evidence 或 claim-era 來冒充相容，也不能只避開一次 reset 卻使後續 tick 因 digest 不合反覆 restart；合法 operator recovery 仍明示可用且不被自動觸發。

- [ ] AC04：缺少可信歷史 frozen snapshot、發布 receipt／provenance 缺失或不一致、未知 schema、同 bytes 但陌生 owner／另一 run／另一 work／非該 run 已接受產物等負例，均不可取得 metadata 豁免。不能從現有檔案反推歷史授權；保留受治理拒絕／人工處理或既有合法裁決，具體原因可見。

- [ ] AC05：真正內容、需求範圍、issue／PR／OpenSpec／todo 成員、owner／permission／Trust Root 或其他語意 authority 的變更，逐欄負例仍走既有合法 authority restart／精準 invalidation／換代政策；不得全域關閉 restart、剝除全部 planning source 或保留實際已失效的 gate。

- [ ] AC06：metadata 判定前後注入 authority／source owner／artifact bytes 漂移，以 exact-run／authority 比較與 CAS 或等效持久一致性機制拒絕或重裁決。相同 metadata 重送、兩個競爭 request、monitor 與 resume 交錯不得產生額外 attempt、雙 spawn、重寫 evidence 或放寬模型授權。

- [ ] AC07：分類前後及持久化邊界注入 crash/restart，既有 run／needs_human／accepted gates 與 metadata 決策可安全重建；重送冪等，無永久 digest mismatch 迴圈，延遲舊 terminal 不覆蓋現行 accepted state。缺證據不能在重啟後變成預設等價。

- [ ] AC08：decision／status／audit 明確區分 metadata 等價、真實 authority change 與缺證據；列出 old/new authority reference、source delta、被驗證的 frozen artifact／publication provenance 與保留／失效 stage、合法 next action，不輸出 secrets、不偽造 approval／passed evidence。旁觀者可查明為何沒有重跑或為何必須重裁決。

- [ ] AC09：新增正常 production 路徑的不變量守門與負控制；移除 provenance／owner／其他 authority 不變檢查，或讓等價事件落入 destructive reset，測試必須變紅。重驗 #524 in-flight 保護、#216 精準 invalidation、#776 自產 OpenSpec 與明示 recovery；對 #843／#844 只驗本票與其 recovery conformance／same-era reuse 契約邊界，未交付能力不得標 passed，不要求本票實作 conformance／reuse 引擎，也不要求等待兩票整包完成。全稱結論由 runtime-path invariant test 維持，靜態掃描僅作當下佐證。

- [ ] AC10：正式 source／tests／documentation／CLI status 的 producer→consumer 接線均交付；已載入修正版 runtime 的受治理 canary 記錄 runtime identity、同內容 source-membership 事件、前後 needs_human／gates／attempt／job／model binding 與零額外 spawn。既有資料須相容讀取、未知／缺欄位 fail closed。未實作、source test、merge、installed 與 live 證據分開，不以 unit test 取代 live 驗收。

## Dependencies and delivery accounting

- #982 depends on #992's frozen receipt/marker union and #994's authenticated read-back contract. Both are prerequisites; merge/serialize #994 first, then #982 in the shared `github_delivery.py` module. #982 does not wait for #978 aggregate merge.
- #980 depends on #992/#993/#994/#982/#983 and owns Manager intent/sidecar/witness/result orchestration. It migrates the current caller from legacy integer method to the structured API only after #982 lands.
- #983 owns protected conditional delivery-journal writes; #992 owns WorkflowRun receipt value/canonical IDs; #993 owns registry append/CAS; #994 owns authenticated GET observation. #982 owns only safe create/adopt outcome and marker-preserving client behavior.
- #978 remains the aggregate umbrella until all child/producer/consumer evidence is complete, not a direct prerequisite to #982/#980. #964/#965/#847 keep their consumer, status/CLI, and loaded-runtime canary boundaries.
- No product implementation, live PR mutation, issue change, merge, install, or canary is part of these planning artifacts.
