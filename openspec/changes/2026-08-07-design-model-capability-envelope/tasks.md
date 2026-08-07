---
status: accepted
work_item: design-model-capability-envelope
---

# Tasks

- [x] 1.1 新增 `openspec/changes/2026-08-07-design-model-capability-envelope/`
      （`proposal.md`／`design.md`／本檔）。
- [x] 1.2 新增 `docs/superpowers/specs/design-model-capability-envelope-spec.md`
      （Requirements R1–R8，逐條回答 issue §8.1／§8.2／§5／§9.5，並記錄查證到的更正）。
- [x] 1.3 新增 `docs/superpowers/specs/design-model-capability-envelope-design.md`
      （Decisions D1–D9 與風險緩解，逐項附 main 檔案:行號證據）。
- [x] 1.4 `changelog.d/model-capability-envelope-design.md` fragment 與
      `CHANGELOG.md [Unreleased]` entry。
- [x] 1.5 `python3 -m pytest tests/ -q` 全綠（docs-only，基線不變）；
      `openspec validate 2026-08-07-design-model-capability-envelope --strict` 通過。
      補充（複驗實測，取代任何印象式宣稱）：`openspec validate --changes --strict` 為
      **13 passed, 0 failed**（13 個 change 總數已含本票，非另計 14）；
      `openspec validate --specs --strict` 為 **12 passed, 4 failed**（`cli-version-reporting`
      ／`persona-workflow-orchestration`／`porcelain-guided-bootstrap`／
      `porcelain-inspect-surface` 四個 spec 失敗，非 16/16 全過）——但此 4 項失敗在 main
      基線（`9bda3c0`，未套用本票任何變更）跑同一指令結果逐項相同，證實為既有回歸，非本票
      造成，不阻塞本票落地。

## 本票不做（範圍切分給後續實作票）

- 不改 `paulsha_cortex/coordinator/model_identities.py`（欄位白名單升版、schema v2→v3）。
- 不改 `paulsha_cortex/coordinator/claim_readiness.py`（接上真正的 `capability_lookup`）。
- 不實作 `capable()` 本體（`#138` judge 消費的六項合取式函式）。
- 不新增 `resource-inventory.yaml`（本票已論證其 owner／時程未定，見 spec R3）。
- 不實作 `#137` 的 `track_record()`。
- 不決定 `weight(work)`／`headroom(resource)` 是否為單一標量（issue §9.5「未收斂」原樣保留）。

## 建議後續實作票切分（皆為未來新 issue，不在本批）

1. **欄位 schema PR**——`model-identities.yaml`／`model_identities.py` 升版 schema v3，
   新增四欄位（`accepts_bands`／`invariant_ceiling`／`consistency_scope`／
   `acceptance_modes`），並補至少一個 `build` capability 身分帶齊四欄位（避免 `capable()`
   上線瞬間把現行 build 派工全部擋下，見 design.md D6／風險段）。驗收：既有
   `model_identities.py` 測試全綠＋新增欄位驗證測試（型別／值域／缺省 bypass 語意）；
   `IdentityRegistry.get/require` 對新欄位可查詢。
2. **`capable()` 實作 PR**——落地六項合取式純函式，接上 `claim_readiness.capability_probe`
   的 `capability_lookup` 與 `planning.plan_review_gate` 的 `envelope_lookup`（兩者共用同一份
   底層查表，只做形狀投影，見 design.md D7）。驗收：`claim_readiness.py` 既有 bypass 測試改為
   斷言真實過濾；Red band work 無法被指派給 `accepts_bands` 不含 `red` 的身分（issue §9 驗收
   條件之一）。
3. **judge 整合 PR**（`#138`）——把 `capable()` 接進 judge 的四因子公式，並落地
   `#136`/`#138`/`#209` 三閘的 admission「排隊不擋」語意（design.md D4 指出的
   `CHECK_ORDER` 落差需在此票處理）。驗收：`cortex stat` 可顯示每次指派的 `capable()`
   判定依據與被排除的身分原因（issue §9 驗收條件之一）；以 hippo #18/#41 縮小 canary 證明
   相同 work profile 不會被派給單一 build 身分 end-to-end。

## 驗收（本票）

R1–R8 逐條可對照回 issue #209 原文對應段落；`git diff` 只涉及 `docs/**`、`openspec/**`、
`changelog.d/**`、`CHANGELOG.md`，未觸碰任何 `.py`；`model_identities.py:115-122` 現有欄位
白名單與本票新增四欄位無命名衝突（本票文件已逐一核對，見 spec 驗收面）。
