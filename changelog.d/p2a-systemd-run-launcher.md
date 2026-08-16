# trust-root Phase 2a：降權啟動器（`systemd-run` transient unit，預設關閉）

`Refs #584 #588`

## 背景

trust-root spec §R10 Phase 2 第 5 條要求「Manager 以降權方式 spawn headless job，明確
關閉 FD 傳遞、不傳遞 gh token」。operator 0816 第二輪裁決把「未決 1（降權機制）」收斂為
**`systemd-run` transient unit**、UID **二分**（`cortex-svc` / `cortex-builder`）、
builder env 不傳 gh token。本次落地的是那條裁決的**程式碼前置**。

issue #588 的兩條缺口同時被這個機制解掉一半：

1. builder 繼承 daemon 全部 environ（含 token 與 daemon 自己的 `CLAUDE_CONFIG_DIR`）；
2. builder 走 `bash -lc`（login shell），`~/.profile` 會在 launcher 設好 env 之後重新
   匯入 operator 環境，把任何 env 約束覆寫掉。

## 變更

### 新增 `paulsha_cortex/coordinator/job_runner.py`

- **`PSC_JOB_RUNNER`**：`direct`（**預設**，現行行為逐字不變）或 `systemd-run`（降權）。
  值非法時 **fail-closed**，不靜默當成 `direct`——「打錯字＝以為隔離生效但其實沒有」正是
  要消除的失效模式。
- **builder env 白名單**（不是黑名單 scrub）：transient unit 不繼承呼叫端的 environ，
  因此 job 的環境**就是**白名單本身。轉發類 7 項（`PATH`／`LANG`／`LC_ALL`／`LC_CTYPE`／
  `SSL_CERT_FILE`／`SSL_CERT_DIR`／`NODE_EXTRA_CA_CERTS`，每項在
  `BUILDER_FORWARDED_ENV` 上帶「為何需要」的 rationale 欄位），合成類 5 項
  （`PSC_JOB_ID`／`PSC_SLICE_ID`／`PSC_REPO_ROOT`／選配 `PSC_RELAY_TARGET`／
  選配 `HOME`）。`EXCLUDED_ENV_RATIONALE` 另記錄**刻意不轉發**的項目與理由
  （`HOME`／`USER`／`SHELL` 交給 systemd 依 passwd 填、`TMPDIR`／`XDG_*`／`VIRTUAL_ENV`
  會指回 cortex-svc 的樹、`*_PROXY` 可內嵌 `user:pass@` 屬憑證面）。
- **defense-in-depth 守衛**：白名單成品再過一次憑證形狀（與 launcher 共用同一條
  `CREDENTIAL_ENV_RE`）與注入孔名單（`BASH_ENV`／`LD_PRELOAD`／`PYTHONPATH`／
  `NODE_OPTIONS`／`CLAUDE_CONFIG_DIR`／`GH_CONFIG_DIR` …），命中即 fail-closed。
  白名單是靜態的，這道守衛存在的意義是「有人往白名單加一項憑證時測試當場紅」。
- **封閉的 argv 產生器**：`--quiet --collect --pipe --wait --unit=cortex-job-<slug>-<sha8>.service
  --uid --gid --service-type=exec --working-directory=<worktree>
  --property=NoNewPrivileges=yes --setenv=…（排序）-- bash -c <script>`。
  unit 名前綴 `cortex-job-` 是與 Phase 2b polkit 規則成對的**契約**
  （polkit 以 `action.lookup("unit")` 比對）。
- **fail-fast 診斷（#570 `DiagnosticReason` 契約）**：`preflight_systemd_run()` 在任何
  副作用之前檢查 systemd-run 二進位／`/run/systemd/system`／builder 帳號／group；
  `confirm_transient_unit_started()` 補上只能在起動當下才知道的失敗（polkit 拒絕、
  unit 名衝突），判準是「systemd-run 已結束**且** exit sentinel 不存在」，並把
  systemd-run 的實際錯誤訊息帶進 `detail`。**任何一條都不會退回 direct。**

### `paulsha_cortex/coordinator/launcher.py`

- `SubprocessLauncher.launch()` 在降權模式下改走 transient unit。判定點與既有 persona
  分支對齊：`review_only`＝reviewer、`read_only`＝planner，**兩者皆非才是 builder**；
  二分方案裡 reviewer／planner 與 Manager 同帳號，**不經降權**。
- 降權模式的 shell 改 `bash -c`（#588 第 2 點）；**direct 模式維持 `-lc` 不動**。
- FD：`--pipe` 只交出 stdin/stdout/stderr（Popen `close_fds` 預設 True），且 stdin
  顯式接 `/dev/null`——direct 模式今天仍把 daemon 的 stdin 交給 job，降權模式在這點上
  比 direct 更緊。
- `_CREDENTIAL_ENV_RE` 改為 `job_runner.CREDENTIAL_ENV_RE` 的別名，reviewer sandbox
  政策與 builder 白名單守衛永遠共用同一條 pattern。
- `executor_environment()`（#262 D2）在降權模式下回報 builder env，否則 preflight 報的
  PATH／HOME 與正式 job 無關，只是安慰劑。

### 文件

- `docs/superpowers/runbooks/trust-root-phase2b-setup.md` 第 5 步改寫：未決 1 標為已裁決、
  補上 Manager 端實際會發出的 argv 形狀、可執行的 polkit 規則（含**誠實標註**：polkit 的
  `manage-units` action 只暴露 unit 名、**不暴露 `--uid=`**，「只能降到 cortex-builder」
  那一半只能由 Manager 端封閉 argv 保證）、開關寫法與負控制驗證。
- `README.md` 補 `PSC_JOB_RUNNER` 與 builder 相關選配變數。

## 誠實邊界

**本 PR 是機制，不是生效。** 預設 `PSC_JOB_RUNNER=direct`＝現行行為完全不變。實際降權要
等 Phase 2b 建好 `cortex-builder` 帳號＋polkit 規則，再把 `PSC_JOB_RUNNER=systemd-run`
寫進 Manager env——那是部署期動作。另外兩點必須一併知道：

- **builder 帳號要有自己的模型 CLI 登入態**。Manager 不傳自己的憑證，因此 copilot builder
  在降權模式下拿不到 `COPILOT_GITHUB_TOKEN`（`_copilot_credential_env()` 讀的是白名單
  env，裡面沒有任何 token 候選，自然變 no-op）。
- **builder 必須能寫該 job 的 log 目錄**（JSONL log／exit sentinel／gate ledger 都落在
  那裡），並能讀 `PSC_REPO_ROOT`（wrapper 內 gate ledger writer 的 `PYTHONPATH`）。
  這兩條的權限由 Phase 2b 的 permgen 計畫決定。

## 測試

`tests/test_trust_root_job_runner_p2a.py`（61 測試）：direct 零回歸（預設與顯式 `direct`
形狀一致、仍走 `bash -lc`、仍繼承 daemon env）、systemd-run argv 組裝、env 白名單**不含
token 類**（`GH_TOKEN`／`GITHUB_TOKEN`／`ANTHROPIC_API_KEY`／`CLAUDE_CONFIG_DIR` … 逐一
斷言，並在整條 argv 上做值層面兜底）、reviewer／planner 不降權、四條 fail-fast 路徑。
`systemd-run` 本體全程 mock，測試不真跑 systemd、不建帳號、不碰 polkit。
