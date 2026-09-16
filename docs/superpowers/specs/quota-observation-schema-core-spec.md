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

# Quota observation schema core 規格與 proposal

## Why

[母 issue #836](https://github.com/hamanpaul/paulsha-cortex/issues/836) 要求共享 account 的多 pool／window 額度觀測、原生單位、來源、未知與 coverage gap。現有 terminal usage parser 已可擷取部分 token 資料，卻不是 account remaining、pool mapping、ledger 或 admission。本 child A 只交付純資料契約，讓後續來源與 ledger 可以有共同、可拒收錯誤的輸入邊界；不重做既有 parser。

Owner 為 [#866](https://github.com/hamanpaul/paulsha-cortex/issues/866)，parent 為 #836。R3 fresh independent planning review PASS 後，root 已接受本三件組與 own OpenSpec 的規劃內容，metadata 為 accepted；尚未 PR／exact publication／formal frozen authority／dispatch／qualification，也不是產品 PASS。所有 product Tasks 均未完成。文中 draft／memory-only accepted 的 sizing 分帳保留為 R3 受審歷史，不是現在的 metadata 狀態；本次接受不改其數值或 gate 邊界。

## What Changes

唯一預定 production 變動：新增 `paulsha_cortex/coordinator/quota_observation.py`，提供 versioned immutable records（含不依附 account/pool/issuer 的 UnitDefinition）、caller 明示 bounded unit catalog、strict shape／reference validation、caller 注入時間的 freshness 分類，以及 source-issued event identity 的可用／不可用結果。沒有 durable state transition、時鐘讀取、I/O、provider branch、global catalog、配置／registry 更新或現有 consumer 接線。

測試新增 `tests/test_quota_observation.py`；synthetic fixtures 置於該測試或其專用 fixture 目錄。產品交付另更新 `docs/unified-work-lifecycle.md`、必要 README 與 changelog，新增 candidate 專屬 evidence/review receipts；不得覆寫既有歷史報告。正式 freeze 後，本規劃／自有 OpenSpec 六 views 的 operator baseline 不改，candidate 只容許有證據的 checkbox toggles；任何非 checkbox 修改先回 root 走正式 authority 流程。現有 CI 已執行 tests；新增測試須以本地 collection 證明可收集並核對 CI argv，remote CI 實跑另屬下游，不預定修改 CI workflow。本次規劃內容接受仍只修改已列的八件文件，不提前新增上述產品／測試／changelog。

## Capabilities

### New Capabilities

- `quota-observation-schema-core`：有限資料結構、精確 Decimal wire、opaque identity refs、多 window、source provenance、unknown 與 pure freshness／event identity。

### Modified Capabilities

無；現有 job usage、backoff、profile resolution、資格、dispatch、ledger、admission 全部不改。

## Impact

A 以 stdlib 實作，獨立可測、可交付但暫無 production caller。只 import 已存在的 stdlib，不 import 尚未產品落地的 #849 profile API，也不新增 PatchMUD／Hippo runtime dependency。import、parse、validation、serialization 與 helper 都不得開檔、讀 env／credential、啟動 subprocess、網路或模型。

## Requirements

以下 I1–I10 是十個獨立的行為不變量；數目來自需要分別 falsify 的 contract，不是為滿足 model envelope。Design D1–D9 是精確 wire 與錯誤 oracle。

| ID | 不變量與理由 | 必須證明的反例／結果 |
| --- | --- | --- |
| I1 | 純函式、無 consumer 接線；schema 不得偷偷建立 quota 或 authority side effect。 | import／parse／helper 不 I/O；既有 terminal usage 與 registry 接線不變。 |
| I2 | 嚴格 version、keys、types、資源上限與 deep immutability；不讓隱藏欄位或後續 mutation 改變裁決。 | extra／missing key、bool-as-int、未知 version、cycle、catalog/跨輸入超限拒收；UnitDefinition、descriptor/context、to_dict 的修改不影響已解析 record；錯誤不回顯 payload。 |
| I3 | profile 僅凍結、具 domain 的 versioned opaque ref；新 agent／model／native effort 不須 quota core 分支。 | request／resolved／observed／actual domain 原樣保存；fake key 的 shape 合法不等於真 profile、actual 或資格；不重算 fingerprint。 |
| I4 | pool/account/authority 與 profile/group binding 皆由 caller 明示；共享 pool 不能因 executor/model 分拆。 | 同 PoolRef 可被不同 profile 引用；不同 account 不混同；alias／group／constraint 未解以 unknown/incomplete 表示，不能猜 ID 或憑 secret hash 分戶。 |
| I5 | 每一 window constraint 保留身份與原生量綱，固定／rolling／instantaneous 不互加。 | 同 pool 的 short/week 均保留；duplicate known constraint 拒收；已知 gauge window 只能 instantaneous、未知 window 必須明示 unknown；unit/window/measurement 已知矛盾拒收，缺 window 不冒充 unconstrained。 |
| I6 | 精確十進位量與 account/pool 無關的原生 UnitDefinition/version 分離；不做跨單位轉換。 | descriptors=() 用 explicit standalone catalog 保留 unknown scope 的 observed native "123"；空兩 context 時 unknown unit+unknown quantity 四種 quantity measurement 可記；known ref 缺失／definition 衝突／numeric 無 unit／已知 amount-gauge 矛盾拒收；fractional 值保留，token≠credit≠time≠API-equivalent cost。 |
| I7 | observed、estimated、unknown 與 source／coverage 分別存證，不藉有效格式提升可信度。 | estimate 不可標 observed；legacy 無可辨來源不升級；unknown 無 amount；coverage complete 只是 caller assertion；limit signal 不合成 remaining=0。 |
| I8 | 時間只分類 freshness，不回補額度；received_at 不能更新 observed_at 的年齡。 | TTL 邊界、future timestamp、missing time、已過 reset、clock rollback 的有限判斷皆可測；不得 full reset、永遠 fresh 或把未知當零／無限。 |
| I9 | source event identity 和 receipt、usage kind、ledger 去重分離。 | 無 provider/source-owned event ID 回 unavailable；同 event 可產多 measurement；重收改 receipt 不改 event key；不以內容 hash／時間戳偽造唯一 ID，不執行 dedup 或 reconcile。 |
| I10 | 合法 record、完整 binding、fresh 或 event key 都不是 admission／資格／權限。 | unknown coverage、未認證 authority、共享 account 外部用量缺口可表達；任何回傳型別均無 can_dispatch、eligible、authorized 或 reservation 判斷。 |

## Acceptance Criteria

- AC1（I1–I3）：public API、bounded strict input／error vocabulary、round trip、deep immutability 與 frozen profile-ref fixture 正反例通過；沒有新增任何現有產品 caller。
- AC2（I4–I5）：synthetic 同／不同 account、profile/group 多對多、short/week/gauge、unknown alias 與 unresolved reference 測試；只保留 constraint，不執行全供應商 snapshot 覆蓋算法。
- AC3（I6–I7）：Decimal grammar、exact/bounds、原生單位隔離、observed/estimated/unknown、legacy/limit_signal/coverage 測試；必須包含 D4a standalone cold-start 123 正例、四種 unknown-unit+unknown-quantity 正例，以及 malformed/missing known ref、來源內 duplicate、跨來源 equality/conflict、scope/window ref mismatch、numeric-without-unit、known amount/gauge mismatch 負例。不能為縮小 child 刪掉未歸戶 native evidence 的保留能力。
- AC4（I8–I9）：顯式 now/skew 與 TTL/reset 等號、missing／future time、receipt 重播、無 ID unavailable、同 ID 多 measurement／衝突 payload 測試；結果不宣稱已消重或防 crash。
- AC5（I10）：無 dispatch／qualification／account authenticity 判斷；synthetic fixture 不當作真模型、真剩餘額度或 official-provider 覆蓋證據。
- AC6（I1–I10）：pre-archive 留存 local focused/full suite、OpenSpec strict、CI-exact pinned local preflight/policy、diff、local build/wheel install smoke 與 test collection 證據；PR 尚未存在時以明示 intended title/body/labels/base/head，不假稱真 PR。正式 Manager independent exact-candidate review、remote Python 3.10–3.13 CI/build/install smoke、真 PR-context gate 仍是完整交付必需證據，依 workflow 時序另核收，不設為 builder pre-archive checkbox 的依賴；未完成項明列 pending。

## Boundaries and Dependency DAG

`#835 → #849 profile core（尚未產品）→ A 正式 upstream conformance`；A 開發先用 versioned profile-ref frozen fixtures，**不阻斷純 A 實作，但正式相容性證據 pending**。A 不複製 profile descriptor、native effort taxonomy 或 key 演算法。

`A → B source adapters → C durable replay/reconciliation → D shadow collection/read projection`；C 也直接依賴 A 的 record contract，D 依賴 A/B/C 的已接受版本。B 重用既有 usage／StreamEvidence parser，依實際官方可讀介面拆來源；C 管 source-event 去重、多 collector、cumulative/delta watermark、restart/corrupt、時序／reset／來源衝突與 shared account；D 才接正式 account pool mapping、外部 coverage、shadow runtime consumer。這不是要求 A 提前實作 B/C/D。

#836 母 AC 的「相同 account 不重算、舊事件不回補、所有來源覆蓋」仍屬 B/C/D，A 完成不關閉母 issue。#837 forecast、#838 reservation、#839 admission 另須對應觀測／ledger，以及 #835 profile 和 #842 qualification；#825 只承擔既有 backoff/reset，不被 A 擴寫。PatchMUD [#37](https://github.com/hamanpaul/paulsha-patchmud/issues/37) 只具 issue authority，提供 versioned usage/unit/source/profile evidence 的 CLI/file consumer boundary，不是 Cortex account remaining；不讀其外部產品實作、不執行付費 campaign。

正式 live pool-ID issuer nomination、憑證到 account 的授權 mapping、真來源認證與 operator/shared state 屬 B/C/D／admission 部署驗收。A 只驗 caller 給的 opaque IDs、provenance refs 與相互引用，不發行、不猜測、不認證；未知 authority 必須由 caller 使用 unknown scope/constraint 表達，已填字串也不表示 authorized。

## Intake Prerequisites

進入產品 intake 的前置分帳：child issue／owner #866 與 parent #836 已明確，R3 獨立 planning review PASS，root 已接受規劃內容；仍須發布 exact 三件組及 own OpenSpec、對發布版本重驗 strict、完成唯一 work-item ↔ child ↔ own change ↔ spec/design/plan 的正式 mapping 與 source refs/hash authority binding。不得等產品 openspec-propose 才補 baseline。PR／發布、正式 intake／frozen authority、dispatch、qualification 尚未完成，不能由 accepted metadata 代替。

正式 freeze 後，六 views 的 operator baseline 與既有 immutable reports 不得修改；candidate 只可依現有 authority tolerance 做有證據的 checkbox toggles，非 checkbox 的需求／文字／scope/hash 改動必須先回 root 正式 authority 流程。新證據寫入獨立新 receipt，不重寫歷史或用新 hash 蓋掉舊核收。

自身 OpenSpec 的 Tasks 只含可在 archive 前完成的產品實作／本地測試／文件／證據核對。正式 Manager independent review 仍必需，且可以依 workflow 時序在 archive 前發生，但不要求 builder 先取得或冒簽該 review 才能勾 T12。archive、archive 後 reverify、remote CI、PR／merge、installed revision、live 維護窗口／qualification、母 #836 closure 各自保留為下游交付要求；不得把自身 archive 或這些下游結果放進 archive 的前置 checkbox。

## Sizing and Delivery Status

宣告 domain_breadth=0：唯一 production domain 是 in-memory quota observation contract；沒有修改 registry、extractor、provider、manager 或 policy consumer 的寫入／裁決。這不是由「一個新檔案」推導；只要未來增加 provider adapter、持久化、consumer routing 或 authority decision，就必須重估 domain。

state_consistency=1：parse／validate／to_dict／freshness／event identity 沒有 durable transition，卻新增 schema-version compatibility 與 reference consistency contract；不是 ledger 狀態機的「一個狀態」。Standalone UnitDefinition/catalog 是同一 observation contract 的 caller-input 型別與有限 reference resolution，沒有第二 owner/domain、global registry、account issuer 或 conversion；增加的 API/collision/cap/unknown 測試必須做，不能因 sizing 不變就略去。新增 schema migration、watermark／reset transition 或跨記錄 reconciliation 必須移交 C 並重新 sizing。十個 invariants 不折減 AC、不代表任何 builder envelope。

以 pinned main 588d7d8 的 helper + 既有 fix-standard（9 cards、9 bindings、2 core gates、R-09/R-16/R-19）分帳：draft 預期 0/1/2/0/2=5 Yellow 但 completeness=false；僅記憶體改 accepted 預期 0/1/2/2/2=7 Red，仍非 authority。實際值以本次 review report 的逐 view 輸出為準。#831 未載入現行 control，修正後 5 Yellow 只是假設完整 accepted 之投影，不能替代現行 7 Red，也不能繞過 review／readiness。

## Open Questions

無尚未裁決的 A wire／語意問題。保守工程決策與反例見 design；進件尚待授權與下游 live issuer／consumer conformance 是明列前置或依賴，不偽裝為已完成，也不與 A schema 的未決設計混在一起。

## Evidence

以下 source evidence 固定於 `588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86`，不是目前 installed/live 證據：

- [Execution domain](https://github.com/hamanpaul/paulsha-cortex/blob/588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86/docs/superpowers/specs/2026-09-07-cortex-execution-domain.md)：quota pool、observation、forecast、reservation、qualification 不同概念。
- [完整 refine plan](https://github.com/hamanpaul/paulsha-cortex/blob/588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86/docs/superpowers/plans/2026-09-07-cortex-refine-complete.md)：B2/B3/B4 與 #835–#842 分工。
- [Registry terminal usage 入口](https://github.com/hamanpaul/paulsha-cortex/blob/588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86/paulsha_cortex/coordinator/registry.py#L1333)：outcome 先定，再於 L1384 擷取 usage；不是 quota admission。
- [Usage extractors](https://github.com/hamanpaul/paulsha-cortex/blob/588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86/paulsha_cortex/coordinator/usage_extractors.py#L25)：Codex/Claude/Copilot 既有 token shape 與 AGY unsupported；A 不重寫或冒充新 qualification。
- [StreamEvidence](https://github.com/hamanpaul/paulsha-cortex/blob/588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86/paulsha_cortex/coordinator/outcome_taxonomy.py#L391)：structured rate-limit/reset 訊號已有來源分層；signal 不等於 numeric remaining。
- [Workflow usage aggregation](https://github.com/hamanpaul/paulsha-cortex/blob/588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86/paulsha_cortex/coordinator/usage_aggregate.py#L15)：per-run totals 不是 account ledger。
- [#849 profile core design](https://github.com/hamanpaul/paulsha-cortex/blob/588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86/docs/superpowers/specs/execution-profile-schema-core-design.md)：frozen framing 來源，不是已存在的 import API。
- [現有 model_profile](https://github.com/hamanpaul/paulsha-cortex/blob/588d7d8bd3dbfe9e758a6ac1750f1bff2ba45e86/paulsha_cortex/coordinator/model_profile.py#L260)：舊六欄 fingerprint 與 side-effecting run/apply 不可挪來冒充新 profile／quota authority。
