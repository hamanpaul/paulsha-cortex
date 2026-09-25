---
status: accepted
work_item: monitor-canonical-todo-qualification
owner_issue: 1063
parent_issue: 1054
domain_breadth: 1
state_consistency: 1
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
applicable_contract_rules:
  - R-09
  - R-16
  - R-19
  - R-22
---

# Monitor canonical Todo qualification 設計（#1063）

本設計是 live [#1063](https://github.com/hamanpaul/paulsha-cortex/issues/1063) 的 Monitor/WorkAuthority source contract。Parent #1054 消費 qualification，但負責自己的 Manager first-Builder gate；本票不把該 gate提前併入。

## 現況與責任邊界

在規劃基準 origin/main 6a32a3e5e0af841794f340313c11f60f2999f6ae：

- monitor/providers.py 的 RepoWorkProvider._scan_sources() 以固定 glob 找來源，對發現檔案呼叫 _safe_file() 並保存 revision；Todo metadata 尚未成為 source contract。
- monitor/providers.py 的 _frontmatter_work_item_text() 只抽取 work_item，沒有讀 issue 或 Tasks。
- monitor/correlation.py 的 _validate_repo_path() 用 resolve(strict=False) 檢查 escape，repo 內不存在的 path 仍可通過。
- coordinator/work_actions.py 的 _mutate_override() 擋 symlink traversal，但寫 path link override 前沒有要求 target 已存在或是受支援的 scanner source。
- coordinator/claim.py 的 WorkAuthority 暴露 mapped_todo_paths 和 confirmed_todo，沒有可由 Manager 消費的 Todo qualification record。

因此規劃把下列兩個獨立結果分清楚：

1. 一般 path link：確認 path 是 Monitor 支援且目前存在的 safe scanner source；不代表 Todo 合格。
2. Todo qualification：確認 canonical Todo、issue/work_item correlation 與 Tasks contract，並把 typed result 帶入 WorkAuthority.qualified_todos；#1054 才用它計數。

## Decisions

### D1 — 一份 versioned qualification record 綁定同一 source revision

在 monitor/work_models.py 定義 immutable TodoQualification/TasksValidation record，並由 WorkSource 的 optional field 暴露。格式採 canonical-todo/v1。每筆至少包含 source_id、path、revision、issue、work_item、task_validation、status、reason_codes。既有非 Todo WorkSource 不帶 qualification；既有 snapshot 讀取兼容缺少該 optional field。

RepoWorkProvider 對 Todo 先安全開啟一次 source bytes，以同一 bytes 計算 local-sha256 revision、解析 frontmatter 與 Tasks。它產生 source-level candidate result；不在 provider 層猜測 source 是否屬於某個 WorkAuthority。

### D2 — Correlation 是 issue 與 work-item 的唯一 join owner

monitor/correlation.py 在建立 confirmed group 後，為每個 Todo candidate 交叉驗證：

- source path 是單一 slug segment 的 canonical workstream Todo path。
- source 有同 work item 的明確 path link owner。
- YAML work_item 等於該 CorrelatedWork.work_id。
- YAML issue 在同 repo、同一 group 的 confirmed github_issue refs 中。

通過後把 qualification 標成 qualified；缺欄位、格式錯誤、issue 不在同 authority、work item 不匹配、path 未明確連結或 Tasks validation 失敗，保留 rejected record 與穩定 reason code。Rejected Todo 不刪除其他 source、不污染整份 Monitor refresh；檔案 I/O failure 仍走既有 provider failure/last-good 規則。

Owner-published 的唯一可驗證語意是這個 monitored repo path + explicit correlation。不得從 Git author、checkbox 或 metadata 推斷帳號身份。

### D3 — WorkAuthority 專用 typed output，不改舊 mapped 欄位語意

coordinator/claim.py 從同一 Monitor WorkItem source rows 建立 WorkAuthority.qualified_todos。每列都重新做 bounded shape/identity checks，確認 TodoQualification path/revision 等於 WorkSource 的 ref/revision，issue 屬於 mapped_issues，work_item 等於 WorkAuthority.work_id，且來源有 confirmed explicit path ownership。

mapped_todo_paths、confirmed_todo 和現有 ship 唯一 Todo backstop維持既有語意。新的 Manager admission 必須只 count qualified_todos；不得用 raw paths、basename 或 checkbox 補資格。WorkAuthority 不新增 mutable store、claim writer 或 freshness clock。record revision 由既有 source revision 綁定；Monitor snapshot hash 包含序列化 qualification record。#1064/#1065 仍負責 freshness/generation。

### D4 — Tasks-v1 是機械格式檢查，不是語意模型

格式只接受 spec R3 的 task checkbox 單行格式；Task 範圍中的每個非空白行都必須是該格式，不接受巢狀 checkbox、其他 checkbox 樣式或說明行。task ID 不可重複，title/action trim 後不可空或單獨為 placeholder，並要求至少一項 pending task。任一不符合格式的行使整個 Tasks validation rejected，不能忽略 malformed line 後選擇性通過其餘項目。

Parser 不作自由文字 NLP，不宣稱兩個非空欄位保證工程品質。Owner 對需求是否合理的審查仍屬既有 planning review。重複 heading、缺 heading、空 section、checked-only 都有獨立 reason code。

### D5 — 共用 scanner path validator 保持通用 path link

新增小型純路徑 helper（monitor/source_paths.py）提供：

- fixed scanner path kind 分類，對齊 RepoWorkProvider 當前 `docs/superpowers/workstreams/**/todo.md`、`docs/superpowers/specs/**/*.md`、`docs/superpowers/plans/**/*.md` scanner patterns。
- lexical POSIX canonicality（拒絕 absolute、`..`、`.`/重複分隔符與反斜線）、repo containment、各 component 非 symlink、existing regular file 檢查。
- canonical Todo 的較窄 slug 路徑 predicate，與 generic scanner path predicate 分開。

monitor/correlation.py 的 override parser 使用 strict existing-source validator，取代 strict=False path guard。coordinator/work_actions.py 在寫入 path link 前呼叫同一 validator，並在 atomic replace 前確認 prospective payload 全部仍可讀。link target 可為既有 spec、design、plan，也可為掃描到但尚未具 Todo qualification 的 Todo。此 gate 僅回答「path link 是否存在且可掃描」，不把 Todo 的 issue/Tasks qualification 混進通用 link API。

對舊有 dangling/unsafe path override，link 不能成功寫入；unlink 仍可透過既有 exact ref 移除該 row，且不要求不存在的 target 先通過新 link gate。path link 失敗必須在原子寫入前拒絕，不能寫了再靠 readback 失敗。

### D6 — Scanner 保留 rejected Todo 的診斷與 source revision

Canonical path 但 metadata/Tasks 不合格時仍可留下 source inventory 與 revision，並帶 rejected qualification，讓 Monitor explain 有直接原因；WorkAuthority.qualified_todos 排除它。不得把 parser rejection 轉成 qualified、不得讓 invalid Todo 觸發 #1054 的 Todo count。非canonical Todo path 若屬現有 glob，仍是普通 scanner source，不可成為 qualified Todo。

若 source 不存在、讀取競態或不是 regular file，scanner 不發 qualified record；既有 provider scan failure語意不降級為「source absent 已確認」。輸出診斷不得包含個人絕對路徑或 raw Todo body。

### D7 — 測試以真 parser/correlation/link seams 驗證結果與零寫入

集中使用 temporary repo fixtures，不使用正式 Monitor snapshot 或實際 GitHub：

- Provider：同 bytes revision、valid/invalid frontmatter、Tasks grammar、canonical/noncanonical path、symlink 與 I/O race。
- Correlation/WorkAuthority：issue 同組/不同組、work_item 同/不同、無 explicit path owner、valid rejected record、qualified output identity/revision 完全相等。
- Link mutation：missing、escape、symlink、非scanner path 均在 override file 初始不存在或 bytes 固定時拒絕，assert bytes 不變；既有 spec/design/plan 仍能 link；unlink stale path可修復。
- 回歸 existing snapshot schema/readers、confirmed_todo/mapped_todo_paths 與 ship backstop 語意不變；沒有 first-Builder side effects 的斷言，因該 gate屬 #1054。

## 目標程式責任

| Path | 本票責任 |
|---|---|
| paulsha_cortex/monitor/source_paths.py | Shared lexical/scanner/existing-safe path validation |
| paulsha_cortex/monitor/work_models.py | Typed TasksValidation/TodoQualification on WorkSource |
| paulsha_cortex/monitor/providers.py | Safe bytes parse, revision binding, candidate qualification |
| paulsha_cortex/monitor/correlation.py | WorkAuthority issue/work_item/path-link join and qualified result |
| paulsha_cortex/coordinator/claim.py | WorkAuthority.qualified_todos strict projection |
| paulsha_cortex/coordinator/work_actions.py | Pre-write generic path scanner/existence gate |
| tests/** | Parser, correlation, WorkAuthority and link mutation fixtures |
| README.md, docs/unified-work-lifecycle.md | Explain qualified_todos vs generic path links and owner-published limit |
| CLI help surface | Clarify supported path roots and link validation behavior, preserving syntax |

## Acceptance matrix

| Input | Path link | Qualification result | Override write |
|---|---|---|---|
| Existing safe scanner spec/design/plan | accepted | not applicable | one atomic write |
| Existing safe scanner canonical Todo, valid issue/work_item/Tasks and same confirmed authority | accepted | qualified | one atomic write |
| Existing safe scanner Todo with issue/work_item/Tasks mismatch | accepted as a generic path link | rejected; absent from qualified_todos | one atomic write |
| Existing safe scanner Todo with noncanonical Todo path | accepted only as generic scanner path | rejected; absent from qualified_todos | one atomic write |
| Missing, repo-escaping, symlinked or non-scanner path | rejected | no new qualification | zero mutation |
| Existing ordinary repo file outside scanner roots | rejected | not applicable | zero mutation |

## Non-goals

- No Manager first-Builder admission, zero/multiple typed diagnostic, dispatch/worktree/job side-effect gate, claim reconciliation or direct resume behavior (#1054).
- No Monitor generation/input revision watermark or WorkAuthority trusted freshness (#1064/#1065); a locally parsed record is not a freshness proof.
- No existing Candidate/PR recovery (#1055), ship logic, unique-Todo closeout, PR operation or #983 recovery.
- No change to #810 checkbox closure, #911 OpenSpec=0 support, #972/#973 conflict work, runtime install or deployment.
- No author/account attestation and no broad heuristic author detection.

## Sizing and re-evaluation

Official five-dimension sizing uses the repository helper on this accepted triplet plus the bound OpenSpec proposal/design/tasks with fix-standard. The measured result and all five dimensions are recorded in the canonical workstream Todo. It is a planning projection only. Recompute at formal implementation intake if a new persistent state writer, Manager admission responsibility, freshness generation, or additional production boundary enters scope.
