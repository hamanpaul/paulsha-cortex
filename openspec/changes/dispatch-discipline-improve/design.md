## Context

現行coordinator有可沿用的骨架：frontmatter scan、per-slice worktree、headless launcher、atomic `JobRegistry`、manager tick、handoff JSON與DAG readiness。缺口位於load-bearing completion path：poller可直接寫`done`，`complete_tick`將它映射為`gate_status=passed`，shadow gate的false/exception不阻擋，`default_is_satisfied`再僅憑該欄位釋放downstream。manager daemon另會periodic自動apply全域broker reaper。

完整狀態與驗收語意見`docs/superpowers/specs/2026-07-12-dispatch-discipline-improve.md`；逐檔TDD步驟見`docs/superpowers/plans/2026-07-12-dispatch-discipline-improve.md`。

## Goals / Non-Goals

**Goals:**

- 在現有Job/manager/handoff/DAG骨架內阻止false completion。
- 讓deterministic verification、必要的ForeignReview與target ancestry都產生可重讀證據。
- 讓crash、stale evidence、unknown identity與fetch failure保持fail-closed。
- 停止periodic automatic broker signaling，保留受cwd root縮限的manual best-effort cleanup。
- 將所有`needs_human`狀態與可執行動作一次清楚列出，避免逐筆打斷operator。

**Non-Goals:**

- TaskRevision/TaskRun平台、journal/snapshot/hash-chain。
- 自動fix-loop、resume、cost routing、batch integration、rollback engine。
- remote/signed override、standing authorization或human attention digest service。
- untrusted-code network/filesystem sandbox、pidfd/cgroup ownership證明。
- squash/cherry-pick後的content identity。

## Decisions

### 1. 擴充現有atomic state，不建立平行workflow engine

`jobs.json`加入`schema_version`與`slices`；Job只描述一次execution，Slice描述delivery lifecycle。這保留現有single-writer與atomic replace特性，也讓`exited`、`verified`、`completed`不再混為一談。

替代方案是建立TaskRun aggregate與event journal；目前沒有正式runtime scale或resume需求證據，先不採用。

### 2. Evidence immutable，current ref可替換

verification、review verdict與CompletionRecord各有schema version與hash-bound identity。每個reviewer Job建立一筆terminal後不可改的GateEvaluation；Candidate或input hash改變時建立fresh evaluation，舊資料只留audit。

這比重設單一gate state更容易驗restart與stale-result fencing，且不需要完整attempt平台。

### 3. Typed argv deterministic verification先於semantic review

runner只有implicit typed-argv subprocess，接受worktree內cwd。contract明列`persona-scope`與command checks；auto-dispatch必須有policy command。env只保留`PATH`、`HOME`、`LANG`、`LC_ALL`、`TMPDIR`、`VIRTUAL_ENV`中既有值。required artifacts、Candidate commands與Candidate full suite必須通過；base full suite可red而Candidate必須green，任一runner error/timeout則`needs_human`。Candidate等於dispatch base一律拒絕，v1不建立no-op proof。

v1只允許trusted、shareable contract。清env不是sandbox，因此不接受untrusted verification，也不宣稱network isolation。

### 4. ForeignReview以manager launch metadata證明身分與subject

靜態ModelIdentityRegistry將`(executor, model_id)`映射到`independence_domain`；manager固定builder/reviewer launch metadata與detached Candidate checkout。reviewer自述不能覆蓋launch authority；不同CLI但同domain不算foreign。

informational/trivial以明確`review_policy=not-required` proof跳過review；normative/code找不到foreign reviewer時Gate=`absent`並停在`needs_human`。

CompletionRecord採discriminated shape：required review保存reviewer Job/Gate refs；not-required時兩欄必為null並保存docs class+contract hash proof。GateEvaluation row永不改；stale只清除Slice current ref並記invalidation reason。

### 5. Artifact dependency以remote target ancestry閉合

Slice只在Candidate已verified且為remote-tracking target ancestor時完成。同一dependency chain使用同target branch，且只支援保留Candidate commit identity的merge。readiness後、worktree建立前，再以actual downstream base SHA重驗每個upstream Candidate，關閉TOCTOU。

逐task merge的throughput代價先接受；只有真實瓶頸出現才考慮integration branch/queue。

### 6. CompletionRecord先寫，Slice completed後寫

先atomic寫CompletionRecord，再atomic更新Slice。`default_is_satisfied`同時要求兩者一致，所以兩步間crash保持blocked；restart只補完完全匹配的orphan record，不匹配者隔離。

restart補第二步前仍須重新fetch並確認current target ancestry；remote已移除Candidate時不得把Slice標completed。

跨兩個檔案做transaction/journal可提供更強原子性，但對目前單機JSON規模是不必要平台化。

### 7. Broker cleanup只由operator觸發

manager periodic path完全不接reaper。manual command預設dry-run；apply需resolved cwd root，signal前重讀start-time/cmdline/parent/cwd且只送SIGTERM。這是best-effort降低誤殺，不宣稱等同pidfd ownership。

### 8. Human action沿用control request queue

CLI的`dispatch/fanout/tick/complete/slice-action`只送control request；daemon/manager是`jobs.json`唯一writer。無spec metadata的舊低階direct dispatch移除；daemon未運行時mutation明確失敗。`jobs/stat/ready/status`只讀atomic snapshot。這沿用既有request/done contract，避免CLI與daemon競寫state。dependency resolution同樣回傳含target ref SHA與upstream Candidates的`ResolvedDependencySet`，讓readiness proof一路傳到worktree creation。

## Risks / Trade-offs

- [逐task preserving merge降低DAG throughput] → 先以canary觀測；有重複瓶頸證據才啟動integration workstream。
- [sanitized env仍可執行具網路能力的trusted command] → v1限shareable/trusted contract並明文揭露邊界。
- [靜態model identity不是provider cryptographic attestation] → manager launch metadata為本機authority；unknown/mismatch一律absent。
- [JSON state與evidence數量成長] → 先沿用atomic files；只有scale/audit證據顯示不足才導入journal。
- [cwd/process reread仍有check-to-signal race] → periodic path不signal、apply需operator明確scope、任何未知值skip、只SIGTERM。

## Migration Plan

1. 先移除periodic automatic reaper，這項可獨立安全落地。
2. 引入versioned state與`done → exited`；因尚無正式task runtime，遇legacy/unknown state直接拒載並提示operator archive/remove。
3. 依序接上verification、ForeignReview、CompletionRecord與ancestry readiness；每階段以RED regression證明舊false-green被關閉。
4. 以temporary repo/bare remote/fake agents跑disposable canary後再啟用真實spec。
5. rollback只回退code並使用deployment前保存的state archive；不得用新版程式自動重寫未知舊state。

## Open Questions

無blocking open question。Deferred workstreams及其證據觸發條件由source spec第10節管理，不在本change預先決定。
