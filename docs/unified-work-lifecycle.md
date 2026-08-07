# Unified Work Lifecycle 操作與遷移

對應 OpenSpec 已於 2026-07-20 由 official CLI 封存至 `openspec/changes/archive/2026-07-20-unified-work-lifecycle/`；governed delivery closure、persona workflow orchestration 與 unified work read model 已發佈至 `openspec/specs/` 作為 canonical 規格。

## 四態 read model

Monitor 對每個 repo/work item 只公開 `topic`、`todo`、`on-going`、`done`。`blocked`、`needs_human`、`degraded` 是 facet，不是第五種狀態。

- `topic`：只有 open GitHub issue，尚無 confirmed Todo artifact。
- `todo`：有 `todo.md`、accepted superpowers spec/plan 或 active OpenSpec，尚未 claim。
- `on-going`：Manager 已建立 `WorkflowRun`；queued 到 ship 都維持此狀態。
- `done`：merge commit、所有 issue closed、default branch active OpenSpec 消失、archive 存在、Todo 完成與 CompletionRecord 全部驗證成功。

Provider 失敗時會保留 last-good snapshot 並標 `degraded`。GitHub provider 超過 900 秒沒有成功 snapshot 時，auto claim 與 merge 都會 fail-closed。

GitHub terminal closure scan 會以 authenticated default revision 的 Contents API 讀取 remote Todo，並重驗 path、blob SHA 與 base64 encoding；production 只對 canonical WorkflowRegistry 已連結的 PR 做 merge ancestry compare。只有 HTTP 502/503/504 會有限次 backoff retry，auth、rate-limit、其他 HTTP error、malformed JSON 或 identity mismatch 都立即保留 last-good 並標 degraded。

## Correlation authority

可授權 mutation 的關聯只來自：

1. repo 內 `.cortex/work-items.yaml` version 1；
2. Markdown scalar frontmatter `work_item`；
3. GitHub closing reference；
4. Manager workflow metadata。

Title、slug、branch 或 issue token 只形成 inferred display group，不能 start、merge 或判定 done。`cortex list --explain` 會列出 accepted/rejected signals。

Override 範例：

```yaml
version: 1
work_items:
  unified-work-lifecycle:
    title: 統一工作生命週期
    links:
      - kind: github_issue
        ref: owner/repo#14
      - kind: openspec
        ref: unified-work-lifecycle
    excludes:
      - kind: github_pr
        ref: owner/repo#999
```

`unlink` 會留下 exclusion，避免 inferred grouping 下次重新合併。單一 source 若被兩個 confirmed work item claim，整個 provider 會 degraded，Manager 不得派工。

## CLI

```bash
cortex list --repo owner/repo --state todo --explain
cortex work show unified-work-lifecycle --repo owner/repo --json
cortex work link unified-work-lifecycle --repo owner/repo --kind github_issue --ref owner/repo#14
cortex work unlink unified-work-lifecycle --repo owner/repo --kind github_issue --ref owner/repo#14
cortex work intake unified-work-lifecycle --repo owner/repo --issue 14
cortex work start unified-work-lifecycle --repo owner/repo
cortex work start unified-work-lifecycle --repo owner/repo --combo fix-standard
cortex work resume unified-work-lifecycle --repo owner/repo
cortex work retry-build unified-work-lifecycle --repo owner/repo --issue 14 --actor operator \
  --payload <(printf '%s\n' '{"expected_candidate":"<40-char SHA>"}')
cortex work abandon stale-canary --repo owner/repo --actor operator \
  --expected-run-id workflow-0123456789abcdef0123 \
  --reason 'Superseded by the terminal canary.'
cortex work auto unified-work-lifecycle --repo owner/repo --enable
cortex work auto unified-work-lifecycle --repo owner/repo --disable
cortex stat --combo-selections
cortex doctor --probe-live --repo owner/repo --json
```

### Combo 自動選擇

`cortex work start` 與 auto claim 建立 workflow 時，Manager 會先讀 durable snapshot 內已確認的 GitHub issue title，交給 `paulsha_cortex/deck/task_types.py` 的 taxonomy 做機械分類，再映射到 combo。現況 `feat` 會選 `feature-oneshot`、`fix` 會選 `fix-standard`；`docs`／`test`／`ci`／`refactor` 目前仍是明示缺口，會帶 `bypass-default` provenance 沿用既有 `feature-oneshot`。

若標題是 `unknown_type`、scope 不在受控詞典、或多個 mapped issue 得到互斥 type，claim 會 fail-closed，且不建立 WorkflowRun。修法只有兩種：修正 issue title，或用 `cortex work start <work_id> --repo <owner/repo> --combo <id>` 做 authoritative override。override 永遠優先於自動選牌，並會在 run 的 `combo_selection` 留下 `explicit-override` 來源。

`cortex stat --combo-selections` 會彙總 `source × task_type`，直接看出多少 run 是自動選牌、多少走 override、多少因 title 缺席／unparseable／combo 缺口而 bypass。`fix-standard` 雖然比 comment 草稿多了 `openspec-propose` 與 `writing-plans` 兩張 planner 卡，但這是為了滿足 `validate_manager_spine` 的完整 phase spine；verification 與 code-review 兩條核心 gate 維持不變。

### Intake（`link` + `start` 合成，#203）

`cortex work intake <work_id> --repo <owner/repo>` 是「拿到一個 issue/task 就進件」的單一入口，取代已停用的低階 `dispatch`。它等價於「（必要時）`link` 後接 `start`」，但收斂成一次呼叫：

- 帶 `--issue N`（或 `--kind/--ref`）時，若該來源尚未反映在受監控快照的 `mapped_issues`／`mapped_openspec`／`mapped_todo_paths`，會先寫一筆 override link（與 `cortex work link` 相同語法、相同 fail-closed 驗證），再重新載入 authority。
- 省略 `--issue`／`--kind`／`--ref` 時，直接沿用 work_id 現有的 confirmed authority（等價於單獨呼叫 `start`）——這是「work_id 已有 confirmed Todo 或已 link issue」時的常見用法。
- 兩種路徑最終都轉交既有 `start` 語意（`claim_key` 去重、`--combo` override 皆比照 `start`），不繞過 `default_workflow_manifest`／`validate_manager_spine`。
- **Intake 不會憑空建立新 authority**：`.cortex/work-items.yaml` 這份 override 檔與受監控的 `work-items.snapshot.json` 是兩份分開維護的狀態，override 寫入後仍要等下一輪 Monitor correlation 才會併入快照。因此若 work_id 既無 confirmed Todo、也未曾 link 過 issue，且本次呼叫也沒有帶 `--issue`／`--kind`/`--ref`，intake 會 fail-closed 拒絕，不建立 WorkflowRun；純文字任務仍需要先有明文授權來源（confirmed Todo 或 linked issue）才能進件。

```bash
cortex work intake unified-work-lifecycle --repo owner/repo --issue 14
cortex work intake unified-work-lifecycle --repo owner/repo
cortex work intake unified-work-lifecycle --repo owner/repo --issue 14 --combo fix-standard
```

Telegram 等 bot 宿主若要提供「貼一段文字/issue 就進件」的入口，應呼叫 `submit_work_action(action="intake", ...)`（`paulsha_cortex/control/client.py`）；既有的 `/dispatch <slice_id>` 走既存 slice_id 派工，維持原樣不變，不在本次範圍內改動。
### Work identity migration（設計中，見 ADR-0002）

`link`／`unlink` 目前一次只能對單一 `(work_id, source)` pair 生效，重識別
（例如 `-v2` 世代熔斷）要把一批來源整批從舊 work_id 搬到新 work_id 時，只能
靠多次分開的 `link`／`unlink` 呼叫加上「等 Monitor 下一次 rescan 確認生效」
的手動判斷——`hamanpaul/paulsha-cortex#326`–`#330` 是本專案自己實際跑過一次
的完整記錄，橫跨 5 個 PR、近 9 小時。`docs/adr/0002-work-identity-migration.md`
定了收斂成單一 `cortex work migrate` 動詞的設計（單一 atomic override
transaction、凍結 authority 做 abandon CAS、不放寬 `claim.py` 既有的碰撞
不變量），供後續 code 票直接實作；本文件的 `## CLI` 範例區塊會在該動詞落地
後同步補上。

`retry-build` payload只接受`{"expected_candidate":"<40-char SHA>"}`。Manager會把它當CAS，不把caller內容當evidence；通常只有ongoing `needs_human` verify/review run、無active job、舊build全passed且Candidate完全相同時，才原子重開最後一張builder card，清除舊verify/review authority並立刻派出新builder。另一個窄化入口只處理final builder terminalization失敗：run必須停在build phase、前置build card全passed、final card pending，而且最新同card job已成功退出（`exited/0`）卻沒有workflow evidence；真正的failed job不符合此入口。所有recovery prompt都要求先檢查worktree是否已有repair commit，並允許builder提交或採用已測試的descendant Candidate；Manager仍獨立驗證exact舊Candidate CAS與單調ancestry。terminalization recovery另要求保留declared input snapshot並先檢查未綁定commit。Plan/build terminal的`outputs`只可列出符合manifest的repo-relative artifact paths；manifest沒有outputs時必須精確回`[]`，不得塞入action/summary物件。Ship authority 原則上必須仍為pending；唯一例外是已通過且 identity 精確為 `cortex-manager/deterministic/cortex` 的 `openspec-archive`，此時保留official archive step並只重設後續gate，讓post-archive finding可由tested descendant Candidate修正。Manager會把已移走的active brainstorm artifact對應到同hash且唯一的official archive path重證，不接受caller改寫authority、模糊archive或symlink；任何其他已通過ship card仍拒絕retry。新Candidate仍必須是舊Candidate的exact descendant。`link`、`unlink`、`start` 與 `resume` 不要求 caller 提供 repo root；Manager 只會從 installer 的 `PSC_REPO_ROOT` 或 Monitor workspace registry 解析與 `owner/repo` remote 完全一致的 canonical git top-level。當同work只有一個`done/ship` run且terminal journal binding完整時，explicit `resume`會重跑current authority的ship validator來刷新stale CompletionRecord；不會建立新run、重開builder或dispatch card，pending／needs-human／malformed結果也不會覆寫既有completion。`auto` 未指定相容用的 `--issue` 時會套用到全部 confirmed mapped issues。

`recover-repair-commit`（#260）處理另一種 build phase 卡死：repair job 以 `status == "failed"`、exited 且 exit code 非 0，或 exited/0 但 terminal payload 缺漏／malformed 終止，卻已在既有 builder worktree 留下合法 descendant commit。此窄化入口只在 run 為 ongoing、帶 `needs_human`、停在 build phase、前置 build card 全 passed、final builder card pending、最新同 card job 無 bound `workflow_evidence` 且無 active job 時可用，且不啟動任何 model session——adoption 完全由 Manager 側確定性驗證完成。判準全部取自系統事實：worktree 路徑取自該 failed job row（不接受 caller 指定路徑）；operator 提供的 `expected_run_id`＋`expected_candidate`（40-char SHA）只做交叉比對——`expected_candidate` 必須精確等於該 worktree的 `git rev-parse HEAD`、worktree 必須乾淨（`git status --porcelain` 為空）、必須為原 candidate 的合法 descendant（`git merge-base --is-ancestor`）且不得與原 candidate 相同；任一不符即 fail closed 並回報具體原因，candidate authority 不變。成功後會寫入一筆 immutable `cortex-work-repair-adoption/v1` evidence record（含 failed job id、observed HEAD、adopted／previous candidate、actor），並登錄一筆沿用既有欄位集合的 adoption job row：identity（executor／model／independence_domain）、worktree 與 dispatch_head 複製自 failed job，`subject_head` 為 adopted candidate，狀態 exited/0，`workflow_evidence` 指向該 record；failed job 原始 row 原樣保留不被改寫。Manager 接著原子完成 `candidate_head` 換綁、final build card 標 passed（以 adoption job 的 builder identity）、`current_phase` 進 verify，讓既有 verify → foreign review → exact-head final 管線對 adopted candidate 重新把關，不重跑已完成的 planning。重送相同 request（`candidate_head` 已是 expected SHA 且對應 record 存在）回報 `already-recovered`，不產生第二次 adoption、第二個 job row 或任何 model session。此 action 與 `retry-build` 的 exited/0 unbound 窄化入口對同一情境刻意重疊——两个入口都合法，由 operator 依 commit 是否可信選擇；两者 CAS 各自獨立，不互相放寬，`retry-build` 的 exact `expected_candidate` CAS 與既有窄化入口行為維持原封不動。periodic runner 不取得此 recovery authority。

`resume`／`retry-build` 的 job 選擇同樣於 #260 收斂：operator resume 遇到已 terminalized 的失敗 job 時（`status == "failed"`，或 `status == "exited"` 且 exit code 非 0），第一次 `cortex work resume` 即 dispatch replacement job，不再重選 stale failed job 空轉一輪（過去只認 `status == "failed"`，`exited` 非 0 的 stale terminal 會讓第一次 resume 只重新回報 `job-failed`、要再執行一次才 dispatch replacement）；exited/0 的既有三條路徑（unbound terminal recovery、malformed schema retry、正常 terminalize）條件式不動。replacement dispatch 後再次 resume 回報 in-flight，不產生第二個 replacement job。失敗回報一律附掛 `_terminal_parse_diagnostics` 的唯讀 `terminal_diagnostics`（observed HEAD、job id、失敗原因），與既有的 `authority_granted: false` 模型一致：可觀測不等於可授權，不會因此讓 candidate 取得任何 authority。

`abandon`只處理尚未進入delivery的舊run：exact run CAS、current WorkAuthority refs、actor與單行reason全部重驗，且任何active Job、PR ref、passed ship step或CompletionRecord都會拒絕。成功後只把該run標成`superseded`，並以immutable `cortex-work-abandon/v1` evidence保存reason；不勾未完成tasks、不建立CompletionRecord，也不把abandoned work投影成done。重送同一CAS/reason冪等，不同reason或已有另一個active run則fail-closed。

若 delivery 尚未建立 immutable binding，就因 PR／OpenSpec／Todo target 數量不是各一個而停在 `needs_human: multiple-delivery-targets-unsupported`，operator 修正 repo-local correlation 後可明確 `resume` 同一 WorkflowRun。Manager 只會在 current authority 已重新收斂為恰好一組 target 時清除此特定 stop；已建立 binding 或其他 `needs_human` 原因仍維持 fail-closed。

工作啟動後，`$PSC_COORDINATOR_ROOT/jobs.json` 內的 `workflows` 是唯一 workflow lifecycle truth。Delivery journal 只保存以同一 `run_id` 為 key 的 resumable ship phase，不另建 lifecycle state。ship transition 現固定分成 `local-closeout → pr-preflight → external-ship` 三段：沒有既有 PR 時，Manager 先在 builder worktree 完成本地 closeout（canonical report cleanup、official `openspec archive`、archive commit 與 candidate reset 回 verify），此段零 `gh`／`git push`；closeout 完成後才會在乾淨、policy-compliant且完成後刪除的暫存 `feature/preflight-*` exact-Candidate checkout 以 metadata context 跑 PR preflight。preflight 失敗會停在可 resume 的 `pr-preflight-blocked` typed stop，本地 closeout 結果保留、run 可直接 resume 重試。preflight 通過後 Manager 接著 push exact Candidate、冪等呼叫 `create_or_get_pull_request` 建立 PR，並把 `pr_ref` 原子寫回同一個 `WorkflowRun`——沿用既有 operator authorization 模型（push／PR 建立不需額外手動授權，merge 仍受 `merge_authorization`／operator `resume` 把關）。既有 PR 的 push、metadata 寫入、review request 與 merge 仍全部留在 external-ship 段；builder worktree 內的 accepted planning overlay 不會混入這條 exact-tree gate。若 review 完成後 default branch 或 provider refresh 使 WorkAuthority digest 前進，Manager 會在 push 前只重綁同一 run 的 current `source_revision` 與 delivery journal authority；不可變的`planning_source_revision`、claim、Candidate及verify/review evidence不變，registry/journal間的crash window可於resume冪等重播。Canonical Job envelope持續以Job dispatch時保存的immutable source revision重驗，不會因run current revision前進而改寫或誤判舊證據。slice-based foreign review worktree 與 workflow reviewer sandbox 兩條路徑都會 materialize frozen authority refs，逐檔重算 sha256 驗證；缺檔、hash drift 或未紀錄的 overlay 一律 fail-closed。Manager啟動quick policy與configured CI-parity gate時會移除所有繼承的`PSC_*` runtime authority，並改用完成後刪除的disposable `HOME`／`XDG_CACHE_HOME`；Python user-site與GitHub config等必要工具／認證root則顯式保留，避免preflight測試經由installed bootstrap重新取得production coordinator、executor或repo。Manager systemd unit固定`UMask=0022`，讓exact-Candidate suite不受operator service umask影響。Verify/review report是Manager-owned evidence material：最後一張review已取得immutable canonical evidence後，delivery只會清除hash完全吻合且未被Candidate追蹤的report，並在刪除前寫入hash-addressed immutable cleanup intent；只有同一intent的crash/retry evidence reader可接受report已不存在，unknown、tracked、symlink、可寫或malformed intent、未授權缺檔或drift一律阻擋。若review-complete run的ship validator失敗，Manager會先持久化`needs_human`與failed gate再回報錯誤。後續 merge 與 CompletionRecord 也綁定該 run 的 exact Candidate 與 canonical verification/review evidence。

既有 PR metadata transaction中的 title/body PATCH、labels PUT及PR/issue identity reread，只有在明確 HTTP 502/503/504 時做有限次 backoff retry；每次成功仍須完整reread。PR create、Candidate push、review request、merge與其他 delivery side effect不套用這個 retry，auth、rate-limit、其他 HTTP error 或 malformed response 立即 fail-closed。

Manager在metadata write前先authenticated reread PR title/body與完整labels；若三者已精確符合canonical metadata，就不發PATCH/PUT。只有確認drift才執行冪等write，之後再完整reread；因此write omission仍是有remote evidence的validated no-op，不是跳過gate。

Verify/Review dispatch只接受schema v2明示`review` capability、且independence domain不同於Builder的identity。Reviewer以enforced read-only mode在exact Candidate的disposable clone執行；Claude reviewer固定使用`dontAsk`與`safe-mode`而非Plan Mode，只暴露OS-sandboxed Bash，並由Manager-generated phase contract把StructuredOutput收緊成verification或review exact schema，不載入Candidate customization、remote session或MCP。Filesystem拒讀home、`/run/user`與Docker sockets；Linux/WSL會先解析並去重`/run`、`/var/run`等symlink aliases，避免同一socket形成衝突bind，仍只重開Candidate、Python user-site工具鏈與解析後的官方SRT package root（供`apply-seccomp` helper執行），並以`failIfUnavailable`、禁止unsandboxed fallback及Candidate deny-write執行測試；review subprocess只保留非密鑰基礎環境且使用非login shell，避免parent env或shell profile匯入credentials。Linux/WSL缺Claude Code 2.1.187+、必要CLI surface、`bubblewrap`、`socat`或`srt`，或live native/configured-policy/Unix-socket seccomp smoke失敗即fail-closed。Manager把Claude protected-path bind targets建立在deterministic disposable session root，exact Candidate固定置於其`candidate/` checkout，避免污染Candidate material tree；terminal、launch failure與operator retry路徑都會重驗原Candidate完整tree snapshot後清除整個session root。terminal只回substantive verification/findings與inline Markdown body；Manager依durable Job自行建立report frontmatter、Candidate/job/identity binding與GateEvaluation。Report路徑限於phase專屬的`reports/verify/*.md`／`reports/review/*.md`，durable publication journal可在多檔partial write、canonical evidence或registry save fault後rollback，亦可在已bind的crash replay中roll-forward。整份log恰為單一JSON fenced object時可解析，但含prose、第二個fence或錯誤schema仍fail-closed。

舊版曾把 planning-only canonical Agy 誤派成 reviewer，亦曾把Claude reviewer啟動在Plan Mode而得到`exited-0`卻沒有terminal payload。這些既存 terminal 不會成為 evidence；只有 operator 明確執行 `cortex work resume`，且最新 Job 的 run/claim/repo/source/card/phase/Candidate/builder/reviewer identity/output/sandbox snapshot contract 全部精確吻合時，Manager 才保留舊 Job/log並重派一次。Reviewer的原始Candidate root必須精確等於已驗證Builder Job worktree，而不是WorkflowRun主workspace。Periodic runner 不取得此 recovery authority。

工作預設 manual。Auto claim 同時要求 confirmed Todo、confirmed issue 與 `cortex:auto-on-going` label；移除 label 只阻止尚未 claim 的工作，不會中止 active workflow。Todo 缺 issue 時不會自動建立 issue，而是 `needs_human: missing_issue`。

合法且exact-bound的review `state=rejected`會保存immutable GateEvaluation、把當前card標成`needs_human`並停在原phase；periodic runner不得重派。只有operator explicit `cortex work resume`可在Candidate、report與evaluation hash重驗後建立fresh reviewer Job。Blocking category只描述Candidate或acceptance缺陷；若只是前份review report的措辭／列舉精度且不改變Candidate verdict，fresh reviewer應以non-blocking `style`留下更正，不得冒充Candidate correctness。

若合法`state=passed` review evidence已canonical bind，但step audit或registry save在完成前中斷，operator resume會重驗同一份exact evidence並冪等重播，不建立fresh reviewer Job；forged、stale或unknown state仍停在`needs_human`。

## Snapshot 與 registry migration

- Work snapshot：`$PSC_MONITOR_STATE_ROOT/work-items.snapshot.json`；未設定時為 `$PSC_AGENTS_ROOT/monitor/work-items.snapshot.json`。
- Installed service 先依 unit 宣告順序合併 `<instance>.env` 與 `<instance>-manager.env`；預設 socket 為 `$PSC_AGENTS_ROOT/run/<instance>/project-monitor.sock`，`monitor.socket_path` override 優先。
- `doctor --probe-live` 必須以 production Monitor config 解出 socket，再用 read-only `list_work_items` 驗證 `ok` 與 `cortex-work/v1` envelope；裸 listener 或只完成 connect 都視為失敗。Identity registry若配置Claude `review` capability，doctor亦把Claude Code版本/CLI surface、`bubblewrap`、`socat`、`srt`、live native與Unix-socket seccomp smoke列為required probe；未配置Claude reviewer時只回非必要warn。
- Snapshot schema：`work-items-snapshot/v1`，mode `0600`，atomic replace + file/directory fsync。
- Coordinator registry：首次載入合法 v1 時先建立 read-only、content-hash 命名的 backup，再升級為 v2。
- 舊 jobs/slices 只進 `legacy_records`，不會猜測 work item association。
- Unknown/malformed schema 不會覆寫現有合法檔案；先修復或從已驗證 backup 恢復，再 restart service。

## Delivery gate

Manager 是唯一 writer。每次 push 都會使上一個 delivery review epoch 失效，並重新要求 current-HEAD review。Merge 前必須同時具備：

- exact tree 的 policy + pinned preflight；
- deterministic verification 與不同 independence domain 的 ForeignReview；
- 恰好一種 current-HEAD typed delivery review：非 error 且 threads resolved/outdated 的 Copilot review，或 immutable exact-HEAD maintainer attestation；
- terminal-green checks/statuses、closing refs、archive diff 與 mergeability；
- fresh GitHub provider snapshot。

最多兩輪 builder fix/re-review，每個 HEAD 等待 15 分鐘；current-HEAD review 出現 finding 時，delivery adapter 會把 `fix-required` fail-closed 投影為 `needs_human`，只有 operator 的 exact-Candidate `retry-build` 才能重開 builder；第三次仍有 finding 或逾時也維持 `needs_human`。若後續已由Manager綁定exact-HEAD maintainer evidence，只有完整path/hash可重入這類`copilot-*` stop，其他stop reason仍fail-closed。合併只使用 `gh pr merge --merge --match-head-commit <HEAD>`，不使用 auto/squash/rebase。Merge 後會重新 fetch default branch，驗證雙親 merge commit ancestry、issue、archive、Todo 與 CompletionRecord；CompletionRecord會保留實際使用的`copilot`或`maintainer-review` kind/ref/hash，並要求恰好一種delivery review authority。Completion Draft以排除`completed_at`的normalized closure語意hash版控：同語意重試沿用首份immutable draft，default branch或authority前進則建立新revision並保留舊檔，任何malformed collision都拒絕覆寫。Verify與ForeignReview原始canonical envelope會先依各自per-card slice完整重驗，再以共同WorkflowRun ID派生只供closure使用的evidence，使CompletionRecord strict reader可交叉驗證slice、Candidate與builder/reviewer jobs；原始證據不改寫。若post-archive retry-build產生descendant final Candidate，ship audit只對registry仍標示passed的Manager archive job接受Git驗證過的ancestor；policy-commit仍須exact final Candidate，unrelated commit或ancestry錯誤全部拒絕。此時active OpenSpec planning path已由official archive移走、issue/PR/archive狀態使WorkAuthority digest前進都屬預期terminal transition；完整綁定`merged`或cached `done` Candidate/merge commit/authorization的journal會直接進入ship validator closure，涵蓋validator已完成但WorkflowRun finalization尚未落盤的crash window。若default snapshot在此期間前進，Manager會提供current semantic draft，validator以它完整重驗成功後更新journal的CompletionRecord；沒有replacement時仍重驗cached record。Immutable authorization保留merge當下的digest而其他binding仍完整重驗，不回退要求active path；merge-authorized與merge前gate完全不使用此例外。全部remote facts成立才投影 `done`。

V1 terminal delivery 僅支援 GitHub。其他 forge 仍可顯示 read model，但 ship 會停在 `needs_human`。

## Terminal lifecycle canary

`terminal-lifecycle-canary` confirmed mapping 對應 issue #31。這條 docs-only canary 保持 persona-domain separation：primary `planner` 必須整合 `agy/google` evidence 完成 heterogeneous brainstorm，`planner`、`builder` 與 `reviewer` 則分別在不同 independence domain 產出規劃、最小文件 diff 與獨立審查 evidence。

候選變更必須通過 OpenSpec validation、policy、full preflight、ForeignReview，以及 exact current-HEAD 的 adversarial maintainer review。任一 typed output 或 gate 缺失、失敗或無法對準同一 HEAD 時，workflow 必須 fail-closed 保持 `needs_human`，不得宣稱 terminal completion。

PR #54 僅識別目前仍為 open 的 delivery target；此編號本身不是 merge、issue closure 或 `done` evidence。Manager 先以 official archive 流程封存 OpenSpec change，後續只有在其餘 strict gates 通過後，才可透過該 PR 以帶 closing reference 的 merge commit 交付並關閉 issue #31。重新讀取 default branch 與 remote authority 後，只有 PR、archive、merge ancestry、issue closure、Todo 與 CompletionRecord 全部成立，Monitor 才能投影為 `done`。

## Terminal/result contract（#261）

`paulsha_cortex/coordinator/terminal_contract.py` 是 terminal/result 契約的單一真相源，供 build、verify、review 三類 card 共用。

**Canonical envelope。** envelope 帶 `schema_version`，並完整支援 `passed`、`failed`、`needs_human` 三種終局狀態與結構化 `diagnostics`；三類 card 都不存在「只有成功形狀才合法」的路徑。不帶 canonical 版本的舊 payload 走相容讀取路徑並記 legacy 標記，既有 run 不因版本差異被拒收。

**gate ledger 由 manager 產生，不是模型自述。** 重驗只有在「被驗的東西不是模型講的話」時才有意義。`launcher.build_wrapper_script` 產生的 headless wrapper 是 manager 擁有的，形狀為：

```text
<模型 argv>; printf %s "$?" > <sentinel>; python3 -m paulsha_cortex.coordinator.gate_ledger --out <ledger> --worktree <wt> >/dev/null 2>&1
```

三段以 `;` 串接，因此模型失敗時 sentinel 與 ledger 仍會產生；sentinel 早於 gate 階段寫入，模型的 exit code 不會被 gate 耗時污染；gate 階段輸出導向 `/dev/null`，不污染 JSONL 的 terminal evidence。gate 清單由 operator 以 `PSC_GATE_CMD_<NAME>` 環境變數宣告（沿用 `PSC_PREFLIGHT_CMD` 的 typed-argv 規範，拒絕 shell wrapper），exit code 由真實 subprocess 產生。模型既不能選擇跑哪些 gate、不能決定 exit code，也拿不到 ledger 路徑（`<log_dir>/<slice_id>.gates.json` 由 job 的 `log_path` 推導，模型的 cwd 是 worktree）。跑不起來或逾時的 gate 一律記為 `failed`，避免 operator 設定壞掉靜默變成 fail-open。

**成功必須被證明。** `manager.terminalize_workflow_job` 在任何狀態採信之前，先以 `_assert_terminal_gate_consistency` 做確定性 cross-check：只要 ledger 中有任何 gate 的實際結果不是 passed，terminal 自稱的 `passed` 一律 fail closed，錯誤訊息保留哪一個 gate、期望值與實際值。「沒提到」不能當作「沒失敗」——terminal 完全不引用某個失敗的 gate 也一樣被否決。ledger 自身矛盾（記了非 0 exit code 卻標 passed）視同失敗。envelope 內的 `gate_evidence` 是模型「自述跑了哪些 gate」的宣告，manager 以 ledger 對照：宣稱跑了 ledger 中不存在的 gate，或宣稱的結果與 ledger 不符，皆 fail closed。會實際跑 gate 的 phase（`build`／`verify`，見 `GATE_LEDGER_REQUIRED_PHASES`）若連 ledger 都不存在，代表 wrapper 的 gate 階段沒跑完，同樣 fail closed。模型輸出的自然語言、exit code 為 0、以及「沒有明確錯誤」三者皆不構成成功授權。

**operator 未宣告任何 gate 時的語意。** 沒有 `PSC_GATE_CMD_*` 時 wrapper 仍會寫出 `gates: []` 的 ledger：ledger 的**存在**證明 wrapper 跑完了，內容為空則代表 operator 明確選擇不設 gate。此時 `passed` 會被放行——這是 operator 的顯式設定，不是靜默旁路，但也表示此設定下沒有 R2 保護。要讓保護生效，至少宣告一個確定性 gate。

**`test_policy=red-required` 卡的語意反轉（#307）。** tdd-red 卡（`execution.test_policy=red-required`）的正確產出是「新增並 commit 會失敗的 RED regression test」；宣告 `PSC_GATE_CMD_PYTEST` 時，這張卡的 pytest gate *理應* failed，若照一般規則會與 terminal 自稱 `passed` 矛盾，結構性地讓這類卡永遠不可能通過。`_assert_terminal_gate_consistency` 因此會從 job 綁定的 `WorkflowRun.steps` 查出目前 card 的 `test_policy`，傳給 `terminal_contract.authorize_terminal`；只在 `test_policy="red-required"` 時，才對 ledger 中名為 `pytest`（`terminal_contract.RED_REQUIRED_TEST_GATE_NAME`）的那一項做反轉：exit code 精確等於 `1`（pytest 的 `TESTS_FAILED`：測試被收集、確實執行，且至少一個失敗）視為合格 RED 並反轉為 `passed`；exit code `0`（全綠，未產生 RED）反轉為 `failed`；其餘 exit code（`2`／`3`／`4`／`5`，對應 collection error／interrupted、internal error、usage error、no tests collected）維持既有的 `failed` 判定、不做任何轉換，避免「builder 根本沒寫測試」或「測試檔壞掉」被誤判為合格 RED。反轉只精準命中這一個 gate 名稱，其他 gate（`openspec`／`policy`…）與一般卡（`test_policy` 非 `red-required`）完全不受影響，仍走上一段的 fail-closed 規則；envelope 內模型自述的 `gate_evidence` 也刻意繼續對照未反轉的原始 ledger 事實，模型應誠實回報觀察到的結果（例如 `pytest: failed`），不需要也不應該自己做語意反轉。

**升級與運維。** 派工 prompt 現在發的是 canonical envelope（`schema_version: 2`，多帶 `diagnostics` 與 `gate_evidence`）。不帶該版本的舊 payload 仍走相容讀取路徑，不會因版本差異被拒收。切換當下已在飛行、且沒有 gate ledger 的 build／verify run，其 `passed` terminal 會 fail closed 並轉 `needs_human`——這是預期行為（沒有獨立證據就不放行），不是資料損毀：candidate 與 worktree 都還在，只是未被授權。處理方式是對該 run 重新派工該張 card（resume 會以新的 wrapper 重跑並產生 ledger），不需要 abandon 整個 work item；只有在 candidate 本身已被判定不可用時才需要 abandon 重跑。

**schema mismatch 是有上限的確定性失敗。** StructuredOutput 的 wrapper 正規化只認明確白名單外層鍵（`input`／`params`／`parameters`／`arguments`／`payload`／`response`），且同一個確定性 mismatch 只嘗試一次修復；未知形狀終止為帶 machine-readable validation errors 的可操作錯誤，不以寬鬆解析吞掉未知欄位。`resume_workflow_run` 的 malformed-terminal 重派帶上限與計數器：計數持久化於 `WorkflowRun.attempts["schema-mismatch:<card>"]`，逾限即停止重派、轉 `needs_human`，並在回傳結果上曝光 `schema_retry_count`、`schema_retry_limit`、`last_validation_path` 與 `last_validation_reason`。計數同時經 workflow provider 的 observations 投影到 Monitor work item envelope，`cortex inspect work <id>` 會在發生過 mismatch 時列出 `schema_retry[<card>]: <count>/<limit>`（逾限標 `(exhausted)`）。計數刻意存放在既有的 `attempts` 欄位而非新增 `WorkflowRun` 欄位——新欄位會落在 `providers._WORKFLOW_V2_OPTIONAL_ROW_KEYS` 白名單之外，使每一個 run row 被判為 unsupported、整份 workflow projection 變 `degraded`（#205 曾實際踩到）。

**診斷與授權分離。** terminal parse 失敗時，`_terminal_parse_diagnostics` 保留 observed HEAD、job id 與失敗原因的唯讀診斷（`terminal_diagnostics`），但該 payload 明確標示 `authority_granted: false`，且不含任何 candidate authority 欄位——可觀測不等於可授權。
