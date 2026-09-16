---
status: accepted
work_item: agy-print-timeout-only
authority_state: registered
dispatch_readiness: ready
depends_on:
  - agy-probe-construction-containment
dependency_issues:
  - "hamanpaul/paulsha-cortex#851"
domain_breadth: 0
state_consistency: 0
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# AGY print timeout 單獨交付工作清單

## Current integration gate

前置 `agy-probe-construction-containment`（#851）已由 PR #873 於 2026-09-15 交付並
merge 進 main（commit c1ff347b），daemon 現行 runtime pin（75565400）已包含該修正；
dependency 解除。#824 於 2026-09-15 正式登錄 `.cortex/work-items.yaml`
（PR #905）並掛 `cortex:auto-on-going` 交由管線派工。舊 blocked-dependency marker
於同日由 operator 解除；0／0／7 與 R1–R7、Tasks 的 resolver／tests 契約均不變。

## Open Questions

- 無。

## Tasks

- [x] **T0 dependency/tests**（2026-09-15 由 operator 核對解除：#851 由 PR #873 交付，daemon pin 75565400 已含保護）：原要求——本 child 不可先 freeze／dispatch；先完成 `agy-probe-construction-containment` 的 argv-construction try 邊界修正與 fake regression，root 核對預定 base／實際 runtime 已含保護後才解除 block；保留 R4 probe 的 timeout resolve 與全部非法 env／Go range 驗證，不在此 child 改 `model_identities.py` 或忽略 probe 值。
- [x] **T1 tests/RED**：以本 child spec R1–R7 與 design D1–D5 為需求，先在 `tests/test_coordinator_agy_launcher.py` 新增預設／override／exact flag 的 focused RED tests；確認舊 launcher 缺 flag 或 resolver 的真實失敗，再提交 RED candidate；此卡不先重跑全庫 baseline，不探索無關歷史，不修改 source 或 pinned plan。
- [x] **T2 source/resolver**：只改 `launcher.py`，新增 `AGY_PRINT_TIMEOUT_ENV`、600 秒 buffer／9223372036 秒上界、canonical duration 驗證與 `resolve_agy_print_timeout`；env strip 後 ASCII digits only／容許前導零，顯式空白／零／非法／超界一律 `ValueError`；未設定時直接重用 `gate_ledger._gate_timeout(env)`，invalid gate 仍 fallback，最後推導結果才檢查 Go 上界。
- [x] **T3 source/argv**：`build_agy_argv(print_timeout: str | None = None)` 對 None 自行 resolve，顯式 keyword 做 fullmatch 與上界驗證；所有 AGY 形狀、包含 `json_envelope=False`，於 JSON flag 後／model 前恰加入一組 timeout；正式 `SubprocessLauncher.launch` 對 AGY 顯式傳入 resolver 值，非法設定在 Popen 前失敗。
- [x] **T4 tests/matrix**：涵蓋 spec R2/R3 的完整值矩陣、最大／最大+1、derived gate 9223371436／9223371437、5000-digit 值、Unicode／符號／浮點／尾 newline／非字串；以 fake gate helper 驗證重用與 override priority；保留 helper 本體不變，若需修改則停止回 root。
- [x] **T5 tests/shapes**：更新 AGY 七個 direct shape cases、兩個 exact argv lists、probe opt-out case；保留前綴 slices／permission／scope／JSON assertions；用 fake builder 與 fake Popen 分別證 kwargs 和最終 script，測 unset=2400s、override=900s、invalid 不 spawn；非 AGY 四個 builder 維持無 timeout flag，不改 #823 的 Popen/session 行為。
- [x] **T6 documentation/CLI/docs**：candidate evidence 記錄 AGY 版本及 D4 無憑證／無 prompt／未知 sentinel 的五列 parser 負控制；不是用 help exit0當 parser proof；更新 `docs/unified-work-lifecycle.md` 的 AGY timeout 設定／範圍／override與gate fallback語意，再從 checkout 外以 candidate Python 環境實跑 `python3 -m paulsha_cortex.cli --help`、`python3 -m paulsha_cortex.cli run work --help` 並記退出碼；無新增 Cortex CLI flag也需保留 help smoke。
- [x] **T7 changelog/docs**：實作 candidate 新增並 commit `changelog.d/agy-print-timeout-only.md`（root 已定案的 child fragment，內容只寫 timeout），同步 `CHANGELOG.md [Unreleased]`；canonical build branch 依 Manager 固定為 `feature/824-agy-print-timeout-only`，不可自行換名；root 已於2026-09-07T13:40:04Z正式同步 #824 AC5 的名稱與 branch（並更新AC6／range／#851 dependency），registration仍待另期進件、block不解除；母 `launcher-session-and-timeout` fragment 責任留 #823，不產生重複 fragment；檢查 README/docs 引用、symlinks、VERSION與policy pin不變。
- [x] **T8 tests/policy**：containment base上用真resolver／非法PSC_AGY_PRINT_TIMEOUT證direct launch仍ValueError零Popen、真probe回failed且無smoke，再以fresh tmpcache＋真runtime constructor證ready非AGYprimary仍可用，不只沿用dependency的fault injection；focused 執行 `tests/test_coordinator_agy_launcher.py`、`tests/test_coordinator_launcher.py`、`tests/test_planning_runtime.py`、`tests/test_planning_job_argv_687.py`、`tests/test_model_identities.py`、`tests/test_trust_root_agy_builder_grant_805.py`；再執行全 `python3 -m pytest tests/ -q`、manifest `openspec validate --specs`、`git diff --check`，保存實際結果；不將 focused RED/green 冒充 Manager 全庫 pytest gate 結果。
- [x] **T9 policy/CLI/交付**：按 `preflight-ci` 以精確 PR title/body/labels/base/head 跑 pinned `policy-preflight`，另核對 R-09/R-16/R-19/R-22、R-14 symlink、R-20/R-23 pin；只在 candidate commit 完成後以 PR context 驗证 R-09。root裁決唯一owner及authority重綁、Yellow強plan review通過後才freeze；final review須exact candidate，遠端CI／threads／mergeability各自確認，保留 #823 母範圍。

## Boundary

- dependency `agy-probe-construction-containment` 的唯一 owner [#851](https://github.com/hamanpaul/paulsha-cortex/issues/851) 已由 PR #873 交付並進入 daemon runtime pin；本 child 屬 #824，已於 2026-09-15 正式登錄並解除 blocked-dependency。
- Child 需求來源：[#824](https://github.com/hamanpaul/paulsha-cortex/issues/824)；母 work item `launcher-session-and-timeout` 的 #823 不在此交付。
- Spec：`docs/superpowers/specs/agy-print-timeout-only-spec.md`；design：`docs/superpowers/specs/agy-print-timeout-only-design.md`。
- 唯一 production 寫入：`paulsha_cortex/coordinator/launcher.py`；測試、操作文件及 changelog 允許同步，registry/schema/loader/installer/job_runner/gate_ledger/CLI production皆不改。
- 原author時期僅四份planning文件，未操作產品、issue或registration；目前root另負責本PR登錄，本八檔整合不代行registration／commit／push／服務／runtime／模型session操作。
- R1 已證 argv construction 例外可穿透非 AGY primary runtime 建立；該 blocker 已由 #851（PR #873）修正，dependency 不再阻擋派工。
- 切面依#208真rubric仍domain=0／state=0；原author完整三件套與記憶體移除marker控制組為6 Yellow，blocked-dependency marker 於 2026-09-15 解除後恢復完整三件套；不使用#831候選算法，不改band門檻。
- `invariant_count: 7` 對應 spec R1–R7；`artifact_classes` 列 source、tests、documentation，未隱藏 CLI proof／policy／changelog acceptance surfaces。
