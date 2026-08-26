---
status: accepted
work_item: phase2-closeout-reconcile
---

## Context

三支候選都從舊 baseline 分岔，之後 `main` 已建立 transactional installer、root-owned
hash-bound toolchain、完整 install inventory、production full-dispatch canary 與 exact-SHA
RC/release。逐項行為比對證明現行架構已覆蓋舊分支的有效目的，舊 API 本身不是交付
契約；直接 merge 反而會引入非原子 reinstall、第二套 inventory authority 與較弱 probe。

## Decisions

### 1. 使用 governed reconciliation，不做 branch replay

以 `7ced8df0a24c55c49ee894b3118ea18d2a97b552` 為 target，三個 PR final head
只作 provenance source。依「能力是否存在且更強」判斷 supersession，不以舊 module／test
檔名是否存在判斷。#789 的手動 publisher、#790 的第二套 inventory、#791 的 standalone
probe 均拒絕回灌；只修正比對中證實仍存在的現行 attestation normalization 缺陷。

### 2. Attestation normalization 依 artifact category 判定

- `shim` 與 `toolchain_wrappers`：`#!` 決定 interpreter，必須保留為 functional line；
  其他獨立 shell/Python 註解可忽略。
- `polkit`：獨立 `//` 與 `/* ... */` 註解可忽略；規則內容仍逐行比較。
- `polkit` block comment 到 EOF 未閉合時是 malformed functional content，必須 fail closed。
- `polkit` 的 `#`／`;` 都是 functional content，不沿用 unit／gitconfig 的 comment semantics。
- `units`／`gitconfigs`／`environment` 只忽略各自支援的行首 `#`／`;`；shell shim／wrapper
  只忽略獨立 `#`（保留 `#!`），其 `;` 是 shell statement；JSON enforcement 不接受註解。

focused regressions 分別鎖住兩種 shebang fail-closed、polkit comment-only warning、
inline rule preservation 與 unterminated EOF fail-closed。

### 3. Release qualification 與 deployment canary 邊界維持分離，補強 canary 證據

Deterministic RC 只證明 artifact install/systemd/attestation/attack matrix，且不得取用
live credentials。需要 provider 的 agent-loop live execution 只屬 deployment canary。
PR #796 的 `_full_dispatch()` 已建立較完整的 intake-to-terminal seam，但原本未 pin builder，
也未驗證 executor/model/runtime 或任何真實 command event，因此不能直接視為 #716 驗收。

本 change 改用 `cortex run work intake` 的 run-scoped override，固定 builder 為
`codex/gpt-5.3-codex-spark`。closeout 同時驗證 workflow resolution、所有 build job 的
typed runtime identity，並只對 `workflow_card=worktree-isolation` 的唯一 job 綁定
Manager-owned `job-specs/builder/<instance>.json`。spec 必須指向同一 worktree、log、template
instance，完整 wrapper 必須 byte-match canonical Codex argv、rc capture、bundle/last-message
publication 與 terminal exit，任何縮寫 alias、第二個 Codex、額外 suffix 或 unsafe flag 都拒絕。
job JSONL 必須有唯一 persisted thread。Codex 0.149 會把 shell execution 以 `shlex_join`
投影成三段式 `/bin/bash|/usr/bin/bash -c|-lc '<inner>'`；validator 只解一層 exact envelope，
inner argv 必須逐字是 absolute `/usr/bin/git rev-parse HEAD`（或同一 binary 加 exact
`-C <bound-worktree>`），且只輸出該 `worktree-isolation` job 的 `subject_head`。repo-local
`./git`、pipe、boolean fallback、redirection、suffix、`printf` 或 wrong HEAD 都不能形成假綠。
這個 probe SHA、workflow 最終 candidate 與被測 Cortex release SHA 是三個不同 identity；
canary 會逐一記錄並各自綁定，不要求它們相等，因為後續 commit-required cards 本來就會
推進 probe repository 的 candidate。

Manager job spec 還必須把 `CODEX_HOME` 綁到 registry 推導、無 symlink 且 Manager-owned 的
exact per-job slot，`PATH` 固定為 installed toolchain/system path，拒絕所有會改變 Git
repository/object/worktree 選擇的 env，且 `safe.directory` 必須唯一且逐字等於 job worktree。
requested identity 之外，canary 會在同一個 exact job `CODEX_HOME` 以
Codex 0.149 app-server
`thread/resume(excludeTurns=true)` 讀 provider-persisted metadata，逐字驗 model=Spark、
reasoning=xhigh、provider=openai 與 cwd=該 worktree。provider smoke 也改跑 Spark/xhigh 並由
persisted thread 取 native metadata，不再把 registry/spec request 當 runtime proof。
`worktree-isolation` 的首張 builder card 另用專屬 autonomous preamble，只要求模型自行選擇
有用的 read-only repo inspection，不把驗收用 Git command 寫進 prompt；qualification driver
從 workflow/card contract 重建完整 prompt 與 terminal schema 並逐 byte 比對，不能由 fixture
自報一份「期待 prompt」形成循環假綠。

JSONL 是 job-writable observational telemetry，不提升為獨立 authority。對外 evidence 只記
固定 schema 的 terminal state/work/run IDs、job IDs、count、booleans、model identity 與
command/output/log/thread/artifact-set hashes；不得把 `work show` 的 raw provider observation、
title、next-actions、command 或 output envelope 直接上傳；
marker list 與 job ID 也採 exact bounded contract，不能夾帶 raw text。Manager-owned mutable
檔案只記「實際通過驗證的那份 bytes」digest，不在驗證完成後重新讀檔、誤把較新的未驗
內容包成已驗 artifact；commit bundle 在 `verify` 與 `list-heads` 前後 digest 必須一致。
獨立 validator 還必須由 workflow 外部收到 repo/work-id/issue，與 dispatch terminal、Manager
GitHub repo、唯一 raw-log artifact row 交叉綁定。command/output 原文刻意不出 evidence，
因此其 digest 是隱私保留 observation，不宣稱可由 validator 反算。
「此刻 provider/live rollout 健康」仍需受保護環境的成功 canary，不能由 code 或 package
release 推論。

### 4. Production upgrade 使用 sealed candidate 與明示 receipt handoff

現行 v0.1.9 host 已有 accounts/assets，新的 canonical plan SHA 必然不同；把它當 fresh
install 會因 provenance fail closed。反過來，直接用 ambient `cortex` plan、再用另一支
`sudo cortex` apply，會讓 planner 與 mutator 可由不同 package tree 產生自洽但錯誤的 plan。

因此 runbook 先把 qualification input 放進 root-owned ingress，驗 external candidate/wheel/
bundle hashes、完整 manifest 與 actual=declared wheelhouse inventory，再由 manifest 產生每顆
wheel 都帶 SHA-256 的 bootstrap requirements；`pip --no-index --no-deps --require-hashes` 不再
自行探索依賴。`--copies` venv 封存 owner/mode 並算 deterministic tree hash。非 root plan、
plan digest/rendering 與 root apply 都走絕對 trusted binaries／closed env，apply 前重驗 tree
digest 並停止 services。

`umask 077` 建出的 ingress/venv 在 owner/hash 驗證後會只開放 read/traverse，讓非 root plan
真的可執行；`venv --copies` 唯一允許清理的是 canonical `lib64 -> lib`，確認 target 後移除，
其餘 symlink 一律拒絕。stop 前先查 unit `LoadState`，只停止存在的 unit；任一 stop 或 apply
失敗、shell exit、INT 或 TERM 都會經同一 trap rollback 已開始的 apply，並 best-effort
恢復原先 active units；fresh host 不會因缺少 unit 半途退出。

upgrade 必須明示 `--prior-receipt`。舊 receipt 必須 applied+qualified、不同 plan、同
scheme/instance/roots/repository remote；account step 必須 exact-equal，asset/repository 則需
舊 step 與當下 installed state 相符且 journal 有 creation/adoption proof。新 journal 寫入
`adopted_from_receipt`，所以 retry、rollback 與下一次升級仍有連續 authority。fresh install
不傳 prior receipt；任何不相容在 backend preflight 前拒絕。

在 receipt 進入 `applying` 或第一個 backend mutation 前，installer 先掃過全部既存
asset/repository/toolchain、candidate venv slot 與 active link；任何較後面的
foreign/drifted step 都會讓整筆 upgrade 零 mutation 失敗。CLI 同時在固定
`/run` authority 持有單一 host-global transaction lock，不接受 roots、plan 或
`--receipt` override 改變 lock identity；apply、credential import、activate、verify、rollback
全部共用。runbook 另在讀 service snapshot 之前取得 host-global maintenance lease，
並一路持有到 rollback/restore 或 verify/active checks 完成，才能封住兩個合作 runbook
之間的 command-gap window。lease 發出綁 reviewed plan 的隨機 token；active lease 期間，
沒有 exact token 或 plan 不符的 mutation 一律拒絕。runbook 每次在 canonical receipt parent
產生新的 random effective receipt path；root helper 在停服務與回傳 ready/token 前驗證該路徑
不存在，並將 effective receipt、present units 與 previously-active units 寫入
`/var/lib/cortex-installer/maintenance-snapshot.json`。因此 abort trap 可 full rollback 自己建立的
receipt，也能涵蓋 apply child 已完成但 signal 先抵達 parent 的窗口，不會誤拆先前 applied
receipt。helper 單獨失效時，durable plan/token marker 會拒絕新 lease 與 tokenless mutation，
原 shell 仍可用 exact token 完成 rollback/restore；若整個 shell hard-crash 而遺失 token，只有
explicit `recover` 可在同 plan 下旋轉 stale authority，停止當下存在的 Cortex units、依 durable
snapshot rollback，並只在 `restore_safe=true` 時恢復原先 active units。正常完成協定或安全
recovery 才會清除 snapshot/marker；任何 drift、rollback 或 service restore 失敗都保留它們，
且 snapshot 位於跨 reboot 的 root-private state，而不是 `/run`。
為了讓 whole-shell crash 後的「同 plan」不是一個無法實作的要求，operator 三方確認 digest 後、
取得 lease 前，runbook 先在 root-only publication lock 下把 exact bytes 完整寫入 staging、fsync，
再原子改名為 `/var/lib/cortex-installer/plans/<sha>.json`。後續 lease/apply 與 fresh-shell recovery
只讀該檔；既有同名檔只有 owner/mode/nlink/bytes 全相符才可重用。recovery 不重跑會拒絕既有
immutable input／venv 的第 1–2 節，而是重新輸入先前人工確認的 SHA，驗 plan digest、sealed CLI
topology 後直接執行。maintenance snapshot 同樣先完整 staging＋fsync，再 no-replace publish，
避免 hard kill 留下可被誤讀的半份 final file。

candidate venv 也把 crash authority 拆成 `planned → building → ready`：在 deterministic staging
mkdir 前先記 path intent，mkdir/fsync 後記 device/inode，完整 tree/fsync 後、final-name rename 前
再記 tree hash。rollback 只刪 exact receipt-bound unpublished staging；rename 已完成則依 ready
authority 重播並保留 content-addressed slot。mount adoption authority 則跨 metadata-only receipt
繼續傳遞最初 device/inode，防止第三次升級把同內容 foreign mount 認成已授權資產。
backend 不支援覆換既存 toolchain leaf，因此「prior receipt 證明舊 bytes、current plan 要求
同一路徑新 bytes」也在 sweep 階段拒絕；toolchain 升級必須使用尚不存在的 versioned path，
不能先修改前段 asset 才在 toolchain step 失敗。
能忽略該 lock 的另一個 root/admin writer 不在 job-account 威脅模型內；逐 step re-inspection
遇到這類晚到 drift 會以 durable receipt fail closed，operator 必須 rollback，不能把它說成
零 mutation。

### 5. 修正必須以新 immutable patch release 交付

`v0.1.9` 保持不可變歷史；本 change 合併後以 `v0.1.10` 重新產生 exact-main RC。
RC artifact 必須保留完整 `qualification-input`；release gate 重跑 bundle inventory/hash
驗證、重生並 byte-compare canonical install config，再產生 deterministic archive。
annotated tag 與 GitHub Release 會同時發布唯一 qualified wheel、該 install-input archive
與永久 qualification manifest，且逐一核對 GitHub REST asset digest；INT/TERM/ERR/一般
失敗都由同一個 transaction cleanup 回收本次 draft release 與 annotated tag。annotated tag
message 另持久保存 run/attempt/release-SHA marker；若 SIGKILL 讓 trap 來不及執行，後續 run
只在 exact tag target、合法 marker，以及同 marker 的 draft release 全吻合時刪除殘留並重試，
foreign/lightweight/wrong-target tag 或 non-draft release 一律 fail closed。
#681/#695 依 shipped replacement evidence 關閉；
#716 保持 open，直到上述 contract 對 release SHA 有成功 live run；它不阻擋 Phase 2
source/package，但 code contract 必須隨 `v0.1.10` 交付。

## Risks / Trade-offs

- normalization 若過度忽略內容會造成 fail-open；只忽略 category 明確定義的獨立註解，
  shebang 與 inline code 一律保留。
- 舊 branch 的具名檔案不存在不代表能力不存在；merge summary 必須列出現行替代 call path
  與拒絕移植的風險。
- agent-loop package code 的存在不等於 live provider 成功；docs、issue comment 與
  qualification profile 必須維持這個誠實邊界。
- job JSONL 可由被觀察 job 寫入，只能稱為綁定 Manager launch authority 的 live
  observation；provider-persisted thread 與 Manager spec 可阻止 requested/runtime identity
  假綠，但若要讓 command/output 本身升為獨立 attestation，仍需另設 Manager-owned event
  channel。
- prior receipt 只能授權上一版逐 step 已證明的 host state；它不允許跨 roots/repository
  搬移，也不會自動探索舊 receipt。runbook apply 失敗會 rollback 新 receipt 並只恢復原先
  active units，避免服務停在半升級狀態。

## Rollback

整併以獨立 feature branch／PR 交付；合併前可直接丟棄該 worktree。合併後若發現回歸，
revert closeout merge commit；新 tag 只能在 exact-main RC 通過後建立，不覆寫 `v0.1.9`。
