---
status: accepted
work_item: agy-probe-construction-containment
authority_state: repository-intake-unfrozen
owner_issue: "hamanpaul/paulsha-cortex#851"
registration_state: repository-intake
dispatch_readiness: awaiting-freeze
---

# AGY probe 建構失敗邊界設計

## Decisions

### D1：最小例外邊界

原 author 規劃 base=`25c9a4e4040476523d0bb5f2132236f87b8d65f8`。當時核對
`model_identities.py:1071-1078` 建 argv 在 smoke `try` 外；
`planning_runtime.py:1664` 對 canonical AGY roster probe 不另 catch，
即使 primary 是非 AGY，建構 `ValueError` 也會中止整個 runtime 建立。

唯一 production diff 是將原 argv assignment 整塊縮排移入既有第二個 `try`。
`except Exception` 仍使用原 `_failed_agy("smoke-failed", ...)`，其內容不變。
原 prompt 構造、第一段 models discovery 和 smoke 後的 payload 驗證位置不變。
不把整個 runtime loop 包成寬廣 catch，也不在 launcher 吃掉 invalid 設定。

```text
models discovery 成功
  → 同一 smoke try：[build argv → process runner → process fields]
      Exception → 既有 failed AGY result
      BaseException → 繼續向 caller 傳播
  → 原 payload／identity 驗證 → 原 ready result
```

下游 timeout resolver 可因此維持嚴格驗證；probe 設定非法只變成明確不 ready，
不再意外阻止其他 roster identity 的 runtime construction。

### D2：精確 seam 與 RED test

`model_identities.py:16` 已用 `from .launcher import build_agy_argv` 存本地 alias，
所以 probe fault injection 必須 patch `model_identities.build_agy_argv`。
只 patch `launcher.build_agy_argv` 不會替換已匯入 alias，會形成假測試。

在 `tests/test_model_identities.py` 增 focused test：fake runner 的 discovery
回 canonical `AGY_MODEL_ID`，fake builder 記下 kwargs 然後拋帶 harmless marker
的 plain `ValueError`。舊 source 必因例外外洩 RED；修後 assert `ready=False`、
reason=`smoke-failed`、diagnostic=`ValueError`、原 executor/model/domain、
runner 只收到一次 `["agy", "models"]`、builder 僅一次且 safe kwargs 原樣。
另以 `KeyboardInterrupt`／`SystemExit` 參數化驗證仍拋出，不能擴成 BaseException。

### D3：真正 runtime construction integration

在 `tests/test_planning_runtime.py`，沿用現有 safe-runner 測試模式，
monkeypatch `planning_runtime.load_model_identities` 返回兩列 isolated registry：
非 AGY primary（例如 codex/primary）與 canonical AGY planning identity。
兩種 roster 順序都走真 `build_production_planning_runtime`，不 mock 真 probe。
fake 非 AGY runner 回原 compact JSON echo；fake AGY discovery 回 canonical
model。probe builder alias 拋 `ValueError`；禁止任何 real subprocess/model。

顯式新 `tmp_path / "probe-cache.json"` 保證 miss，不讓既有 cached ready 掩蓋
建構路徑。worktree 使用 test 暫存樹，registry 只在 monkeypatch 內替換；需要
的 fingerprint/stat 依 test tmp fixtures 注入，不讀 credentials 內容。
assert runtime.identity_registry 是該 registry、primary ready、AGY failed、
builder 恰一次、沒有 AGY smoke invocation；再以既有 secondary selection
介面確認 failed AGY 不符合 ready 候選。若只有該 AGY 異質候選，允許既有
no-heterogeneous-planner 結果，不能為滿足測試而提升 ready。

不改 cache get/put/flush 語意；只證本次 cache miss 上的 construction 例外。
cache 命中原有 ready 值的 freshness、env fingerprint 等議題不在本 child
新增行為範圍，不能拿本測試宣稱所有 cache 情境已被覆蓋。

### D4：直接 launcher 仍 fail closed

在 `tests/test_coordinator_agy_launcher.py` 使用既有 tmp-path launcher fixture，
`PSC_JOB_RUNNER=direct`，patch `launcher._ARGV_BUILDERS["agy"]` 為拋
`ValueError` 的 builder，fake Popen 只記次數、不建程序。
`SubprocessLauncher("agy").launch(...)` 必須傳播 ValueError、零 Popen。
必要 workspace helper 只回 test 暫存路徑，不觸及 operator／runtime。

這驗證 probe-local catch 沒有變成全域吞錯，不聲稱未落地的 resolver 已能解析 env。
timeout-only 後續須加入真非法 env 的 direct-launch 與 capability-probe regression，
並保持 `json_envelope=False` 仍 resolve／帶 timeout。兩 child 不形成反向依賴：
containment 的 code acceptance 由 fault-injection 證明，真 timeout env proof
是下游 timeout candidate 的 acceptance，不拿它阻擋此 child 的先行實作。

### D5：成功／既有失敗與 policy gates

保留現有 model token、code fence、exact payload、safe argv、runner timeout
與 error family 測試；新增測試不修改 oracle 以接納 ready 升格。
focused suite 涵蓋三個修改 test 檔及 `tests/test_planning_job_argv_687.py`。
focused RED 卡不先跑全庫 baseline、不讀無關歷史；全庫 gate 仍在後續 verify
完整跑 `python3 -m pytest tests/ -q`，不得將 focused green 當全庫結果。

future candidate 依 `preflight-ci` 執行 manifest 的 `openspec validate --specs`
與 tests，並以 candidate 環境、checkout 外 cwd 執行
`python3 -m paulsha_cortex.cli --help` 及
`python3 -m paulsha_cortex.cli run work --help`。沒有新 CLI flag 也保留 R-16 smoke。
本規劃不要求 live AGY session；沒有安裝／真模型 acceptance 的假宣稱。

同步 operation docs、Unreleased 與 canonical fragment；真正 candidate commit
完成後，带精確 PR title/body/labels/base main/head 跑 pinned policy-preflight。
policy 版本=v1.0.17，CI workflow 的 engine pin為
`9e7fabbf0b5eea9ad933fa6798764b723934a0b7`。root 已核對當下 local preflight
skill engine `b281c5da` 的 `policy_check` bytes 與 CI pin 無差；兩者不是同一
SHA／執行身份，bytes 比對也不代替 candidate 的真 policy gate。
必查 R-09/R-16/R-19/R-22、R-14 symlink、R-20/R-23 pin；VERSION 非 release 不改。
PR 文案 zh-tw，closing keyword 使用唯一 owner `Closes #851`；不自創豁免 label。

### D6：owner／branch／dependency 與交付狀態

work_id=`agy-probe-construction-containment`；原 author 規劃 branch 是
`feature/agy-probe-construction-containment-planning`，不是 Manager builder branch。
`manager.workflow_build_branch:3510-3528` 使用本 repo issue refs 最小正整數：
root 已建立唯一 owner
[#851](https://github.com/hamanpaul/paulsha-cortex/issues/851)，故正式 builder
branch 固定為 `feature/851-agy-probe-construction-containment`。
沒有 issue refs 時函式會退到 `feature/agy-probe-construction-containment`，
但這個 fallback 不代表無 owner 也獲准派工。fragment 去掉 issue 前綴後固定為
`changelog.d/agy-probe-construction-containment.md`，不使用另一 child 的名稱。

root 回報獨立 `agy_containment_review` R1 內容審查及 fresh-reader 五題 PASS；
registration 已由 root 納入本 PR 的 repository intake。
狀態為 `awaiting-freeze`，不是已核准派工：仍須整合審查、
完整 preflight、真 builder envelope 與 Cortex Yellow plan-review gate，
通過後凍結精確 source revisions/base，再由合法 route build。
此 child 不依賴 timeout resolver；若原 AGY deadline 不足，route 決定仍由 root
依已授權來源裁決，不能自行 fallback 或重跑模型。

timeout-only 四檔已 blocked-dependency。其解除需要 root 確認 containment
candidate 通過、timeout 的預定 base 及實際 runtime 已含相同保護；安裝和舊 run
正式 retry 是另外的 operation authority，不在本四檔 authoring 中執行。

## Current integration gate

目前整合 branch 為 `feature/refine-agy-dependencies-20260907`，base 為 #852 merge
`984fce5b`；本 PR 登錄 #851 三件套，未 freeze、未 start。#851 的現行文件仍
completeness=true、6 Yellow，surface-only gate 的 envelope bypass 不代表
真 builder capability，正式 gate 待 root 的合法 start 流程。
root 已把 #824 從母 #823 mapping 移除，但故意不登錄 timeout child；
其目前帶 marker 的 plan 不完整、4 Yellow，不得誤用作者原 6 Yellow 當下游 ready。
marker 不能阻止所有 start 路徑：incomplete start 可能進 brainstorm，故保持
不登錄／不 auto-label／不 start #824，待本 child 產品、base及 loaded runtime
保護均確認後再由 root 解除和正式登錄。
