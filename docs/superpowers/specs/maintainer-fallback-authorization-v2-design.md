---
status: accepted
work_item: maintainer-fallback-authorization-v2
---

# Maintainer fallback 以 content-addressed v2 授權取代同 run/head 既有 v1 設計

## Decisions

### D1 檔名依 schema 分流：v1 legacy、v2 content-addressed

`_authorization_record(body, *, state_path)` 在既有 identity 檢查（`run_id` 符合 `workflow-[0-9a-f]{20}`、`head` 為 40 hex）之後依 `body.get("schema")` 選檔名：

- `cortex-merge-authorization/v1` → `root / f"{run_id}-{head.lower()}.json"`（與現行逐字相同，既有「同名內容不同即 conflict」語意保留）。
- `cortex-merge-authorization/v2` → `root / f"{run_id}-{head.lower()}-{digest}.json"`，`digest = verification.canonical_json_hash(body)`（即 wrapper 的 `hash`）。沿用本檔 `_abandon_record`／planning-recovery 等 `{run_id}-{digest}.json` 的 content-addressed 慣例，另保留 head 方便稽核。
- 其他 → `ValueError("merge authorization identity malformed")`。

temp＋`os.link`＋`chmod 0o444`＋directory fsync 的寫入流程、`target.exists()` 時的 symlink／可寫／內容比對、回傳 `{"payload","hash","path"}` 形狀全部不變。v2 與 v1 路徑永不相同，寫 v2 不觸碰 v1。

### D2 v2 body 可選綁定 superseded v1

`_merge_authorization_body` 新增 keyword-only `superseded_authorization: dict[str, Any] | None = None`：

- `maintainer_review is not None` 且 `superseded_authorization is not None` → v2 dict 在現行欄位之外加 `"superseded_authorization_ref": superseded_authorization["path"]`、`"superseded_authorization_hash": superseded_authorization["hash"]`。
- `maintainer_review is None` 而 `superseded_authorization is not None` → `ValueError`（Copilot／v1 路徑不得 supersede）。
- `superseded_authorization is None` → v2／v1 形狀與現行逐字相同。

欄位進入 v2 digest，因此 v2 的 path／hash（也就是 CompletionRecord 的 `merge_authorization` trusted ref）以 content-addressed 方式涵蓋 v1 的 path／hash。

### D3 `_ship_with_maintainer_review` 解析 superseded v1，驗證先於任何寫入

在既有 `_validate_maintainer_review`、`ForeignReviewEvidence` 建構與 `evaluate_delivery_gate(review_kind="maintainer-review")` 之後、呼叫 `_authorization_record` 之前：

```text
existing = ship.get("merge_authorization") if ship else None
prior    = ship.get("superseded_merge_authorization") if ship else None
if _schema_of(existing) == v1:               # _schema_of：dict 且 payload 為 dict 才取 schema，否則 None
    if prior is not None and prior != existing: raise R4
    prior, existing = existing, None          # v1 退為 superseded，不再參與 persisted 比對
if prior is not None:
    if _schema_of(prior) != v1 or not _authorization_identity_matches(
        prior, active=active, authority=authority, binding=binding,
        head=preflight.head, tree_hash=preflight.tree_hash,
        terminal_reconciliation=True,
    ): raise R4
body = _merge_authorization_body(..., maintainer_review=maintainer, superseded_authorization=prior)
if existing is not None and (not isinstance(existing, dict) or existing.get("payload") != body): raise R4
authorization = _authorization_record(body, state_path=state_path)
if existing is not None and existing != authorization: raise R4
```

R4 = `RuntimeError("persisted merge authorization differs from current gate evidence")`（沿用現行訊息，不新增字串）。`terminal_reconciliation=True` 只放寬 authority_digest 為任一 64 hex——superseded v1 是 audit-only、不授予任何 merge 權限；新 v2 以 current authority 重算並由 `_validate_maintainer_review` 綁 current authority digest，因此在 `needs_human`／`copilot-*` 重入（及其他不經 `merge-authorized` 前置檢查的 phase）時，authority 在 v1 之後前進（issue 情境的 provider refresh；ship validator 每次以 `_rebase_delivery_journal_authority` 把 journal row 的 `authority_digest` 更新為 current，`work_bridge.py:2070`）不會卡住 recovery，也不會讓舊 authority 取得授權。`_ship_action` 在 `merge-authorized` phase 的前置檢查（`work_actions.py:5793-5809`，strict、不帶 `terminal_reconciliation`）不改：該 phase 且 v1 `authority_digest` 落後 current 時仍 `ValueError("ship merge-authorized state malformed")`，屬 spec Boundary 延後項（合乎 `docs/unified-work-lifecycle.md`「merge-authorized 與 merge 前 gate 不使用此例外」）。journal 寫入改為 `{**(ship or {}), "phase": "merge-authorized", ..., "merge_authorization": authorization}`，`prior is not None` 時再加 `"superseded_merge_authorization": prior`；後續 `merged` 寫入沿用 `{**active["ship"], ...}`，欄位自然保留。只認 journal 帶的授權，不掃磁碟 orphan v1。

### D4 replay 驗證 superseded pair

`_authorization_identity_matches` 的 key 檢查改為：v1 `review_required` 不變；v2 允許 `common_required | review_required` 或再 ∪ `{"superseded_authorization_ref","superseded_authorization_hash"}`（只帶其一 → False）。pair 存在時呼叫同檔新 helper（建議名 `_superseded_authorization_valid(body)`）：ref 絕對路徑、非 symlink、`is_file()`、`st_mode & 0o222 == 0`；hash 為 64 hex；讀檔 JSON（`OSError`／`UnicodeDecodeError`／`JSONDecodeError` → False）後要求 `set(wrapper) == {"payload","hash"}`、`wrapper["hash"] == superseded_authorization_hash`、`canonical_json_hash(wrapper["payload"]) == wrapper["hash"]`、payload schema 為 v1、`run_id`／`repo`／`work_id`／`head`／`tree_hash` 與 v2 body 相等。helper 在 v2 既有 `review_ref` 檢查之前或之後皆可，但任一不符整體回 False。`_trusted_evidence_refs` 不改：v2 仍輸出 4 筆、`maintainer-review` 為 current review、`merge_authorization` 指 v2。

### D5 風險／測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| issue 主路徑（stop 重入） | 沿 `tests/test_work_actions.py::test_ship_reenters_copilot_stop_through_bound_maintainer_review` 樣板；先以 `_authorization_record` 寫同 run/head 的 v1 body，journal `ship` = `needs_human`／`copilot-review-timeout`＋`merge_authorization: v1` | `merged-awaiting-closure`；v1 bytes／mode 不變；v2 檔名 `{run}-{head}-{digest}.json`、pair 指 v1；journal `superseded_merge_authorization == v1`；`_trusted_evidence_refs(v2)` 4 筆；v1 `authority_digest` 落後 current 的參數化結果相同 |
| merge-authorized 入口 | journal `phase: merge-authorized`＋v1（`authority_digest` 與 current 相同）、`MergeStatus(merged=False)`、args 帶 maintainer path／hash | 同上 |
| merge-authorized＋authority 落後（Boundary 護欄） | journal `phase: merge-authorized`＋v1 `authority_digest` 為另一個 64 hex、args 帶 maintainer path／hash | 現行 `ship merge-authorized state malformed`、無 v2 檔、`merge_if_ready` 0 次、v1 不變 |
| crash orphan v1 | 磁碟有 legacy v1、journal 無 `merge_authorization` | merge 成功、v2 無 pair、v1 不變 |
| 冪等／重試 | Orchestrator 第一次 raise `RuntimeError`、merge status 仍 false；第二次成功 | 兩次 v2 path／hash 相同；`glob(f"{run}-{head}-*.json")` 恰 1 |
| v1 身分不符 | journal phase 同主路徑（`needs_human`／`copilot-review-timeout`），v1 的 head 為另一 SHA | R4、無 v2 檔、`merge_if_ready` 0 次 |
| v1 竄改 | journal phase 同主路徑，v1 檔改內容或 chmod `0o644` | R4、無 v2 檔、無 merge |
| maintainer 證據不符 | args hash 錯／maintainer review candidate 不同 | `maintainer review does not authorize exact HEAD`、v1 不變 |
| 既有 v2 漂移 | journal 已有 v2，第二次 `checks` 內容不同 | R4、evidence 目錄 v2 數量不增 |
| replay pair | 直接呼叫 `_authorization_identity_matches` | pair 缺一 → False；v1 事後竄改 → False；pair 正確 → True（含 `terminal_reconciliation=True`） |
| Copilot 路徑不變 | `_authorization_record(v1 body)`；未知 schema | 路徑 `{run}-{head}.json`；未知 schema `ValueError`；既有 Copilot ship 測試原斷言 |

### D6 Sizing

1 個 production 模組（`work_actions.py`）→ `domain_breadth=0`；新增的持久化只有 v2 evidence 檔名、v2 兩個選填欄位與 journal `superseded_merge_authorization` 一個欄位，沿用既有「先寫 immutable evidence、再 `_save_runs`」順序與 content-addressed 冪等，不新增跨物件 CAS → `state_consistency=1`；三件齊全時機械三維固定 4，總分 5／Yellow。
