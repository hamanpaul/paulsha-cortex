# Unified Work Lifecycle 操作與遷移

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
cortex work start unified-work-lifecycle --repo owner/repo
cortex work resume unified-work-lifecycle --repo owner/repo
cortex work retry-build unified-work-lifecycle --repo owner/repo --issue 14 --actor operator \
  --payload <(printf '%s\n' '{"expected_candidate":"<40-char SHA>"}')
cortex work auto unified-work-lifecycle --repo owner/repo --enable
cortex work auto unified-work-lifecycle --repo owner/repo --disable
cortex doctor --probe-live --repo owner/repo --json
```

`retry-build` payload只接受`{"expected_candidate":"<40-char SHA>"}`。Manager會把它當CAS，不把caller內容當evidence；通常只有ongoing `needs_human` verify/review run、無active job、舊build全passed且Candidate完全相同時，才原子重開最後一張builder card，清除舊verify/review authority並立刻派出新builder。另一個窄化入口只處理final builder terminalization失敗：run必須停在build phase、前置build card全passed、final card pending，而且最新同card job已成功退出（`exited/0`）卻沒有workflow evidence；真正的failed job不符合此入口。所有recovery prompt都要求先檢查worktree是否已有repair commit，並允許builder提交或採用已測試的descendant Candidate；Manager仍獨立驗證exact舊Candidate CAS與單調ancestry。terminalization recovery另要求保留declared input snapshot並先檢查未綁定commit。Ship authority 原則上必須仍為pending；唯一例外是已通過且 identity 精確為 `cortex-manager/deterministic/cortex` 的 `openspec-archive`，此時保留official archive step並只重設後續gate，讓post-archive finding可由tested descendant Candidate修正。Manager會把已移走的active brainstorm artifact對應到同hash且唯一的official archive path重證，不接受caller改寫authority、模糊archive或symlink；任何其他已通過ship card仍拒絕retry。新Candidate仍必須是舊Candidate的exact descendant。`link`、`unlink`、`start` 與 `resume` 不要求 caller 提供 repo root；Manager 只會從 installer 的 `PSC_REPO_ROOT` 或 Monitor workspace registry 解析與 `owner/repo` remote 完全一致的 canonical git top-level。`auto` 未指定相容用的 `--issue` 時會套用到全部 confirmed mapped issues。

若 delivery 尚未建立 immutable binding，就因 PR／OpenSpec／Todo target 數量不是各一個而停在 `needs_human: multiple-delivery-targets-unsupported`，operator 修正 repo-local correlation 後可明確 `resume` 同一 WorkflowRun。Manager 只會在 current authority 已重新收斂為恰好一組 target 時清除此特定 stop；已建立 binding 或其他 `needs_human` 原因仍維持 fail-closed。

工作啟動後，`$PSC_COORDINATOR_ROOT/jobs.json` 內的 `workflows` 是唯一 workflow lifecycle truth。Delivery journal 只保存以同一 `run_id` 為 key 的 resumable ship phase，不另建 lifecycle state。沒有既有 PR 時，Manager 會從 reviewed builder job、confirmed issue 與 OpenSpec authority 產生 zh-TW metadata，先驗delivery branch符合`feature/<slug>`，再於乾淨、policy-compliant且完成後刪除的暫存`feature/preflight-*` exact-Candidate checkout以metadata context跑preflight，之後冪等建立PR並把`pr_ref`原子寫回同一個`WorkflowRun`；builder worktree內的accepted planning overlay不會混入此exact-tree gate。若review完成後default branch或provider refresh使WorkAuthority digest前進，Manager會在push前只重綁同一run的current `source_revision`與delivery journal authority；不可變的`planning_source_revision`、claim、Candidate及verify/review evidence不變，registry/journal間的crash window可於resume冪等重播。Manager啟動quick policy與configured CI-parity gate時會移除所有繼承的`PSC_*` runtime authority，並改用完成後刪除的disposable `HOME`／`XDG_CACHE_HOME`；Python user-site與GitHub config等必要工具／認證root則顯式保留，避免preflight測試經由installed bootstrap重新取得production coordinator、executor或repo。Manager systemd unit固定`UMask=0022`，讓exact-Candidate suite不受operator service umask影響。Verify/review report是Manager-owned evidence material：最後一張review已取得immutable canonical evidence後，delivery只會清除hash完全吻合且未被Candidate追蹤的report，並在刪除前寫入hash-addressed immutable cleanup intent；只有同一intent的crash/retry evidence reader可接受report已不存在，unknown、tracked、symlink、可寫或malformed intent、未授權缺檔或drift一律阻擋。若review-complete run的ship validator失敗，Manager會先持久化`needs_human`與failed gate再回報錯誤。後續 merge 與 CompletionRecord 也綁定該 run 的 exact Candidate 與 canonical verification/review evidence。

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

最多兩輪 builder fix/re-review，每個 HEAD 等待 15 分鐘；current-HEAD review 出現 finding 時，delivery adapter 會把 `fix-required` fail-closed 投影為 `needs_human`，只有 operator 的 exact-Candidate `retry-build` 才能重開 builder；第三次仍有 finding 或逾時也維持 `needs_human`。合併只使用 `gh pr merge --merge --match-head-commit <HEAD>`，不使用 auto/squash/rebase。Merge 後會重新 fetch default branch，驗證雙親 merge commit ancestry、issue、archive、Todo 與 CompletionRecord，全部成立才投影 `done`。

V1 terminal delivery 僅支援 GitHub。其他 forge 仍可顯示 read model，但 ship 會停在 `needs_human`。
