---
status: accepted
work_item: recovery-registry-receipt
issue: 862
---

# #862 registry receipt 規劃接受、審查與計分

## Status and Boundary

2026-09-08 root 全文核對八件，並回報 fresh refine_plan_review PASS／0 未處置 BLOCKER/MAJOR 後，正式接受內容作 repository planning intake。唯一 child owner 為 [#862](https://github.com/hamanpaul/paulsha-cortex/issues/862)，work_id=`recovery-registry-receipt`，父項 #497。本輪八件 status=accepted，移除過時 ready-for-root-review metadata；這是內容接受，不是產品 dispatch-ready。規劃 PR 尚未合併發布、mapping/source binding 尚未完成，T10 前置證據仍缺，現行 accepted8 Red 不能派工；#831 loaded 後6 Yellow只是條件投影。

歷史 authoring：2026-09-07 首次建立草稿，2026-09-08 依 root 工程裁決修訂原四件 planning；原四件 fresh review 由 root 回報 PASS／0 BLOCKER/MAJOR。Root 隨後擴大當輪 authoring 為原四件加 own OpenSpec proposal/design/tasks 與 `specs/recovery-registry-receipt/spec.md` 共八檔，當時全部保持 draft／ready-for-root-review、尚未開 child issue。當時未做 commit/push、產品 code/tests、registry/runtime/服務或模型 probe/benchmark；以下 Prior 各節保留該歷史證據，不回寫為當時已 accepted。

branch `feature/refine-recovery-registry-20260907`；worktree 以 repo 相鄰 `paulsha-cortex-worktrees/refine-recovery-registry-20260907` 辨識。先確認 branch/path 不存在，從 origin/main 建立隔離 worktree，再於該 worktree 執行 `git pull --ff-only origin main`，成功 fast-forward 到 `b1b44bc476dc49481d8467c9595e7c4db3f1a87d`。原 operator dirty worktree 與既有 evidence 未修改。

歷史八件 draft 階段的六份 consumer artifacts 因 draft 而 completeness=false；此結果保留在 Prior 節。OQ01/OQ02 已依 2026-09-08 明示裁決關閉，自身 delta spec 另作 OpenSpec strict validation、不多算第七種 planning authority。本輪依 root 批准改為 accepted，另跑下面 Current 節的真六份輸入；不加入自訂 blocked marker 使 accepted assessment 誤判，也不因此消除 T10 或 Red dispatch gate。

## Source Anchors

以下行號以 authoring base `b1b44bc476dc49481d8467c9595e7c4db3f1a87d` 為準，路徑皆相對 repo：

- `paulsha_cortex/coordinator/registry.py:387–484`：loader；409–429 既有 v1 backup/migration，479–482 既有 #501 normalization 寫回。OQ02 已裁決保留原政策，A 不宣稱全面 read-only。
- `paulsha_cortex/coordinator/registry.py:572–641`：已有單檔 atomic replace/fsync/rollback，A 重用而不重做 #821。
- `paulsha_cortex/coordinator/registry.py:875–986`：slice load/copy 需要覆蓋 nested receipt；新欄位缺值不能觸發 normalization 寫回。
- `paulsha_cortex/coordinator/registry.py:1175–1180,1395–1444,1446–1489,1500–1588,1591–1675`：job copy、create、repin、update、action。None 不清 binding；revision writer inventory 與實際行為測試的起點。
- `paulsha_cortex/coordinator/manager.py:1671–1703,1744–1755,1801–1817,11976–12018`：現有 gate-before-replay、None 清綁錯誤及 work shim；屬 B，不塞入 A。
- `paulsha_cortex/coordinator/work_actions.py:4713–4761,4788–4816,6084–6096,6202–6208`：缺 context/pins、target fallback 與 recovery update；屬 C，#547 廣泛選擇政策仍不在本拆分。
- `paulsha_cortex/control/contract.py:33–34,62–88`：req_id 目前已含 UUID，但不是完整 pinned recovery schema。`paulsha_cortex/coordinator/manager_daemon.py:691–708,830–845,1391–1438`：metadata 丟失與 done crash window，屬 D2。
- `paulsha_cortex/coordinator/cli.py:186–199,403–414,445–458`、`paulsha_cortex/porcelain/recover.py:39–74,90–119`：D1 需兩套 CLI；`paulsha_cortex/porcelain/run.py:163–170` 已有 work payload 通道可重用。完整 D 是四模組，不是舊估計三模組。
- `tests/test_record_action_atomic_382.py:120,191`、`tests/test_workflow_registry.py:277`、`tests/test_coordinator_registry_headless.py:312,416`：既有 atomic/repin 回歸起點，未在本輪重跑。
- 母 `docs/superpowers/specs/fix-superseded-terminal-replay-spec.md` 的 S01–S13 與 design 的 Decisions 3–10、Decomposition and Compatibility：A 完成不關母票，跨第四模組先重規劃。

## Planning Decisions and Residuals

1. A 唯一 production registry.py；domain=0、state=2，I01–I09 九組完整不變量。顯式 checkpoint 是新增責任，故由八組增為九組，不刪 AC、不降 state。原語可獨立測試，不意味 C/B/D 或 public action 修好。
2. Proposed schema 凍結 request ID/digest、exact binding、ABA revision、prepared/complete、同 snapshot 原子 persist；不自行引入人類批准、permission bypass、legacy target grandfather。
3. **OQ01 closed by root，2026-09-08**：獨立 versioned checkpoint context、完整 observed legacy row/binding/fingerprint/job refs/immutable ID，單 registry transaction 建 revision1＋不可變 receipt/provenance，不改舊 binding/history/disposition。Read/prepare/commit/舊 mutator 不自動升級；legacy mutator 繼續相容且 unversioned，新 versioned writer 依 D3 計次。真 owner/proof 與明示 action 由 C/B 後續負責；A 不認證 actor 字串、不操作 live legacy。先挑錯後未找到需要反駁的單 writer 內安全缺口。
4. **OQ02 closed by root，2026-09-08**：採最小相容範圍，A additive 不引發 load-time backfill/persist；v1 migration/#501 repair 原政策與 fixtures 不改。全面歷史格式 read-only 沒有被證明，也不是本新增欄位承諾的範圍。
5. A 的 proof closure 是內部資料契約；真資源/proof驗證由 B/C 負責。prepared 不是鎖；#818 跨 manager writer、#821 retention、queue/done 均未修。歷史 receipt 不自行刪除，成長/保留政策沒有在本 child 新增權限。
6. 文件使用 doc-coauthoring 的 context→scope/AC refinement→reader questions 方法；root 提供既定 bounded 格式，故不重啟泛用訪談。Reader 測試與純 gates 的結果於下節分帳，不能因文件可讀而稱產品通過。
7. **明示歷史殘餘**：完整 legacy snapshot 若在觀測間曾 A→B→A 且所有資料恢復一致，fingerprint 無法追證。Checkpoint 只從本次明示觀測建立新起點，不補過去 revision/授權/audit；同 ID 後續 recovery 必須 conflict，而非拿 checkpoint 當 rev1 recovery。原 public unpinned recovery 的 S03 閉合仍須 C/B/D，沒有因新增 A 原語消失。

## Prior Four-file Pure Checks

2026-09-08 實跑從 checkout 外 `python3 -B` import runtime checkout 當下 HEAD `e989a055a73fd6228857928ce830e9e7a13bc2f4` 的 `planning.assess_planning_completeness`、`compute_sizing_score`、`claim.sizing_band` 與 `deck.schema.load_cards/load_combo`；核對 module.__file__。本次 planning.py SHA-256 仍為 `dc62f1867778c4e01beba8bfbb71b9133e17590df9f615a538cd4cd2a3da371e`，與 authoring base/先前79版本相同；不沿用舊 HEAD 冒稱整個 runtime 未變。沒有呼叫 runtime 執行器／模型，這不是 active daemon 已載入 A 的證據。

實際 packaged fix-standard 為 9 cards、9 persona bindings、2 core gates；R-09/R-16/R-19 全列，因此 acceptance=2、orchestration=2。本次新增 I09 後已重新跑真函式及 qualification 負控制；讀回 invariant_count=9、domain=0、state=2，不沿用八組舊宣告。

| 輸入情境 | domain | state | acceptance | stability | orchestration | total/band |
|---|---:|---:|---:|---:|---:|---|
| 三件真草稿，現行 runtime 真函式 | 0 | 2 | 2 | 0 | 2 | 6 / Yellow |
| 同一真草稿，#831 accepted 規格映射的純投影 | 0 | 2 | 2 | 2 | 2 | 8 / Red |
| 假設未來正式 accepted 的記憶體情境，現行真函式 | 0 | 2 | 2 | 2 | 2 | 8 / Red |
| 同一未來情境，#831 的純投影 | 0 | 2 | 2 | 0 | 2 | 6 / Yellow |

上表現行欄為本次真函式結果，#831 欄為明示規格投影。真 draft completeness=false、missing_kinds=(spec,design,plan)，三份 assessment 的 blockers 全空、reasons 恰為 status-not-accepted；只改 accepted 的 counterfactual completeness=true。舊算法 `max(0,2-missing-blocking)` 對缺三種 accepted 材料仍給 stability=0，不表示 ready。#831 R2/D1 對任何未 accepted artifact 給 risk=2，**不是不分情境一律 stability=0**。

未來情境只在 memory 將 status 改 accepted，不再刪除已關閉的決策章；結果不回寫三件草稿，也不是 accepted authority。#831 欄只作 stability 的算術投影，未修改目前函式、未宣稱 #831 已安裝；真正接受/部署後仍要重跑。

另外已重跑 `plan_review_gate` 的 source/tests/documentation/CLI 四 surfaces 與 R-09/R-16/R-19/R-22，ready=true、三個 check 均跑；envelope observation 仍是 `bypass: envelope_unavailable`。**這個 helper 只檢 Tasks/contract/envelope，不能證明三件 artifact 已 accepted，也不代表模型測量資格。** 真三件 completeness=false 仍阻塞，不把 helper 的 ready 當整體可派工。

原五類 in-memory 負控制全部保留並命中預期：對 accepted counterfactual 注入 Open Questions 仍 blocked；去 Requirements heading 不完整；state=true 拋 ValueError；缺 domain 拋 ValueError；fixture envelope ceiling=7 對新宣告9 仍拒絕，另增 ceiling=8 的邊界拒絕。加上真 draft-status 拒絕，共七項具體 checks；不以關 OQ 為由移除 failure matrix。Fixture envelope 不是實際 model/agent/effort 測量資料。

另以不 import 新產品 API 的 in-memory 參考計算核對 D7 encoding 邊界：int1/float1.0/booltrue/string1/+0.0/-0.0 六種值的 fingerprint 各異、object key 重排同值、checkpoint/recovery prefix 分離、NaN/+Inf/-Inf 拒絕。這只是文件 canonical 算術驗算，不是新 checkpoint schema/CAS/rollback 或 owner/proof 驗收；完整產品 golden 與所有 I09 負例仍是待 Cortex 實作測試。

前次 checkpoint reader 複核回 PASS／新增 BLOCKER/MAJOR=0，七問皆正確辨識 checkpoint/recovery/歷史 ABA/loader 邊界；完整 snapshot 的 deep-copy 防 alias 界線也獲確認。這是該輪文件理解結果，不是 accepted、產品驗收，也不覆蓋下列後續 fresh review finding。

前次交付已跑 pure checks／七項負控制與文件檢查；本次 T10 lifecycle 最小修訂的 hash/局部檢查另於下節記錄，不沿用舊 hash 冒充 fresh review 通過。Report 自身 hash 交付時另回報，不在本檔自我雜湊。

## Prior Four-file Fresh Review: Own OpenSpec Lifecycle

Root 全文核對後，最新 fresh review 回 FAIL，唯一缺口是 A 自身 OpenSpec lifecycle 未明列。初稿把建立責任放在未來產品 run 的 propose 階段；root 以完整 accepted triad 可直接 plan gate→build 的 source trace 指出不可能時序，已直接替換，不保留相互矛盾文字。現改為 Todo Repository Intake Prerequisites：正式 planning 責任方在產品 run start 前建立、審完、合併/發布 own 四件、strict validate、唯一 mapping/source binding/read-back；own proposal/design self-contained 等價、tasks 完整0/2/9/surfaces/rules 與 Todo 一致，避免 first-plan/last-sizing 分歧。T10 只核對既存前置證據，不期待產品 run 補前置件；candidate frozen planning 只准 checkbox toggle，operator baseline immutable。Spec S13/Design D1 同步；產品 merge/installed/live、C/B/D/parent 收尾留 prose，archive 只准本 child，不可封存 umbrella。

該輪 OQ01/OQ02、I01–I09、domain=0/state=2、draft／ready-for-root-review 均保留；當時未建立 OpenSpec 檔、改產品/registry、開 issue、commit/push 或 dispatch，交 hashes/diff 後停等 fresh review。Root 後續回報該原四件 fresh refine_plan_review PASS／0 BLOCKER/MAJOR，才另外授權本輪八件 authoring；此 PASS 不自動涵蓋新增 own OpenSpec 四件。

Root 時序更正後，該輪從 checkout 外以 authoring base `b1b44bc476dc49481d8467c9595e7c4db3f1a87d` 的真 pure 函式重跑局部檢查：三件 draft／ready-for-root-review、0/2/9、14 個未勾 Tasks 均保留；completeness=false 且僅 status-not-accepted，現行6 Yellow；僅 memory 改 accepted 的情境 completeness=true、現行8 Red。#831 僅分別投影8 Red／6 Yellow。Helper ready=true 但 envelope_unavailable，不是整體可派工。當時四件相對連結、newline／尾端空白及範圍檢查通過；git diff --check 通過、tracked diff 空，只有原四件未追蹤草稿。當時尚無自身 OpenSpec 目錄；這是歷史檢查，不當作本輪八件檢查。

## Prior Eight-file Draft OpenSpec Intake Authoring

本節完整記錄上一輪八件仍為 draft、尚未建立 #862 的 authoring 與測量；其中「本輪／目前」均指該歷史階段，不是下方 root 已接受後的現況。

Root 授權本步驟後，先盤點原四件未追蹤草稿，再正常執行 `git pull --ff-only origin main`，結果 Already up to date，HEAD 仍為 `b1b44bc476dc49481d8467c9595e7c4db3f1a87d`。只新增自身 `openspec/changes/recovery-registry-receipt/proposal.md`、`design.md`、`tasks.md`、`specs/recovery-registry-receipt/spec.md`，沒有使用 umbrella change 或新增第五個 OpenSpec 檔。原四件僅更新 authoring 範圍／時態／T10–T11 與本 report；I01–I09、D2–D8 及原產品負例未改。

Own proposal 完整承載 I01–I09、母 S01–S13 mapping、non-goals/工程裁決；own design 完整承載 D1–D8、Verification 與 residual，和 Superpowers design bytes 相同。Own tasks 與 Todo 的 `## Tasks` 內容逐字相同，14 個 pre-archive checkbox 全未勾；兩者皆 domain=0/state=2/invariant=9、artifact_classes=source/tests/documentation，source/tests/documentation/CLI surfaces 與 R-09/R-16/R-19/R-22 一致。Archive 及產品 merge/installed/live/C/B/D/parent 待辦只列 prose，不封存 umbrella。Own delta 逐字保留九組規範本文，另給19個正負 scenarios；未減少原 failure matrix 或把 source 數量當成 domain 降分。

本步依 doc-coauthoring 以已審文件為 context、逐件等價展開，不重新裁決核心契約；依 preflight-ci 檢查本階段實際可執行的文件 gate。因本輪禁止 commit/PR/changelog/產品測試與額外檔案，沒有執行完整 PR-context policy-preflight，也不稱 PREFLIGHT PASS。Reader 與 issue-owner metadata 留 root 後續處理。

實際執行 `openspec validate recovery-registry-receipt --strict --no-interactive --json`（本機 OpenSpec CLI 1.4.1）：1 change passed、0 failed、issues=[]。這只證明 delta 格式合法，不是 Cortex accepted、正式 source binding 或產品通過。

從 checkout 外以 `python3 -B` import 此 authoring base 的真 `_artifact_rows`、`assess_planning_artifact`／`assess_planning_completeness`、`compute_sizing_score`／`current_sizing_snapshot`、`plan_review_gate` 與 `_checkbox_insensitive_equal`。planning.py SHA-256 為 `dc62f1867778c4e01beba8bfbb71b9133e17590df9f615a538cd4cd2a3da371e`；未執行模型、manager action 或 runtime 寫入，未查驗 active daemon 是否載入 #831。

`work_bridge.py:220–248` 的 `_artifact_rows` 以 **純 in-memory fixture authority**（不是已註冊 mapping）產生下列六項；delta spec 不在此 planning 三種 kind 的 loader 內，另由 strict validation 檢查。`work_bridge.py:274–282` sizing 使用最後 plan；`manager.py:9353` helper 使用第一 plan。Fixture 順序中 first 是 own tasks、last 是 Todo；反轉順序也得到同樣6 Yellow，兩個 helper 契約結果相同。沒有真的建立 WorkAuthority 或執行 binding。

| artifact ref | kind | 真 draft assessment | 僅 memory 改 accepted |
|---|---|---|---|
| docs/superpowers/specs/recovery-registry-receipt-spec.md | spec | false／status-not-accepted | true |
| docs/superpowers/specs/recovery-registry-receipt-design.md | design | false／status-not-accepted | true |
| openspec/changes/recovery-registry-receipt/proposal.md | spec | false／status-not-accepted | true |
| openspec/changes/recovery-registry-receipt/design.md | design | false／status-not-accepted | true |
| openspec/changes/recovery-registry-receipt/tasks.md | plan | false／status-not-accepted | true |
| docs/superpowers/workstreams/recovery-registry-receipt/todo.md | plan | false／status-not-accepted | true |

六份真 assessment blockers 全空、reasons 僅 status-not-accepted；兩組各自 triad 與六份合併皆 completeness=false、missing=(spec,design,plan)、現行6 Yellow。兩組和合併的 accepted-memory 情境皆 completeness=true、現行8 Red。實際 packaged fix-standard 仍是9 cards／9 bindings／2 core gates，適用 sizing rules 全 R-09/R-16/R-19；#831 **只有規格投影**：draft8 Red、全 accepted6 Yellow，沒有聲稱 #831 已載入或在此實作。兩個 plan helper 皆 ready=true、envelope_unavailable；這個 observable bypass 不是新 run 的測量／派工授權。

本輪純負控制：兩個 plan 各以 envelope ceiling7/8 拒絕9組 invariants、state=true/缺domain 拋 ValueError；兩組 spec 各缺 Requirements 拒絕、注入 Open Questions 阻塞。每個 plan 的 candidate-only checkbox toggle 被既有 tolerance 接受，連帶改 state metadata 則拒絕；沒有更動磁碟 checkbox 或 operator baseline。所有 fixture 均只證明 parser/helper 的邊界，不冒充模型資格、owner/proof 或 A 產品測試。

另實測現行 aggregate 限制：其餘同 kind 已 accepted 時，單一重複 view 仍 draft，`assess_planning_completeness` 可回 true（planning.py:405–412 按 accepted kinds 彙總）。因此正式 intake 必須逐件核對六份 assessment.accepted 與等價正文，不能只依 aggregate green；這是本輪記錄的既有 consumer 限制，未在 A 改產品邏輯。目前六份全 draft，所以實際 aggregate=false，沒有借此啟用 work。T10 仍缺正式 owner/review/發布 revision/mapping/binding 證據，即使 local strict/helper 通過亦不得派工。

八件交付前已核對全部 frontmatter 為 draft／ready-for-root-review、13 個相對連結存在、newline／尾端空白與個人絕對路徑檢查通過、所有 pre-archive checkbox 未勾。git diff --check 通過、tracked diff 空，git status 精確只有八個允許的未追蹤檔；最後 strict validation 重跑仍1 change PASS／0 issues。七個正文檔與本 report 的 SHA-256 於交付回覆列出，不做 report 自我雜湊。停止供 root 獨立 review；原四件 fresh review PASS 不延伸宣稱新增八件已 PASS。

## Current Root Acceptance and Issue Read-back

Root 明示：八件 fresh review PASS／0 未處置 BLOCKER/MAJOR，I01–I09、D1–D8、六 views 與兩份14 Tasks 等價；root 隨後建立並讀回 #862，批准內容作 repository planning intake，尚未授權產品 dispatch。本 agent 本輪另以 `gh issue view 862 --repo hamanpaul/paulsha-cortex --json number,title,body,state,url` 全文讀回：state=OPEN，title=`fix(registry): 建立 recovery disposition 與 atomic receipt 原語`，URL 為本文開頭的 #862 連結；issue 確認 registry.py 唯一 production、I01–I09、0/2/9 與 A→C→B→D2、D1→D2，不擴父項或後續 consumers。本 agent 沒有新增或修改 GitHub issue。

接受對象為原八檔內容，唯一 owner `#862 / recovery-registry-receipt`。所有 status 已依批准改 accepted，舊 ready-for-root-review metadata 移除；report 歷史段仍保留當時 draft 與未開票事實。I01–I09、D2–D8、既有正負測試、單 registry 原子/legacy 邊界、domain0/state2/invariant9 均不變。兩個 Tasks 只同步接受狀態時態與對應 pure-check 描述，14 checkbox 全未勾；內容接受不等於產品任務完成。

規劃 PR 尚未合併發布、正式唯一 mapping/source binding 尚未完成；#862 已存在不等於 registry 已綁定。T10 尚缺發布 revision 與 binding/read-back 證據，仍不能 dispatch。依 root 現場裁決，#831 尚未 loaded；本輪只用 authoring base 真函式重算，沒有 runtime reload/服務檢查或寫入，6 Yellow 仍是待實際 loaded/重評後才可能成立的條件投影。

## Current Accepted Pure Checks

已以磁碟六份 accepted 內容重跑，沒有沿用上一輪 accepted-memory 代替真輸入。Authoring base 仍為 `b1b44bc476dc49481d8467c9595e7c4db3f1a87d`，實際 import 的 planning.py SHA-256 為 `dc62f1867778c4e01beba8bfbb71b9133e17590df9f615a538cd4cd2a3da371e`。六份每一個 assessment 均 accepted=true、reasons/blockers 全空；SP triad、own triad 與六份合併皆 completeness=true、現行8 Red。只在 memory 將各 artifact 改 draft 的六個負控制均為 status-not-accepted，沒有更改磁碟狀態。

真 `_artifact_rows` 純 fixture 順序及其反序的 current_sizing_snapshot 皆8 Red；first-plan helper 與 last-plan sizing 不因選到 own tasks/Todo 而分歧。兩份 Tasks 正文相同，各14 checkbox 仍未勾；metadata 均 domain0/state2/invariant9、source/tests/documentation。實際 fix-standard 仍9 cards／9 bindings／2 core gates，sizing rules R-09/R-16/R-19 全列；helper 另核 source/tests/documentation/CLI surfaces 與 R-22，兩個 helper ready=true 但 envelope_unavailable，不構成測量或派工批准。#831 的 stability 變更只作條件算術投影：全 accepted8 Red→6 Yellow，未聲稱已 loaded。

負控制均保留且重跑：兩個 plan 的 envelope ceiling7/8 拒絕9組 invariants，state=true／缺domain 拋 ValueError；兩組 spec 缺 Requirements 拒絕、注入 Open Questions 阻塞；candidate checkbox-only 在 memory 通過既有 tolerance，metadata drift 拒絕。I01–I09、D2–D8、delta 九組規範／19 scenarios 的原內容未變，沒有改產品 code/tests、registry 或 live 資料。

接受狀態後 `openspec validate recovery-registry-receipt --strict --no-interactive --json` 通過：1 change passed、0 failed、issues=[]。八份 frontmatter 都是 accepted／work_item=recovery-registry-receipt／issue862，無舊 design_readiness；13 個相對連結、newline／尾端空白／個人絕對路徑檢查通過，tracked diff 空，精確只有八個允許的未追蹤檔。最終 SHA-256 另於交付列出。這些是規劃/本地 pure 證據，不是產品測試、完整 PR-context preflight、qualification、merge、發布、binding 或 dispatch。

## Reader Questions

1. 只合併 A 能宣稱 S03 公開 recovery 已修嗎？
2. 舊 row 無 binding_revision 時，哪些入口不能自動初始化，哪個明示原語可以建立新起點？
3. prepared、complete、consumed 分別證明什麼，誰驗外部 proof？
4. 同 request_id 異 payload、相同 tuple 不同 revision，各應如何處理？
5. 為何 draft 的 Yellow 分數不能授權 build，#831 後是否真的更低？
6. Checkpoint 如何防止同 ID 被重用成 recovery，restart 後同 ID/new ID 各如何處理？
7. Checkpoint 能否證明 legacy 歷史 ABA？V1/#501 load-time 寫回是否被本次改掉？

2026-09-07 首輪 reader 前四問正確，第五問當時 pure 表未填故拒絕宣稱 #831「總是更低」。該輪 FAIL／1 MAJOR 為 required_steps 未進 request_digest；已修 D2/D4、I02、T02 並獲 reader 確認。這段是歷史 finding 的處置，不把首輪 FAIL 抹成從未存在，也不拿它替代 2026-09-08 新增 checkpoint 的複核。

## Delivery Accounting

| 帳目 | 本輪狀態 |
|---|---|
| Planning content acceptance | Root 批准八件 accepted，唯一 #862 owner；尚未發布或 binding，非產品 dispatch 授權 |
| Pure completeness/sizing | 六份真 accepted 均通過；SP/own/合併 completeness=true、first/last8 Red；#8316 Yellow僅條件投影 |
| Own OpenSpec strict validation | 接受後重跑1 change passed、0 issues；非正式 binding/publish |
| Reader review | Root 回報完整八件 fresh review PASS／0 未處置 BLOCKER/MAJOR；本輪僅同步接受狀態、owner、時態與證據 |
| Product implementation / RED-GREEN / full tests | 未執行 |
| PR-context policy / remote CI / merge | 未執行，沒有 commit/push/PR |
| Installed / live / parent #497 AC closure | 未執行／未完成 |
