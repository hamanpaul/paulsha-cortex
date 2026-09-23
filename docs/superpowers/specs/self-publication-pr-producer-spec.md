---
status: accepted
work_item: self-publication-pr-producer
issue: 980
parent: 963
---

# Manager PR Publication Producer Specification (#980)

## Requirements

### R1 — Canonical immutable intent before create

Before any GitHub PR create request, Manager MUST derive the #992 `manager_pull_request` immutable intent core from exact `repo`, `work_id`, `run_id`, `claim_key`, base repository/branch, head repository/branch, the 40-hex candidate commit SHA, and `request_metadata_digest`. Keep `ship_step_card` and ship `attempt` in #992’s producer-event basis, separate from the immutable publication intent core. Keep the candidate commit SHA typed separately from SHA-256 artifact hashes.

Build `request_metadata` exactly as #992/#994 specify: `title` normalized to NFC; marker-free `body` normalized only by CRLF/CR → LF while preserving every other code point; `labels` normalized to NFC, sorted, and unique. Reject a marker-free body containing any case-insensitive occurrence of `cortex-self-publication-intent:`. Compute `request_metadata_digest = H("cortex-pr-request-metadata/v1", request_metadata)` before rendering the marker-bearing body. Canonical JSON `J(v)` and domain-separated `H(domain, v)` MUST use #992's exact rules. Compute `publication_id = H("cortex-manager-pr-intent/v1", immutable_intent_core)`; the core excludes publication ID, marker, PR number, read-back, result, and mutable phase.

Render the final body per #992/#994: if normalized marker-free body is nonempty and does not end in LF, add exactly one LF; if it is empty or already ends in LF, add no separator; append exactly one standalone `<!-- cortex-self-publication-intent:v1:<publication_id> -->` line. The marker-bearing body and marker-free request metadata MUST NOT be confused in the digest.

Persist the immutable sidecar at the `intent_ref` used by #992. Its bytes MUST be exactly `J(intent_core)`; `intent_sha256` MUST be the lowercase SHA-256 of those exact raw bytes. Retain it permanently. Before remote create, conditionally persist the same intent identity, `intent_ref`, and `intent_sha256` in #983's delivery journal and confirm a `committed` result plus a fresh read-back matching the immutable row. The journal is operational state and cannot replace the #992 sidecar. Conflict, `unknown`, malformed result, mismatched bytes/hash, or unverifiable read-back MUST stop before POST.

### R2 — Confirmed create witness precedes authenticated GET

Call #982's structured create/adopt API using the canonical request and marker. Only a successful POST response with exact repository, positive PR number, positive REST `id`, and nonempty immutable `node_id` is `created`. Immediately persist that exact `{repository, number, id, node_id}` witness under the same intent through #983; proceed only after a `committed` outcome and a fresh journal read-back prove it is durable. A number, marker, matching metadata, or later GET is not a substitute for the successful POST response.

`adopted`, `ambiguous`, incomplete witness, conditional-write conflict, or `unknown` persistence result MUST NOT proceed to a producer receipt. A lost/timeout POST response remains ambiguous even if a later GET finds the exact marker and metadata. Do not blindly retry create or create a replacement intent.

### R3 — #994 verifies exact remote object and request metadata

Only after the successful POST witness is durably recorded may Manager call #994's authenticated read-only GET with the expected repository, PR number, POST REST ID and node ID, head repository/branch/candidate SHA, base repository/branch, publication ID, and normalized request metadata. Require exact tuple equality, canonical repo identity, `state="open"`, exact head/base facts, exactly one exact marker line, and exact normalized title/body/labels. #994's GET observation is identity/read-back evidence only; it is not creator proof. Missing witness or any mismatch fails closed. Label verification uses #994's authenticated issue GET as specified there.

### R4 — Append the closed #992 receipt through #993 once

Construct only the closed #992 `manager_pull_request` receipt union. Preserve top-level `schema`, `receipt_id`, `producer_kind`, `producer_event_id`, `publication_id`, `repo`, `work_id`, `run_id`, `claim_key`, `pre_publication_authority_sha256`, `accepted_input`, `acceptance_evidence`, `published_object`, and `transaction`; do not add keys. `accepted_input` keeps repository, branch, and candidate commit SHA as separate fields. `acceptance_evidence` binds the durable intent and successful POST witness. `published_object` binds the exact open PR, head/base facts, marker, number, REST ID, and node ID. `transaction` uses #992's exact closed keys, including `kind`, `intent_ref`, and `intent_sha256`, referencing the retained sidecar bytes `J(intent_core)`.

Use `pre_publication_authority_sha256 = work_authority_digest(WorkAuthority)`; never relabel `WorkflowRun.source_revision` as an artifact hash. Derive `producer_event_id` and `publication_id` with #992's exact domains and inputs, and derive `receipt_id` with #992's canonical envelope rule. After the exact #994 observation, conditionally persist the observation in #983 and verify it by fresh read-back. Then call #993's private append seam once with the exact event/receipt and coupled PR fields. #993 commits the receipt plus `WorkflowRun.pr_refs`/`source_revision` under its registry revision CAS. Fresh-reload the registry and revalidate the complete receipt and sidecar. Exact replay is idempotent; same event/publication pair with changed payload conflicts.

### R5 — Restart and uncertain writes remain fail-closed

A restart may continue only from the same immutable `intent_ref`/`intent_sha256` sidecar, same #983 intent row, and a durably committed successful POST witness. Verify sidecar bytes/hash, reload the journal, repeat #994 GET against that exact witness, persist/read back the result, then resolve or replay the same #993 append. If POST response was lost, or the witness was not durably committed before crash, remain ambiguous and mint no receipt even if the marker matches.

For every #983 `conflict`, `unknown`, persist-then-raise, stale-snapshot, fsync/replace failure, or mismatched reload, stop before the next side effect and retain an explicit run-bound diagnostic. Do not infer commit from mere file visibility or stale in-memory data. For #993 uncertain append outcome, reload and resolve the same `(producer_event_id, publication_id)` before any retry; never overwrite a winner with stale registry state.

### R6 — Guard adopted and legacy `pr_refs` paths

An adopted/external PR, existing `run.pr_refs`, matching branch/head/marker, `planning_authority`, source membership, or equal bytes/hash is not this run's producer receipt. Before any metadata synchronization or delegation to merge-capable `work_actions._ship_action`, the existing `run.pr_refs` path MUST require a valid #992 receipt bound to this exact run/claim, retained intent sidecar, confirmed POST witness, and matching #994 observation. If any evidence is missing or invalid, stop with explicit insufficient-provenance diagnostics. Never retrofit a marker onto an adopted PR. The canonical Manager metadata body written to `_metadata_file` includes the exact marker so safe metadata synchronization preserves it.

### R7 — Preserve old/invalid history and parent scope

Old `WorkflowRun` objects without receipts remain readable as empty receipt collections per #992. Unknown schema, malformed row/container, bad digest, duplicate/conflicting event, cross-repo/work/run/claim binding, missing sidecar, or drift remains invalid/insufficient and MUST NOT be upgraded by read, adoption, start/intake, or restart. Preserve raw invalid diagnostics through the #992 value contract and #993 registry carry-forward.

### R8 — Keep production ownership and dependencies exact

#980 changes only Manager orchestration in `paulsha_cortex/coordinator/work_bridge.py`. It consumes #992's frozen receipt/intent schema, #993's registry append, #994's authenticated read-back, #982's structured create/adopt API, and #983's conditional delivery-journal API. It does not change `workflow.py`, `registry.py`, `github_delivery.py`, or `work_actions.py`; it does not implement #979 planning publication, #964 consumers, #965 status/CLI/delivery integration, or the #965/#847 loaded-runtime canary. #978 remains the aggregate umbrella and is not a prerequisite to this producer.

## Acceptance

- [ ] Positive path follows the exact ordering: canonical intent and permanent `J(intent_core)` sidecar; #983 committed intent row; #982 successful POST witness; #983 committed witness; #994 authenticated exact GET; #983 committed read-back result; #993 single append; fresh registry reload. Lost/unknown/conflicting writes stop before downstream side effects.
- [ ] Golden vectors match #992/#994 canonical JSON/hash, exact intent fields, marker-free metadata digest, and final body rendering. Marker-free input containing a casefold sentinel fails; body line endings preserve all non-CR/LF code points; title/labels use NFC; labels sort/deduplicate.
- [ ] Receipt has exactly #992's closed manager PR union; `transaction.kind/intent_ref/intent_sha256` resolve to retained raw bytes exactly `J(intent_core)`; sidecar SHA, IDs, and confirmed POST witness are re-computable.
- [ ] #994 independently rejects wrong repo/number/id/node_id/state/head/base/marker/title/body/labels. GET/marker alone never proves create origin. Timeout/lost POST, adopted PR, or missing durable witness mints no receipt.
- [ ] #983 tests cover two writers/stale revision, committed/conflict/unknown, same-run immutable replay/conflicting payload, persist-then-raise, and crash at write boundaries; #980 never advances on conflict/unknown. #993 exact replay/conflict and registry CAS are separately verified by its owner.
- [ ] The legacy `run.pr_refs` path is guarded before metadata sync and merge-capable shipping; full formal start/intake foreign-artifact negative remains negative even with matching `planning_authority` and bytes/hash.
- [ ] Fault/restart matrix covers sidecar write/read, journal intent/witness/result durability, POST lost response, GET mismatch, registry commit uncertainty, and every crash boundary. No automatic replacement create, remote PR deletion, merge, or old-receipt backfill.
- [ ] Full #847 AC01–AC10 remains in the retained parent block; #980 contributes PR producer evidence only and does not close #847 or AC10.
## Parent #847 AC01–AC10 (retained in full)

These parent acceptance criteria are reproduced unchanged. #980 implements only the Manager PR publication producer slice that contributes evidence to AC01/AC04; it does not close any parent criterion by itself.

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

- #980 is blocked by #992, #993, #994, #982, and #983. It does not wait for #978 aggregate merge.
- #994 and #982 both change `github_delivery.py`: finish/merge #994 first, then serialize #982, then wire only `work_bridge.py` in #980. Do not parallelize the same production module.
- #992 owns frozen receipt/intent unions and canonical formulas; #993 owns registry carry-forward/private append/full registry revision CAS; #994 owns authenticated read-only GET observation; #982 owns structured create/adopt; #983 owns conditional delivery journal writes. #980 owns Manager orchestration and durable sidecar creation/reference plus the producer transaction only.
- #979 is the separate planning producer and may proceed after #992/#993. #964 consumes valid receipts after producer slices; #965 owns status/CLI/delivery and AC10 live canary. #978 stays open until aggregate evidence is complete but is not a direct prerequisite to #980.
- Keep the complete #847 AC01–AC10 aggregate gate. This packet adds no authority to treat tests, a merge, install, or a marker as live-canary or parent completion.

## Evidence and limits

Read-only source inspection found the current Manager ship path calls the integer-returning `create_or_get_pull_request()`, then updates `WorkflowRun.source_revision`/`pr_refs`; that integer does not distinguish create from adoption. The existing `run.pr_refs` continuation reaches metadata sync and merge-capable shipping, so #980 must gate that path before either side effect. #982 preserves the current integer caller contract until #980 migrates it. No product source/test, receipt, issue mutation, PR, merge, installed runtime, or live canary is included in these planning artifacts.
