---
status: accepted
work_item: launcher-session-and-timeout
---

# Headless launcher session 設計

## Intake metadata

本 PR 於 `feature/refine-runtime-intake-20260907`（base `2c1bba01`）整合 repository intake，
不是產品實作、正式 freeze／claim／dispatch，現行仍 6/Yellow。以下 D1–D7 保留原契約，
984fce5b 行號是原 author 的 source snapshot。Root 已轉交獨立 reviewer R1 PASS 與 fresh
reader 五題全正確結果，歸因及證據界見 author report；root 本 PR 後續 full preflight 另留證。
#823 唯一 owner 與三件 links 已在本 PR 整合，正式 authority/source-condition 於 freeze 重驗。

## Decisions

### D1 — 最小 production 接線

依 [Spec](launcher-session-and-timeout-spec.md)，只修改 launcher 一個 production module。
基底 `984fce5b86604aa71c6e8ecf82cc253a1441a106` 的資料流為：

```text
launch (1764) → runner/role preflight → executor argv/wrapper
 → 共用 popen_kwargs (2086) → runner env/stdin/cwd overrides
 → stdout log handle → Popen (2257) → stdin-only TypeError retry (2262)
 → 既有 prompt/start confirmation/LaunchHandle
```

`build_headless_popen_kwargs` 是待新增 helper，不是目前存在的 API。參數全部 caller 明示傳入，
回一份新 dict，恰含既有 cwd/env/stderr 與 `start_new_session=True`，僅 executor=claude
再放 `stdin=PIPE`；不讀環境、不 probe、不 spawn、不修改 caller env。`env` 仍由既有 launch
建立並持有，本件不新增 immutable snapshot 或 serialization 契約。launch 以該回傳 dict 繼續
原本的 runner 覆寫流程，不將 helper 放到覆寫之後重新蓋掉 runner 的 I/O／cwd。

正常及 retry 共用 kwargs；現有 `if "stdin" not in str(exc): raise` 保持窄範圍，retry
僅 pop stdin。第二次呼叫不放入新的 retry loop。未知參數 `start_new_session` 的 TypeError
不可被當 stdin 兼容而吞掉。這項變更不產生 Manager 的 kill／reap 路徑；signal/reap 僅屬測試。

### D2 — Recording tests 與 fake 兼容

單元／component 測試沿既有 pytest、tmp fixture、monkeypatch/mock，不新增框架。
以 `_ARGV_BUILDERS` 目前的 copilot/claude/codex/agy/cg 作參數來源，建立合法 launcher；
cg 使用 read_only/review_only，不能為矩陣移除其拒絕 builder 的守衛。5×3 是 coverage
inventory：14 個現有合法配置的真 launch 接線捕捉最外層 Popen；cg/template 則按
`job_runner.py:696–710,1959` 保留 `job-runner-hardening-profile-unknown` 與 Popen=0。
該拒絕不可被假 preflight stub 繞過，亦不新增 permgen/job_runner 的 cg 登記。
不依賴 paid CLI／真 systemd；可在既有 test 檔分列案例，無需共享新的 production wrapper。

保留 direct codex builder 的 `bash -lc` 斷言；read-only/reviewer 或 degraded 的 `bash -c`
是既有行為，不一律改 login shell。claude direct 使用 PIPE；systemd-run client 使用原 client
env、DEVNULL 和原 cwd；template client 使用原 client env、DEVNULL 和 `cwd=None`。
兩種 systemd 模式的 argv 可能是 Manager exit-recorder shell 包住 systemd client，
測試必須沿既有 unwrap/assertions 核對，不能假定 Popen argv[0] 直接是 systemctl。

AGY construction 的 help/capability probe 須用現有 fixture seam stub；#851 尚未完成也不能
令此 recording 測試觸發真 CLI。測試 fake 不證明 #851 containment，#823 不改該路徑。
runner 的 account/unit/ACL/spec/sentinel preflight 使用現有 harness 的隔離 seam；
通過表示 launch kwargs 接線，沒有真 systemd cgroup 證據。

當下固定 keyword-only fake 共 22 處：`tests/test_coordinator_launcher.py` 19 處、
`tests/test_headless_claude_hook_506.py` 3 處。新增顯式 `start_new_session` 參數並捕捉／斷言，
或在必要處接 `**kwargs`；保持每個案例原有 argv/env/permissions/log oracle。
不要靠放寬 production TypeError 補償 fake，也不要把原有未接受 stdin 的 fake 一律變寬，
造成原 stdin retry coverage 消失。另設 dedicated retry fake，第一次記 kwargs 並拋
`TypeError("stdin")`，第二次只差 stdin；非 stdin 與第二次再錯的負例都驗 call count。

### D3 — 真 session／group fixture 與安全負控制

新落點 `tests/test_coordinator_launcher_session.py`，僅 POSIX，本 repo Linux CI 必須實跑。
在不支援 setsid/getpgid/getsid 的平台可明確 skip，但不能把 skip 算 C04/C07 通過。
預先以純 helper + fake 接線測試定位 kwargs，真 fixture 自 production helper 取 kwargs，
指定 cwd=tmp_path、最小 env、executor=codex，再加 stdout=DEVNULL。
啟動 `bash -c 'exec sleep 30'`：與 issue 的 sleep 範例相同隔離面，明示 exec 讓單一
process handle 可在 assertion 失敗時完整 reap，不依 shell 是否最佳化、也不留下 sleep 孫程序。
此測試不宣稱已驗任意 provider 的孫程序／逃離 group 行為。

順序與 oracle：

1. 記 parent PGID/SID；Popen 成功後立刻保存 child handle，不用任意 sleep 同步。
2. `getpgid(child.pid)==child.pid` 且 `getsid(child.pid)==child.pid`，兩者各不等 parent。
   POSIX Popen return 已跨過 setsid/exec 的啟動握手；若 child 提早退出，測試失敗並 finally 收尾。
3. 只有前項全部成功，才 `killpg(child.pid, SIGTERM)`。`wait(timeout=5)` 完成，驗
   returncode 為 SIGTERM 終止，parent 繼續執行後續斷言。測試無需求終止自身 group。
4. finally 若仍 alive，以該自建 `Popen` handle 的 kill + bounded wait 收尾；PIPE 若有就關閉。
   這個 exec fixture 沒有 descendants，PID 在未 wait/reap 前仍由本 process 持有，不能依
   process name、任意 PID 掃描或 operator registry 清理。成功與 assertion/timeout 路徑都走 finally。

負控制必在 disposable mutation 環境進行：移除 helper session flag，先前 PGID/SID 斷言
必紅，且 `killpg` call count 必為 0；finally 只 kill 本 fixture child。另在 fake seam 測
「launch 不使用 helper」「只改 direct」「retry 丟 flag」「吞非 stdin TypeError」均變紅。
不得讓 mutant 把 pytest 或 agent 的 parent group 當 signal target；不對 live process 做實驗。

### D4 — Risk／oracle matrix

| ID／AC | Surface／風險 | Harness／observable | Oracle／負控制 |
|---|---|---|---|
| T01 C01 | helper kwargs／遺漏旗標 | 純 helper 五 executor | 全數 literal True；caller env 不被改 |
| T02 C01/C05 | launch 與 runner 覆寫／只修其中一支 | 既有 fake Popen，15 格 coverage inventory | 14 合法配置每次 Popen True；cg/template 原拒絕且 Popen=0；漏 helper 接線／flag 必紅 |
| T03 C02 | stdin retry／靜默退回同 group | dedicated recording fake＋TypeError 注入 | 最多兩次、只 pop stdin；其他 TypeError 一次即傳出 |
| T04 C03 | 22 固定 fake／既有安全斷言被抹掉 | 原 launcher/hook 與 runner harness | 簽名相容、原 argv/env/I/O/preflight oracle 保留 |
| T05 C04 | setsid/group signal／parent 陪葬 | 真自建 exec fixture、PGID/SID/returncode | group 隔離、SIGTERM、5 秒內 reap；負控制 signal 前拒 |
| T06 C03/C06 | session diff 覆蓋 #820 或偷帶 timeout | AGY/planning 與 full suite、diff scope | JSON/argv 原行為保持，#824/#851 無新實作 |
| T07 C06/C07 | 文件／package 與 source 不一致 | CLI help、完整 policy/CI、checkout 外 wheel helper | exact candidate/import path、隔離 fixture；無 live/資格冒稱 |

### D5 — 產品驗證入口與 artifacts

先從原碼得到 RED，再實作最小 diff，保留完整 command/output/candidate SHA。focused 命令：

```bash
python -m pytest tests/test_coordinator_launcher.py tests/test_coordinator_launcher_session.py tests/test_headless_claude_hook_506.py tests/test_coordinator_agy_launcher.py tests/test_trust_root_job_runner_p2a.py tests/test_trust_root_job_template_ab.py tests/test_inner_sandbox_714.py tests/test_reviewer_planner_downgrade_615.py tests/test_manager_authored_job_accounting_604.py tests/test_planning_job_argv_687.py tests/test_planning_runtime.py -q
python -m pytest tests/ -q
```

新 session test 目前不存在，上列是正式產品待辦，不是本次通過紀錄。`tests/conftest.py`
已將 runtime/repo/config roots 轉到 tmp fixtures 並啟 network guard，保留該隔離；不使用
ALLOW_NETWORK 逃生口，不讀寫 live root。`.github/workflows/tests.yml` 已配置執行四版 Python
3.10–3.13 full pytest、build/twine 與 wheel smoke；沿既有 CI，不為本小改動修改 workflow。

CLI compatibility 以 `cortex --help`、`cortex status --help`、`cortex stat --help` 與
`cortex dispatch --help` 核對；dispatch 是停用的舊低階入口，只驗 help，不執行派工。
README/docs 只在語意需要時更新 session/cgroup 邊界。正式 artifacts 允許必要 tests、
docs/verification/review reports、`changelog.d/launcher-session-and-timeout.md`、
`CHANGELOG.md [Unreleased]`，以及既有 feature-oneshot 的 planning/OpenSpec/archive 產物；
不得以「唯一 production module」排除 policy 或 deck 必要文件。VERSION 不變。

完整 feature-oneshot gates／Yellow adversarial gate、R-09/R-16/R-19/R-22 與其他適用 policy
都保留。`policy_check` 使用真 PR title/body/labels/base/head；正式 delivery PR 只 `Closes #823`，
不關閉 #824/#851 或家族票。source suite、CI、merge、checkout 外 wheel 的 helper／真 fixture
分別留 evidence；installed 測試依舊用自建 process，不操作 Manager/service。

### D6 — Sizing 與 authority

依 #208 rubric，domain=0：production 只有 launcher 一模組、一條 kwargs→Popen 資料流。
state=0：只改 local dict 與既有 Popen 的標準參數，不新增 schema、durable transaction、
restart/reconciliation、跨 writer atomicity 或 concurrency protocol；fake 簽名修補不改 public
backward-compatible state 契約。真多程序測試是 OS 既有 setsid 行為的驗收，不是產品新增
協調狀態機。若後續要求 pgid 持久化／Manager cancel／cgroup ownership，必須重裁 scope/state。

採 root 指定現行 packaged feature-oneshot，不換 combo 壓分：11 cards、11 bindings、
4 core gates；R-09/R-16/R-19 全集使 acceptance=2，orchestration=2。
現行 79 runtime 完整 accepted 規劃的 stability=2，故 `[0,0,2,2,2]=6/Yellow`。
#831 完整 case 的 stability=0 只是 `[0,0,2,0,2]=4/Yellow` 投影，不能當已載入算法。
Spec I1–I6 對應 invariant_count=6，artifact_classes 為 source/tests/documentation。

Yellow 必須在正式 build 前完成獨立強審與 freeze/source-owner/fresh-base 檢查。
缺真 measured envelope 的 helper bypass 不是模型合格；author 不替 root 啟動 workflow。
Reviewer 從首輪固定：未處置 BLOCKER/MAJOR→FAIL；明文承認、影響有界且文件列管的 residual
不單獨 FAIL；若不同意接受，須具體反駁其影響分析。Reviewer 必須讀 exact 三件組 hash，
反證每個 AC、branch/fragment/source owner、OS signal 安全界與完整 gate 可交付性。

### D7 — 有界 residual

setsid 不移 cgroup，故不承諾 service restart survival；最外層 systemd wrapper 測試也不認證
內層 job。新增 Manager cancel 或處理手動 kill 的終局 taxonomy 仍由後續 owner 負責。
本票 signal fixture 只證自建 process group，沒有對任意 executor 子孫的保證。以上界線不削弱
#823 的「本 launcher spawn 不與 parent 同 session/group」目標；完整 refine plan R13 其餘
生命週期要求仍保留。#824/#851 的尚未交付不阻止本票離線 fixture authoring，亦不由此冒稱完成。
