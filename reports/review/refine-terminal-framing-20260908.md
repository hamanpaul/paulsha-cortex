# Terminal JSONL framing：作者歷史草稿、root 接受與唯讀證據

Author scope 僅四份 planning/report。以下首稿歷史記錄：於 2026-09-08 建立隔離 branch `feature/refine-terminal-framing-20260908`，在新 worktree 執行 `git pull --ff-only origin main`，回 Already up to date，base=`b1b44bc476dc49481d8467c9595e7c4db3f1a87d`。本作者當時沒有 commit/push、issue mutation、repository links、freeze、dispatch、產品碼或 live 狀態變更；當時三件組為 draft，交 root 獨立審查，未宣稱 accepted 或 issue-owned。現在 root 接受內容與建立 #860 的新狀態另記於末節，不回寫為作者首稿當時已做。

## 來源與 route

事故 run=`workflow-97a9aa661e4816e38964`，job=`wf-855aa5366b-verification-94`，Candidate=`b448ce84022ac124e1e225cfdf86a5616d6f9c68`；Claude/sonnet，2026-09-07T15:54:56.451537Z exited/0，16:04:41.315953Z NH `resume-workflow-failed: ValueError workflow terminal log has no JSON evidence`。事件 source revision=`5560e6180c0a110e4a9ca757f006a9bcec767499815e51c5d943f890b6107834`；claim=`claim:v1:685715d38ce2299bd7e0d8b5bcd232d09adc3c6fb4cf6347c2d7e82e92c8672b`。

原 log 路徑以環境表示為 `$PSC_COORDINATOR_ROOT/logs/workflow/wf-855aa5366b-verification-94.jsonl`；SHA256=`ba8efd04a7f61c265a684699fae353bff950334edf5c6c339548eb433c26f378`。不附私有 prompt／原 log 內容。本案是 refine R13 primary／R06 secondary；ProblemMap strict：PM1 8（debugging black box，medium），Atlas F7 primary／F4 secondary，F5 為 misleading no-JSON 診斷症狀；broken invariant=`representation_container_fidelity_broken`，family-level fit、confidence high、evidence sufficient。先壞的是 framing，不是推理、quota 或 candidate schema。

Skills 的影響：doc-coauthoring 區分 author draft／root reader；code-tracker 先按 exact parser seed 追 consumer；test-playbook 以 root cause 建立最便宜 deterministic oracle 並保留 negative controls。語意工具當時 Python server unconfigured、ctags 無 tags、cscope 無 index，採 `trace_mode=rg`（文字 call-edge conf=0.4）；純 parser 重播是獨立執行證據，不冒稱 LSP 或完整產品採信。依本次限定四檔，不另寫 experience、memory 或 vault trace。

## Source／trust boundary 錨點

| 錨點（相對 repo；base b1b44bc4） | 確認內容 |
|---|---|
| `coordinator/manager.py:10488–10508` | 同 run/card/phase/claim era/Candidate 篩選後取最新 job，非任意較早 verification |
| `coordinator/manager.py:6321` | 消費 registry job.log_path，不讀人手指定報告取代 terminal |
| `coordinator/manager.py:4355–4405` | UTF-8 reader、splitlines、reverse records、carrier、fence；唯一未來 production 修改函式 |
| `coordinator/manager.py:3920–3938`／`terminal_contract.py:49` | 單層且白名單 wrapper；任意 structured_output 非新增採信來源 |
| `coordinator/manager.py:4384–4392` | tool data.content 被 type gate 拒收，但 generic text 等鍵沒有完整 event-type allowlist，不能作全稱安全保證 |
| `coordinator/manager.py:4441–4454、6358–6392` | extraction shape／verification schema/report 與 gate authorization 分離 |
| `tests/test_workflow_production_wiring.py:4318–4484` | 現有 Codex/Copilot/AGY/fence/tool-data negative harness；預設 json.dumps escape Unicode，未釘本案 raw NEL |
| `tests/test_terminal_result_contract.py:472–487` | known one-layer wrapper 與 unknown wrapper 負例 |
| `coordinator/work_actions.py:2466–2491、2580–2628` | retry-card era/Candidate/accepted-evidence 篩選；不是本產品修補可直接 mutation 的權限 |

上表 `coordinator/...` 的完整 repo prefix 為 `paulsha_cortex/`。Caller 的 breadth 是唯讀 trace，不代表新增 production 模組。

作者以 AST 取 `_extract_terminal_json` 原始函式片段後做 SHA256，author base 與事故 runtime 兩份皆為 `84d267e6b15e274e73c8db6a6e2b53b0caa67647f85c81cf3e9a0d92781706b8`，並非只以 HEAD 名稱推定相同。本四件組的 local Markdown links 實際 resolve 全部存在；untracked 四檔各自 `git diff --no-index --check /dev/null FILE` 與 `git diff --check` 均無 whitespace error，沒有以尚未 tracked 而空 diff 的成功冒充四檔檢查。

## 已執行的機制證據（不是產品 AC 完成）

- 原檔 246816 bytes、203 個 physical LF records、206 個 `str.splitlines()` 片段；**整檔 3 個 NEL，最後 record 2 個**。
- 末筆 physical line 203，byte `[222391,246816)`；raw U+0085 於 absolute byte 229251、239086。兩份載體的 decoded 值相同，欄位為 `details.job49_evidence`。
- 原完整 bytes／單獨末筆 record → 正式 `_extract_terminal_json` 都報 no JSON；直接 `_parse_terminal_json_text(result)` 與 `_is_workflow_terminal_payload` 成功。
- Memory-only LFFramed `splitlines` override → 回傳逐值等於原 `structured_output`；只 escape raw non-LF separator 的記憶體對照同樣成功；原檔讀後 SHA256 未變。
- 正式 producer JSON schema、`_inline_terminal_reports`、`validate_verification_evidence` 純 shape 檢查 PASS；未跑完整 terminalization／gate／review／ship。
- 合成雙層 fixture：raw NEL → FAIL；inner/outer 都 escape → PASS；只有 inner result escape 而 outer structured_output 有 raw NEL → FAIL；用 ASCII 名稱描述該字元 → PASS。後者只支持臨時新卡說明策略，不是保留原 Unicode 值的產品 oracle。
- 真 library 的 memory `TextIOWrapper(BytesIO(...), newline=None)` 會把 bare CR 轉 LF；`newline=""` 保留 CR。證明為何 D1 同時限制 reader 與 splitter；尚未執行候選 production 的真 file-open 改動。
- 合成 trust controls：tool.execution_complete.data.content → reject；structured_output-only → reject；tool.execution_complete 頂層 text → baseline accepted。最後一項如實列為既存 carrier-authentication 殘餘，沒有用靜態掃描支持「所有 tool spoof 都拒收」。

## 風險／test matrix

| Surface／觸發 | Oracle／harness | 規格／task |
|---|---|---|
| raw/escaped NEL/LS/PS、雙層 serializers | tmp_path actual parser，payload deep equality，舊 RED → 候選 GREEN | R2–R3／T1,T4 |
| LF/CRLF／EOF／裸 CR 相接 | 真 newline-preserving reader；CR 不是 record delimiter，合法 JSON whitespace 仍可用 | R1,R6／T3 |
| 末尾雜訊／tool-data spoof／unknown wrappers | 較早合法 terminal 保留；無合法 terminal 則原 error；不新增 carrier | R4–R5／T4,T5 |
| invalid UTF-8／缺檔／wrong shape | 原例外分類；schema 不放寬 | R5–R6／T6 |
| immutable bytes／私有來源 | synthetic CI fixture＋可選 owner-only replay；before/after SHA256 相等，無 live writer | R7–R8／T7 |
| docs／CLI／own OpenSpec／policy／delivery | 真 help exits、self-owned change validate、full/policy/exact-head review，來源層次分開 | R9–R10／T8–T10 |

State／concurrency 不新增：此為純讀 parser，不製造 shared-state transition；完整 job lifecycle、安全維護窗口及其他 owner 的 runs 不在本 child 變更內。O(bytes) 全檔讀取、concurrent append/rotation、generic carriers／latest-malformed 選擇與其他 parser 均見 design D4，母 refine R06/R13 仍保有整體驗收。

## Author draft 歷史 pure planning gate

2026-09-08 作者在 checkout 外 `/tmp`、`PYTHONDONTWRITEBYTECODE=1`／Python `-B` 下，以 `79ba644780bf1c697c722ac24a297e7d02416100` runtime 的真 `assess_planning_completeness`、`compute_sizing_score`、`_evaluate_yellow_plan_review` 重播，沒有建立 registry 或模型 session。固定完整 `fix-standard`：gate_spine_count=2、cards_count=9、persona_binding_count=9；適用規則為真完整 `ACCEPTANCE_SURFACE_RULES={R-09,R-16,R-19}`。三件組的真宣告為 domain=0、state=0、invariants=10、artifact_classes=source/tests/documentation。

| Case（負控制只改記憶體） | 真 artifact completeness | 現行五維／total | #831 條件投影 total | 真 Yellow helper |
|---|---|---|---|---|
| 磁碟原 draft | false；三件 status-not-accepted，missing=spec/design/plan | 0/0/2/0/2=4 Yellow | 6 Yellow；未 accepted 仍 risk=2 | ready=true；不能代替左欄 completeness |
| 只將三份 status 換 accepted 的 counterfactual | true；missing/reasons/markers 空 | 0/0/2/2/2=6 Yellow | 4 Yellow | ready=true |
| accepted counterfactual 移除 design | false；missing=design | 0/0/2/1/2=5 Yellow | 5 Yellow | ready=true；仍無 artifact completeness |
| accepted spec 移除 Requirements heading | false；required-section-missing | 0/0/2/1/2=5 Yellow | 6 Yellow | ready=true；仍不得進件 |
| accepted spec 加 standalone blocking marker | false；blocking-decision | 0/0/2/0/2=4 Yellow | 6 Yellow | ready=true；低 raw score 不是許可 |
| Tasks 的 documentation coverage 移除 | true | 0/0/2/2/2=6 Yellow | 4 Yellow | ready=false；missing-task-for-surface: documentation |
| plan 增 scope_excludes=[cli] | true | 0/0/2/2/2=6 Yellow | 4 Yellow | ready=false、terminal=true；policy-scope-conflict: R-16 |
| plan 移除 state_consistency | true | 真 compute 拋 ValueError，明示欄位 absent | 不造分數 | 不用無效分數呼叫後續 gate |

`_evaluate_yellow_plan_review` 只對 plan tasks/contract/envelope 判斷（`manager.py:9336–9370`），不是三件組 accepted gate；negative controls 正好證明不能只報 ready=true。所有本表 ready=true 的 envelope observation 都是 `bypass=envelope_unavailable`，來自真 `load_model_identities()`＋`_plan_review_envelope_lookup`；不是能力量測 PASS。

#831 欄是依 accepted spec 的 risk mapping，在記憶體以 `dataclasses.replace(score, spec_stability=projected_risk)` 計算：任一 marker／rejected assessment／至少兩缺 kind→2；單純一缺 kind→1；完整 accepted→0。不是已合併或已載入的 #831 runtime helper，沒有回填舊 run。其餘四維原值不動，沒有縮 combo 或 gate。首稿檢查當時磁碟始終 draft，當時禁止 register/freeze/dispatch；所有產品 T1–T10 迄本次 metadata 更新仍未勾。

首稿最小重播節錄（輸入是下列歷史 SHA256 所示 draft bytes；不是將目前 accepted 檔案誤標為 draft 的命令。在隔離 checkout 外執行，`FRAMING_DOC_ROOT` 指歷史四檔所在 worktree，`PYTHONPATH` 指要查驗的 runtime；本命令不傳真 registry）：

```python
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import os
from paulsha_cortex.coordinator import planning, manager
from paulsha_cortex.coordinator.claim import sizing_band
from paulsha_cortex.coordinator.model_identities import load_model_identities
from paulsha_cortex.deck.schema import load_cards, load_combo, resolve_combo_path, DEFAULT_CARDS_PATH

root = Path(os.environ['FRAMING_DOC_ROOT'])
slug = 'workflow-terminal-jsonl-framing'
refs = [('spec', f'docs/superpowers/specs/{slug}-spec.md'),
        ('design', f'docs/superpowers/specs/{slug}-design.md'),
        ('plan', f'docs/superpowers/workstreams/{slug}/todo.md')]
original = tuple(planning.PlanningArtifact(k, r, (root / r).read_text()) for k, r in refs)
cards = load_cards(DEFAULT_CARDS_PATH)
combo = load_combo(resolve_combo_path('fix-standard'), cards)
identities = load_model_identities()
for label, artifacts in [('draft', original), ('accepted-counterfactual', tuple(
        replace(a, text=a.text.replace('status: draft', 'status: accepted', 1)) for a in original))]:
    report = planning.assess_planning_completeness(artifacts)
    score = planning.compute_sizing_score(
        plan_artifact=artifacts[-1], completeness_report=report,
        gate_spine_count=len(combo.gate_spine), applicable_contract_rules=planning.ACCEPTANCE_SURFACE_RULES,
        cards_count=len(combo.cards),
        persona_binding_count=sum(cards[e.ref].persona_binding is not None for e in combo.cards))
    run = SimpleNamespace(steps=(), primary_domain=None, model_chain_override=None,
                          sizing_band=sizing_band(score.total), run_id='read-only-framing-check')
    gate = manager._evaluate_yellow_plan_review(
        artifacts, envelope_lookup=manager._plan_review_envelope_lookup(run, identities))
    print(label, report.complete, report.missing_kinds, score.to_dict(), gate)
```

首稿 author 當時沒有跑產品 focused/full tests、PR-context policy、CI 或 live CLI。僅為核對未來自有 OpenSpec 命令，唯讀執行了已安裝 `openspec validate --help`，確認支援 item-name／--strict／--no-interactive；不是 validation 通過。

三件組 SHA256（首稿歷史 draft，不是目前 accepted bytes 或 evidence authority）：

| 檔案 | SHA256 |
|---|---|
| spec | b3930206ea28e6b51d858f8e101bb29151ec69b785bbd15b0daed773f35e3ffa |
| design | 39cd76289a6782eea2390211a9304f86a4e09e513efedf65adeb2a90ed2fb714 |
| todo | 8950c097caaaee70b331688072795c2bd8b0cff2bdc6dc9b3db8a20b160c3102 |

## 首稿歷史獨立 review 交接

判定標準：未處置缺陷／缺口 FAIL；已承認、影響分析有界且文件列管 residual 不單獨 FAIL；反對接受須具體反駁影響分析。本作者當時只交草稿，當時尚無獨立 review PASS。

當時交 fresh reader 的問題：唯一 production 邊界？為何不能只換 split？draft raw 分數能否進件？tool spoof 的已保護／既存未保護形狀？Parser replay 能否當 verified 或 live loaded 證據？Root 負責 reader 與 reviewer，不由作者自批。

## 2026-09-08 root 接受／#860 owner metadata

Root 轉交的獨立審查證據：root 已完整閱讀四件，fresh `bounded_contracts_review` 對抗審查 PASS／0 BLOCKER-MAJOR，覆核 36 組 framing／carrier cases、8 組 planning gates 與原 log 唯讀 proof。這是 root／reviewer 的內容與機制審查，不是本作者自行核准，也不是產品實作、正式 verification 或 runtime qualification 通過。原始 log 與其 evidence 均未因本次 metadata 更新而改寫。

Root 已正式建立並 readback owner [#860](https://github.com/hamanpaul/paulsha-cortex/issues/860)，接受內容可供 repository intake。因此本次只把 spec/design/todo status 更新為 accepted、補 owner 與正確時態。Repository links、整合 PR 與正式 authority 由 root 另行管理；accepted 不等於已 merge、freeze、產品 dispatch 或 qualification。歷史 draft／accepted-counterfactual 的分數、hash 與當時未操作狀態保留於前節，不冒充當時已通過。

R1–R10、D1–D5 與 T1–T8/T10 的技術內容維持原 bytes；D6 僅更新接受時點與 gate 敘述。T9 明定未來產品 branch `feature/860-workflow-terminal-jsonl-framing`，並釐清本 child 的 active OpenSpec checkbox 只列 pre-archive 工作；archive 與下游交付仍列未完成 prose，不形成自身 archive 的循環前置條件，也不刪除 T10 完整交付要求。

本次 metadata 後作者實跑的純 helper，改固定 root 提供的 exact-79ba 隔離 clone，`git rev-parse HEAD` 確認 `79ba644780bf1c697c722ac24a297e7d02416100`，並確認 `planning.__file__`／`manager.__file__` 都來自該 clone；沒有宣稱 live runtime-main HEAD 仍為 79ba。原因是 root 通知其他 owner 已將 runtime-main source 移至另一 revision，而非本作者操作或部署。檢查維持 `PYTHONDONTWRITEBYTECODE=1`／Python `-B`、不建立 registry、不送模型或 control request。

第一次 harness 在取得 actual accepted complete=true／6 Yellow 後，把 `PlanReviewOutcome` 誤以 dict 取值而報 TypeError；改用真 `.ready` 屬性並固定上述隔離 source 後，以下完整八組才是本次成功的 proof（exit 0）。沒有把 harness 錯誤解讀為產品 RED，也沒有以第一次部分輸出聲稱全組通過。

| 本次實跑 case（變異僅記憶體） | 真 completeness | 真 total／Yellow helper |
|---|---|---|
| 磁碟 accepted 三件組 | true；missing kinds 空 | 6 Yellow；ready=true |
| 三件 status 改回 draft | false；missing=spec/design/plan | 4 Yellow；ready=true 不授權進件 |
| 缺 design | false | 5 Yellow；ready=true 不代替 artifact completeness |
| spec 缺 Requirements heading | false | 5 Yellow；ready=true 不授權進件 |
| spec standalone blocking marker | false | 4 Yellow；raw 降分不授權進件 |
| Tasks 缺 documentation coverage | true | 6 Yellow；ready=false，missing-task-for-surface |
| scope_excludes=[cli] | true | 6 Yellow；ready=false／terminal=true，R-16 conflict |
| 缺 state_consistency | true | 真 compute 拋 ValueError，不造分數或呼叫後續 gate |

固定完整 combo 仍為 gate_spine=2／cards=9／persona bindings=9，真適用規則 R-09/R-16/R-19；accepted 五維確為 0/0/2/2/2。Ready=true 的 envelope observation 仍為 `bypass=envelope_unavailable`，不當成能力量測或正式 readiness。僅以 `dataclasses.replace(score, spec_stability=0)` 另驗 #831 完整 accepted 條件投影 0/0/2/0/2=4 Yellow；沒有聲稱 #831 已 loaded，也沒有改既有 frozen revisions。

該輪 metadata 前後 core SHA256 比對相等：spec 自 Requirements 至文末、design D1–D5、todo T1–T8/T10 三個獨立片段均不變。下列為新增 upfront OpenSpec 之前的 accepted 三件組歷史 SHA256；不是後續新增 baseline links 的最新 bytes。Report 本身的 hash 由各次交接訊息另列，避免自我參照。

| 檔案 | Upfront OpenSpec 之前的 accepted SHA256 |
|---|---|
| spec | bd72bd17875381bb2e8dba55790bd6e45de5da9e4c3f28c07ed8ab676dab36f7 |
| design | 05b8e059a0be20ef811500e1fcf2d5c469824a6641dc0f349fb8d564c5e5d5f0 |
| todo | 68820b0e1cc4f2229afbdcb35493dea94f47cb6eade5d75eec400bb12dd98115 |

前一輪 metadata 當時仍只更新四份 planning/report；沒有跑產品 focused/full tests、PR-context policy、CI 或 live qualification，沒有 product／issue／registry／service mutation，也沒有 commit/push。Root 的 repository intake／planning preflight 與未來產品 T1–T10 gates 是不同證據，不在此代填。

## 2026-09-08 自有 OpenSpec upfront baseline integration

Root 指派本輪 scope 為原四件加本 child 自有 OpenSpec proposal/design/tasks/spec delta，共八件；root 另管唯一 OpenSpec mapping 與其五份 integration 文件。此處不改 `.cortex`、changelog、母 plan/tasks、operator/runtime、registry、產品碼或原事故 log；不 commit/push/dispatch。本輪是避免後續 builder 在 frozen authority 後新增 planning inputs 的 intake 修正，不把所有產品工作當已完成。

當下 source（author base b1b44bc4，對應 exact 79ba helper）確認：`work_bridge.py:241–245` 先追加 mapped OpenSpec proposal/spec、design/design、tasks/plan；`:246–247` 再追加 todo/plan。`current_sizing_snapshot` 的 `:274–282` 在迭代每個 plan 時覆寫變數，最後 plan 用作 sizing；`manager.py:9353` 的 Yellow review 用 `next(...)` 取第一份 plan。因此僅有 todo 完整、OpenSpec tasks 空殼並不足夠；本次 proposal/design 都自足承接原 R1–R10／D1–D6，tasks 與 todo 同樣完整宣告 0/0/10/source-tests-documentation，且保留完整 R-09/R-16/R-19 與 R1–R10 coverage。

`manager.py:5055–5069` 的 checkbox 正規化只容忍 checkbox 狀態；`:5072–5099` reviewer authority map 要求 operator baseline 的 hash 先相等，才採 candidate checkbox-only hash。`:5117–5140` 的缺檔 seeding 必須有既有 authority refs 且 operator bytes 符合 baseline；`:5177–5203` 同樣先驗 operator baseline，scope／文字／metadata 變更仍 fail-closed。既有 `tests/test_checkbox_drift_tolerance.py:59–123` 與 `tests/test_builder_tasks_tick_verify_dispatch.py:1–30` 是此契約的 source/test 錨點，本輪只讀、不冒稱新跑產品測試。

新 OpenSpec tasks 的十個 checkbox 只含未完成 pre-archive 工作；own archive、產品獨立 review／CI、archive 後再驗、PR／merge／closure／runtime loaded 等完整交付在 prose 明列 pending。原 todo T10 不減，T9 改沿用 upfront baseline；不把 archive 本身或下游成功放入 active change 的前置 checkbox，不令 closure 形成自相依。正式 freeze 後 operator baseline 永不由 builder 修改；candidate 只更新真證據支持的 checkbox，其他變更回 root 走正式 authority。

### 本輪實跑與負控制

從 `/tmp` 以 Python `-B`／`PYTHONDONTWRITEBYTECODE=1` 載入 root 指定的 exact `79ba644780bf1c697c722ac24a297e7d02416100` 隔離 clone。只以記憶體 `SimpleNamespace` 表示預期 authority links，呼叫真 `_artifact_rows` 讀本八件範圍中的六個 planning artifacts；沒有呼叫會建立 registry、frozen inputs 或派工的入口。Artifact 順序為原 spec、原 design、自有 proposal、自有 design、自有 tasks、原 todo；delta 另交 OpenSpec strict，不被誤算為第七份 planning artifact。

實體六份 `assess_planning_artifact` 全部 accepted、reasons/markers 空；整體 `assess_planning_completeness.complete=true`，自有 OpenSpec 三件組單獨也 complete=true。兩份 plan 各自真 `compute_sizing_score` 都為 0/0/2/2/2=6 Yellow；真 `current_sizing_snapshot` 回 `(6, "yellow")`；真 Yellow helper 讀第一份 tasks，ready=true。固定完整 fix-standard=2 gate spine／9 cards／9 persona bindings，規則全集 R-09/R-16/R-19 不縮。Envelope observation 仍是 `bypass=envelope_unavailable`，不是能力量測；#831 的 4 Yellow 只以記憶體 stability=0 條件投影，不聲稱已載入。

| 本輪 case（變異僅記憶體） | 真 completeness／逐件 rejected 數 | 真 helper 結果 |
|---|---|---|
| 實體六件 | true／0 | first-plan Yellow ready=true；last-plan sizing=6 |
| 只移除第一份 tasks 的 documentation 任務標記 | true／0 | ready=false，missing-task-for-surface: documentation；完整 todo 不掩蓋 |
| 只在第一份 tasks 排除 CLI | true／0 | ready=false，policy-scope-conflict: R-16 |
| 只有自有 proposal 改 draft | true／1 | Yellow ready=true；kind OR 不證明所有 artifact accepted |
| 只有自有 design 缺 Decisions | true／1 | Yellow ready=true；同 kind 的原 design 仍 accepted |
| 第一份 tasks 加 standalone blocking marker | false／1 | Yellow ready=true 仍不得取代 completeness |
| 兩份 spec 都改 draft | false／2 | Yellow ready=true 仍不得取代 completeness |
| 兩份 design 都缺 Decisions | false／2 | Yellow ready=true 仍不得取代 completeness |
| 最後 todo 缺 state_consistency | 內容只在讀取 mock 變異 | 真 sizing snapshot 回 `(None, None)`，不造分數 |
| 第一份 tasks 缺 artifact_classes | 宣告僅記憶體移除 | 真 Yellow helper 回 None（既有 fail-soft），不冒稱拒收或允許進件 |

以上十組皆按真結果完成斷言（exit 0）。第一版 harness 曾錯估「單一 proposal draft 會使 aggregate incomplete」而中止；核對 `planning.py:405–411` 確認是 accepted-kind OR，修正的是 harness 預期，不是產品或 planning 數值。本次額外要求逐件 assessment 全 accepted，以及 OpenSpec 三件組獨立 complete，避免用 aggregate true 掩蓋壞複本。此 OR／missing-declaration fail-soft 是已存在 helper 邊界，不在 #860 parser-only production scope 暗改；若今後 source 組成或內容改變，必須重新驗證，不以本次結果作全稱保證。

真 `_checkbox_insensitive_equal` 對 tasks 與 todo 的記憶體單一 checkbox toggle 回 true；toggle 加 substantive 文字或修改 state metadata 都回 false。這只證純比對 seam，不是實際 candidate／operator／reviewer 全鏈已運行；實體兩份 plan 都仍 10/10 checkbox 未勾。

在 author worktree 實跑 `openspec validate workflow-terminal-jsonl-framing --strict --no-interactive`：exit 0，`Change 'workflow-terminal-jsonl-framing' is valid`。這證明新增 change 有有效 delta／scenario，不代表產品實作、archive 或 CI 已完成。Root 另負責整合後的 canonical-spec／policy／full preflight 與 fresh review；本作者沒有以 strict 代替產品 T1–T10 gates。

機械一致性檢查：八件的本地 Markdown links 全部存在；proposal R1–R10 原文與 spec 相等，自有 design D1–D6 與原 design 相等；tasks T1–T8 與原 todo 原文相等，原 todo T10 沒有改動；delta 有十個 requirements 與十個 scenarios。原 R1–R10／D1–D6 的產品內容與前輪完全相同，只加 upfront links／接線說明及 T9 baseline 使用方式。以下為本輪七份 planning docs SHA256，report 本身另由 hash-stop 交接提供。

| 檔案 | Upfront baseline SHA256 |
|---|---|
| `docs/superpowers/specs/workflow-terminal-jsonl-framing-spec.md` | 3366ce02e7f7b86ce8a1741dcf3b3b24fad8c3e2e02d95152d1627ab640f09cc |
| `docs/superpowers/specs/workflow-terminal-jsonl-framing-design.md` | 64cccfa5174a9dcc04a4f16757ac81687699f45284c1dc8d8e2eb5a70649d7ce |
| `docs/superpowers/workstreams/workflow-terminal-jsonl-framing/todo.md` | 426321f40eb253fb6bb4532b7bc32c096891f64bab5b5812249ec54c05eb5c1a |
| `openspec/changes/workflow-terminal-jsonl-framing/proposal.md` | f6872339728d71ebf0e071ed6f18cd9889407f28f9918db08ca319c19cff8585 |
| `openspec/changes/workflow-terminal-jsonl-framing/design.md` | 4d581f60283fff12c9c2d42b696fa20a7c219f1a2993e782dc07668311df878b |
| `openspec/changes/workflow-terminal-jsonl-framing/tasks.md` | 82ee46b07efa87f5a426c0dc33d3ba1262c816a4ee6c921b1495430040522098 |
| `openspec/changes/workflow-terminal-jsonl-framing/specs/workflow-terminal-jsonl-framing/spec.md` | f5ffa1091042e6bb0531e29a2b186b4358ca987e37bce47995552e53d9c75132 |
