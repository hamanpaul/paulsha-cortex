---
status: accepted
work_item: quota-observation-schema-core
owner_issue: 866
parent_issue: 836
domain_breadth: 0
state_consistency: 1
invariant_count: 10
artifact_classes:
  - source
  - tests
  - documentation
applicable_contract_rules:
  - R-09
  - R-16
  - R-19
---

# Quota observation schema core 工作清單

這是 owner [#866](https://github.com/hamanpaul/paulsha-cortex/issues/866)、parent [#836](https://github.com/hamanpaul/paulsha-cortex/issues/836) 的 A child 規劃。R3 fresh independent planning review PASS，root 已接受規劃內容；尚未 PR／發布／formal frozen authority／dispatch／qualification。所有 checkbox 是未來產品工作，不是此次八檔內容接受已完成事項。唯一 production 檔案為 `paulsha_cortex/coordinator/quota_observation.py`；不接 consumer、不改 registry/extractor/manager/model identity/dispatch/shared state。Sizing 節的 draft／memory-only accepted 分帳保留為 R3 受審歷史，數值與 gate 邊界不因 metadata 接受而改動。

## Intake Prerequisites

產品 intake 前置中，child issue／owner #866、parent #836、R3 獨立 planning review PASS 與 root 規劃內容 acceptance 已完成；exact 三件組＋own OpenSpec 的 PR／發布、發布版 strict、唯一 child/work_item/change/spec/design/plan 正式 mapping 及 source refs/hash binding 仍待 root 完成，不等產品 openspec-propose 再補。當前 accepted 不是 frozen authority 或 dispatch 許可，沒有 model／effort 資格提升。正式 live issuer nomination 是 B/C/D 部署依賴，不是 A 擅自發行或驗證 account ID 的授權。

八檔對應：`docs/superpowers/specs/quota-observation-schema-core-{spec,design}.md`、本 workstream todo、`reports/review/refine-quota-schema-20260908.md`、`openspec/changes/quota-observation-schema-core/{proposal,design,tasks}.md` 及 own capability delta。spec=proposal、design=own design、todo=own tasks 必須 byte-identical，六 view 的 metadata 均為 0/1/10 + source/tests/documentation + R-09/R-16/R-19。upfront baseline 一開始就存在，非產品階段才補。

正式 freeze 後，六 views 的 operator baseline 及歷史／immutable reports 不改；candidate 僅容許依現有 authority tolerance 且有證據的 checkbox toggles。需求、Task 文字、scope/hash 等非 checkbox 改動先回 root 正式 authority 流程，不能以「更新產品證據」自行改寫 frozen content。新的本地 evidence、正式 review/finding 處置各留獨立新 receipt，不覆寫舊 report。

## Tasks

- [ ] **T1 tests／RED（I1–I3）**：新增 `tests/test_quota_observation.py`，先釘 D1 的 public API（含 parse_unit_definition 與 parse_observation 的 required explicit unit_catalog）／四種 DTO／error contract 和 synthetic fixtures，確認 baseline 缺 API 的收集／失敗結果；補 import-ready 後 assertion RED 的 strict keys/version、catalog/quantity mutation 與 profile domain cases。RED evidence 與產品 test pass 分帳，不把 import error 當完整語意 RED。
- [ ] **T2 source／core（I1–I2）**：只新增 `paulsha_cortex/coordinator/quota_observation.py` 的 D1 bounded walker、D2.1 standalone UnitDefinition parser、D7 immutable records/error/to_dict；exact built-ins/DTO tuples、cycle、deep snapshot、locator redaction、stdlib-only import，禁止 consumer 接線、I/O、global catalog 或要求未知 account/issuer。test sentinel 阻止 open/env/subprocess/network；前後 payload/catalog/descriptor source 深度相等。
- [ ] **T3 source/tests／descriptor binding（I3–I6）**：實作 D2 descriptor、standalone+inline unit union、profile/group subject、多 constraint 與 binding_status；known ref 精確 resolve，unknown alias 明示 incomplete。驗來源內 duplicate UnitRef、跨 catalog/不同 pools 全等 definition 接受與 quantity_kind/semantics_ref 衝突拒收、malformed/missing known UnitRef、known scope/window ref mismatch；原四欄 inline unit wire 與 descriptor-only path 明示 unit_catalog=() 仍可用。同 account/shared pool、多 profile、不同 account、short/week、duplicate/missing window、group unknown/member repeat 仍完整測，不展開 provider coverage。
- [ ] **T4 source/tests／quantity（I6–I7）**：實作 D3/D4 known-unit 與 unknown-unit 明確分支，實跑 D4a C1：descriptors=()、一筆 standalone UnitDefinition 保留 unknown scope 的 observed native "123"；C2：descriptors=()/unit_catalog=() 的四種 quantity measurement 配 unknown quantity 均接受（usage_total 仍必有 counter）。C3 驗 unknown unit 的 observed/estimated 數值拒收、known amount/gauge mismatch 即使 quantity unknown 仍拒收、known window 矛盾亦拒收。Decimal precision/last digit、0/-0、float/bool、NaN/Inf、exponent、非 canonical、單雙側 bounds/method_ref 與 ambient Decimal context 測試不刪；不同 native UnitRef 無轉換／加總 API。
- [ ] **T5 source/tests／observation and time（I5、I7–I8）**：實作 D4 scope/unit/window-instance consistency、measurement variants 與 D6 explicit-now freshness。釘 fixed/rolling interval width、instantaneous gauge、unknown instance、usage_total counter epoch unknown、TTL/time cap、now=expiry/reset/window-end、future +/-skew、receipt 早晚/重收與 stateless rollback 限制；不 refill、滑窗、差分或修復 ledger。
- [ ] **T6 source/tests／source event identity（I7、I9–I10）**：實作 source methods、provenance、coverage、D5 key/unavailable；無 provider/source-issued event ID、estimate/legacy 偽 observed、limit_signal 夾 numeric/counter 或不允許的 source method、partial 無 gap、complete 有 gap、同 event 多 measurement、同 key 衝突 payload、改 receipt/adapter version 不改 identity 等反例。對 D4a cold-start 123 assert quantity 保留而 event ID 仍 unavailable、scope 仍 unknown，不合成任何 account/pool/issuer；沒有 dedup／exactly-once／可派工結果。
- [ ] **T7 tests／bounded immutability and grammar（I2–I3、I6）**：parameterize 四種 root/variants 的 extra/missing keys、strict types/versions 與錯誤；UnitDefinition standalone 五欄與 PoolDescriptor inline 四欄不互相偷偷補 key。驗 unit_catalog 0/16/17、standalone J<=2048、各65536 payload、跨輸入上界1146880、重複 occurrence 不抵扣、其他既有 array/depth/node/string/Decimal/Time caps。依 D1 分開 shape 上限與 bounded walker seam；UnitDefinition input、catalog/descriptor context、to_dict 深層突變不影響已解析 record；roundtrip 明確重傳context，缺 known ref context 拒收，error 不回顯 payload/ref/path。
- [ ] **T8 tests／frozen upstream conformance boundary（I1、I3、I10）**：在 synthetic fixture 註明 profile schema/key framing 來源 pin 588d7d8 的 #849 design；四種 domain、未知 native model/effort 只作 opaque key 變化，不複製 profile parser／fingerprint、不 import 尚不存在 API。測 fake-but-well-shaped key 不產生 actual／qualification。正式 #849 產品 API 的相容性仍 pending，下游 prose 列管，不以 fixture PASS 填滿。
- [ ] **T9 documentation/tests／reuse seams（I1、I9–I10）**：更新 `docs/unified-work-lifecycle.md` 的純 schema versus existing usage／future quota ledger 邊界，README 依實際 public API 影響同步；read-only source trace 固定 registry.update_headless_result→extract_usage 與既有 StreamEvidence。不改這些模組／tests 的既有語意；以 diff scope 與 scoped import test 佐證無 caller 接線，全稱純度由 I1 runtime sentinel test 持續守護，不以靜態 grep 當永遠無其他 writer 的證明。
- [ ] **T10 documentation／own OpenSpec、CLI、changelog（I1–I10）**：沿用已 upfront 發布且 source-bound 的 own proposal/design/tasks/spec delta，保持六 view 同 Tasks／0/1/10／surfaces/rules，實跑 `openspec validate quota-observation-schema-core --strict --no-interactive` 與 canonical `openspec validate --specs`（依 .project-policy.yml，不額外加 strict）。產品 PR 新增並 commit `changelog.d/quota-observation-schema-core.md` 及 `CHANGELOG.md [Unreleased]`；規劃 authoring 不提前改它們。沒有新 CLI，仍在隔離候選安裝環境以 `python3 -m paulsha_cortex.cli --help` 做 CLI help smoke（R-16），不得送 work/dispatch request；CLI、docs、test、changelog 契約逐項留結果。
- [ ] **T11 tests／local candidate gates（I1–I10）**：focused 新測試 → `python3 -m pytest tests/ -q` → 以 CI exact engine pin 與 manifest steps 執行 local preflight-ci policy/OpenSpec/full tests → diff check → local build／隔離 wheel install smoke。policy/preflight 必帶 title/body/labels/base/head；PR 尚未存在時明示 honest intended PR context 並保留輸入身分，不能假稱真 PR 或 remote CI PASS。另以本地 pytest collection 及 read-only 核對既有 CI 的 `python -m pytest tests/ -q` 證明新測試會被收集；不要求 pre-archive 實跑 remote Python matrix，也不預先修改 CI workflow。只在候選／tmp 環境執行，不讀真 credential／呼叫模型／重啟服務；本地 gate 缺能力則明列未完成，不能拿 downstream CI 代替。
- [ ] **T12 tests/documentation／local coverage ledger（I1–I10）**：建立 candidate 專屬的新 evidence receipt，逐項對映 I1–I10／AC1–AC6 的本地已驗／未驗／下游 pending，核對 T1–T11 的指令、退出碼、collection、scope/hash 與可回放本地證據；缺項不得虛勾。此 task 不要求 builder 先取得或冒簽正式 Manager independent review，不要求 remote CI、archive、PR/merge 或 installed/live 先成功。只依核實證據與正式 tolerance 切換 candidate checkbox；不改 frozen 六 views 的非 checkbox 內容，不覆寫本 planning report／歷史 receipt。正式 review/remote CI 等下游義務保留於下段，planning-only author check 不能填成本 task 完成。

## Downstream Delivery — 非 archive 前置 checkbox

上述 T1–T12 全部可在 own change archive 前執行閉合；所有產品 Tasks 在本 accepted 規劃保持 unchecked。正式 Manager independent exact-candidate review、finding 的逐項修／駁／列管與必要重驗仍是必需 gate；它可依 workflow 時序在 archive 前發生，但不是要求 builder 先完成才可勾 T12 的依賴 checkbox。

root/owner 正式交付另外仍須 own archive、archive 後 reverify／policy、真 PR-context preflight／PR、remote Python 3.10–3.13 CI/build/install smoke、review threads 處置、merge、installed revision 與 live 維護窗口／驗收的各自證據。intended-context local PASS 不代替真 PR／remote CI；本地隔離 wheel smoke 不代表已部署或真實 Manager loaded revision。凍結後任何非 checkbox governed 變更不論 archive 前後都須回 root 正式 authority/review/source binding，新的證據另出 receipt，不重寫歷史。這些要求沒有刪除，只不放入 active change 必須先全勾的 Tasks，避免 archive 自相依；A 不含 live consumer，不能因 merge 宣稱 D 已運作。

#849 正式 upstream conformance、B adapters／真來源／issuer mapping、C ledger 去重／重啟與 reconcile、D shadow integration、#837 forecast／#838 reservation／#839 admission 與 #842 qualification 仍未完成；母 #836 不因 A 或本規劃完成而關閉。PatchMUD #37 只有 issue authority，不跑 paid campaign 或實施外部產品變更。

## Sizing and Gate Status

domain_breadth=0：只有純 in-memory observation contract owner；standalone UnitDefinition/caller catalog 是同 contract 的 unit reference 型別，不引入 account issuer/global registry/conversion/第二 writer 或 consumer。增加的 API、collision/cap/unknown 分支測試已納 T1–T7，不因分數不變縮減 AC；如新增 provider、registry 或 consumer 接線仍須重估。state_consistency=1：versioned schema/reference compatibility、零 durable transition；每 call bounded catalog 非持久化 ledger，counter/watermark/reset/reconciliation 全是 C。invariant_count=10 仍由 I1–I10 的獨立行為界線導出，不等於單 state 或 model 能力宣告。

pin 588d7d8 真 helper + fix-standard 9 cards/9 bindings/2 core gates，R-09/R-16/R-19 與 source/tests/documentation。實際 draft 預期 0/1/2/0/2=5 Yellow、completeness=false、NO GO；memory-only accepted 預期 0/1/2/2/2=7 Red，非 authority／非 readiness。#831 後 5 Yellow 只是投影，不改 current control、不以降低完整度換 dispatch。模型 envelope_lookup=None 的 bypass 只可觀測，沒有新 model/native effort 資格；full review/gates 未完成不得派工。

## Open Questions

無未定 A wire／語意問題。Intake Prerequisites 與 Downstream Delivery 的未完成項仍有效，不能以本次規劃內容 accepted 或純 helper ready/bypass 覆蓋。
