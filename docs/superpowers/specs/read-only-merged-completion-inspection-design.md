---
status: accepted
work_item: read-only-merged-completion-inspection
issue: 995
---

# 唯讀 merged completion closure inspection 設計

## Decisions

### D1 — 將讀取與 durable writer 分成兩個 API

在 delivery.py 新增明確命名的 ShipOrchestrator.inspect_remote_closure（名稱可由實作保持同語意）。它只做 remote read、in-memory CompletionRecord validation/identity comparison、closure evaluation，回傳 inspection result。既有 verify_remote_closure 維持 writer/read-back 語意；不加 allow_write=False 這種容易被誤用的旗標，也不讓讀者誤以為它沒有副作用。

### D2 — 共用現有 closure 判準

由既有 GitHubDeliveryClient.fetch_remote_closure 取得 RemoteClosureFacts，透過 evaluate_remote_closure 做 PR head、candidate parent、merge commit、default ancestry、issue、OpenSpec/archive、Todo gate。helper 對 default_head 格式和 remote payload malformed 保持 fail-closed。不要在 Manager 複製第二套 closure 判準。

### D3 — 驗 draft 後才把 evaluator 的 record-valid bit 設 true

第一階段呼叫 `fetch_remote_closure`，保留原始 `RemoteClosureFacts.completion_record_valid=False` 並回傳同次 `default_head`；不接收 draft、不呼叫 evaluator。Manager 以此 head 組成完整 draft 後，第二階段只接受原 snapshot 與該 draft，再用 `completion.validate_completion_record` 正規化記憶體 draft；由 authority、fresh merge facts、run ID、step IDs 與 proof-validated refs 組成 expected `work_authority`，要求 draft 完整欄位 exact equal；candidate 必須等於 expected_head，target_ref_sha 必須等於當次 facts.default_head。只有這些檢查全數成功，才以 `dataclasses.replace(facts, completion_record_valid=True)` 產生 evaluator facts 並呼叫 `evaluate_remote_closure`。invalid draft 絕不可到達 evaluator。不可呼叫 `completion_records_semantically_match` 作為 exact check，因其忽略 volatile source revision 與 optional fields。旗標只代表本次記憶體 draft 已通過 validation，不表示磁碟已有 record。

此 API 不驗證 evidence 檔案的內容。#975 proof oracle 必須先提供已驗證 ref/hash；inspector 驗證這些 identity 在 draft 與 expected binding 中 exact 相同，不把 remote observation 當成 workflow gate pass。

### D4 — 回傳只讀觀察，不代替 completion persistence

第一階段 result 帶 raw facts/default_head 且 record-valid=false；第二階段 result 至少帶 normalized/evaluated facts、gate result、default_head、merge_commit、PR head/parent、Todo revisions、normalized record draft、expected authority binding。它不宣稱 CompletionRecord 已存在、已寫入或已 read-back；Manager #977 在 record/outcome boundary 另做 exact persistence/read-back。測試應 spy evaluator：valid draft 走到 evaluator 時旗標必為 true；invalid draft 必須在 evaluator 前拒絕。

### D5 — GitHub client read-only boundary

fetch_remote_closure 必須保持只使用 GET。任何 closure 新需求若需要 GitHub write、repo push 或本地 workspace 操作，不放進本 API；由其他 owner/issue 處理。測試應對 runner 捕捉 method/path，拒絕非 GET request。

### D6 — 不改既有 ship flow

verify_remote_closure 可留在原控制流程，或只重用抽出的純 helper；呼叫它的 production ship validator、_ship_action 和 existing callers 的 persistence/order/return semantics 必須不變。測試檢查舊方法仍 write record/read-back，而新 inspector 不寫。

### D7 — Scope and sizing remain one module

唯一 production module 為 delivery.py，domain_breadth=0。多個 remote source 在同次唯讀 observation 中需一致比對，state_consistency=1；不做跨-store durable transition。fix-standard gate spine=2、R-09/R-16/R-19=3，所以 acceptance_surfaces=2；accepted artifacts 使 spec_stability=0；9 cards/9 persona bindings 使 orchestration=2。正式 sizing 總分為 5 / Yellow。若需改 GitHub client、CompletionRecord model 或其他 production module，立刻停止並重新實算，不能沿用本結果。

## API sketch

The implementation should expose two read-only API results for one durable-boundary observation. The first call returns an immutable raw snapshot with fresh RemoteClosureFacts, default_head and Todo revisions, keeping completion_record_valid=false and without invoking the evaluator. Manager uses that default_head to create a complete draft. The second call accepts only that snapshot plus the draft and returns:

- allowed/reasons from the existing closure evaluator after validation;
- normalized CompletionRecord draft;
- complete expected WorkAuthority binding derived from the exact inputs;
- the same snapshot identity/default_head used by the first call;
- no path, write receipt, registry revision, journal row, or claim that persistence has occurred.

Each later durable boundary starts a fresh first call; neither API caches or reuses a previous observation.

Inputs must be explicit keyword arguments. The API must not accept an arbitrary temporary WorkflowRun or let the caller claim passed gate status. It receives proof-validated evidence references only as identity values.

## Verification design

Add tests/test_delivery_read_only_closure.py with a fake runner returning GET payloads for PR, repo/default ref, compare, merge commit, issues, OpenSpec tree and Todo files. Assert first-stage GET returns default_head with record-valid=false, Manager builds a complete draft using that head, and second-stage inspection validates and normalizes the draft first, then calls the evaluator with completion_record_valid=True; invalid/missing/conflicting draft must not call the evaluator or promote the flag. Reject non-merged PR, wrong head, missing candidate parent, non-merge commit, wrong default ancestry, open issue, active/missing archive, incomplete Todo, target_ref_sha/default drift, authority/source revision drift and malformed responses.

Mutation audit should patch CompletionRecord writer, quarantine/atomic writers, evidence writer, registry/journal/outcome writers, workspace creator, preflight/push, archive helper and _ship_action to raise if reached. Assert the request runner sees only GET and temp workspace/durable stores remain byte-identical. Separately retain a regression proving verify_remote_closure still performs its established CompletionRecord write/read-back. Run focused tests, full repo-required tests and policy check with the eventual PR context; report external GitHub state only as read evidence, never as a mutation.
