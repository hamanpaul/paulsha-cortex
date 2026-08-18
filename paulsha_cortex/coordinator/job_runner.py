"""Phase 2a 降權啟動器：builder headless job 經 `systemd-run` transient unit 降權。

trust-root spec（`docs/superpowers/specs/trust-root-isolation-spec.md`）§R10 Phase 2
第 5 條要求「**降權啟動器**：Manager 以降權方式 spawn headless job，明確關閉 FD 傳遞、
不傳遞 gh token」。operator 0816 第二輪裁決把「未決 1（降權機制）」收斂為
**systemd-run transient unit**、UID **二分**（`cortex-svc` / `cortex-builder`），並要求
polkit 授權面收窄到「cortex-svc 只能以 cortex-builder 身分、限定 unit 屬性起 job」。

本模組是那條裁決的**程式碼面**：組出降權 argv、算出 builder 專屬的 env 白名單、對
「systemd 不可用／帳號不存在／transient unit 起不來（含 polkit 拒絕）」fail-closed。

## 誠實邊界（非常重要）

**本模組不會讓任何既有部署自動降權。** `PSC_JOB_RUNNER` 預設 `direct`＝現行行為
逐字不變；只有 operator 在 Phase 2b 建好 `cortex-builder` 帳號＋polkit 規則之後，把
`PSC_JOB_RUNNER=systemd-run` 寫進 Manager 的 env，降權才會生效。切換是**部署期動作**，
不是這支程式碼的預設。

## 為什麼 transient unit 天然解掉 #588 的一半

issue #588 的兩條缺口：(1) builder 繼承 daemon 全部 environ（含 token 與 daemon 自己的
`CLAUDE_CONFIG_DIR`）；(2) builder 走 `bash -lc`（login shell），`~/.profile` 會在
launcher 設好 env 之後重新匯入 operator 環境，把任何 env 約束覆寫掉。

- 對 (1)：transient unit **不繼承呼叫端的環境**——unit 的環境是 PID 1 的 manager
  environment 加上我們顯式列出的 `--setenv`。因此「不傳 token」在這裡不是靠 scrub
  黑名單，而是靠「只有白名單裡的名字會被送進去」這個結構事實（見
  :data:`BUILDER_FORWARDED_ENV`）。為了不讓 PID 1 環境的殘留（主要是 PATH）滲進來，
  白名單一律顯式設定 `PATH`。
- 對 (2)：降權模式的 shell 一律 `bash -c`（非 `-lc`），與 reviewer 早已採用的作法一致。
  direct 模式**維持現行 `-lc` 不動**（零回歸），只有 systemd-run 模式改。

## FD

`--pipe` 讓 transient unit 的 stdio 接上呼叫端的 stdin/stdout/stderr——這正是 JSONL log
與 exit sentinel 得以沿用既有 harvest 管線的原因。除了這三個 fd 之外沒有任何東西被傳遞：
Python `Popen` 預設 `close_fds=True`，且 launcher 在降權模式顯式把 stdin 接 `/dev/null`
（direct 模式今天還會把 daemon 的 stdin 交給 job——降權模式在這點上比 direct 更緊）。

## 為什麼要 `--wait`

`dispatcher.pid_alive()` 以 `os.kill(pid, 0)` 判活。`--wait` 讓 `systemd-run` 這個 client
行程存活到 unit 結束為止，因此 `LaunchHandle.pid` 仍然是一個「與 job 同壽命」的 pid，
既有的存活判定不必改。少了 `--wait`，client 會立刻返回，dispatcher 會把還在跑的 job
判成已死。

## 為什麼要 `--quiet`

`systemd-run` 會把 "Running as unit: …" 之類的狀態訊息寫到 stderr，而 launcher 把
stderr 併進 JSONL log（`stderr=STDOUT`）。不加 `--quiet` 就等於在 terminal evidence 的
來源檔裡插入非 JSON 行。

## 第三種模式：`systemd-template`（operator 0816 **第三輪**裁決 A+B 的 B 半）

#603 的實測結論是：`org.freedesktop.systemd1.manage-units` 這個 polkit action
**只暴露 unit 名稱**，不暴露 `User=`／`--uid=`。因此 `systemd-run` 模式裡「只能
降到 builder」這一半在 OS 層是**沒有強制的**——持授權的帳號被攻陷即可請求
`--uid=root`。裁決把它換成 root-owned 的**模板 unit**：

    systemctl start --wait cortex-job@<instance>.service

- `User=` 與 `ExecStart=` 都寫死在 root 擁有的 `cortex-job@.service` 裡，Manager
  帳號**選不了 UID、也給不了命令列**（unit 檔它改不動）。
- 命令列既然給不了，per-job 參數改走**帶外通道**：一份 spec JSON 原子寫進
  Manager-owned spool（登記表資產 `job-spec-spool-<principal>`，**該** job 帳號
  唯讀；#657 起一個降權身分一格），`ExecStart=` 的 root-owned shim 讀完才 exec
  真正的 job（見 :mod:`paulsha_cortex.coordinator.job_shim`）。
- spec **不含** `User`／`uid` 這類欄位（:data:`SPEC_FORBIDDEN_KEYS` 在寫端與讀端
  各擋一次），也不含任何 token（沿用同一份 env 白名單）。

**判活與 log 與 systemd-run 模式同一條路**：

- `--wait` 讓 `systemctl` client 存活到 unit 結束（`systemctl(1)`：「For (re)start,
  wait until service stopped again」，systemd ≥ 232），因此
  `dispatcher.pid_alive()` 的 pid 判活不必改一行。
- exit sentinel **不再由 job 進程自寫**（#604）：改由 Manager 側的 exit 記帳 shell
  （:func:`build_manager_exit_recorder_argv`）包住 client argv，記下 client 的 `$?`。
  `poll_headless_done` 讀的路徑逐字不變，變的只有「誰是寫者」——降權模式下 job 帳號
  對那個目錄本來就無寫入權（`gate-ledger` 資產＝`0700 cortex-manager`，且不在 job
  模板 unit 的 `ReadWritePaths=` 內），繼續要求 job 自寫等於要求它做一件必定 EROFS
  的事，而在 direct 模式下它又等於讓被驗方自報 exit code。
- log：`systemctl` **沒有** `--pipe`，且 unit 的 `StandardOutput=append:` 會由
  **root 在降權前**開檔（Manager 可寫的路徑上放 symlink 即成提權面），因此改由
  shim 在**已降權之後**依 spec 的 `log_path` 以 `O_NOFOLLOW` 接管 stdout/stderr。
  **harvest 讀的 log 路徑因此逐字不變**（`<log_dir>/<slice_id>.jsonl`）。

fail-fast 三案（任一命中即 `DiagnosticReason` fail-closed，**絕不**退回其他模式）：
模板 unit／shim 未安裝、同名 instance 已在跑、spec 寫入失敗。

本模組只組字串、做唯讀探測、與寫入 Manager 自己的 spec spool，**不執行任何 root
操作、不建帳號、不寫 polkit、不安裝任何 unit**。
"""
from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from . import job_workspace
from .diagnostics import DiagnosticReason, diagnostic_reason

__all__ = [
    "BUILDER_ACCOUNT_ENV",
    "BUILDER_FORWARDED_ENV",
    "BUILDER_GROUP_ENV",
    "BUILDER_HOME_ENV",
    "BUILDER_PATH_ENV",
    "BUILDER_SYNTHESIZED_ENV",
    "CREDENTIAL_ENV_RE",
    "DEFAULT_BUILDER_ACCOUNT",
    "DEFAULT_GATE_ACCOUNT",
    "DEFAULT_GATE_TEMPLATE_UNIT",
    "DEFAULT_JOB_SHIM",
    "DEFAULT_JOB_SPEC_SPOOL",
    "DEFAULT_GATE_JOB_SPEC_SPOOL",
    "DEFAULT_REVIEW_JOB_SPEC_SPOOL",
    "DEFAULT_REVIEWER_ACCOUNT",
    "DEFAULT_REVIEW_TEMPLATE_UNIT",
    "DEFAULT_START_TIMEOUT_MS",
    "DEFAULT_TEMPLATE_UNIT",
    "EXECUTOR_HARDENING_PROFILE",
    "ForwardedEnvVar",
    "GATE_ACCOUNT_ENV",
    "GATE_GROUP_ENV",
    "GATE_HOME_ENV",
    "GATE_PATH_ENV",
    "GATE_TEMPLATE_UNIT_ENV",
    "HARDENING_PROFILE_JIT",
    "HARDENING_PROFILE_STRICT",
    "JOB_ROLES",
    "JOB_ROLE_BUILDER",
    "JOB_ROLE_CONFIG",
    "JOB_ROLE_GATE",
    "JOB_ROLE_REVIEW",
    "JOB_RUNNER_ENV",
    "JOB_SHIM_ENV",
    "JOB_SPEC_SPOOL_ENV",
    "GATE_JOB_SPEC_SPOOL_ENV",
    "REVIEW_JOB_SPEC_SPOOL_ENV",
    "POSIX_ACL_ACCESS_XATTR",
    "POSIX_ACL_DEFAULT_XATTR",
    "JOB_SPEC_VERSION",
    "JobRoleConfig",
    "JobRunnerError",
    "REVIEWER_ACCOUNT_ENV",
    "REVIEWER_GROUP_ENV",
    "REVIEWER_HOME_ENV",
    "REVIEWER_PATH_ENV",
    "REVIEW_TEMPLATE_UNIT_ENV",
    "RUNNER_DIRECT",
    "RUNNER_MODES",
    "RUNNER_SYSTEMD_RUN",
    "RUNNER_SYSTEMD_TEMPLATE",
    "SPEC_FORBIDDEN_KEYS",
    "SPEC_REQUIRED_KEYS",
    "START_TIMEOUT_ENV",
    "SystemdRunPlan",
    "SystemdTemplatePlan",
    "TEMPLATE_UNIT_ENV",
    "TEMPLATE_UNIT_PREFIX",
    "TEMPLATE_UNIT_SUFFIX_BY_PROFILE",
    "TRANSIENT_UNIT_PROPERTIES",
    "UNIT_NAME_PREFIX",
    "build_builder_env",
    "build_job_env",
    "build_job_spec",
    "build_manager_exit_recorder_argv",
    "build_systemctl_start_argv",
    "build_systemd_run_argv",
    "confirm_template_instance_started",
    "confirm_transient_unit_started",
    "effective_perms_for_account",
    "forbidden_spec_keys",
    "inherited_perms_for_account",
    "instance_name_valid",
    "job_spec_path",
    "preflight_systemd_run",
    "preflight_systemd_template",
    "prepare_systemd_run",
    "prepare_systemd_template",
    "reject_unsafe_env",
    "resolve_hardening_profile",
    "resolve_job_account",
    "resolve_job_group",
    "resolve_job_path",
    "resolve_job_spec_spool",
    "resolve_job_role",
    "template_instance_id",
    "template_unit_for_profile",
    "template_unit_name",
    "transient_unit_name",
    "write_job_spec",
]


# ---------------------------------------------------------------------------
# config（全部走 env；預設值＝現行行為）
# ---------------------------------------------------------------------------

#: 執行模式開關。`direct`（預設）＝現行 `Popen(bash -lc …)`；`systemd-run`＝降權。
JOB_RUNNER_ENV = "PSC_JOB_RUNNER"

#: builder 的 OS 帳號名。預設對齊 `trust_root.permgen.TWO_WAY_SCHEME` 的
#: `Principal.BUILDER` → `cortex-builder`；可 config 是為了對齊 permgen 的
#: `UidScheme` 參數化（二分→三分只換 config，不改程式碼）。
BUILDER_ACCOUNT_ENV = "PSC_BUILDER_ACCOUNT"
DEFAULT_BUILDER_ACCOUNT = "cortex-builder"

#: builder 的 primary group。未設時沿用 `UidScheme.group_of()` 的慣例（每帳號一個同名
#: group），因此預設等於帳號名。
BUILDER_GROUP_ENV = "PSC_BUILDER_GROUP"

#: builder 自己的 HOME。**未設時刻意不傳 HOME**——systemd 對設了 `User=` 的 unit 會
#: 依 passwd 自行填入該帳號的 `$HOME`／`$USER`／`$LOGNAME`／`$SHELL`，那份值必然正確；
#: 反之 daemon 的 HOME 絕不可轉發（那是 cortex-svc 的樹，且是 #588 第 1 點的核心）。
BUILDER_HOME_ENV = "PSC_BUILDER_HOME"

#: builder 的 PATH。**必填，且未設時 fail-closed**（#679）。
#:
#: #640 起它就已經是「必填」的部署契約（runbook 明講），但程式碼這一側直到 #679 都
#: 還是 fail-open：未設就整個不寫 `PATH`，於是 `execvpe` 退回 `os.defpath`
#: （`:/bin:/usr/bin`）——`codex` 靜默解到系統層那份舊 CLI，不報錯、只是產出來自一支
#: operator 從未判讀過的執行檔。宣告與實作的落差本身就是那個缺陷。
BUILDER_PATH_ENV = "PSC_BUILDER_PATH"

#: reviewer＋planner 的 OS 帳號名（#615 M2）。三分方案把兩個 persona 映到**同一個**
#: 帳號，因此只有一組變數、一份模板 unit——不是漏了 planner，是 planner 就是它。
REVIEWER_ACCOUNT_ENV = "PSC_REVIEWER_ACCOUNT"
DEFAULT_REVIEWER_ACCOUNT = "cortex-reviewer-planner"
REVIEWER_GROUP_ENV = "PSC_REVIEWER_GROUP"
REVIEWER_HOME_ENV = "PSC_REVIEWER_HOME"
REVIEWER_PATH_ENV = "PSC_REVIEWER_PATH"

#: gate 執行身分的 OS 帳號名（#629）。**第四個帳號**，與 builder／reviewer-planner／
#: manager 三者皆不同——理由見 `trust_root.permgen.FOUR_WAY_SCHEME` 的說明。
GATE_ACCOUNT_ENV = "PSC_GATE_ACCOUNT"
DEFAULT_GATE_ACCOUNT = "cortex-gate"
GATE_GROUP_ENV = "PSC_GATE_GROUP"
GATE_HOME_ENV = "PSC_GATE_HOME"
GATE_PATH_ENV = "PSC_GATE_PATH"

#: gate 的模板 unit 名（與 `permgen.job_unit_stem(…, GATE)` 成對契約）。
GATE_TEMPLATE_UNIT_ENV = "PSC_GATE_JOB_TEMPLATE_UNIT"
DEFAULT_GATE_TEMPLATE_UNIT = "cortex-gate-job@.service"

#: reviewer／planner 的模板 unit 名（與 `permgen.job_unit_stem(…, REVIEWER)` 成對契約）。
REVIEW_TEMPLATE_UNIT_ENV = "PSC_REVIEW_JOB_TEMPLATE_UNIT"
DEFAULT_REVIEW_TEMPLATE_UNIT = "cortex-reviewer-job@.service"

#: transient unit 起動確認窗（毫秒）。見 :func:`confirm_transient_unit_started`。
START_TIMEOUT_ENV = "PSC_JOB_RUNNER_START_TIMEOUT_MS"
DEFAULT_START_TIMEOUT_MS = 200

RUNNER_DIRECT = "direct"
RUNNER_SYSTEMD_RUN = "systemd-run"
#: 0816 第三輪裁決 B：root-owned 模板 unit。見本檔「模板實例模式」段。
RUNNER_SYSTEMD_TEMPLATE = "systemd-template"
RUNNER_MODES = (RUNNER_DIRECT, RUNNER_SYSTEMD_RUN, RUNNER_SYSTEMD_TEMPLATE)

#: transient unit 名前綴。polkit 規則以 `action.lookup("unit")` 收窄授權面時，比對的
#: 就是這個前綴（見 Phase 2b runbook 第 5 步）——因此它是**契約**，不可隨手改。
UNIT_NAME_PREFIX = "cortex-job-"

#: transient unit 的封閉屬性集合。polkit 規則要「限定 unit 屬性」，前提是 Manager 端
#: 產出的屬性集合是**固定且最小**的；因此這裡刻意不開放 config 疊加。
#:
#: - `NoNewPrivileges=yes`：builder 內部不得再提權（setuid 二進位對它失效）。
#:   `ReadWritePaths=` 等與部署路徑相關的加固**不在這裡**——它們由 Phase 1 登記表經
#:   `trust_root.permgen` 機械產生並寫進 unit／runbook（operator 未決 5 的裁決），
#:   在啟動器裡手寫會變成第二份真相。
TRANSIENT_UNIT_PROPERTIES = ("NoNewPrivileges=yes",)


# ---------------------------------------------------------------------------
# 模板實例模式（0816 第三輪裁決 B）的 config
#
# 下面四個預設值與 `trust_root.permgen.DEFAULT_LAYOUT` 是**成對契約**（同一份路徑
# 裁決的兩個落點）。刻意不在此 import permgen——`job_runner` 至今只依賴 stdlib ＋
# `diagnostics`，讓派工熱路徑不必拖進整個 trust_root 子套件；改以
# `tests/test_trust_root_job_template_ab.py` 的契約測試釘住兩邊逐字相等，任一邊
# 漂移都會當場紅。
# ---------------------------------------------------------------------------

#: 模板 unit 名。polkit 規則（`permgen.build_polkit_rule(plan=TEMPLATE)`）比對的
#: 就是它的實例形狀 `cortex-job@<id>.service`，因此是契約，不可隨手改。
#:
#: **這是「基底」模板名**（＝strict 剖面）。#643 之後真正被起動的 unit 還要再過一次
#: :func:`template_unit_for_profile`：node 型 executor 走 `cortex-job-jit@.service`。
TEMPLATE_UNIT_ENV = "PSC_JOB_TEMPLATE_UNIT"
TEMPLATE_UNIT_PREFIX = "cortex-job@"
TEMPLATE_UNIT_SUFFIX = ".service"
DEFAULT_TEMPLATE_UNIT = f"{TEMPLATE_UNIT_PREFIX}{TEMPLATE_UNIT_SUFFIX}"

# ---------------------------------------------------------------------------
# per-principal spec spool（#657）
#
# 在此之前三份模板 unit 共用同一個 spool 根，而登記表只授 builder 唯讀 ACL——shim 是
# systemd 套完 `User=` **之後**才執行的，因此 reviewer／gate 的每一個 job 都在讀 spec
# 時 `EACCES` → `78/CONFIG`（實機實測）。現在每個角色有自己的 spool（登記表資產
# `job-spec-spool-<principal>`），路徑由 root-owned 的模板 unit 以
# `Environment=PSC_JOB_SPEC_SPOOL=` 宣告——**shim 端因此一行都不必改**，它讀的永遠是
# 「這份 unit 說的那一個」，而那一行是可稽核的。
#
# 下面三組預設值與 `trust_root.permgen.PathLayout.job_spec_spool_for()` 是**成對契約**
# （與 `DEFAULT_TEMPLATE_UNIT` 同一個既有模式：job_runner 刻意不 import permgen，改由
# `tests/test_per_principal_spec_spool_657.py` 釘住兩邊逐字相等）。
#
# **per-role 的變數名是刻意的**：共用一個 `PSC_JOB_SPEC_SPOOL` 會讓「Manager 的環境裡
# 剛好有這個變數」把三個角色一起導回同一個目錄——那正好是本票要修掉的那個狀態，而且
# 會靜默生效。
# ---------------------------------------------------------------------------

JOB_SPEC_SPOOL_ENV = "PSC_JOB_SPEC_SPOOL"
DEFAULT_JOB_SPEC_SPOOL = "/var/lib/cortex/coordinator/job-specs/builder"
REVIEW_JOB_SPEC_SPOOL_ENV = "PSC_REVIEW_JOB_SPEC_SPOOL"
DEFAULT_REVIEW_JOB_SPEC_SPOOL = "/var/lib/cortex/coordinator/job-specs/reviewer"
GATE_JOB_SPEC_SPOOL_ENV = "PSC_GATE_JOB_SPEC_SPOOL"
DEFAULT_GATE_JOB_SPEC_SPOOL = "/var/lib/cortex/coordinator/job-specs/gate"


# ---------------------------------------------------------------------------
# job 角色（#615 M2：reviewer／planner 啟動面降權）
#
# M1 只有一個降權角色（builder），因此「哪個帳號、哪份模板、哪個 PATH 覆寫」四組
# config 直接寫成模組層常數就夠了。M2 之後有**兩個**角色，而它們的差異全部落在
# 「用哪一組 config」——身分、模板、加固剖面選法、env 白名單、preflight、spec 形狀、
# 起動確認**逐條相同**。因此這裡不是兩條 code path，而是**一張表 ＋ 一個參數**。
#
# 這張表是唯一真相：`resolve_job_account()`／`resolve_job_group()`／
# `build_job_env()`／`prepare_systemd_template()`／`prepare_systemd_run()` 全部由
# `role` 查表，沒有任何一支帶 `if role == …` 的分支。
#
# **為什麼 planner 不是第三個角色**：三分方案（`permgen.THREE_WAY_SCHEME`）把
# REVIEWER 與 PLANNER 映到同一個 OS 帳號 `cortex-reviewer-planner`。角色的全部內容
# 就是「哪個帳號」——同帳號 ⇒ 同 unit、同 RWP、同 HOME，多一個角色只會多一個要同步
# 維護的名字與一個要放進 polkit pattern 的字幹，換不到任何隔離。
# ---------------------------------------------------------------------------

JOB_ROLE_BUILDER = "builder"
#: reviewer ＋ planner（同一個 OS 帳號、同一份模板 unit）。
JOB_ROLE_REVIEW = "review"
#: operator 宣告的 gate 命令（#629）。**不跑模型**，但跑的是 builder 工作樹裡的
#: `conftest.py`／plugin，因此與前兩者同級地必須被關進盒子——只是**不同的**盒子。
JOB_ROLE_GATE = "gate"
JOB_ROLES = (JOB_ROLE_BUILDER, JOB_ROLE_REVIEW, JOB_ROLE_GATE)


@dataclass(frozen=True)
class JobRoleConfig:
    """一個降權 job 角色的完整 config 面（env 變數名 ＋ 預設值 ＋ 為什麼）。"""

    role_id: str
    account_env: str
    default_account: str
    group_env: str
    home_env: str
    path_env: str
    template_env: str
    default_template: str
    #: #657：**本角色專屬**的 spec spool。與模板 unit 的
    #: `Environment=PSC_JOB_SPEC_SPOOL=` 是同一條路徑的兩個落點（Manager 寫端／
    #: shim 讀端），由 `trust_root.permgen` 機械產出。
    spec_spool_env: str
    default_spec_spool: str
    #: 這個角色是誰、為什麼要獨立一份（進錯誤訊息與產物註解）。
    rationale: str


JOB_ROLE_CONFIG: Mapping[str, JobRoleConfig] = MappingProxyType(
    {
        JOB_ROLE_BUILDER: JobRoleConfig(
            role_id=JOB_ROLE_BUILDER,
            account_env=BUILDER_ACCOUNT_ENV,
            default_account=DEFAULT_BUILDER_ACCOUNT,
            group_env=BUILDER_GROUP_ENV,
            home_env=BUILDER_HOME_ENV,
            path_env=BUILDER_PATH_ENV,
            template_env=TEMPLATE_UNIT_ENV,
            default_template=DEFAULT_TEMPLATE_UNIT,
            spec_spool_env=JOB_SPEC_SPOOL_ENV,
            default_spec_spool=DEFAULT_JOB_SPEC_SPOOL,
            rationale=(
                "builder persona——唯一會在自己完全掌控的工作區裡跑 untrusted repo "
                "code 的角色，攻擊面最大。M1（#603／#584）已落地。"
            ),
        ),
        JOB_ROLE_REVIEW: JobRoleConfig(
            role_id=JOB_ROLE_REVIEW,
            account_env=REVIEWER_ACCOUNT_ENV,
            default_account=DEFAULT_REVIEWER_ACCOUNT,
            group_env=REVIEWER_GROUP_ENV,
            home_env=REVIEWER_HOME_ENV,
            path_env=REVIEWER_PATH_ENV,
            template_env=REVIEW_TEMPLATE_UNIT_ENV,
            default_template=DEFAULT_REVIEW_TEMPLATE_UNIT,
            spec_spool_env=REVIEW_JOB_SPEC_SPOOL_ENV,
            default_spec_spool=DEFAULT_REVIEW_JOB_SPEC_SPOOL,
            rationale=(
                "reviewer ＋ planner persona（三分方案下同一個 OS 帳號）。M2（#615）："
                "在此之前它們仍在 Manager 行程內以 Manager 帳號執行，"
                "「injection 可達的進程皆無 spawn 授權」因此只對 builder 成立。"
            ),
        ),
        JOB_ROLE_GATE: JobRoleConfig(
            role_id=JOB_ROLE_GATE,
            account_env=GATE_ACCOUNT_ENV,
            default_account=DEFAULT_GATE_ACCOUNT,
            group_env=GATE_GROUP_ENV,
            home_env=GATE_HOME_ENV,
            path_env=GATE_PATH_ENV,
            template_env=GATE_TEMPLATE_UNIT_ENV,
            default_template=DEFAULT_GATE_TEMPLATE_UNIT,
            spec_spool_env=GATE_JOB_SPEC_SPOOL_ENV,
            default_spec_spool=DEFAULT_GATE_JOB_SPEC_SPOOL,
            rationale=(
                "gate 執行身分（#629）——operator 宣告的 `PSC_GATE_CMD_*` 在這裡執行。"
                "它不跑模型，但那些命令載入的是 builder 完全掌控的工作樹裡的 "
                "`conftest.py`／plugin，等於一條任意程式碼執行；既有三個帳號逐一皆不可"
                "承接（builder＝模型自證、manager＝授權線內側、reviewer-planner＝寫 "
                "verdict 的那一個），故必須是第四個帳號。"
            ),
        ),
    }
)


def resolve_job_role(role: str) -> JobRoleConfig:
    """角色 id → config。**未知角色 fail-closed**，不落回 builder。

    落回 builder 會是最糟的失敗模式：一個 reviewer job 會被以 `cortex-builder`
    起跑，等於把 verdict 的寫入面交還給 builder 帳號——正好抵銷 #639／#638 修好的
    東西，而且**看起來是成功的**。
    """

    name = str(role or "").strip()
    config = JOB_ROLE_CONFIG.get(name)
    if config is None:
        raise _fail(
            "job-runner-role-unknown",
            f"未知的降權 job 角色 {role!r}（已登記：{sorted(JOB_ROLE_CONFIG)}）",
            source="resolve_job_role",
            requested=name,
        )
    return config


# ---------------------------------------------------------------------------
# per-executor 加固剖面（#643，operator 裁決＝方向 2）
#
# `MemoryDenyWriteExecute=yes` 與 JS runtime 天生互斥（V8 的 JIT 必須 W+X），而預設
# executor（`codex`）正是 node 型。裁決是「node 型走一份只放寬這一項的 root-owned
# 模板 unit，原生執行檔型維持嚴格」。
#
# **剖面的選擇不可由 job 決定**，否則整個設計退化成「全域移除 MDWE」。守法：
#
#   1. 唯一的輸入是 **executor**，而 executor 是 Manager 的 dispatch 決定
#      （`SubprocessLauncher(executor=...)`，在 job spec 產生之前就固定了）；
#   2. 對應表在下方**列舉且封閉**，未知 executor **fail-closed**（不落到寬鬆那份）；
#   3. job spec 結構性禁止攜帶任何剖面欄位（見 :data:`SPEC_FORBIDDEN_KEYS`，
#      寫端 `build_job_spec()` 與讀端 `job_shim.load_spec()` 各掃一次）；
#   4. 兩份 unit 都是 root-owned、`User=`／`ExecStart=` 寫死，polkit 的 unit pattern
#      只列舉這兩個字幹——呼叫端選得了「哪一份模板」，但兩份都選不出 UID 或命令列。
#
# 下面三個常數與 `trust_root.permgen` 的 `HARDENING_PROFILES`／
# `EXECUTOR_HARDENING_PROFILE` 是**成對契約**（與 `DEFAULT_TEMPLATE_UNIT` 同一個
# 理由：job_runner 刻意不 import permgen，讓派工熱路徑不必拖進整個 trust_root
# 子套件），由 `tests/test_trust_root_hardening_profile_643.py` 釘住兩邊逐字相等。
# permgen 那一份才是真相來源：它由 `EXECUTOR_TOOLS.needs_node` 機械導出。
# ---------------------------------------------------------------------------

HARDENING_PROFILE_STRICT = "strict"
HARDENING_PROFILE_JIT = "jit"

#: 剖面 → 模板字幹後綴。strict 為空字串，因此 `cortex-job@.service` 逐字不變。
TEMPLATE_UNIT_SUFFIX_BY_PROFILE: Mapping[str, str] = MappingProxyType(
    {HARDENING_PROFILE_STRICT: "", HARDENING_PROFILE_JIT: "-jit"}
)

#: executor → 剖面。**封閉列舉**：這裡沒有 fallback、沒有預設值、沒有 `.get(x, jit)`。
#:
#: `cg` 刻意仍不在表內。#615（M2）之後 reviewer／planner 也走降權啟動器，因此以 `cg`
#: 派出的 reviewer **會**走到這裡並 fail-closed——**那是正確結果，不是缺口**：`cg` 是
#: operator 提供的 wrapper（自帶 throwaway HOME），本 repo 從未盤點過它實際 exec 什麼，
#: 而剖面的判準正是「它內部是不是 node」（#643：`copilot` 就是 shell script 外殼、內部
#: exec node，量到症狀才回填的）。猜嚴格 ⇒ 它可能靜默起不來（症狀是空輸出）；猜寬鬆 ⇒
#: per-executor 設計退化。要在降權模式用 `cg`，先把它登記進 `permgen.EXECUTOR_TOOLS`
#: 並標明 `needs_node`（`head -n 20 $(command -v cg)` 一次就查得出來）。`direct` 模式
#: 完全不受影響。
EXECUTOR_HARDENING_PROFILE: Mapping[str, str] = MappingProxyType(
    {
        "codex": HARDENING_PROFILE_JIT,
        "copilot": HARDENING_PROFILE_JIT,
        "claude": HARDENING_PROFILE_STRICT,
        "agy": HARDENING_PROFILE_STRICT,
    }
)

#: root-owned shim（模板 unit 的固定 `ExecStart=`）。preflight 只檢查它存在且可執行。
JOB_SHIM_ENV = "PSC_JOB_SHIM"
DEFAULT_JOB_SHIM = "/opt/cortex/bin/cortex-job-shim"

#: 模板 unit 檔的安裝位置（preflight 檢查「template 未安裝」用）。
DEFAULT_TEMPLATE_UNIT_DIR = "/etc/systemd/system"

#: spec schema 版本。shim 讀到不符的版本一律拒絕執行——「舊 Manager ＋ 新 shim」
#: 或反之都不該靠猜。
JOB_SPEC_VERSION = 1

#: spec 的必要欄位（shim 端逐項檢查；缺一即 fail-closed）。
SPEC_REQUIRED_KEYS: tuple[str, ...] = (
    "spec_version",
    "instance",
    "job_id",
    "unit",
    "command",
    "working_directory",
    "log_path",
    "env",
)

#: spec **絕不可**出現的欄位。兩族，同一條原則：
#:
#: 1. **身分**（`user`／`uid`／…）——身分只有一個來源：root-owned unit 檔的 `User=`。
#:    這是 B 案全部價值的所在。
#: 2. **加固剖面**（`hardening_profile`／`template`／…，#643）——剖面只有一個來源：
#:    executor（Manager 的 dispatch 決定）。spec 是 Manager→shim 的**參數**通道；
#:    讓它承載剖面等於把「用哪一份 unit」變成一個可寫進 spec 的輸入，而那正是
#:    「per-executor 剖面」退化成「全域移除 MDWE」的路徑。
#:
#: 兩族都在寫端（`build_job_spec`）與讀端（`job_shim.load_spec`）各擋一次。
#:
#: 註：`unit` 仍是必要欄位（`SPEC_REQUIRED_KEYS`），而它的值確實隱含剖面——但它是
#: 決定的**紀錄**，不是決定的**輸入**：shim 執行時降權與加固都已由 systemd 套用完畢，
#: shim 只拿它與自己的 instance 交叉核對，改它不會改變任何已生效的加固面。
SPEC_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        # 身分族
        "user", "group", "uid", "gid", "User", "Group", "properties", "exec_start",
        # 剖面族（#643）
        "hardening", "hardening_profile", "profile",
        "template", "template_unit", "unit_suffix",
        "MemoryDenyWriteExecute",
    }
)

#: systemd unit 實例名允許的字元。systemd 本身還允許更多（`/` 需 escape），這裡
#: 刻意更窄：instance 名會被 polkit 的 unit pattern 比對、被拼成 spec 檔名，**也是
#: job 工作區在 pool 底下的目錄名**（#645）。因此形狀的真相在 `job_workspace`，
#: 本模組只別名過來——兩份 pattern 就是兩個會漂移的來源。
INSTANCE_NAME_RE = job_workspace.JOB_SEGMENT_RE


# ---------------------------------------------------------------------------
# 憑證形狀（與 launcher 共用同一份 pattern，避免兩處漂移）
# ---------------------------------------------------------------------------

#: 憑證形狀的 env 名稱。`launcher._CREDENTIAL_ENV_RE` 直接別名到這裡，兩處永遠同一份。
CREDENTIAL_ENV_RE = re.compile(
    r"(?:^|_)(?:API_?KEY|AUTH|COOKIE|CREDENTIALS?|PASSWORD|PRIVATE_?KEY|SECRET|TOKEN)(?:$|_)",
    re.IGNORECASE,
)

#: 明列的必拒名單。`CREDENTIAL_ENV_RE` 已涵蓋 `*_TOKEN`／`*_API_KEY` 這類形狀，但
#: 有幾個名字不帶憑證字樣卻同樣是 trust-root 資產，必須逐一點名：
#:
#: - `CLAUDE_CONFIG_DIR`：#588 第 1 點——builder 繼承 daemon 的 config dir 等於拿到
#:   daemon 的登入態與信任設定（Tier-0 資產）。builder 的登入態必須是 builder 帳號
#:   自己 HOME 底下那一份（Phase 2b 由 operator 以 cortex-builder 身分登入）。
#: - `GH_CONFIG_DIR`／`GH_HOST`：同理，gh CLI 的設定樹指向 daemon 的憑證。
#: - `BASH_ENV`／`ENV`／`SHELLOPTS`／`BASHOPTS`：非互動 bash 會 source `$BASH_ENV`，
#:   等於在 `bash -c` 之下重開一個 #588 第 2 點的注入孔。
#: - `LD_PRELOAD`／`LD_LIBRARY_PATH`／`PYTHONPATH`／`PYTHONSTARTUP`／`NODE_OPTIONS`：
#:   都能讓呼叫端把程式碼注進 builder 的行程。
DENIED_ENV_NAMES = frozenset(
    {
        "BASHOPTS",
        "BASH_ENV",
        "CLAUDE_CONFIG_DIR",
        "ENV",
        "GH_CONFIG_DIR",
        "GH_HOST",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "SHELLOPTS",
    }
)


class JobRunnerError(ValueError):
    """降權啟動器 fail-closed。

    刻意繼承 ``ValueError``：本 repo 全部的 fail-closed 驗證都是 ``ValueError``，
    daemon 的 tick isolation（#246）攔的也是 ``(ValueError, RuntimeError, OSError)``，
    因此一次派工失敗不會打掛 daemon，但**絕不會**退回 direct 模式。

    ``diagnostic`` 是 #570／#527 的 :class:`DiagnosticReason` 契約：呼叫端把它落進
    run／slice 的 needs_human 理由，operator 不必反推是哪一條路徑拒絕的。
    """

    def __init__(self, diagnostic: DiagnosticReason) -> None:
        self.diagnostic = diagnostic
        # `rendered()`＝`<reason>: <detail> (source=…)`。既有呼叫端（autonomy 的
        # `_mark_slice_needs_human(reason=str(exc))`）拿到的字串因此已經含有機器可讀
        # 分類碼與來源位置，不必先改呼叫端就有可稽核的理由。
        super().__init__(diagnostic.rendered())


def _fail(reason: str, detail: str, *, source: str, **context: object) -> JobRunnerError:
    return JobRunnerError(
        diagnostic_reason(reason, detail, source=f"job_runner.{source}", **context)
    )


# ---------------------------------------------------------------------------
# 模式與身分解析
# ---------------------------------------------------------------------------

def resolve_runner_mode(env: Mapping[str, str]) -> str:
    """解析 `PSC_JOB_RUNNER`。未設＝`direct`；不認得的值 fail-closed。

    刻意**不**把非法值當成 `direct`：一個打錯字的部署設定若被靜默解讀成「不降權」，
    operator 會以為隔離已生效而實際上沒有——那正是本票要消除的失效模式。
    """

    raw = (env.get(JOB_RUNNER_ENV) or "").strip()
    if not raw:
        return RUNNER_DIRECT
    mode = raw.lower()
    if mode not in RUNNER_MODES:
        raise _fail(
            "job-runner-mode-invalid",
            f"{JOB_RUNNER_ENV} 只能是 {' 或 '.join(RUNNER_MODES)}，收到 {raw!r}",
            source="resolve_runner_mode",
            requested=raw,
        )
    return mode


def _resolve_identity(env: Mapping[str, str], *, key: str, default: str, source: str) -> str:
    raw = (env.get(key) or "").strip()
    name = raw or default
    # POSIX 帳號名的保守形狀；同時擋掉把 shell/argv metacharacter 塞進 `--uid=` 的用法。
    if re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", name) is None:
        raise _fail(
            "job-runner-account-name-invalid",
            f"{key} 非法的 POSIX 帳號／群組名: {name!r}",
            source=source,
            requested=name,
        )
    return name


def resolve_job_account(env: Mapping[str, str], *, role: str = JOB_ROLE_BUILDER) -> str:
    """該角色的 OS 帳號名（由 :data:`JOB_ROLE_CONFIG` 查表）。"""

    config = resolve_job_role(role)
    return _resolve_identity(
        env,
        key=config.account_env,
        default=config.default_account,
        source="resolve_job_account",
    )


def resolve_job_group(env: Mapping[str, str], *, role: str = JOB_ROLE_BUILDER) -> str:
    """該角色的 primary group（未設時＝帳號名）。

    預設值沿用 `trust_root.permgen.UidScheme.group_of()`：每帳號一個同名 group。
    """

    config = resolve_job_role(role)
    return _resolve_identity(
        env,
        key=config.group_env,
        default=resolve_job_account(env, role=role),
        source="resolve_job_group",
    )


def resolve_builder_account(env: Mapping[str, str]) -> str:
    """builder 的 OS 帳號名（`PSC_BUILDER_ACCOUNT`，預設 `cortex-builder`）。

    **保留為 builder 角色的具名別名**（既有呼叫端與 runbook 診斷片段直接用它）。
    """

    return resolve_job_account(env, role=JOB_ROLE_BUILDER)


def resolve_builder_group(env: Mapping[str, str]) -> str:
    """builder 的 primary group（`PSC_BUILDER_GROUP`，預設＝帳號名）。"""

    return resolve_job_group(env, role=JOB_ROLE_BUILDER)


def resolve_start_timeout_ms(env: Mapping[str, str]) -> int:
    """起動確認窗（毫秒）。非法值 fail-closed，不靜默落回預設。"""

    raw = (env.get(START_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_START_TIMEOUT_MS
    try:
        value = int(raw)
    except ValueError as exc:
        raise _fail(
            "job-runner-start-timeout-invalid",
            f"{START_TIMEOUT_ENV} 必須是整數毫秒，收到 {raw!r}",
            source="resolve_start_timeout_ms",
            requested=raw,
        ) from exc
    if value < 0:
        raise _fail(
            "job-runner-start-timeout-invalid",
            f"{START_TIMEOUT_ENV} 不得為負，收到 {raw!r}",
            source="resolve_start_timeout_ms",
            requested=raw,
        )
    return value


# ---------------------------------------------------------------------------
# env 白名單
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForwardedEnvVar:
    """一個可從 Manager env 轉發給 builder 的變數，附「為何需要」。

    `rationale` 不是註解裝飾：白名單的每一項都是一個刻意打開的孔，理由必須跟著值走，
    未來要收窄時才有依據。
    """

    name: str
    rationale: str


#: **轉發類**白名單：只有這些名字會從 Manager env 複製進 transient unit。
#:
#: 白名單的判準：(a) 缺了它模型 CLI 或 wrapper 會直接失敗；(b) 它本身不是憑證、
#: 也不指向任何 trust-root 資產。不符合任一條的一律不列——包含所有 token、
#: `CLAUDE_CONFIG_DIR`／`GH_CONFIG_DIR`、以及下面「刻意排除」段列出的項目。
BUILDER_FORWARDED_ENV: tuple[ForwardedEnvVar, ...] = (
    ForwardedEnvVar(
        "LANG",
        # claude／copilot 是 node CLI、codex 是 rust CLI，prompt 與 candidate 檔案
        # 常含非 ASCII（本 repo 的 spec／CHANGELOG 全是 zh-tw）；locale 缺失時
        # wrapper 內的 python3 會以 ASCII 解讀檔案而丟 UnicodeDecodeError。
        "UTF-8 locale：prompt／spec／CHANGELOG 皆含 zh-tw，缺 locale 會讓 gate writer 解碼失敗",
    ),
    ForwardedEnvVar(
        "LC_ALL",
        "同 LANG（覆寫優先序較高的那一個），與 reviewer 既有最小 env 的 LC_* 處置一致",
    ),
    ForwardedEnvVar(
        "LC_CTYPE",
        "同 LANG；只轉發字元分類這一項，其餘 LC_*（金額／日期格式）對 headless job 無意義",
    ),
    ForwardedEnvVar(
        "SSL_CERT_FILE",
        # 值是 CA bundle 的**路徑**，不是憑證內容；企業／自簽 CA 環境下缺它，
        # 模型 CLI 對 API 端點的 HTTPS 直接失敗，且失敗訊息與「未登入」難以區分。
        "自訂 CA 環境下 python/openssl 的 CA bundle 路徑（值是路徑，非憑證內容）",
    ),
    ForwardedEnvVar(
        "SSL_CERT_DIR",
        "同 SSL_CERT_FILE 的目錄形式",
    ),
    ForwardedEnvVar(
        "NODE_EXTRA_CA_CERTS",
        # claude／copilot CLI 是 node 進程，node 不讀 SSL_CERT_FILE，只讀這一個。
        "node 版模型 CLI（claude／copilot）唯一認得的額外 CA 路徑",
    ),
)

#: **合成類**白名單：由 launcher 現場算出、絕不從 Manager env 繼承的變數。
#:
#: - `PSC_JOB_ID`：#506／D5 的 headless job 標記，`porcelain/headless_hook.py`
#:   沒有它就是完全 no-op；#587／#588 第 3 點另有 job_id 語意收斂的後續。
#: - `PSC_SLICE_ID`：既有 job 標記（launcher 現行行為）。
#: - `PSC_REPO_ROOT`：relay hook 的 script 解析點，以及 wrapper 內 gate ledger writer
#:   的 `PYTHONPATH`。Phase 2b 之後它指向 root-owned 部署樹，builder 唯讀。
#: - `PSC_RELAY_TARGET`：僅在 launcher 有設定時才出現（與 direct 模式同條件）。
#: - `HOME`：僅在 `PSC_BUILDER_HOME` 明示時才設；未設時交給 systemd 依 passwd 填入
#:   builder 帳號自己的 HOME。**任何情況下都不會是 daemon 的 HOME。**
#: - `PATH`：#679 起由 :func:`resolve_job_path` 從**本角色的** `PSC_*_PATH` 算出，
#:   未宣告即 fail-closed。**它以前在轉發類白名單上，那是本票真正的 fail-open**：
#:   未宣告時 job 靜默拿到 **Manager daemon 的** `PATH`——一個沒有人為「job 該解到
#:   哪一份 CLI」做過決定的值，而且它是否含 `<toolchain>/bin` 完全看那台機器的
#:   EnvironmentFile 被誰手動加過什麼。轉發 daemon 的 `PATH` 與轉發 daemon 的
#:   `HOME`／`VIRTUAL_ENV`（早就在排除表上）是同一類錯誤：daemon 的 `PATH` 還帶著
#:   `<deploy_root>/venv/bin`，等於把 job 的 `python3` 綁回 Manager 的 venv。
BUILDER_SYNTHESIZED_ENV = (
    "HOME",
    "PATH",
    "PSC_JOB_ID",
    "PSC_RELAY_TARGET",
    "PSC_REPO_ROOT",
    "PSC_SLICE_ID",
)

#: 刻意**不**轉發、且值得記錄理由的項目（本身不是憑證，但轉發會出錯或擴大信任面）：
#:
#: - `HOME`／`USER`／`LOGNAME`／`SHELL`：systemd 對設了 `User=` 的 unit 會依 passwd
#:   自行填入正確的值；轉發 daemon 的版本只會指回 cortex-svc 的樹。
#: - `TMPDIR`：Manager unit 若啟用 `PrivateTmp=`，它的 TMPDIR 是 builder 進不去的
#:   命名空間路徑；不轉發，讓 builder 用預設 `/tmp`。
#: - `XDG_*`：預設由 HOME 推導；轉發 daemon 的值會讓 builder 的 cache／config 落回
#:   cortex-svc 的樹（Manager-owned，builder 無寫入權 → 模型 CLI 起不來）。
#: - `HTTP_PROXY`／`HTTPS_PROXY`／`NO_PROXY`：proxy URL 可內嵌 `user:pass@`，屬憑證面。
#:   需要 proxy 的部署由 builder 自己的 systemd drop-in／EnvironmentFile 提供，不由
#:   Manager 轉發。
#: - `VIRTUAL_ENV`／`PYTHONHOME`：指向 Manager 的部署 venv；builder 的 python 應由
#:   PATH 決定，不該被綁進 Manager 的 venv。
EXCLUDED_ENV_RATIONALE: Mapping[str, str] = {
    "HOME": "systemd 依 passwd 填入 builder 自己的 HOME；daemon 的 HOME 絕不轉發",
    "PATH": (
        "#679：daemon 的 PATH 絕不轉發——它帶著 <deploy_root>/venv/bin，且是否含 "
        "toolchain 全看那台機器被手動加過什麼。job 的 PATH 只由本角色的 "
        "PSC_*_PATH 決定（resolve_job_path，未宣告即 fail-closed）"
    ),
    "HTTPS_PROXY": "proxy URL 可內嵌 user:pass，屬憑證面；由 builder 自己的 drop-in 提供",
    "HTTP_PROXY": "同 HTTPS_PROXY",
    "LOGNAME": "systemd 依 passwd 填入",
    "NO_PROXY": "與 *_PROXY 成對，單獨轉發無意義",
    "PYTHONHOME": "會把 builder 綁進 Manager 的部署 venv",
    "SHELL": "systemd 依 passwd 填入",
    "TMPDIR": "Manager 若啟用 PrivateTmp=，該路徑 builder 進不去",
    "USER": "systemd 依 passwd 填入",
    "VIRTUAL_ENV": "指向 Manager 部署 venv，builder 的 python 應由 PATH 決定",
    "XDG_CACHE_HOME": "轉發會讓 builder 的 cache 落回 cortex-svc 的樹",
    "XDG_CONFIG_HOME": "轉發會讓 builder 的 config 落回 cortex-svc 的樹",
    "XDG_RUNTIME_DIR": "cortex-svc 的 runtime dir，builder 無權存取",
}


def reject_unsafe_env(env: Mapping[str, str], *, source: str) -> None:
    """公開別名——讓 shim 端（`job_shim`）用**同一條**守衛驗它讀到的 spec env。

    寫端與讀端共用一份判準，是為了讓「白名單被改壞」在兩邊都會炸；只在寫端自律，
    等於相信一個 Manager 帳號可寫的檔案沒被動過手腳。
    """

    _reject_unsafe(env, source=source)


def _reject_unsafe(env: Mapping[str, str], *, source: str) -> None:
    """最後一道守衛：白名單即使被改壞，憑證形狀與注入孔仍然出不去。

    白名單是靜態的，正常情況下這個檢查永遠不會命中——它存在的意義是：往
    `BUILDER_FORWARDED_ENV` 加一項憑證形狀的變數時，**測試會當場紅**，而不是等
    dogfooding 現場才發現 token 又跟著 job 出去了。
    """

    for key, value in env.items():
        if CREDENTIAL_ENV_RE.search(key) is not None or key in DENIED_ENV_NAMES:
            raise _fail(
                "job-runner-credential-env-leak",
                f"builder env 白名單命中憑證／注入形狀變數: {key}",
                source=source,
                variable=key,
            )
        if "\n" in value or "\0" in value:
            raise _fail(
                "job-runner-env-value-invalid",
                f"builder env 值含換行或 NUL，無法安全交給 systemd --setenv: {key}",
                source=source,
                variable=key,
            )


#: `resolve_job_path()` 的錯誤訊息要指出「去哪一份 unit 取正規值」——角色與
#: `trust_root unit` 旗標的對應（與 `permgen.JOB_UNIT_CLI_FLAG` 成對契約，由
#: `tests/test_job_path_fail_closed_679.py` 釘住兩邊一致）。
_UNIT_FLAG_HINT: Mapping[str, str] = MappingProxyType(
    {
        JOB_ROLE_BUILDER: "--job",
        JOB_ROLE_REVIEW: "--review-job",
        JOB_ROLE_GATE: "--gate-job",
    }
)


def resolve_job_path(manager_env: Mapping[str, str], *, role: str = JOB_ROLE_BUILDER) -> str:
    """解析本角色的 job `PATH`。**未宣告即 fail-closed，絕不省略、絕不猜預設。**

    ## 為什麼是 raise 而不是「退回一份預設值」（#679 裁決 (a)）

    在此之前這裡是 fail-open：

        path_override = (manager_env.get(config.path_env) or "").strip()
        if path_override:
            env["PATH"] = path_override      # 沒設就整個不寫 PATH

    而 job spec 的 `env` 就是 job 的**完整**環境（shim 以
    `os.execvpe(command[0], command, spec["env"])` 整份換掉），少了 `PATH` 這個鍵
    不是「用系統預設」，是 `execvpe` 退回 `os.defpath`＝`:/bin:/usr/bin`。實機後果：
    `claude`／`agy` rc=127（只存在於 toolchain），而 `codex` **靜默**解到
    `/usr/bin/codex`——系統層 0.42.0，toolchain 那份是 0.147.0。不失敗、不報錯，
    只是每一筆產出都來自一支 operator 從未判讀過的 CLI。

    退回「permgen 導出的預設」（#679 的選項 (b)）看起來溫和，但那正是本 repo 已經
    否決過的形態：#453「registry 永不寫入預設值」。一個沒有宣告 `PSC_*_PATH` 的部署
    ＝operator 沒有對「job 解哪一份 CLI」做過決定，而那是**必須有人做**的決定；
    替他做一次、只在 spec 上留一行痕跡，等於把「未宣告」與「宣告成這樣」壓成同一種
    狀態，下一次漂移一樣看不見。

    **升級既有部署會痛，而那是對的**：現況是靜默跑錯版本，改完之後是下一次派工當場
    以可讀理由失敗。runbook 第 5-5 步有逐字的補宣告步驟。
    """

    config = resolve_job_role(role)
    value = (manager_env.get(config.path_env) or "").strip()
    if not value:
        raise _fail(
            "job-runner-path-undeclared",
            (
                f"{config.path_env} 未宣告——{config.role_id} job 會拿不到 PATH。"
                "job spec 的 env 就是 job 的完整環境（shim 以 execvpe 整份換掉），"
                "少了 PATH 不是「用系統預設」而是退回 os.defpath（:/bin:/usr/bin）："
                "toolchain 裡的 CLI 一律 rc=127，而系統層同名的舊版本會被**靜默**解到。"
                "請在 Manager 的 root-owned EnvironmentFile 宣告（值由產生器導出，"
                "不要手打）：`python3 -m paulsha_cortex.trust_root unit four-way "
                f"{_UNIT_FLAG_HINT.get(config.role_id, '--job')} | grep '^Environment=PATH='`"
            ),
            source="resolve_job_path",
            role=config.role_id,
            variable=config.path_env,
        )
    return value


def build_job_env(
    *,
    manager_env: Mapping[str, str],
    job_id: str,
    slice_id: str,
    repo_root: str,
    relay_target: str | None = None,
    role: str = JOB_ROLE_BUILDER,
) -> dict[str, str]:
    """算出降權 job unit 的**完整**環境（白名單，非黑名單 scrub）。

    回傳值就是會逐項變成 `--setenv=`／spec `env` 的內容——沒列在這裡的名字不會出現
    在 job 的環境裡，因為 transient／模板 unit 本來就不繼承呼叫端的 environ。

    **白名單本身不分角色**（#615）：`BUILDER_FORWARDED_ENV` 的判準是「缺了它模型
    CLI 或 wrapper 會直接失敗，且它本身不是憑證」——那對 reviewer 與 builder 逐條
    相同。角色只決定 `PATH`／`HOME` 的**覆寫變數名**（見 :data:`JOB_ROLE_CONFIG`）：
    兩個帳號的 HOME 與 toolchain 可見性不同，共用一個 `PSC_BUILDER_PATH` 會讓
    reviewer 的 PATH 只能跟著 builder 走。

    **`PATH` 是必要鍵，不是選配**（#679）：未宣告 `PSC_*_PATH` 時
    :func:`resolve_job_path` 直接 raise。`HOME` 仍是選配，兩者的差別是實質的——
    `HOME` 未給時 systemd 依 passwd 填入該帳號自己的正確值（而且模板 unit 另有一行
    `Environment=HOME=`），`PATH` 未給時沒有任何一層會填出正確值。
    """

    config = resolve_job_role(role)
    env: dict[str, str] = {}
    for forwarded in BUILDER_FORWARDED_ENV:
        value = manager_env.get(forwarded.name)
        if value:
            env[forwarded.name] = value
    env["PATH"] = resolve_job_path(manager_env, role=config.role_id)
    home = (manager_env.get(config.home_env) or "").strip()
    if home:
        env["HOME"] = home
    env["PSC_SLICE_ID"] = slice_id
    env["PSC_JOB_ID"] = job_id
    env["PSC_REPO_ROOT"] = repo_root
    if relay_target is not None:
        env["PSC_RELAY_TARGET"] = relay_target
    if config.role_id == JOB_ROLE_GATE:
        env.update(gate_declaration_env(manager_env))
    _reject_unsafe(env, source="build_job_env")
    return env


#: gate 角色**額外**轉發的變數（#629）：operator 的 gate 宣告本身。
#:
#: 它們不在 `BUILDER_FORWARDED_ENV` 裡，因為對模型 job 而言那是純多餘的暴露面
#: （builder 不該知道自己等一下會被哪些命令驗——#606 的 scope 紀律靠 prompt 給，
#: 不靠 env）。對 gate 而言它們**就是工作內容**：`gate_ledger.load_gate_specs()`
#: 只認 `PSC_GATE_CMD_*`，讓 Manager 改用 argv 傳命令會多出第二份真實來源。
GATE_DECLARATION_ENV_PREFIX = "PSC_GATE_CMD_"
GATE_DECLARATION_ENV_NAMES = ("PSC_GATE_TIMEOUT",)


def gate_declaration_env(manager_env: Mapping[str, str]) -> dict[str, str]:
    """從 Manager env 取出要轉發給 gate 的宣告（`PSC_GATE_CMD_*` ＋ 逾時）。

    刻意只認**前綴 ＋ 具名白名單**，不認任何 `PSC_GATE_*`：`PSC_GATE_ACCOUNT`／
    `PSC_GATE_HARDENING_PROFILE`／`PSC_GATE_JOB_TEMPLATE_UNIT` 都是**身分與加固**
    的設定，轉發進 job 的環境等於把「這個 job 該以什麼形態跑」放進它自己看得到、
    未來也可能被誰讀去用的地方。gate 需要知道的只有「要跑哪些命令、逾時多久」。
    """

    forwarded: dict[str, str] = {}
    for name in sorted(manager_env):
        if name.startswith(GATE_DECLARATION_ENV_PREFIX) or name in GATE_DECLARATION_ENV_NAMES:
            value = manager_env.get(name)
            if value:
                forwarded[name] = str(value)
    return forwarded


def build_builder_env(
    *,
    manager_env: Mapping[str, str],
    job_id: str,
    slice_id: str,
    repo_root: str,
    relay_target: str | None = None,
) -> dict[str, str]:
    """builder 角色的具名別名（既有呼叫端與測試直接用它）。"""

    return build_job_env(
        manager_env=manager_env,
        job_id=job_id,
        slice_id=slice_id,
        repo_root=repo_root,
        relay_target=relay_target,
        role=JOB_ROLE_BUILDER,
    )


# ---------------------------------------------------------------------------
# transient unit
# ---------------------------------------------------------------------------

def transient_unit_name(job_id: str) -> str:
    """`cortex-job-<可辨識片段>-<job_id 雜湊>.service`。

    兩個需求同時滿足：
    - **可追蹤**：unit 名帶得出 job_id 的可讀片段，`systemctl list-units` 與 journal
      能一眼對回 registry 的 job。
    - **唯一且合法**：job_id 不保證只含 systemd unit 名允許的字元，直接消毒又可能
      讓兩個不同 job_id 撞成同一個 unit（第二個會起不來）。因此固定接上 job_id 的
      sha256 前 8 碼——消毒後相同也不會碰撞。
    """

    raw = str(job_id).strip()
    if not raw:
        raise _fail(
            "job-runner-unit-name-invalid",
            "job_id 為空，無法組出可追蹤的 transient unit 名",
            source="transient_unit_name",
        )
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", raw).strip("-")[:64]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    if not slug:
        slug = "job"
    return f"{UNIT_NAME_PREFIX}{slug}-{digest}.service"


def build_systemd_run_argv(
    *,
    systemd_run: str,
    unit: str,
    account: str,
    group: str,
    working_directory: str,
    env: Mapping[str, str],
    command: Sequence[str],
) -> list[str]:
    """組出降權 argv。

    旗標逐項理由（同時是 Phase 2b polkit 規則要收窄的那個面）：

    - `--quiet`：不要把 "Running as unit: …" 寫進 JSONL log（terminal evidence 來源）。
    - `--collect`：unit 結束即卸載，不留 failed unit 殘骸讓下一次同名 job 起不來。
    - `--pipe`：把 job 的 stdout/stderr 接回 launcher 開的 log fd，沿用既有 harvest。
    - `--wait`：client 存活到 unit 結束，`dispatcher.pid_alive()` 的 pid 判活仍成立。
    - `--unit=`：可追蹤，且是 polkit `action.lookup("unit")` 收窄授權的比對對象。
    - `--uid=`／`--gid=`：降權本身（等價於 `--property=User=`／`Group=`）。
    - `--service-type=exec`：exec 失敗（CLI 不在 PATH、帳號無法切換）會在**起動**階段
      就報錯，而不是先報成功再讓 job 靜默死掉。
    - `--working-directory=`：對齊 direct 模式的 `Popen(cwd=worktree)`。刻意不用
      `--same-dir`——Manager 的 cwd 不是 worktree。
    - `--setenv=`：白名單 env 逐項送入（排序後輸出，argv 因此可預期、可比對、可稽核）。
    """

    argv = [
        systemd_run,
        "--quiet",
        "--collect",
        "--pipe",
        "--wait",
        f"--unit={unit}",
        f"--uid={account}",
        f"--gid={group}",
        "--service-type=exec",
        f"--working-directory={working_directory}",
    ]
    argv.extend(f"--property={prop}" for prop in TRANSIENT_UNIT_PROPERTIES)
    argv.extend(f"--setenv={key}={env[key]}" for key in sorted(env))
    argv.append("--")
    argv.extend(command)
    return argv


def preflight_systemd_run(
    *,
    account: str,
    group: str,
    # 預設值刻意是 None 而不是 `shutil.which`：後者會在 **def 時**就把函式物件綁進
    # 預設值，`mock.patch.object(job_runner.shutil, "which", …)` 因此打不到它——
    # 測試會靜默地驗到「真實主機上有沒有 systemd-run」而不是它想驗的分支。
    which: Callable[[str], str | None] | None = None,
    account_exists: Callable[[str], bool] | None = None,
    group_exists: Callable[[str], bool] | None = None,
    systemd_booted: Callable[[], bool] | None = None,
) -> str:
    """降權前的靜態檢查；任何一項不成立即 fail-closed，回傳 systemd-run 絕對路徑。

    **絕不退回 direct**：這裡每一個 raise 都代表「operator 以為隔離生效、實際不生效」
    的風險，靜默降級正是本票要消除的失效模式。

    polkit 拒絕沒有可靠的唯讀探測面（systemd 沒有 dry-run／can-start 介面），它由
    :func:`confirm_transient_unit_started` 在起動階段補上。
    """

    resolved = (which or shutil.which)("systemd-run")
    if not resolved:
        raise _fail(
            "job-runner-systemd-run-missing",
            "PATH 上找不到 systemd-run；降權模式無法執行",
            source="preflight_systemd_run",
        )
    booted = systemd_booted or _systemd_booted
    if not booted():
        raise _fail(
            "job-runner-systemd-unavailable",
            "/run/systemd/system 不存在——本機未以 systemd 開機，transient unit 不可用",
            source="preflight_systemd_run",
        )
    exists_account = account_exists or _account_exists
    if not exists_account(account):
        raise _fail(
            "job-runner-builder-account-missing",
            f"job 帳號不存在: {account}（Phase 2b runbook 第 1 步尚未執行？）",
            source="preflight_systemd_run",
            account=account,
        )
    exists_group = group_exists or _group_exists
    if not exists_group(group):
        raise _fail(
            "job-runner-builder-group-missing",
            f"job group 不存在: {group}",
            source="preflight_systemd_run",
            group=group,
        )
    return resolved


@dataclass(frozen=True)
class SystemdRunPlan:
    """一次降權派工要用到的、**已驗證過**的身分與 unit 資訊。

    把「解析 config → 靜態 preflight → 算 unit 名」收成一個回傳值，呼叫端就不必在
    launch 的主流程裡拿著四個 `str | None` 再逐一判斷（也就不需要 `assert` 收窄型別）。
    """

    binary: str
    unit: str
    account: str
    group: str
    #: 本次派工的降權角色（#615）。留在 plan 上供診斷／稽核。
    role: str = JOB_ROLE_BUILDER


def prepare_systemd_run(
    env: Mapping[str, str], *, job_id: str, role: str = JOB_ROLE_BUILDER
) -> SystemdRunPlan:
    """降權派工的前置：解析身分 config、跑靜態 preflight、算出 transient unit 名。

    **在任何副作用之前呼叫**——這裡的每一個 raise 都代表本次派工不該發生，呼叫端
    必須讓它往上傳（fail-closed），不得改走 direct。
    """

    account = resolve_job_account(env, role=role)
    group = resolve_job_group(env, role=role)
    binary = preflight_systemd_run(account=account, group=group)
    return SystemdRunPlan(
        binary=binary,
        unit=transient_unit_name(job_id),
        account=account,
        group=group,
        role=resolve_job_role(role).role_id,
    )


def confirm_transient_unit_started(
    *,
    process,
    sentinel: str,
    unit: str,
    account: str,
    log_path: str | None = None,
    timeout_ms: int = DEFAULT_START_TIMEOUT_MS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    manager_authored_sentinel: bool = False,
) -> None:
    """確認 transient unit 真的起來了；起不來就 fail-closed。

    polkit 拒絕、unit 名衝突、`--uid` 帳號在 systemd 端被拒——這些都只在**起動當下**
    才知道，而 `systemd-run` 在這些情況下會**立刻**以非零 exit 收場。

    判準刻意是**兩個條件同時成立**才算失敗：``systemd-run 已結束`` **且**
    ``exit sentinel 不存在``。sentinel 由 wrapper 在模型結束後寫入
    （見 `launcher.build_wrapper_script`），所以一個真的跑完的極短命 job 必然留下
    sentinel——不會被誤判。反之「行程沒了、sentinel 也沒有」就是 job 從未真正執行。

    成功路徑會等滿整個確認窗（預設 200ms）才返回；這是為了讓 polkit 的 D-Bus 往返有
    時間收斂，代價是每次 builder 派工多 200ms，相對於一次 headless job 可忽略。
    """

    status = _await_start(
        process=process,
        sentinel=sentinel,
        timeout_ms=timeout_ms,
        monotonic=monotonic,
        sleep=sleep,
        manager_authored_sentinel=manager_authored_sentinel,
    )
    if status is None:
        return
    raise _fail(
        "job-runner-transient-unit-start-failed",
        (
            f"transient unit {unit} 未能以 {account} 起動"
            f"（systemd-run exit={status}；常見原因：polkit 拒絕、"
            f"unit 名衝突、帳號無法切換）{_log_tail(log_path)}"
        ),
        source="confirm_transient_unit_started",
        unit=unit,
        account=account,
        exit_status=status,
    )


def _await_start(
    *,
    process,
    sentinel: str,
    timeout_ms: int,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    manager_authored_sentinel: bool = False,
) -> int | None:
    """起動確認的共用核心：回傳「起動失敗時的 client exit status」，成功回 None。

    判準對 `systemd-run --wait` 與 `systemctl start --wait` 完全一樣（兩者都是
    「client 存活到 unit 結束」的語意）：**client 已結束且 exit sentinel 不存在**
    才算起動失敗。真的跑完的極短命 job 必然留下 sentinel，因此不會誤判。

    ``manager_authored_sentinel``（#604）：sentinel 改由 Manager 側的 exit 記帳
    wrapper 寫入（:func:`build_manager_exit_recorder_argv`）之後，「sentinel 存在」
    不再能區分兩件事——**那層 wrapper 在 client 起不來時也會寫**（它記的正是那個
    非零狀態）。因此這條路徑改用單一判準：**確認窗內 client 已結束且狀態非 0**。
    這在語意上成立，因為 `--wait` 的 client 只有在 unit 已經跑完之後才會返回，而
    unit 啟動（systemd 排程 → 降權 → shim 讀 spec → exec 模型 CLI）不可能在預設
    200ms 的窗內走完；反之 polkit 拒絕／模板未安裝是**立刻**回非 0。極端情況下若
    真有一個 job 在窗內就以非零收場，結果是**拒絕這次派工**（fail-closed，可重派），
    不是採信一個沒跑過的 job。
    """

    deadline = monotonic() + max(timeout_ms, 0) / 1000.0
    while True:
        status = process.poll()
        if status is not None:
            if manager_authored_sentinel:
                # Manager 記帳模式：sentinel 一定會被寫，故不能拿它當判準。
                return status if status != 0 else None
            if Path(sentinel).exists():
                # job 真的跑完了（且已寫下 exit sentinel）——不是起動失敗。
                return None
            return status
        remaining = deadline - monotonic()
        if remaining <= 0:
            return None
        sleep(min(0.01, remaining))


# ---------------------------------------------------------------------------
# 模板實例模式（B 案）
# ---------------------------------------------------------------------------

def instance_name_valid(name: str) -> bool:
    """instance 名是否符合 :data:`INSTANCE_NAME_RE`（shim 端共用同一條判準）。"""

    return job_workspace.job_segment_valid(name)


def template_instance_id(job_id: str) -> str:
    """job_id → systemd 模板實例名（`cortex-job@<這個>.service`）。

    **推導本身不在這裡**：唯一推導點是 :func:`job_workspace.job_segment`，因為同一個
    字串同時是 job 工作區在 pool 底下的目錄名——模板 unit 只有 `%i` 可用，
    `ReadWritePaths=<pool>/%i` 因此把「instance 名」與「目錄名」綁成同一個東西
    （#645）。本函式只負責把那裡的錯誤翻成 `job_runner` 的診斷契約。

    與 :func:`transient_unit_name` 同一套「可追蹤 ＋ 唯一」的形狀（消毒後的可讀片段
    ＋ job_id 的 sha256 前 8 碼），差別只在這裡產出的是**實例名**而不是完整 unit 名。
    """

    try:
        return job_workspace.job_segment(job_id)
    except job_workspace.WorkspaceError as exc:
        raise _fail(
            "job-runner-instance-name-invalid",
            f"無法由 job_id 導出模板實例名: {exc}",
            source="template_instance_id",
            job_id=str(job_id).strip(),
        ) from exc


def template_unit_name(instance: str, *, template: str = DEFAULT_TEMPLATE_UNIT) -> str:
    """`cortex-job@.service` ＋ instance → `cortex-job@<instance>.service`。

    模板名從 config 來（`PSC_JOB_TEMPLATE_UNIT`），因此必須現場驗形狀：值只要不是
    `<前綴>@.service` 就 fail-closed——一個打錯的模板名會讓 polkit 全數拒絕，那時
    的錯誤訊息（「Access denied」）完全指不出真正的原因。
    """

    if not template.endswith(f"@{TEMPLATE_UNIT_SUFFIX}"):
        raise _fail(
            "job-runner-template-unit-invalid",
            f"{TEMPLATE_UNIT_ENV} 必須是 `<name>@{TEMPLATE_UNIT_SUFFIX}` 形狀，收到 {template!r}",
            source="template_unit_name",
            requested=template,
        )
    if not instance_name_valid(instance):
        raise _fail(
            "job-runner-instance-name-invalid",
            f"模板實例名不合法（只允許 [A-Za-z0-9_.-]，首字元須為英數）: {instance!r}",
            source="template_unit_name",
            requested=instance,
        )
    stem = template[: -len(TEMPLATE_UNIT_SUFFIX)]  # `cortex-job@`
    return f"{stem}{instance}{TEMPLATE_UNIT_SUFFIX}"


def resolve_hardening_profile(executor: str) -> str:
    """executor → 加固剖面 id。**未知 executor 一律 fail-closed。**

    fail-closed 的方向是本函式的全部重點：這裡**不是**「不確定就給嚴格的」，而是
    「不確定就拒絕」。

    - 若預設落到**寬鬆**那份，整個 per-executor 設計當場退化成「全域移除 MDWE」——
      任何未登記的名字都能拿到放寬的 unit。
    - 若預設落到**嚴格**那份，一個未被盤點過的 node 型 CLI 會在真實加固面下靜默
      起不來（症狀是空輸出，離原因很遠——#643 就是這樣被埋掉半個 milestone 的）。

    因此唯一正確的行為是要求它先被登記進 `permgen.EXECUTOR_TOOLS`（並標明
    `needs_node`），再同步到本檔的 :data:`EXECUTOR_HARDENING_PROFILE`。
    """

    name = str(executor or "").strip()
    profile = EXECUTOR_HARDENING_PROFILE.get(name)
    if profile is None:
        raise _fail(
            "job-runner-hardening-profile-unknown",
            (
                f"未知的 executor {executor!r}，無法決定加固剖面（已登記："
                f"{sorted(EXECUTOR_HARDENING_PROFILE)}）。新增 executor 必須先進 "
                "trust_root.permgen.EXECUTOR_TOOLS 標明 needs_node，再同步到 "
                "job_runner.EXECUTOR_HARDENING_PROFILE——剖面不得靠猜，"
                "更不得預設落到放寬的那一份。"
            ),
            source="resolve_hardening_profile",
            executor=name,
        )
    return profile


#: gate 角色的加固剖面覆寫（#629）。**operator 平面的決定**，不是 job 平面的。
#:
#: #643 對模型 job 立下的紀律是「剖面由 executor 決定，config 選不了」——因為那裡
#: 存在一個 job 碰不到、又能唯一決定剖面的輸入（Manager 選的 executor）。gate 沒有
#: 那個輸入：它跑的是 operator 用 `PSC_GATE_CMD_*` **自己宣告**的命令，因此「這些
#: 命令需要哪一份剖面」與「這些命令是什麼」是同一個人在同一個平面上的決定，宣告
#: 命令卻不能宣告剖面才是不一致的。
#:
#: 預設 `strict`：多數宣告的 gate 是 `pytest`／`make`／原生 ELF，它們在
#: `MemoryDenyWriteExecute=yes` 下正常。宣告 node 型 gate（`npm test`）的部署必須
#: 顯式打出 `jit`——**不顯式就會壞掉，而且壞得看得見**（V8 直接崩，見 #643），
#: 這比「不確定就給寬鬆的」正確：後者等於所有部署都少一層加固。
GATE_HARDENING_PROFILE_ENV = "PSC_GATE_HARDENING_PROFILE"
DEFAULT_GATE_HARDENING_PROFILE = HARDENING_PROFILE_STRICT


def resolve_gate_hardening_profile(env: Mapping[str, str]) -> str:
    """gate 角色的加固剖面 id；未設＝`strict`，值不合法即 fail-closed。

    不合法時**不落回預設**：一個打錯的剖面名（`jti`）落回 strict 會讓 operator
    以為自己開了 jit 卻沒有，症狀是 node 型 gate 全崩而設定看起來是對的（#643
    本身就是這樣被埋掉的）。
    """

    raw = str(env.get(GATE_HARDENING_PROFILE_ENV, "") or "").strip()
    if not raw:
        return DEFAULT_GATE_HARDENING_PROFILE
    if raw not in TEMPLATE_UNIT_SUFFIX_BY_PROFILE:
        raise _fail(
            "job-runner-hardening-profile-unknown",
            (
                f"{GATE_HARDENING_PROFILE_ENV} 只接受 "
                f"{sorted(TEMPLATE_UNIT_SUFFIX_BY_PROFILE)}，收到 {raw!r}"
            ),
            source="resolve_gate_hardening_profile",
            requested=raw,
        )
    return raw


def template_unit_for_profile(template: str, profile: str) -> str:
    """基底模板名 ＋ 剖面 → 該剖面的模板名（`cortex-job@.service` → `cortex-job-jit@.service`）。

    `template` 必須是**基底**（strict）名。config（`PSC_JOB_TEMPLATE_UNIT`）若已經被
    設成某個剖面的名字，這裡 fail-closed 而不是再疊一層後綴——`cortex-job-jit-jit@`
    這種名字會被 polkit 拒掉，而那時的錯誤訊息（Access denied）指不出真正的原因。
    """

    if not template.endswith(f"@{TEMPLATE_UNIT_SUFFIX}"):
        raise _fail(
            "job-runner-template-unit-invalid",
            f"{TEMPLATE_UNIT_ENV} 必須是 `<name>@{TEMPLATE_UNIT_SUFFIX}` 形狀，收到 {template!r}",
            source="template_unit_for_profile",
            requested=template,
        )
    suffix = TEMPLATE_UNIT_SUFFIX_BY_PROFILE.get(profile)
    if suffix is None:
        raise _fail(
            "job-runner-hardening-profile-unknown",
            f"未知的加固剖面 {profile!r}（已登記：{sorted(TEMPLATE_UNIT_SUFFIX_BY_PROFILE)}）",
            source="template_unit_for_profile",
            requested=str(profile),
        )
    stem = template[: -len(f"@{TEMPLATE_UNIT_SUFFIX}")]
    for known in TEMPLATE_UNIT_SUFFIX_BY_PROFILE.values():
        if known and stem.endswith(known):
            raise _fail(
                "job-runner-template-unit-invalid",
                (
                    f"{TEMPLATE_UNIT_ENV} 必須是**基底**模板名（strict 剖面），"
                    f"收到已帶剖面後綴的 {template!r}；剖面由 executor 決定並在此處"
                    "自動套用，不該由 config 預先寫死。"
                ),
                source="template_unit_for_profile",
                requested=template,
            )
    return f"{stem}{suffix}@{TEMPLATE_UNIT_SUFFIX}"


def resolve_template_unit(env: Mapping[str, str], *, role: str = JOB_ROLE_BUILDER) -> str:
    """該角色的**基底**模板 unit 名（未套加固剖面後綴前）。"""

    config = resolve_job_role(role)
    return (env.get(config.template_env) or "").strip() or config.default_template


def resolve_job_spec_spool(env: Mapping[str, str], *, role: str = JOB_ROLE_BUILDER) -> str:
    """該角色**自己的** spec spool（#657；由 :data:`JOB_ROLE_CONFIG` 查表）。

    `role` 的預設值是 builder，與 :func:`resolve_job_account` 等同族函式一致——理由也
    一樣：忘了傳 `role` 的後果是「reviewer 的 spec 被寫進 builder 的 spool」，那個 job
    起不來（它的 unit 讀的是 reviewer 的 spool），而**起不來是安全的失敗方向**；反過來
    若讓寫端落回一個共用目錄，才會回到 #657 那個「看起來成功、實際上每個 job 78/CONFIG」
    的狀態。
    """

    config = resolve_job_role(role)
    return (env.get(config.spec_spool_env) or "").strip() or config.default_spec_spool


def resolve_job_shim(env: Mapping[str, str]) -> str:
    return (env.get(JOB_SHIM_ENV) or "").strip() or DEFAULT_JOB_SHIM


def job_spec_path(spool_dir: str, instance: str) -> str:
    """`<spool>/<instance>.json`——與 `job_shim.resolve_spec_path()` 同一條推導。"""

    return f"{spool_dir.rstrip('/')}/{instance}.json"


def forbidden_spec_keys(spec: Mapping[str, object]) -> list[str]:
    """spec 內出現的 :data:`SPEC_FORBIDDEN_KEYS`（排序後）。空 list＝乾淨。

    **寫端（`build_job_spec`）與讀端（`job_shim.load_spec`）呼叫的是這同一支**，
    兩邊只在「raise 哪一種例外」上不同。抽出來不是為了少寫一行，而是為了讓「兩端
    掃的是同一份判準」成為結構事實而非約定——這條在 #643 之後承載的東西更多了
    （不只身分，還有加固剖面）。
    """

    return sorted(key for key in SPEC_FORBIDDEN_KEYS if key in spec)


def build_job_spec(
    *,
    job_id: str,
    instance: str,
    unit: str,
    command: Sequence[str],
    working_directory: str,
    log_path: str,
    env: Mapping[str, str],
) -> dict[str, object]:
    """組出 per-job spec（**不含任何身分欄位**）。純資料，無 IO。

    「User 不在 spec 裡」是本模式全部的價值：身分只有一個來源＝root-owned unit
    檔的 `User=`。因此這裡除了不放，還在寫端主動掃一次 forbidden key（未來有人
    往 spec 加欄位時測試會紅），讀端（shim）再掃一次。
    """

    argv = [str(item) for item in command]
    if not argv or not all(argv):
        raise _fail(
            "job-runner-job-spec-invalid",
            "spec 的 command 不得為空、且每個元素都必須是非空字串",
            source="build_job_spec",
            instance=instance,
        )
    for label, value in (("working_directory", working_directory), ("log_path", log_path)):
        if not str(value).startswith("/"):
            raise _fail(
                "job-runner-job-spec-invalid",
                f"spec 的 {label} 必須是絕對路徑，收到 {value!r}",
                source="build_job_spec",
                instance=instance,
            )
    # env 走與 systemd-run 模式**同一條**守衛：模式換了，token 不得出去這件事不換。
    _reject_unsafe(env, source="build_job_spec")
    spec: dict[str, object] = {
        "spec_version": JOB_SPEC_VERSION,
        "instance": instance,
        "job_id": str(job_id),
        "unit": unit,
        "command": argv,
        "working_directory": str(working_directory),
        "log_path": str(log_path),
        "env": {str(k): str(v) for k, v in sorted(env.items())},
    }
    leaked = forbidden_spec_keys(spec)
    if leaked:
        raise _fail(
            "job-runner-job-spec-invalid",
            f"spec 不得攜帶身分／加固剖面欄位 {leaked}——身分只由 root-owned unit 的 "
            "User= 決定，剖面只由 executor 決定（#643）",
            source="build_job_spec",
            instance=instance,
        )
    return spec


def write_job_spec(
    spec_path: str, spec: Mapping[str, object], *, account: str | None = None
) -> str:
    """把 spec **原子**寫進 Manager-owned spool；失敗即 fail-closed。

    原子性（同目錄 temp ＋ `os.replace`）不是潔癖：spec 是 job 的命令列，一個被
    讀到一半的檔會變成「執行了半條命令」。`os.replace` 在同一個檔案系統上是
    rename(2)，讀端只會看到舊的或新的完整內容。

    mode 明確設 `0o640` 而不是靠 umask：Manager unit 的 `UMask=0077` 會讓新檔出生
    即 `0600`，而 group 位全關會**連帶把繼承下來的 ACL mask 也關掉**，job 帳號的
    唯讀 ACL 因此形同虛設（讀 spec 會 EACCES）。group 仍是 Manager 自己的 group，
    所以放寬 group-read 不會讓第三方讀到。

    **`account`（#657）**：落地之後就地複驗「那個身分讀得到這個檔」。preflight 算的
    是 spool 目錄的 default ACL（spec 當時還不存在，那是唯一能算的東西）；這裡算的是
    **真的那個 inode**，把上面那段 `chmod 0640` ⟷ ACL mask 的推導從註解升級成斷言。
    `account=None` 或帳號不在 passwd 時略過——那兩種情形在正式派工路徑上不可達
    （`prepare_systemd_template()` 已對帳號 fail-closed），只出現在直接呼叫本函式的
    測試裡。
    """

    directory = os.path.dirname(spec_path) or "."
    payload = json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".job-spec-", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o640)
        os.replace(tmp_path, spec_path)
        tmp_path = None
    except OSError as exc:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise _fail(
            "job-runner-job-spec-write-failed",
            f"寫不進 job spec {spec_path}: {exc}（spool 是否已由 Phase 2b 建立？）",
            source="write_job_spec",
            spec_path=spec_path,
        ) from exc
    if account:
        ok, why = _spec_readable_by(spec_path, account)
        if not ok:
            raise _fail(
                "job-runner-job-spec-unreadable-by-job",
                (
                    f"spec 已落地但 {account} 讀不到它: {why}。shim 會在 systemd 套完 "
                    "User= 之後以該身分讀這個檔，因此本次派工必定以 78/CONFIG 收場"
                    "——在這裡擋掉，而不是讓它變成 journal 裡的一行（#657）。"
                ),
                source="write_job_spec",
                spec_path=spec_path,
                account=account,
            )
    return spec_path


def build_systemctl_start_argv(*, systemctl: str, unit: str) -> list[str]:
    """組出模板實例的啟動 argv。**封閉：除了 unit 名沒有任何可變輸入。**

    逐項理由：

    - `start`：polkit 規則只放行 `start`／`stop` 兩個 verb。
    - `--wait`：client 存活到 unit 結束（`systemctl(1)`「For (re)start, wait until
      service stopped again」，systemd ≥ 232），`dispatcher.pid_alive()` 的 pid 判活
      因此與 direct／systemd-run 模式同語意，dispatcher 零改動。
    - `--no-ask-password`：headless daemon 沒有 tty；缺這個旗標時 polkit 若要互動
      認證，`systemctl` 會**卡住**而不是失敗——fail-closed 的前提是會失敗。
    - `--no-block` **刻意不用**：那會讓 client 在 job 排入佇列後立刻返回，pid 判活
      當場失效。
    - **沒有** `--property=`／`--uid=`：本模式的整個重點就是這些東西給不了。
    """

    return [systemctl, "start", "--wait", "--no-ask-password", unit]


def build_manager_exit_recorder_argv(
    *, client_argv: Sequence[str], sentinel: str
) -> list[str]:
    """把降權啟動器的 client argv 包進一層 **Manager 身分**的 exit 記帳 shell。

    #604：exit sentinel 過去由 job 進程內的 wrapper script 寫（`launcher.
    build_wrapper_script` 的第 2 段）。OS 隔離上線後這條路同時有兩個問題：

    1. **信任面**：sentinel 是 `dispatcher.poll_headless_done` 的第一判準，卻由被
       隔離的一方自報。builder 只要寫得進去就能宣告自己的 exit code。
    2. **可行性**：登記表資產 `gate-ledger`（＝Manager 的 dispatch log 目錄）在
       Phase 2b 是 `0700 cortex-manager`，且**不在** job 模板 unit 的
       `ReadWritePaths=` 內（`ProtectSystem=strict` → EROFS）。job 根本寫不進去，
       於是每個降權 job 都在 `poll_headless_done` 落到「行程已死、無 sentinel」的
       fail-closed 分支，被記成 failed。

    修法：`systemd-run --wait` 與 `systemctl start --wait` 的 client 本來就跑在
    **Manager 這一側**（見 `launcher.launch()` 的 `popen_kwargs["env"]`），且會存活
    到 unit 結束。把它包進一層 `bash -c`，由這層 shell 寫下 client 的 `$?`——寫者
    因此是 Manager 的 uid，落點仍是 Manager-owned 的 log 目錄，job 側完全不參與。

    `exit "$rc"`：把 client 的狀態原樣傳回給 `Popen` 物件，讓
    :func:`_await_start` 仍拿得到「client 起不來時的 exit status」。

    退出碼語意：`systemctl start --wait` 在 unit 成功結束時回 0、unit 失敗（或
    根本起不來）時回非 0。`completion.classify_completion` 只分「0 / 非 0」，因此
    這個粒度足夠；模型的逐字 exit code 本來就不是採信判準（採信走 gate ledger）。
    """

    if not client_argv or not all(isinstance(item, str) and item for item in client_argv):
        raise _fail(
            "job-runner-exit-recorder-invalid",
            "exit 記帳 wrapper 需要非空的 client argv",
            source="build_manager_exit_recorder_argv",
        )
    if not sentinel.startswith("/"):
        raise _fail(
            "job-runner-exit-recorder-invalid",
            f"exit sentinel 必須是絕對路徑（Manager 的 log 目錄）: {sentinel!r}",
            source="build_manager_exit_recorder_argv",
            sentinel=sentinel,
        )
    script = (
        f"{shlex.join(list(client_argv))}; rc=$?; "
        f"printf %s \"$rc\" > {shlex.quote(sentinel)}; exit \"$rc\""
    )
    # `-c` 而非 `-lc`：這層 shell 是 Manager 的一部分，不該重新 source ~/.profile。
    return ["bash", "-c", script]


def preflight_systemd_template(
    *,
    account: str,
    group: str,
    template_unit: str,
    shim: str,
    spool_dir: str,
    which: Callable[[str], str | None] | None = None,
    account_exists: Callable[[str], bool] | None = None,
    group_exists: Callable[[str], bool] | None = None,
    systemd_booted: Callable[[], bool] | None = None,
    unit_file_installed: Callable[[str], bool] | None = None,
    executable: Callable[[str], bool] | None = None,
    directory_exists: Callable[[str], bool] | None = None,
    spool_readable: Callable[[str, str], tuple[bool, str]] | None = None,
) -> str:
    """模板模式的靜態檢查；任一項不成立即 fail-closed，回傳 systemctl 絕對路徑。

    比 A 案多三條，對應「本模式生效需要 Phase 2b 安裝」的三個前置物：模板 unit 檔、
    root-owned shim、Manager-owned spec spool。**任何一條都不會退回其他模式**——
    「以為降權生效但其實沒有」正是這整條票要消除的失效模式。

    **spool 那一條在 #657 之後檢查的是「`account` 讀得到」而不是「目錄存在」。**
    舊的 `os.path.isdir()` 對 #657 完全無感：那台實機上 spool 目錄存在、Manager 也
    寫得進去，缺的只是 `cortex-gate` 的 ACL——於是 preflight 綠、`systemctl start`
    回 0、job 在 shim 讀 spec 時才以 `78/CONFIG` 死掉，逐字原因只在 journal 裡。
    現在改以 :func:`_spool_readable_by` 算該帳號的 **effective** 權限（見該函式的
    誠實邊界），失敗點因此回到「派工之前、Manager 自己的錯誤訊息裡」。

    polkit 拒絕仍然沒有可靠的唯讀探測面，由
    :func:`confirm_template_instance_started` 在起動階段補上。
    """

    resolved = (which or shutil.which)("systemctl")
    if not resolved:
        raise _fail(
            "job-runner-systemctl-missing",
            "PATH 上找不到 systemctl；模板實例模式無法執行",
            source="preflight_systemd_template",
        )
    booted = systemd_booted or _systemd_booted
    if not booted():
        raise _fail(
            "job-runner-systemd-unavailable",
            "/run/systemd/system 不存在——本機未以 systemd 開機，模板 unit 不可用",
            source="preflight_systemd_template",
        )
    exists_account = account_exists or _account_exists
    if not exists_account(account):
        raise _fail(
            "job-runner-builder-account-missing",
            f"job 帳號不存在: {account}（Phase 2b runbook 第 1 步尚未執行？）",
            source="preflight_systemd_template",
            account=account,
        )
    exists_group = group_exists or _group_exists
    if not exists_group(group):
        raise _fail(
            "job-runner-builder-group-missing",
            f"job group 不存在: {group}",
            source="preflight_systemd_template",
            group=group,
        )
    installed = unit_file_installed or _unit_file_installed
    if not installed(template_unit):
        raise _fail(
            "job-runner-job-template-missing",
            (
                f"模板 unit {template_unit} 未安裝於 {DEFAULT_TEMPLATE_UNIT_DIR}"
                "（Phase 2b：`trust_root unit --job` 的輸出尚未落地？）"
            ),
            source="preflight_systemd_template",
            unit=template_unit,
        )
    is_executable = executable or _is_executable
    if not is_executable(shim):
        raise _fail(
            "job-runner-job-shim-missing",
            (
                f"降權 shim 不存在或不可執行: {shim}"
                "（Phase 2b：`trust_root shim` 的輸出尚未落地？）"
            ),
            source="preflight_systemd_template",
            shim=shim,
        )
    has_dir = directory_exists or os.path.isdir
    if not has_dir(spool_dir):
        raise _fail(
            "job-runner-job-spec-spool-missing",
            (
                f"job spec spool 目錄不存在: {spool_dir}（#657 起每個降權角色一格；"
                "Phase 2b 權限套用尚未重跑？）"
            ),
            source="preflight_systemd_template",
            spool_dir=spool_dir,
        )
    readable = spool_readable or _spool_readable_by
    ok, why = readable(spool_dir, account)
    if not ok:
        raise _fail(
            "job-runner-job-spec-spool-unreadable",
            (
                f"job 身分 {account} 讀不到自己的 spec spool: {why}。"
                "（#657：spool 存在不等於那個身分讀得到——shim 是 systemd 套完 "
                "`User=` **之後**才執行的，它以 job 身分讀 spec。重跑 "
                "`python3 -m paulsha_cortex.trust_root permissions --commands --paths` "
                "並套用，或見 runbook 的 per-principal spool 段。）"
            ),
            source="preflight_systemd_template",
            spool_dir=spool_dir,
            account=account,
        )
    return resolved


@dataclass(frozen=True)
class SystemdTemplatePlan:
    """一次模板派工要用到的、**已驗證過**的身分／unit／路徑資訊。"""

    binary: str
    template_unit: str
    instance: str
    unit: str
    account: str
    group: str
    shim: str
    spool_dir: str
    spec_path: str
    #: 生效的加固剖面 id（#643）。由 `executor` 決定，不由 job 決定。
    hardening_profile: str = HARDENING_PROFILE_STRICT
    #: 決定剖面的 executor（Manager 的 dispatch 決定）。留在 plan 上供診斷／稽核。
    executor: str = ""
    #: config 給的**基底**模板名（未套剖面後綴前）。
    base_template_unit: str = DEFAULT_TEMPLATE_UNIT
    #: 本次派工的降權角色（#615）。決定帳號與模板，由 persona 導出、job 碰不到。
    role: str = JOB_ROLE_BUILDER


def _resolve_profile_for_role(
    env: Mapping[str, str], *, role: str, executor: str | None
) -> str:
    """該角色的加固剖面 id。**兩個角色族、兩條來源，互斥且皆 fail-closed。**

    - 模型 job（builder／review）：唯一輸入是 `executor`（#643），`None` 即拒。
    - gate（#629）：沒有 executor 可言，唯一輸入是 operator 的
      `PSC_GATE_HARDENING_PROFILE`；傳了 executor 即拒。
    """

    if role == JOB_ROLE_GATE:
        if executor:
            raise _fail(
                "job-runner-gate-executor-not-applicable",
                (
                    f"gate 角色不得帶 executor（收到 {executor!r}）——它不跑模型 CLI，"
                    f"剖面由 {GATE_HARDENING_PROFILE_ENV} 決定，不隨 Manager 的 "
                    "executor 漂移（#629）"
                ),
                source="_resolve_profile_for_role",
                requested=str(executor),
            )
        return resolve_gate_hardening_profile(env)
    if executor is None:
        raise _fail(
            "job-runner-executor-missing",
            (
                f"角色 {role!r} 的加固剖面唯一輸入是 executor，不得為 None"
                "（#643：忘了說是哪個 executor 就不該派得出去）"
            ),
            source="_resolve_profile_for_role",
            requested=role,
        )
    return resolve_hardening_profile(executor)


def prepare_systemd_template(
    env: Mapping[str, str],
    *,
    job_id: str,
    executor: str | None,
    role: str = JOB_ROLE_BUILDER,
    unit_active: Callable[[str, str], bool] | None = None,
) -> SystemdTemplatePlan:
    """模板派工的前置：解析 config、決定加固剖面、靜態 preflight、算 instance／unit／
    spec 路徑，並確認**同名 instance 沒有正在跑**。

    最後一條特別重要：`systemctl start` 對一個**已經 active** 的 unit 會直接回 0，
    什麼都不做。少了這個檢查，Manager 會以為自己起了一個 job，實際上是掛在別人的
    unit 上等——而且 `--wait` 會一路等到那個別人的 job 結束。

    `executor` 是**必填且無預設**（#643）：剖面選擇的唯一輸入就是它，而它是 Manager
    的 dispatch 決定。給預設值等於允許某條路徑「忘了說是哪個 executor」還能派出去，
    那個預設值不論指向哪一份剖面都是錯的（見 :func:`resolve_hardening_profile`）。

    `role`（#615 M2）決定**身分與模板**，由 persona 導出（`launcher` 的
    `review_only`／`read_only` 分支），同樣是 Manager 的 dispatch 決定。它的預設值
    刻意是 builder 而非「必填」——與 `executor` 不同的地方在於：忘了傳 `role` 的
    後果是「reviewer 被以 builder 帳號起跑」，那是**降權到一個一樣受限的帳號**，
    不是提權；而忘了傳 `executor` 的後果是「拿到一份不該給它的加固剖面」。前者由
    `launcher` 的單一決定點 ＋ 不變式測試守住，後者必須在型別層擋。

    **`role=gate`（#629）時 `executor` 必須是 `None`**，而且是雙向強制：gate 不跑
    任何模型 CLI，所以「哪個 executor」對它沒有意義，剖面改由
    :func:`resolve_gate_hardening_profile` 從 operator 平面取得。傳了 executor 就是
    呼叫端把 gate 當成模型 job 在派——那會讓剖面跟著 `PSC_MANAGER_EXECUTOR` 漂移，
    而 gate 跑什麼命令與 Manager 用哪個模型完全無關。反過來，模型 job 傳 `None`
    也一樣 fail-closed（那正是 #643 要擋的「忘了說是哪個 executor」）。

    **在任何副作用之前呼叫**：這裡每一個 raise 都代表本次派工不該發生。
    """

    role_config = resolve_job_role(role)
    account = resolve_job_account(env, role=role)
    group = resolve_job_group(env, role=role)
    base_template = resolve_template_unit(env, role=role)
    profile = _resolve_profile_for_role(env, role=role, executor=executor)
    template = template_unit_for_profile(base_template, profile)
    shim = resolve_job_shim(env)
    spool_dir = resolve_job_spec_spool(env, role=role)
    binary = preflight_systemd_template(
        account=account,
        group=group,
        template_unit=template,
        shim=shim,
        spool_dir=spool_dir,
    )
    instance = template_instance_id(job_id)
    unit = template_unit_name(instance, template=template)
    is_active = unit_active or _unit_is_active
    if is_active(binary, unit):
        raise _fail(
            "job-runner-template-instance-busy",
            (
                f"模板實例 {unit} 已在執行中——`systemctl start` 會靜默成功卻不起新 job。"
                "同一個 job_id 是否已有未回收的派工？"
            ),
            source="prepare_systemd_template",
            unit=unit,
            account=account,
        )
    return SystemdTemplatePlan(
        binary=binary,
        template_unit=template,
        instance=instance,
        unit=unit,
        account=account,
        group=group,
        shim=shim,
        spool_dir=spool_dir,
        spec_path=job_spec_path(spool_dir, instance),
        hardening_profile=profile,
        executor=str(executor or "").strip(),
        base_template_unit=base_template,
        role=role_config.role_id,
    )


def confirm_template_instance_started(
    *,
    process,
    sentinel: str,
    unit: str,
    account: str,
    log_path: str | None = None,
    timeout_ms: int = DEFAULT_START_TIMEOUT_MS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    manager_authored_sentinel: bool = False,
) -> None:
    """確認模板實例真的起來了；起不來就 fail-closed（**絕不**退回其他模式）。

    判準與 A 案共用 :func:`_await_start`：`systemctl start --wait` 與
    `systemd-run --wait` 都是「client 存活到 unit 結束」，因此「client 已結束且
    exit sentinel 不存在」在兩邊都恰好代表「job 從未真正執行」。
    ``manager_authored_sentinel=True`` 時判準改為「確認窗內 client 以非零收場」
    （#604，理由見 :func:`_await_start`）。
    """

    status = _await_start(
        process=process,
        sentinel=sentinel,
        timeout_ms=timeout_ms,
        monotonic=monotonic,
        sleep=sleep,
        manager_authored_sentinel=manager_authored_sentinel,
    )
    if status is None:
        return
    raise _fail(
        "job-runner-template-instance-start-failed",
        (
            f"模板實例 {unit} 未能起動（systemctl exit={status}；常見原因：polkit 拒絕、"
            f"模板 unit 未安裝、shim 讀 spec 失敗——後者的逐字原因在 journal："
            f"`journalctl -u {unit}`）{_log_tail(log_path)}"
        ),
        source="confirm_template_instance_started",
        unit=unit,
        account=account,
        exit_status=status,
    )


def _unit_file_installed(unit_name: str) -> bool:
    """模板 unit 檔是否已落地。刻意用檔案存在判定而不是 `systemctl cat`：

    後者要開一次 D-Bus、也可能因為 polkit 而失敗，把「未安裝」與「無授權」混成
    同一個錯誤；前者是一次 stat，且答案就是 operator 在 runbook 第 5 步做的事。
    """

    return os.path.isfile(os.path.join(DEFAULT_TEMPLATE_UNIT_DIR, unit_name))


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _unit_is_active(systemctl: str, unit: str) -> bool:
    """`systemctl is-active --quiet <unit>` 的唯讀查詢（seam，測試一律 mock）。

    查詢面不需要 polkit 授權（`is-active` 是唯讀 D-Bus 呼叫）。查不動時回 False
    ——「查不到狀態」不該擋掉一次合法派工，真正起不來的情況由
    :func:`confirm_template_instance_started` fail-closed。
    """

    try:
        completed = subprocess.run(
            [systemctl, "is-active", "--quiet", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _log_tail(log_path: str | None, *, limit: int = 200) -> str:
    """把 systemd-run 自己的錯誤訊息（經 --pipe 落進 job log）帶進診斷理由。

    沒有這一段，operator 只會看到「unit 起不來」而看不到 polkit 的實際拒絕訊息。
    讀不到就回空字串——診斷用的補充資訊不該反過來變成新的失敗來源。
    """

    if not log_path:
        return ""
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    return f"；systemd-run 輸出: {text[-limit:]}"


def _systemd_booted() -> bool:
    """等價於 `sd_booted(3)`：以 `/run/systemd/system` 是否為目錄判定。"""

    return os.path.isdir("/run/systemd/system")


def _account_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
    except KeyError:
        return False
    return True


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
    except KeyError:
        return False
    return True


# ---------------------------------------------------------------------------
# 「**那個身分**讀得到嗎」——effective 權限判定（#657）
#
# 本族 bug（#638／#657）全部是同一個形狀：測試與 preflight 檢查的是「檔案／目錄
# 存在」，而實機失敗的是「**該 job 身分**讀不到它」。兩者在單 UID 環境下無法區分，
# 於是 CI 綠、實機每個 job 以 78/CONFIG 收場。
#
# Manager 不是那個身分，`os.access()` 問的是「**我**讀不讀得到」，因此答不了這題。
# 但核心的判定並不需要變成那個身分：POSIX.1e 的 access check 是**對 inode 的中繼資料
# 做的一次純計算**（owner／group／mode ＋ ACL），而那些中繼資料 Manager 讀得到。
# 下面就是那個計算——不是近似，是 kernel 用的同一條規則。
#
# 它**不**涵蓋的（誠實邊界，見 runbook 的實測步驟）：
#   - mount 選項（`noexec`／唯讀掛載）與 LSM（SELinux／AppArmor）；
#   - mount namespace（模板 unit 的 `ProtectSystem=strict` 等）；
#   - root 對 DAC 的豁免（本族評的都是非 root 的 job 帳號，不適用）。
# 那三項只有「真的以該身分開一次檔」才驗得到，所以 runbook 保留 `sudo -u <帳號>`
# 的實測步驟，而測試對「需要第二個 UID 才驗得到」的部分**明確 skip 並說明理由**。
# ---------------------------------------------------------------------------

#: POSIX.1e access ACL 的 xattr 名（Linux）。`getfacl` 讀的就是這個。
POSIX_ACL_ACCESS_XATTR = "system.posix_acl_access"
#: 目錄的 default ACL——決定**底下新建的檔**會繼承到哪些具名條目。spec 檔在 preflight
#: 當下還不存在（它是 preflight 通過之後才寫的），因此「job 讀不讀得到自己的 spec」
#: 在這個時點只能、也**足以**由這一份決定（另一半是 `write_job_spec()` 的 0640）。
POSIX_ACL_DEFAULT_XATTR = "system.posix_acl_default"

_ACL_TAG_USER_OBJ = 0x01
_ACL_TAG_USER = 0x02
_ACL_TAG_GROUP_OBJ = 0x04
_ACL_TAG_GROUP = 0x08
_ACL_TAG_MASK = 0x10
_ACL_TAG_OTHER = 0x20
_ACL_XATTR_VERSION = 2
_ACL_UNDEFINED_ID = 0xFFFFFFFF

_PERM_R = 0o4
_PERM_W = 0o2
_PERM_X = 0o1


def _perm_str(bits: int) -> str:
    """`0o5` → `"r-x"`。錯誤訊息裡的可讀形狀（與 `getfacl` 同一種寫法）。"""

    return "".join(
        letter if bits & bit else "-"
        for letter, bit in (("r", _PERM_R), ("w", _PERM_W), ("x", _PERM_X))
    )


def _parse_acl_xattr(blob: bytes) -> list[tuple[int, int, int]]:
    """POSIX.1e ACL xattr → `[(tag, perms, id), …]`。格式不認得即空 list。

    佈局（`<linux/posix_acl_xattr.h>`）：4-byte little-endian version，其後每 8 bytes
    一條：`u16 tag`、`u16 perm`、`u32 id`。
    """

    if len(blob) < 4 or (len(blob) - 4) % 8:
        return []
    if int.from_bytes(blob[:4], "little") != _ACL_XATTR_VERSION:
        return []
    entries: list[tuple[int, int, int]] = []
    for offset in range(4, len(blob), 8):
        tag = int.from_bytes(blob[offset:offset + 2], "little")
        perm = int.from_bytes(blob[offset + 2:offset + 4], "little")
        ident = int.from_bytes(blob[offset + 4:offset + 8], "little")
        entries.append((tag, perm, ident))
    return entries


def _read_acl(path: str, xattr_name: str) -> list[tuple[int, int, int]]:
    """讀一份 ACL；沒有 ACL／不支援 xattr／非 Linux 一律回空 list。

    空 list **不是** fail-open：呼叫端在那種情形下退回傳統 mode 位判定，而那正是
    「這個檔系統上沒有 ACL 時 kernel 用的規則」。
    """

    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:  # pragma: no cover - 非 Linux
        return []
    try:
        return _parse_acl_xattr(getxattr(path, xattr_name))
    except OSError:
        return []


def _account_ids(account: str) -> tuple[int, frozenset[int]] | None:
    """帳號 → `(uid, 全部 gid)`；passwd 查不到即 None（呼叫端須 fail-closed）。"""

    try:
        entry = pwd.getpwnam(account)
    except KeyError:
        return None
    try:
        gids = frozenset(os.getgrouplist(entry.pw_name, entry.pw_gid))
    except (OSError, AttributeError):  # pragma: no cover - 平台差異
        gids = frozenset({entry.pw_gid})
    return entry.pw_uid, gids


def effective_perms_for_account(path: str, account: str) -> int | None:
    """`account` 對 `path` 的 **effective** 權限位（`0o7` 之內）；無法判定即 None。

    這是 POSIX.1e §Access Check Algorithm 的直譯：owner 位優先、其次具名 user 條目
    （**經 mask 收斂**）、其次 group 類條目的聯集（同樣經 mask）、最後 other 位。

    **mask 那一段是本族 bug 的核心**：`setfacl -m u:x:rX` 之後再 `chmod 0700` 會把
    mask 打成 `---`，具名條目於是靜默失效——ACL 還在（`getfacl` 看得到），有效權限
    卻是零。只看「有沒有那條 ACL」的檢查會漏掉它；本函式看的是 kernel 實際會算出
    的那個值。
    """

    ids = _account_ids(account)
    if ids is None:
        return None
    uid, gids = ids
    try:
        stat_result = os.stat(path)
    except OSError:
        return None

    mode = stat_result.st_mode
    entries = _read_acl(path, POSIX_ACL_ACCESS_XATTR)
    if not entries:
        # 沒有 ACL：傳統三段式。
        if uid == stat_result.st_uid:
            return (mode >> 6) & 0o7
        if stat_result.st_gid in gids:
            return (mode >> 3) & 0o7
        return mode & 0o7

    mask = 0o7
    has_mask = False
    for tag, perm, _ident in entries:
        if tag == _ACL_TAG_MASK:
            mask = perm & 0o7
            has_mask = True

    if uid == stat_result.st_uid:
        for tag, perm, _ident in entries:
            if tag == _ACL_TAG_USER_OBJ:
                return perm & 0o7  # owner 位不過 mask
        return (mode >> 6) & 0o7
    for tag, perm, ident in entries:
        if tag == _ACL_TAG_USER and ident == uid:
            return perm & mask if has_mask else perm & 0o7
    group_bits = 0
    matched_group = False
    for tag, perm, ident in entries:
        if tag == _ACL_TAG_GROUP_OBJ and stat_result.st_gid in gids:
            matched_group = True
            group_bits |= perm & 0o7
        elif tag == _ACL_TAG_GROUP and ident in gids:
            matched_group = True
            group_bits |= perm & 0o7
    if matched_group:
        return group_bits & mask if has_mask else group_bits
    for tag, perm, _ident in entries:
        if tag == _ACL_TAG_OTHER:
            return perm & 0o7
    return mode & 0o7


def inherited_perms_for_account(directory: str, account: str) -> int | None:
    """新建於 `directory` 的**檔**會讓 `account` 拿到的權限位；無法判定即 None。

    來源是目錄的 default ACL。`write_job_spec()` 在 `os.replace` 前 `chmod 0640`，
    因此新檔的 ACL mask 會被重算成 `r--`——這一段與本函式相加，就是「job 讀不讀得到
    自己的 spec」的完整答案，而且**在 spec 還沒寫出來之前就答得出來**。
    """

    ids = _account_ids(account)
    if ids is None:
        return None
    uid, gids = ids
    entries = _read_acl(directory, POSIX_ACL_DEFAULT_XATTR)
    if not entries:
        # 沒有 default ACL：新檔只帶傳統 mode 位，而 `write_job_spec()` 給的是 0640
        # （owner rw、group r、other 0）。非 owner 的 job 帳號因此只可能經由 group
        # 拿到讀權——那要它與 Manager 同 group，是部署決定，不是本函式能假設的。
        try:
            owner_uid = os.stat(directory).st_uid
        except OSError:
            return None
        return 0o6 if uid == owner_uid else 0
    mask = 0o7
    has_mask = False
    for tag, perm, _ident in entries:
        if tag == _ACL_TAG_MASK:
            mask = perm & 0o7
            has_mask = True
    for tag, perm, ident in entries:
        if tag == _ACL_TAG_USER and ident == uid:
            return perm & mask if has_mask else perm & 0o7
    group_bits = 0
    matched_group = False
    for tag, perm, ident in entries:
        if tag == _ACL_TAG_GROUP and ident in gids:
            matched_group = True
            group_bits |= perm & 0o7
    if matched_group:
        return group_bits & mask if has_mask else group_bits
    try:
        owner_uid = os.stat(directory).st_uid
    except OSError:
        return None
    if uid == owner_uid:
        for tag, perm, _ident in entries:
            if tag == _ACL_TAG_USER_OBJ:
                return perm & 0o7
        return 0o6
    return 0


def _managed_ancestors(path: str) -> list[str]:
    """`path` 由淺至深的祖先目錄（含 `/`，不含 `path` 自己）。

    traverse 權要整條都成立才有意義：葉節點 ACL 再精確，中間有一層走不過去，
    `open()` 就是 `EACCES`——而錯誤訊息指的是那一層，與缺的授權不同層（#620）。
    """

    parts = os.path.normpath(path).split("/")
    ancestors: list[str] = ["/"]
    current = ""
    for segment in parts[1:-1]:
        current = f"{current}/{segment}"
        ancestors.append(current)
    return ancestors


def _spool_readable_by(spool_dir: str, account: str) -> tuple[bool, str]:
    """`account` 是否**真的**讀得到將寫進 `spool_dir` 的 spec。`(ok, 原因)`。

    三段，缺一即不成立：

    1. 路徑上每一層都要有 `x`（traverse）——`derive_traverse_grants()` 產的 `--x`；
    2. spool 目錄本身要有 `x`（shim 以固定檔名開檔，不需要 `r`；只要求 `x` 是刻意
       的——要求 `r` 會讓一個「只給 traverse、能正常運作」的更嚴部署被誤判為壞掉）；
    3. spool 目錄的 **default ACL** 要讓該帳號在新建檔上拿得到 `r`——這一條就是
       #657 的正面判準，而它在 spec 檔還不存在時就答得出來。
    """

    if _account_ids(account) is None:
        return False, (
            f"帳號 {account} 在 passwd 裡查不到，無法判定它讀不讀得到 spec"
            "（前一項帳號存在檢查若被 stub 掉，這裡就是唯一會發現的地方）"
        )
    for ancestor in _managed_ancestors(spool_dir):
        bits = effective_perms_for_account(ancestor, account)
        if bits is None:
            return False, f"{ancestor}：無法判定 {account} 的 effective 權限（stat 失敗？）"
        if not bits & _PERM_X:
            return False, (
                f"{ancestor}：{account} 沒有 traverse（x）權，effective="
                f"{_perm_str(bits)}——整條路徑因此走不通"
                f"（Phase 2b 權限計畫的父目錄 traverse ACL 尚未套用？）"
            )
    bits = effective_perms_for_account(spool_dir, account)
    if bits is None:
        return False, f"{spool_dir}：無法判定 {account} 的 effective 權限"
    if not bits & _PERM_X:
        return False, (
            f"{spool_dir}：{account} 進不去自己的 spool，effective={_perm_str(bits)}"
        )
    inherited = inherited_perms_for_account(spool_dir, account)
    if inherited is None:
        return False, f"{spool_dir}：無法判定新建 spec 檔會給 {account} 什麼權限"
    if not inherited & _PERM_R:
        return False, (
            f"{spool_dir}：新建的 spec 檔不會讓 {account} 讀得到"
            f"（default ACL 推得的 effective={_perm_str(inherited)}）——"
            f"這正是 #657 的形狀：spool 存在、Manager 寫得進去，而 shim 在 systemd "
            f"套完 User={account} 之後讀它會 EACCES 並以 78/CONFIG 收場"
        )
    return True, ""


def _spec_readable_by(spec_path: str, account: str) -> tuple[bool, str]:
    """spec **落地之後**就地複驗「那個身分讀得到這個 inode」。`(ok, 原因)`。

    與 :func:`_spool_readable_by` 的分工：那一支在**寫之前**算目錄的 default ACL
    （spec 當時還不存在），這一支在**寫之後**算真的那個檔。兩支合起來把
    `write_job_spec()` 那段「`chmod 0640` 是為了讓繼承下來的 ACL mask 不被關掉」的
    推導從註解升級成斷言——那段推導錯了的話，症狀就是 #657 的 78/CONFIG。

    帳號不在 passwd 時回 `(True, …)`：那在正式派工路徑上不可達
    （`prepare_systemd_template()` 已對帳號 fail-closed），只會出現在直接呼叫
    `write_job_spec()` 的測試裡；在這裡 fail-closed 只會把測試的 seam 變成產線行為。
    """

    if _account_ids(account) is None:
        return True, f"帳號 {account} 不在 passwd（本機無此身分，略過就地複驗）"
    bits = effective_perms_for_account(spec_path, account)
    if bits is None:
        return False, f"{spec_path}：無法判定 {account} 的 effective 權限"
    if not bits & _PERM_R:
        return False, f"{spec_path}：effective={_perm_str(bits)}（缺 r）"
    return True, ""
