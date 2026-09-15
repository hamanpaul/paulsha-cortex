---
status: draft
work_item: quota-observation-schema-core
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

# Quota observation schema core — planning-only author audit

日期：2026-09-08。這是 author 自檢，不是 independent review，也不是產品 code review／驗收。已依 doc-coauthoring 將 A contract、caller authority、upstream fixture 與 B/C/D 延後責任分層，完成八件 draft。Root acceptance、child owner/issue、正式 publication/source binding 與 independent planning review 尚未完成；本報告不賦予 authority。

## Scope and Source Pin

Branch：`feature/refine-quota-schema-20260908`；固定 source/control baseline：
`588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86`。
此 authoring 只新增下表八檔，不修改 tracked files、產品/tests、umbrella、.cortex、registry、changelog 或 operator/shared state；沒有 commit/push、issue/dispatch/model call、付費 campaign、服務重啟或 credential 讀取。

| # | 新增文件 | 本次角色 |
| --- | --- | --- |
| 1 | docs/superpowers/specs/quota-observation-schema-core-spec.md | SP spec、I1–I10、AC1–AC6、責任 DAG |
| 2 | docs/superpowers/specs/quota-observation-schema-core-design.md | SP design、D1–D9 exact API/wire |
| 3 | docs/superpowers/workstreams/quota-observation-schema-core/todo.md | SP plan、12 個未勾產品 Tasks |
| 4 | reports/review/refine-quota-schema-20260908.md | 本 author audit |
| 5 | openspec/changes/quota-observation-schema-core/proposal.md | own proposal；byte-identical #1 |
| 6 | openspec/changes/quota-observation-schema-core/design.md | own design；byte-identical #2 |
| 7 | openspec/changes/quota-observation-schema-core/tasks.md | own tasks；byte-identical #3 |
| 8 | openspec/changes/quota-observation-schema-core/specs/quota-observation-schema-core/spec.md | own delta、十個 requirement 和正反 scenarios |

GitHub #835/#836 與 PatchMUD #37 僅 read-only issue scope。現有 source trace：registry.update_headless_result 確定 terminal outcome 後才 extract_usage；usage_extractors 的 Codex/Claude/Copilot token parser 和 AGY unsupported 現狀保留；StreamEvidence 已有 structured rate-limit/reset seam；usage_aggregate 是 workflow totals，不是 account ledger。model_profile 舊六欄 fingerprint／run/apply 不當作新 profile key 或 quota authority。精確 pinned source links 收於 spec Evidence，不拿 source-tree 當 installed/live evidence。

## Boundary Decision

A 唯一預定 production module 是 `paulsha_cortex/coordinator/quota_observation.py`，純 input→immutable schema/reference validation、binding status、caller-now freshness、source-event identity；consumer 未接。B 重用現有 parser 發展來源 adapter；C 承擔 durable replay/dedup、cumulative/delta、reset/clock/high-water/reconciliation；D 才接 shadow collector／read projection、真 pool issuer/account mapping 與 coverage。A 完成不關閉母 #836；#837/#838/#839/#842 的後續 gate 仍獨立。

保守工程決策：opaque authority/account/pool IDs、provenance 由 caller 提供；A 不發行、不猜 ID、不用 secret hash、不認證字串。未知 scope/alias/group 明示 unknown/incomplete。#849 尚無產品 API，僅採 pinned versioned ProfileRef framing fixture，不重算 fingerprint；正式 upstream conformance pending。source event identity missing 則 unavailable，available 只識別來源事件，一事件可有多 observation/measurement，無 dedup 承諾。

## Sizing — observed 與 projection 分帳

本次直接使用 baseline 的 `PlanningArtifact`、`assess_planning_artifact`、`assess_planning_completeness`、`compute_sizing_score`、`_evaluate_yellow_plan_review`、`sizing_band`；load 已存在的 `fix-standard` combo/cards，沒有另建極小 combo 或假造 envelope。輸入實測 9 cards、9 persona bindings、2 core gate_spine；規則 R-09/R-16/R-19；surfaces source/tests/documentation。

| 量表 | 實際依據／結果 |
| --- | --- |
| domain_breadth=0 | 單一 quota record contract 的 production domain；沒有 provider/registry/manager/consumer/authority owner 的產品變更。不是由單檔或「schema 很小」推論。 |
| state_consistency=1 | schema-version compatibility 與 record-local reference consistency；零 durable transition。ledger、水位、reset/reconcile 不含在這一分，進 scope 即重估。 |
| invariant_count=10 | I1 純度、I2 嚴格不可變、I3 extensible profile ref、I4 account/pool binding、I5 windows/gauge、I6 精確 unit/quantity、I7 provenance/unknown、I8 freshness、I9 event identity、I10 無 operational authority；十條各有獨立負例。 |
| acceptance_surfaces=2 | 2 個 core gates + 3 條適用規則，現行 helper 訊號為 5。沒有為降色拿掉 CLI/changelog/tests AC。 |
| orchestration=2 | 真 combo 9 cards/9 bindings；不是單卡假想分數。 |
| actual draft | SP 與 own 都 0/1/2/0/2=5 Yellow；complete=false，missing spec/design/plan；NO GO。 |
| memory-only accepted | 只在記憶體替換一次 frontmatter status，保留全部需求／OQ／Tasks；兩套皆 0/1/2/2/2=7 Red、complete=true。沒有寫回 accepted，不代表 authority。 |
| #831 projection | 假設修正後 control 真載入、三件組正式完整 accepted 才可能 5 Yellow；本次未改 control，未實跑該修正，不得取代現行 7 Red。 |

兩種狀態的純 plan-review 骨架皆 `ready=true`，checks_run 為 completeness/contract_compatibility/envelope；envelope 觀察明列 `bypass=envelope_unavailable`（lookup=None）。這只證 Tasks/規則字面覆蓋，**不能覆蓋 draft completeness=false、Red routing、缺 independent review 或 model qualification**。沒有從 registry 查真封套，也沒有借此刷新任何 exhausted model/native effort 資格。

## Six-view and Tasks Checks

下表是逐 view 查核，不只是 aggregate 的結果；declarations 全相同：domain/state/invariants=0/1/10、source/tests/documentation、R-09/R-16/R-19。所有原檔 status 仍 draft。

| View | actual draft assessment | memory-only accepted assessment |
| --- | --- | --- |
| SP spec | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| SP design | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| SP todo | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| own proposal | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| own design | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| own tasks | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |

`_collect_task_items` 對兩套 plan／兩種狀態均收集 12 項；first=`[ ] **T1 tests／RED（I1–I3）**`，last=`[ ] **T12 review/documentation／pre-archive closure（I1–I10）**`。所有 checkbox 未勾，兩套 Tasks 逐字一致。12 來自產品工作分層，不套固定 14。

負例：只在記憶體把 `## Tasks` heading 改名，不動內容；兩套／兩狀態均收集 0 項，純 manager helper 回 `ready=false`、failed_check=`completeness`、reason=`missing-task-for-surface: documentation, source, tests`。不把散落在其他段的 source/tests/documentation 當 Tasks 覆蓋。

所有 T1–T12 可在 own archive 前完成；archive 本身／archive 後 reverify／PR/merge／installed/live／parent closure 明列 downstream prose，不放進自身 archive 必須先全勾的 checkbox。Intake owner/review/publication/strict/unique mapping/source binding 則列在產品 Tasks 之前，不能等 openspec-propose 補 baseline。

## Pure Validation Ledger

執行環境 OpenSpec 1.4.1；以下 Python 命令以 `PYTHONDONTWRITEBYTECODE=1`，只 read candidate docs／既有 source。使用已讀的 preflight-ci 指引區分 configured gate 與額外診斷；沒有在缺真 PR context 的 draft 上宣稱 full preflight PASS。

| Check | 實際結果 | 可宣稱／不能宣稱 |
| --- | --- | --- |
| 六 view、兩 triad、兩狀態、metadata、12 Tasks、首尾與負例的純 Python checks | exit 0；結果如上 | 規劃 parser/helper 行為，不是產品 tests |
| `python3 scripts/openspec validate quota-observation-schema-core --strict --no-interactive` | exit 0，change valid | own 新 change strict 通過 |
| configured canonical `python3 scripts/openspec validate --specs` | exit 0，22 passed / 0 failed | 符合 .project-policy.yml 的實際 argv，沒有 full pytest/policy 結果 |
| 額外 `validate --specs --strict --no-interactive` | exit 1，18 passed / 4 failed | 額外 baseline strict 診斷，不是 configured preflight FAIL |
| 新增檔案 whitespace／scope／pair/hash/source-link existence checks | 最終八檔均無 whitespace error；3/3 mirror pairs 相等；8 個 distinct pinned source links 存在；tracked diff 為空、exact 8 untracked files | no-index diff exit 1 代表新增差異，無 error 輸出才算 whitespace 通過；不是 product tests |
| Design 字面 Decimal grammar | 直接讀 D3 regex，4 個 positive／12 個 negative 均符合 | 只驗規劃 regex，沒有實作或測產品 API |
| full pytest、產品 tests、build/install smoke、真 PR-context preflight／CI、independent review | 未執行 | 不得填產品 Tasks 或 claim product/installed/live PASS |

額外 strict 的四項都報 `[WARNING] overview: Purpose section is too brief (less than 50 characters)`；persona-workflow-orchestration 另有兩個非 failure 的 long-requirement INFO。四 canonical 檔案與 pin588 同 blob，沒有本次變更：

| Existing canonical spec | pin588 blob = working-tree git hash-object |
| --- | --- |
| cli-version-reporting/spec.md | 2aff96d92830b668c1143401d32cca1e5e561976 |
| persona-workflow-orchestration/spec.md | eb9735ae664b5358f06f329072b971ba4eaf6467 |
| porcelain-guided-bootstrap/spec.md | 034848439867ef7c64827dafe842069169eade1e |
| porcelain-inspect-surface/spec.md | 5c7d379fe97cc819adb1f7297f92c43154f8def6 |

`git diff 588d7d8 -- openspec/specs .project-policy.yml` 及所用 planning/manager/claim/deck helper 路徑無差異。沒有修這四份 baseline 或改 strict 政策。首輪新增檔 whitespace check 發現本 report 尾端多一空行，已只在允許檔案修正並完整重跑純 checks 至 exit 0。Root 後續有真 PR/accepted context 才跑正式完整 preflight。

## Content Hashes

SHA-256（report 本身不能穩定自含自己的 hash，最終八檔 hash 由 author handoff 外附）：

| Content | SHA-256 |
| --- | --- |
| SP spec = own proposal | c9529bf9b7aa0708413444f5b458b1ee3b9182905f03a4f864012bf70199c5ac |
| SP design = own design | aa8f0bed40d0c8fb05d16d811395cc5ebc32b2237746df1416f7e36c84cd7f11 |
| SP todo = own tasks | dcf2c7a58366ae34b520729dd04ef1a37030eb7bac1b53fd47d4f68509d42208 |
| own capability delta | 44d23fd9d42b2d440abd2bc37b5d56ad82a43015cd540fca1c38d2070c2ef342 |

## Remaining Decisions and Honest Limits

A wire／semantic design 無尚未裁決事項；Decimal grammar、strict keys/deep immutability、時間/window、reference、unknown/unit/pool 與 missing-event-ID 都有具體決策和負例。Root 可在 independent planning review 推翻設計，但不能只因格式完整自動 accepted。

尚待：child issue/owner、root acceptance、publication/unique mapping/source binding、independent review、完整 PR-context checks。這些是 intake 前置，非 A 產品設計缺欄位；正式 live issuer/account 認證、#849 真產品 conformance 和 B/C/D 則明列下游依賴。接受 stateless helper 的有限 clock 判斷與 caller assertion，不等於允許日後 admission 直接採信；未來 consumer 必須補 source/auth/coverage/ledger gates。

本次到八件 draft、pure checks、hash/scope handoff 即停止；不創 child、改 authority、修改任何產品、呼叫模型或續跑交付。

## R2 Receipt — Lifecycle 修補（latest，2026-09-08）

Receipt ID：`quota-schema-draft-r2-lifecycle-20260908`。本段依 root 本輪限定授權追加在同一 draft report，**不改動上方 R1 原文**；這不是允許未來修改已 frozen／immutable report。正式產品新證據仍須另出 candidate-specific receipt。R1 原 12162 bytes 的 SHA-256 保持 `e3ae1a272a4441b395d78931bd5aae5112efcc2b0b962392943d692d2235d42a`，R1 content hashes／checks 僅對修前版本有效。

Root 指出 R1 T11 把 remote CI 混入 pre-archive Task，T12 要 builder 先取得 formal independent review 並泛稱更新六 views/report，與 archive 時序和 freeze 邊界衝突。**R1「這版所有 Tasks 都可 pre-archive 閉合」的語意聲稱不能成立；本 R2 修正該 lifecycle 缺口，不把舊 helper PASS 當作原文沒有缺陷。** Spec/proposal、design/own design、todo/own tasks 與 capability I10 scenario 已同步；API/wire、十 invariants、0/1 sizing 宣告及產品範圍沒有改動。

### R2 Decisions

- T11 現在只要求 local focused/full、CI-exact engine pin／manifest steps 的 local preflight/policy、diff、local build/wheel smoke，以及本地 collection 加既有 CI argv 核對。PR 尚不存在時明列 intended title/body/labels/base/head，不能假稱 remote CI 或真 PR PASS。
- T12 改為 `tests/documentation／local coverage ledger`，只核對 candidate 專屬本地 evidence receipt 與 I1–I10/AC1–AC6 的已驗／未驗／下游 pending。builder 不必先取得／冒簽 Manager formal independent review 才可勾 T12。
- 正式 Manager independent exact-candidate review、finding 修／駁／列管與必要重驗仍必需，可依 workflow 時序在 archive 前發生；remote Python 3.10–3.13 CI/build/install smoke、真 PR-context gate／PR、archive/reverify、review threads、merge、installed/live、母 closure 仍各自待核收，不因本地 Tasks 閉合而省略。
- 正式 freeze 後 operator baseline 的六 views 不改；candidate 只可依現有 authority tolerance 做有據 checkbox toggles。非 checkbox 文字／需求／scope/hash 改動一律先回 root 正式 authority/review/source binding；新的本地／review 證據另出 receipt，不覆寫歷史。這個 draft authoring 修補不是正式 freeze 後的豁免。

本輪 read-only 核對 pin588 的 `.github/workflows/tests.yml`：Python 3.10–3.13 matrix 與 `python -m pytest tests/ -q` 已存在；policy-check 的 engine pin 是 `9e7fabbf0b5eea9ad933fa6798764b723934a0b7`。這僅是 CI 設定佐證，未執行 remote CI、產品 collection、full pytest、wheel build 或完整 PR-context preflight。

### R2 Revalidation

以同 pin `588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86` 真 helper／fix-standard（9 cards、9 bindings、2 core gates，R-09/R-16/R-19）重新讀六個修後檔案；不是沿用 R1 的結果：

| View | actual draft | memory-only accepted |
| --- | --- | --- |
| SP spec | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| SP design | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| SP todo | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| own proposal | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| own design | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| own tasks | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |

兩 triad 的 actual draft 仍 completeness=false、missing spec/design/plan、0/1/2/0/2=5 Yellow；memory-only accepted 仍 completeness=true、0/1/2/2/2=7 Red。後者只改記憶體字串，不寫回 accepted；#831 後 5 Yellow 仍只是投影。純 plan-review 骨架 ready=true、envelope_unavailable bypass，不是 formal review/qualification 或 dispatch 許可。

兩 plan、兩狀態均 12 Tasks、全部 unchecked；first=`T1 tests／RED`，last=`T12 tests/documentation／local coverage ledger`。只在記憶體移除 Tasks heading 的四個負例全部變成 0 Tasks，ready=false／failed_check=completeness／missing-task-for-surface documentation, source, tests。三組 mirror byte-identical，metadata 仍 0/1/10、source/tests/documentation、R-09/R-16/R-19。

R2 重跑 own change `validate quota-observation-schema-core --strict --no-interactive` exit 0；configured canonical `validate --specs` exit 0、22/22。額外 canonical strict 的四個 baseline warning 為 R1 歷史診斷，本輪未重跑／未修改其檔案。八檔 scope、逐檔 whitespace、8 個 distinct pinned source links、literal Decimal grammar 4 positive/12 negative 與 report 歷史 prefix/hash 核對均通過；不宣稱產品 API tests 通過。

### R2 Content Hashes

| 修後 content | SHA-256 |
| --- | --- |
| SP spec = own proposal | f6b0a2be68dbb0643e71ce31f41b147942ac44a4a449b78aa9ebc7c1a3f191ba |
| SP design = own design | cddc73c62df85b79962b8c49b453e82143de956ac5c302834bf17d2dbdc63370 |
| SP todo = own tasks | 55f095b1e11fe1c72fa7d6e9b5b7edaa69efcb2c7cb5f082df36784d4397556c |
| own capability delta | fdde1d1e5c69e176447b62f40ecdaaa9387dde78374538856462e1a375e5fd99 |

新 report 整體 hash 由本輪 handoff 外附，不能自含自身 hash。R2 仍只有既定八檔、全部 draft、parent #836、無 child issue/authority；沒有 commit/push、accepted/dispatch、產品/tests 變更、credential/model/live 操作。修後交 root 的 fresh review；author 不自行接受或繼續派工。

## R3 Receipt — Standalone unit catalog 與 unknown-unit 修補（latest，2026-09-08）

Receipt ID：`quota-schema-draft-r3-unit-catalog-20260908`。依 root 的 SAME8files bounded draft repair 授權追加本段，**上方 R1＋R2 原 17799 bytes 完整保留**，其 SHA-256 仍是 `3fc4be1cf133a71d35de359872c9247740cbc328557e37f66bba788f35328df4`；其中 R1 原 12162 bytes 的歷史 hash 亦不變。R1/R2 checks 和 hashes 只證修前內容，不能拿來當本版已受 independent review 接受。

### R3 Findings and Decisions

Fresh independent review 為 **FAIL，2 MAJOR**，root 亦獨立同意：

1. R2 D4 承諾 unknown scope 可保留已知 native usage，但 UnitRef 只能從 account-bound PoolDescriptor 解析，全部 account 未知／descriptors=() 的 cold-start 無法保存 123，除非捏造禁止的 pool/account。
2. R2 一面允許 unknown unit+unknown quantity，一面寫成四種 quantity measurement 都必須先有已知 amount/gauge，造成互相矛盾。

本版保留 promised native-evidence AC，沒有收窄為「等知道 account 才能存量」：新增同一純 module 的 `parse_unit_definition`／immutable UnitDefinition、及 parse_observation 的 required explicit `unit_catalog` tuple。Standalone root 五欄（schema_version/unit_id/version/quantity_kind/semantics_ref）不要求 account/pool/issuer，不查來源信任；原 PoolDescriptor.units inline 四欄不變，原 descriptor-only path 明示 unit_catalog=()。

catalog 0..16、standalone J<=2048 bytes；加上原 payload+16 descriptors 的跨輸入上界為 1146880 semantic bytes，最多272 distinct UnitRefs。每個來源 collection 內 duplicate UnitRef 拒收；跨 catalog/不同 descriptor 相同 Ref 只允許完整 definition 相等，否則 unit_conflict；missing/malformed known ref 不降 unknown。to_dict 保留各 wire shape、deep immutable snapshot；roundtrip 明示重傳 context，不藏 global catalog。未傳 required Python keyword 是 signature TypeError，已明示空 context 卻缺 known ref 是 unresolved_reference。

D4 的分支現已明定：unit_ref known 時 amount/gauge 規則永遠檢查，包括 quantity unknown；unit_ref unknown 時四種 quantity measurement 均可搭配 unknown quantity，但 observed/estimated numeric 拒收。known scope/window 的既知 unit-kind 矛盾仍拒收，不暗填 unknown UnitRef；limit_signal 仍 nonnumeric，source methods 限制不變。

D4a 提供完整 standalone definition 與 C1 observation JSON：descriptors=()、一筆 explicit native-unit catalog、unknown scope/profile/window/coverage、observed usage_delta exact "123"，不生成 account/pool/issuer，event identity 仍 unavailable。C2 覆蓋空兩 context 的四種 unknown-unit+unknown-quantity；C3 明列 duplicate/equality/conflict、malformed/missing ref、scope/window mismatch、numeric-without-unit、known amount/gauge/window mismatch、catalog caps 與 limit_signal 負例。Tasks T1–T7 與 capability I6 scenarios 已對齊；這些是未來產品 oracle，**本次未實作／執行產品 API tests**。

### R3 Scope, Sizing and Lifecycle Preservation

僅新增一個 planned data type/parser 及 caller-input resolution operand；沒有第二 production module、owner/writer/decision、account issuer、global unit registry、conversion、provider adapter 或 consumer 接線。因此 domain_breadth 仍0；state_consistency 仍1（versioned schema/reference compatibility，零 durable transition）。API、catalog collision/cap、unknown branches 的實作與測試工作增加，已明列而未用 sizing 不變掩蓋；十個 invariants 仍完整，新增 unit 風險歸 I2/I4/I5/I6，不是宣稱只有「一個 state」或已符合任何 model envelope。

R2 的10段 lifecycle 修補文字在 spec/design/todo/capability 四個代表檔逐字核對不變，三組 mirror 再驗一致：T11 local/intended-context/CI-collector，T12 local evidence ledger，正式 Manager independent review／remote CI／PR/merge/installed/live 下游要求，freeze 後 operator baseline 不改／candidate 僅 checkbox tolerance／非 checkbox 回 root authority，以及歷史 receipt 不覆寫，全部保留。

### R3 Revalidation — 修後六 views

仍以 source/control pin `588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86` 真 helper + fix-standard，9 cards／9 bindings／2 core gates、R-09/R-16/R-19：

| View | actual draft | memory-only accepted |
| --- | --- | --- |
| SP spec | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| SP design | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| SP todo | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| own proposal | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| own design | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |
| own tasks | accepted=false；status-not-accepted；markers=[] | accepted=true；reasons=[]；markers=[] |

兩 triad 實際 draft 均 completeness=false、missing spec/design/plan、0/1/2/0/2=5 Yellow；記憶體 accepted 均 completeness=true、0/1/2/2/2=7 Red，並未寫回 accepted。純 plan-review 骨架 ready=true 但 envelope_unavailable bypass，不是 independent review、authority、model qualification 或 dispatch。#831 後5 Yellow 仍未實跑的投影。

兩 plan、兩狀態皆收集12個未勾 Tasks，first=T1 tests/RED，last=T12 tests/documentation/local coverage ledger；四個 memory-only 移除 Tasks heading 負例皆0 Tasks，failed_check=completeness／missing-task-for-surface documentation, source, tests。metadata 全維持0/1/10 + source/tests/documentation + R-09/R-16/R-19。

本版 own change strict exit0；configured canonical validate --specs exit0、22/22。純 checks exit0：六 view、雙向mirror、十 requirements、scope/whitespace、8個 distinct pinned source links、Decimal literal4正/12反、兩份 D4a JSON syntax／鍵名／123 UnitRef 對應、cross-input cap 算術、R1/R2 歷史 prefix hashes。JSON/static arithmetic checks 不是 parse_observation 的產品測試，也沒有執行 full pytest、build、remote CI 或完整 PR-context preflight。

### R3 Content Hashes

| 修後 content | SHA-256 |
| --- | --- |
| SP spec = own proposal | 0b8d9032a5c8356d9d5f1aa698045280c431bc2cbdb50998e549657a5f3de166 |
| SP design = own design | 05ed061ab0b160bca24afd59341a3a0b3a47d8584d92fd2ac79151513016b992 |
| SP todo = own tasks | 3763c29ea202a39ddbd1dae9380ea40becbce0738ceba01c2a9e7ffd947afd8e |
| own capability delta | 2f883968ee5c8b895a0a9f8906df311fb0a0217a29946e25b1d4227a90797cf9 |

新 report 整體 hash 外附於 R3 handoff；原歷史不回填新 hash。仍只有允許八檔、全 draft、parent #836、無 child issue／acceptance／authority／commit／dispatch 或產品/live/credential/model 操作。此為 author 修補與純檢查完成，不是 MAJOR findings 已經 independent re-review PASS；現在 hash-stop 交 fresh review。

## Acceptance Receipt — #866 規劃內容接受（latest，2026-09-08）

```yaml
receipt_id: quota-schema-planning-acceptance-866-20260908
status: accepted
owner_issue: 866
parent_issue: 836
accepted_scope: planning-content-only
```

Root 已提供並核對 [#866](https://github.com/hamanpaul/paulsha-cortex/issues/866) OPEN/body readback、R3 fresh `quota_r3_review` PASS、原八檔 SHA/tar compare0，並授權本輪 metadata／intake 時態收斂。此為 root 對規劃內容的接受；writer 沒有另開票或自行提升權限。工作樹由 root 安全 ff 至 `94cc83567592a29cb66919c720209d405ed5e543`，R3 原八檔 bytes 在本輪改動前一致。

七份 active planning files 的 frontmatter 現為 status=accepted、owner_issue=866、parent_issue=836；鏡像仍逐位元相等。**本 report 原 frontmatter status:draft 是歷史資料，依 root 明確指示保留，當前規劃接受以本追加 receipt 判讀。** 上方 R1＋R2＋R3 的原24922 bytes 完全不動，SHA-256 仍 `a22ab2eed012bd4a4ed32d9ca43e69f491b25ca79ed90afdad64609f76af9b9a`；R1/R2 prefix hashes 亦維持原值。舊 draft／hash／修前檢查不改成當前新狀態。

### Exact Metadata and Intake Diff

只替換下列 R3 原行號（owner_issue 新增會讓後方目前行號加1）；每份另在 work_item 後新增 `owner_issue: 866`：

| 文件組 | R3 被替換行號 | 範圍 |
| --- | --- | --- |
| SP spec／own proposal | 2、24、30、84 | status、#866/內容接受說明、authoring 時態、已完成與待完成 intake 分帳 |
| SP design／own design | 2、22、277、291 | status、owner/內容接受說明、R3形成時點改歷史式、intake 時態 |
| SP todo／own tasks | 2、20、24、47、61 | status、owner/內容接受、intake 分帳、unchecked 的當前時態 |
| own capability delta | 2、20 | status、owner/內容接受說明 |
| 本 report | 只在原24922 bytes 後追加本 receipt | 不修改歷史 frontmatter／原文／舊 hashes |

七檔合計28個原有行的 allowlisted 替換、7行 owner_issue 新增；無其他 content delta。已用完整修前／修後文字精確比對，不只靠 rg 判讀：

- I1–I10 與 AC1–AC6 區塊 byte-identical。
- D1–D8 wire/API/fixtures/version 規則，除明確授權的 R3形成時點一句歷史時態外，byte-identical。
- 十二個 product Tasks 的完整文字及 unchecked 狀態 byte-identical，first T1／last T12。
- spec/plan sizing 區塊與0/1/10/surfaces/rules 不變；文中的 draft/memory-only accepted 分帳明示是 R3 受審歷史，不拿它聲稱目前仍 draft。
- capability 的整個 ADDED Requirements/scenarios byte-identical；freeze/operator baseline、candidate checkbox tolerance、非 checkbox 回 root authority 與所有下游 lifecycle 要求沒有改動。

### Acceptance Recheck

目前 HEAD94cc835 與原 pin588 的所用 planning/manager/claim/deck helpers 無差異；重新讀取六 views，均 accepted=true、reasons=[]、markers=[]、owner_issue866/parent836。兩 triad completeness=true；真 fix-standard 9 cards／9 bindings／2 core gates、R-09/R-16/R-19，得到 **0/1/2/2/2=7 Red**。此為目前內容 accepted 的真結果，未更改 sizing；#831 的5 Yellow仍僅投影。

兩 plan 均12 Tasks、全未勾；移除 Tasks heading 的 memory-only 負例均 failed_check=completeness。純 plan-review 骨架的 ready=true 仍含 envelope_unavailable bypass，不能當 model/native effort 資格或正式 authority。

metadata 收斂後 own change strict exit0；configured canonical validate --specs exit0、22/22；八檔逐檔 whitespace、三組 mirror、allowlisted diff 與歷史 prefix hashes 核對通過。沒有執行 full pytest／產品 API tests／完整 PR-context preflight／remote CI 或模型。

### Current Content Hashes

| content | SHA-256 |
| --- | --- |
| SP spec = own proposal | 4a52abbe1419eb3a137e045ab28eccc71f8c4f2c20d2770b9dcd65c71014aed8 |
| SP design = own design | ab94630a330db7564e77b6c2f650a07cba0fed5f938a8dc2977b1a2605eae536 |
| SP todo = own tasks | f4dba7a664f4f147f68c291d2ff9e254adfdf2e9b8920e2945e2f9db02c1d0cf |
| own capability delta | da8f33b8bd7ce61856ebdb23c35e35041a5bac2addc98fb8af048c64dcceb32c |

整體 report hash 外附於本輪 handoff，不能自含自身 hash。Root 平行處理的 .cortex/umbrella/CHANGELOG/fragment 等 integration 變更原樣保留，本 writer 未修改或還原；不把 root 的變更算成本輪八檔之外的 author 寫入。

**仍未 PR／exact publication／formal frozen authority／dispatch／qualification／產品 done。** Root 後續負責正式 integration、發布版檢查與核收；目前 only planning-content accepted，不改產品/tests、commit/push 或 operator/runtime。至此停止寫入，等待 root 核收。
