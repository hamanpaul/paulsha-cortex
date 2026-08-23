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
  template harvest 直接讀 spec 指向的 canonical per-job log spool；Manager-only
  exit/ledger controls 由 persisted control anchor 定址，direct/systemd-run 的
  legacy `<log_dir>/<slice_id>.jsonl` 形狀維持不變。

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
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from paulsha_cortex.config import paths

from . import job_workspace
from .diagnostics import DiagnosticReason, diagnostic_reason

__all__ = [
    "BUILDER_ACCOUNT_ENV",
    "BUILDER_FORWARDED_ENV",
    "BUILDER_GROUP_ENV",
    "BUILDER_HOME_ENV",
    "BUILDER_PATH_ENV",
    "BUILDER_SYNTHESIZED_ENV",
    "ALLOWED_GIT_CONFIG_KEYS",
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
    "MAX_JOB_PROMPT_BYTES",
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
    "WORKSPACE_REACH_INHERITED_DEFAULT_ACL",
    "WORKSPACE_REACH_PER_JOB_NAMED_ACL",
    "WORKSPACE_REACH_POOL_OWNED_BY_JOB",
    "WorkspaceAclSpec",
    "ensure_workspace_reachable",
    "git_config_safe_directories",
    "git_workspace_trust_env",
    "workspace_acl_grants",
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
    "job_prompt_spool_path",
    "reap_orphaned_prompt_slots",
    "job_spec_path",
    "job_prompt_spool_dir",
    "job_prompt_path",
    "preflight_systemd_run",
    "preflight_systemd_template",
    "prepare_systemd_run",
    "prepare_systemd_template",
    "read_shim_error",
    "reject_unsafe_env",
    "resolve_hardening_profile",
    "resolve_job_account",
    "resolve_job_group",
    "resolve_job_home",
    "resolve_job_path",
    "resolve_prompt_spec_spool",
    "resolve_job_spec_spool",
    "resolve_job_role",
    "template_instance_id",
    "template_unit_for_profile",
    "template_unit_name",
    "transient_unit_name",
    "write_job_spec",
    "write_job_prompt",
    "prepare_private_prompt_spool",
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
#: 還是 fail-open——而且是兩段的：`PATH` 當時在 :data:`BUILDER_FORWARDED_ENV` 上，
#: 因此未設此變數時 job 先靜默拿到 **Manager daemon 的** `PATH`（一個沒有人為「job
#: 該解到哪一份 CLI」做過決定的值）；daemon 自己也沒有 `PATH` 時，spec 連這個鍵都
#: 沒有，`execvpe` 退回 `os.defpath`（`:/bin:/usr/bin`）。兩段的終點相同：`codex`
#: 靜默解到系統層那份舊 CLI，不報錯、只是產出來自一支 operator 從未判讀過的執行檔。
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
PRIVATE_PROMPT_ROOT_DIRNAME = paths.JOB_PROMPT_ROOT_DIRNAME

# Linux rejects an individual argv element at MAX_ARG_STRLEN (normally 131072
# bytes).  Prompts are Manager-authored input, so the bounded file channel is
# deliberately larger than that limit but still finite.  The bound is checked
# before creating a file; an oversized prompt therefore fails closed rather
# than falling back to argv or an ambient stdin.
MAX_JOB_PROMPT_BYTES = 4 * 1024 * 1024


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


# ---------------------------------------------------------------------------
# #710：工作區可達性——三個角色的形態由 `trust_root.registry.JOB_WORKSPACE_REACH`
# 一張表決定，本模組持有它的**成對契約**（本模組刻意不 import `trust_root`，與
# `log_spool_principal`／`DEFAULT_TEMPLATE_UNIT` 是同一個既有模式；兩邊逐列相等由
# `tests/test_per_job_workspace_acl_710.py` 釘住）。
# ---------------------------------------------------------------------------

#: 可達性由 Manager 在 provision 當下對 per-job 那一格下的**具名 ACL** 供給（builder）。
WORKSPACE_REACH_PER_JOB_NAMED_ACL = "per-job-named-acl"
#: 可達性由 pool 根的 **default ACL** 繼承供給（reviewer／planner）。
WORKSPACE_REACH_INHERITED_DEFAULT_ACL = "inherited-default-acl"
#: 可達性由 pool 根的 **owner 位**供給，per-job 那一格由 job 自己建（gate）。
WORKSPACE_REACH_POOL_OWNED_BY_JOB = "pool-owned-by-job"


# ---------------------------------------------------------------------------
# #712：git 工作區信任——三個角色的形態由 `trust_root.registry.
# JOB_GIT_WORKSPACE_TRUST` 一張表決定，本模組持有它的**成對契約**（同上，本模組刻意
# 不 import `trust_root`；兩邊逐列相等由 `tests/test_per_job_git_safe_directory_712.py`
# 釘住）。
# ---------------------------------------------------------------------------

#: 工作區由 Manager 建（跨 owner）⇒ 逐 job 注入 `safe.directory`（builder／reviewer）。
GIT_WORKSPACE_TRUST_PER_JOB_ENV = "per-job-env"
#: per-job 那一格由 job 自己建 ⇒ owner 就是自己 ⇒ **零動作**（gate）。
GIT_WORKSPACE_TRUST_OWNED_BY_JOB = "owned-by-job"

#: git 的 **command scope** 三件套（`git-config(1)` 的 `GIT_CONFIG_{COUNT,KEY,VALUE}`）。
GIT_CONFIG_COUNT_ENV = "GIT_CONFIG_COUNT"
GIT_CONFIG_KEY_ENV_PREFIX = "GIT_CONFIG_KEY_"
GIT_CONFIG_VALUE_ENV_PREFIX = "GIT_CONFIG_VALUE_"

#: 逐 job 注入時**唯一**放行的 git 設定鍵（#712）。與
#: `trust_root.registry.GIT_SAFE_DIRECTORY_KEY` 是成對契約。
#:
#: **這個白名單就是本票的安全論證本體。** `GIT_CONFIG_*` 是與 `git -c` 同級的
#: protected configuration，`safe.directory` 只吃這一層——但同一條管道也吃
#: `alias.*`／`core.fsmonitor`，而那些鍵**會執行外部命令**。0819 實測（git 2.43.0）::
#:
#:     $ GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=<repo> \
#:         GIT_CONFIG_KEY_1=alias.pwn GIT_CONFIG_VALUE_1='!echo PWNED-EXTERNAL-COMMAND' \
#:         git -C <repo> pwn
#:     PWNED-EXTERNAL-COMMAND
#:
#: 三份 `.gitconfig` 之所以 root-owned、job 唯讀，理由逐字就是這一條。若 job 端能經
#: 這條管道塞任意 git 設定，那條防線等於從檔案權限旁邊繞過去。因此：**白名單只有一個
#: 鍵**，且寫端（:func:`build_job_env`／:func:`build_job_spec`）與讀端
#: （`job_shim.load_spec`）走**同一支** :func:`_reject_unsafe_git_config`。
ALLOWED_GIT_CONFIG_KEYS = frozenset({"safe.directory"})

#: `GIT_CONFIG_KEY_<i>`／`GIT_CONFIG_VALUE_<i>` 的合法形狀（`<i>` 是十進位、無前導 +/-）。
_GIT_CONFIG_INDEXED_RE = re.compile(r"^GIT_CONFIG_(KEY|VALUE)_(0|[1-9][0-9]*)$")


@dataclass(frozen=True)
class WorkspaceAclSpec:
    """per-job 工作區上，**某個角色的帳號**拿到什麼（#710）。

    以 `role_id` 而不是帳號名表達，理由與整張 `JOB_ROLE_CONFIG` 相同：帳號是部署
    決定（env 可覆寫），角色才是這裡講得出來的東西。
    """

    role_id: str
    access_perms: str
    default_perms: str


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
    #: #708：本角色的 job log 落在哪一個 principal 的 log spool（`config.paths.
    #: job_log_spool_root()` 的引數，＝`trust_root.registry.Principal` 的 `value`）。
    #:
    #: **不是** `role_id`：角色字幹是 `review`，principal 是 `reviewer`——兩者恰好
    #: 不同，而「恰好不同」正是不能靠字串推導的理由。與
    #: `registry.JOB_LOG_SPOOLS` 是**成對契約**（本模組刻意不 import `trust_root`，
    #: 同 `DEFAULT_TEMPLATE_UNIT` 的既有模式），由
    #: `tests/test_job_log_spool_708.py` 釘住兩邊逐列相等。
    log_spool_principal: str
    #: #710：本角色的工作區可達性形態（三個 `WORKSPACE_REACH_*` 之一）。與
    #: `registry.JOB_WORKSPACE_REACH` 的 `reach.value` 逐字相等。
    workspace_reach: str
    #: #710：job 在自己的工作區上**最終必須擁有**的權限位（mask 之後的**有效**權限，
    #: 由 :func:`effective_perms_for_account` 判定）。三個角色一律非空——不論可達性
    #: 由 owner 位、繼承或具名 ACL 供給，「進得去」都必須寫得出可驗形式。
    workspace_required_perms: str
    #: #710：`WORKSPACE_REACH_PER_JOB_NAMED_ACL` 那一格要下的具名 ACL（含 #629 宣告
    #: 的 gate 唯讀那條，兩者由**同一次** setfacl 落地）。其餘形態為空 tuple——
    #: 「執行期零動作」與「忘了實作」在輸出上長得一樣，因此判準是 `workspace_reach`，
    #: 不是這個 tuple 的長度。
    workspace_acl: tuple[WorkspaceAclSpec, ...]
    #: #712：本角色在**自己的工作區**上跑 git 時，dubious-ownership 怎麼過（兩個
    #: `GIT_WORKSPACE_TRUST_*` 之一）。與 `registry.JOB_GIT_WORKSPACE_TRUST` 的
    #: `trust.value` 逐字相等。
    #:
    #: **它不是 `workspace_reach` 的別名，但由它導出**：git 的判準是「repo 的 owner
    #: 是不是當下這個 uid」，而「誰建那一格」就寫在 `workspace_reach` 上——
    #: `pool-owned-by-job` ⇒ job 自己建 ⇒ `owned-by-job`；另外兩種 ⇒ Manager 建 ⇒
    #: 跨 owner ⇒ `per-job-env`。兩邊矛盾時 `registry` 在 import 當下就載不起來。
    git_workspace_trust: str
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
            log_spool_principal="builder",
            workspace_reach=WORKSPACE_REACH_PER_JOB_NAMED_ACL,
            workspace_required_perms="rwx",
            workspace_acl=(
                WorkspaceAclSpec(JOB_ROLE_BUILDER, "rwX", "rwx"),
                WorkspaceAclSpec(JOB_ROLE_GATE, "rX", "rX"),
            ),
            git_workspace_trust=GIT_WORKSPACE_TRUST_PER_JOB_ENV,
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
            log_spool_principal="reviewer",
            workspace_reach=WORKSPACE_REACH_INHERITED_DEFAULT_ACL,
            workspace_required_perms="rx",
            workspace_acl=(),
            git_workspace_trust=GIT_WORKSPACE_TRUST_PER_JOB_ENV,
            spec_spool_env=REVIEW_JOB_SPEC_SPOOL_ENV,
            default_spec_spool=DEFAULT_REVIEW_JOB_SPEC_SPOOL,
            rationale=(
                "reviewer ＋ planner persona（三分方案下同一個 OS 帳號）。"
                "**兩者的降權是兩張票、相隔三個月**：\n"
                "M2（#615）只搬了 **reviewer**——`launcher.SubprocessLauncher` 這條路徑。"
                "**planner 的 define／brainstorm 不走 launcher**，它走 "
                "`planning_runtime._invoke_json()`，因此 #615 一行都沒有碰到它；"
                "「injection 可達的進程皆無 spawn 授權」在那之後仍**只對 builder 與 "
                "reviewer 成立**（#672 的證據鏈）。\n"
                "M2′（#672 票 A～F／#682-#687）補上 planner：`JobPlanningInvoker` 讓四個 "
                "planning adapter 與全部 probe 都經本角色起 job，`PSC_JOB_RUNNER="
                "systemd-template` 下 `_select_planning_invoker()` 對其餘值 fail-closed。"
                "**至此該全稱才成立**，實機驗收見 runbook 第 5-6c 步。"
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
            log_spool_principal="gate",
            workspace_reach=WORKSPACE_REACH_POOL_OWNED_BY_JOB,
            workspace_required_perms="rwx",
            workspace_acl=(),
            git_workspace_trust=GIT_WORKSPACE_TRUST_OWNED_BY_JOB,
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
#: - `GIT_CONFIG`／`GIT_CONFIG_GLOBAL`／`GIT_CONFIG_SYSTEM`／`GIT_CONFIG_NOSYSTEM`／
#:   `GIT_CONFIG_PARAMETERS`（#712）：**同一扇門的另外五個把手**。本票為了 per-job 的
#:   `safe.directory` 打開了 `GIT_CONFIG_COUNT`／`GIT_CONFIG_KEY_<i>`／
#:   `GIT_CONFIG_VALUE_<i>`（見 :data:`ALLOWED_GIT_CONFIG_KEYS`，只放行一個鍵），而這
#:   五個能把整份 git 設定換掉或整批灌進來：`GIT_CONFIG_GLOBAL` 讓 `$HOME/.gitconfig`
#:   那份 **root-owned** 的檔失效（改指到 job 自己寫得出來的路徑），
#:   `GIT_CONFIG_PARAMETERS` 是 `git -c` 的序列化管道、**不受本票的單鍵白名單約束**。
#:   放行了單鍵卻留著這五個，等於白做。
DENIED_ENV_NAMES = frozenset(
    {
        "BASHOPTS",
        "BASH_ENV",
        "CLAUDE_CONFIG_DIR",
        "ENV",
        "GH_CONFIG_DIR",
        "GH_HOST",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
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
#: - `HOME`：由本角色的 `PSC_*_HOME` 宣告，#692 起與 `PATH` 同樣 fail-closed；
#:   模板模式下 shim 以 `execvpe(..., env)` 整份換掉環境，unit 的 `Environment=HOME=`
#:   到不了模型。**任何情況下都不會是 daemon 的 HOME。**
#: - `PATH`：#679 起由 :func:`resolve_job_path` 從**本角色的** `PSC_*_PATH` 算出，
#:   未宣告即 fail-closed。**它以前在轉發類白名單上，那是本票真正的 fail-open**：
#:   未宣告時 job 靜默拿到 **Manager daemon 的** `PATH`——一個沒有人為「job 該解到
#:   哪一份 CLI」做過決定的值，而且它是否含 `<toolchain>/bin` 完全看那台機器的
#:   EnvironmentFile 被誰手動加過什麼。轉發 daemon 的 `PATH` 與轉發 daemon 的
#:   `HOME`／`VIRTUAL_ENV`（早就在排除表上）是同一類錯誤：daemon 的 `PATH` 還帶著
#:   `<deploy_root>/venv/bin`，等於把 job 的 `python3` 綁回 Manager 的 venv。
BUILDER_SYNTHESIZED_ENV = (
    "CODEX_HOME",
    "HOME",
    "PATH",
    "PSC_JOB_ID",
    "PSC_RELAY_TARGET",
    "PSC_REPO_ROOT",
    "PSC_SLICE_ID",
    "XDG_CACHE_HOME",
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
    "HOME": (
        "daemon 的 HOME 絕不轉發；job 的 HOME 只能由本角色的 PSC_*_HOME 宣告，再由 "
        "spec 的 env 帶進模型（unit 的 Environment=HOME= 到不了模型）"
    ),
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
    _reject_unsafe_git_config(env, source=source)


def git_config_safe_directories(env: Mapping[str, str]) -> tuple[str, ...]:
    """env 裡經 `GIT_CONFIG_*` 放行的工作區路徑（依 `<i>` 排序）。

    只回傳值，不做驗證——形狀的判準在 :func:`_reject_unsafe_git_config`，呼叫本函式
    之前一定已經先過那一關（`_reject_unsafe()` 是所有寫端與讀端的共同入口）。
    """

    count = env.get(GIT_CONFIG_COUNT_ENV)
    if not count:
        return ()
    values: list[str] = []
    for index in range(int(count)):
        value = env.get(f"{GIT_CONFIG_VALUE_ENV_PREFIX}{index}")
        if value is not None:
            values.append(value)
    return tuple(values)


def _reject_unsafe_git_config(env: Mapping[str, str], *, source: str) -> None:
    """`GIT_CONFIG_*` 這條管道**只准出現 `safe.directory`**（#712）。

    ## 為什麼需要這一支，而不是「Manager 自己不會亂寫」

    `.gitconfig` 之所以 root-owned、job 唯讀，逐字的理由是 `alias.*`／`core.fsmonitor`
    **會執行外部命令**。而 `GIT_CONFIG_COUNT`／`GIT_CONFIG_KEY_<i>`／
    `GIT_CONFIG_VALUE_<i>` 是與 `git -c` 同級的 command scope——0819 實測（git 2.43.0）
    `GIT_CONFIG_KEY_1=alias.pwn` ＋ `GIT_CONFIG_VALUE_1='!echo …'` 之下 `git pwn` 真的
    執行了那條命令。本票為了 `safe.directory` 打開這條管道，就必須同時把它**收窄到
    只有那一個鍵**，否則等於在檔案權限旁邊開一扇同樣寬的門。

    ## 為什麼寫端與讀端都要跑

    與 :func:`_reject_unsafe` 的既有理由逐字相同：只在寫端自律，等於相信一個 Manager
    帳號可寫的檔案（spec spool）沒被動過手腳。`job_shim.load_spec()` 呼叫的
    :func:`reject_unsafe_env` 會走到這裡，因此「白名單被改壞」在兩邊都會炸。

    ## 判準

    1. `GIT_CONFIG_*` 家族只准出現 `GIT_CONFIG_COUNT`／`GIT_CONFIG_KEY_<i>`／
       `GIT_CONFIG_VALUE_<i>`（其餘五個名字已在 :data:`DENIED_ENV_NAMES` 上，上一圈
       就擋掉了；這裡擋的是拼錯／未來新增的變體）；
    2. `<i>` 必須是 `0..count-1` 的十進位，且 KEY／VALUE **成對齊全**——少一個 git 會
       整支 `fatal: unable to parse command-line config`（實測），那會讓 job 死在一個
       與本票無關的訊息上；
    3. 每個 KEY 的**值**必須在 :data:`ALLOWED_GIT_CONFIG_KEYS` 內（大小寫不敏感，因為
       git 的 config 鍵本身不區分大小寫——`Safe.Directory` 與 `safe.directory` 是同一個
       鍵，白名單若區分就等於留一個繞法）；
    4. `safe.directory` 的值必須是**絕對路徑**，且不得是字面 `*`（那是對整個帳號
       opt-out，不是逐 job 授權）。
    """

    raw_count = env.get(GIT_CONFIG_COUNT_ENV)
    indexed = {
        name: value
        for name, value in env.items()
        if _GIT_CONFIG_INDEXED_RE.fullmatch(name) is not None
    }
    stray = sorted(
        name
        for name in env
        if name.startswith("GIT_CONFIG")
        and name != GIT_CONFIG_COUNT_ENV
        and name not in indexed
    )
    if stray:
        raise _fail(
            "job-runner-git-config-env-invalid",
            f"不認得的 GIT_CONFIG_* 變數 {stray}——這條管道只放行 "
            f"{GIT_CONFIG_COUNT_ENV}／{GIT_CONFIG_KEY_ENV_PREFIX}<i>／"
            f"{GIT_CONFIG_VALUE_ENV_PREFIX}<i>（#712）",
            source=source,
            variable=stray[0],
        )
    if raw_count is None:
        if indexed:
            raise _fail(
                "job-runner-git-config-env-invalid",
                f"有 {sorted(indexed)} 卻沒有 {GIT_CONFIG_COUNT_ENV}——git 會完全忽略"
                "它們，而放行看起來像是生效了（#712）",
                source=source,
                variable=sorted(indexed)[0],
            )
        return
    if not re.fullmatch(r"0|[1-9][0-9]*", raw_count):
        raise _fail(
            "job-runner-git-config-env-invalid",
            f"{GIT_CONFIG_COUNT_ENV}={raw_count!r} 不是非負十進位整數（#712）",
            source=source,
            variable=GIT_CONFIG_COUNT_ENV,
        )
    count = int(raw_count)
    expected = {
        f"{prefix}{index}"
        for index in range(count)
        for prefix in (GIT_CONFIG_KEY_ENV_PREFIX, GIT_CONFIG_VALUE_ENV_PREFIX)
    }
    if set(indexed) != expected:
        raise _fail(
            "job-runner-git-config-env-invalid",
            f"{GIT_CONFIG_COUNT_ENV}={count} 與實際的 "
            f"{sorted(indexed)} 對不上（期望 {sorted(expected)}）——git 對缺項會整支 "
            "`fatal: unable to parse command-line config`（實測 git 2.43），job 於是死在"
            "一個與放行無關的訊息上（#712）",
            source=source,
            variable=GIT_CONFIG_COUNT_ENV,
        )
    for index in range(count):
        key = indexed[f"{GIT_CONFIG_KEY_ENV_PREFIX}{index}"]
        value = indexed[f"{GIT_CONFIG_VALUE_ENV_PREFIX}{index}"]
        if key.lower() not in ALLOWED_GIT_CONFIG_KEYS:
            raise _fail(
                "job-runner-git-config-key-not-allowed",
                f"{GIT_CONFIG_KEY_ENV_PREFIX}{index}={key!r} 不在白名單 "
                f"{sorted(ALLOWED_GIT_CONFIG_KEYS)} 內——`GIT_CONFIG_*` 是與 `git -c` "
                "同級的 command scope，`alias.*`／`core.fsmonitor` 這類鍵會**執行外部"
                "命令**（實測 git 2.43：`alias.pwn=!echo …` 之下 `git pwn` 真的跑了）。"
                "那正是三份 `.gitconfig` root-owned 的理由，本管道不得成為它的繞法"
                "（#712）",
                source=source,
                variable=f"{GIT_CONFIG_KEY_ENV_PREFIX}{index}",
            )
        if value == "*" or not value.startswith("/"):
            raise _fail(
                "job-runner-git-config-value-invalid",
                f"{GIT_CONFIG_VALUE_ENV_PREFIX}{index}={value!r} 必須是絕對路徑，且"
                "不得是字面 `*`——`*` 等於對這個帳號整個關掉 dubious-ownership 保護，"
                "**那是 opt-out，不是逐 job 授權**（#712／#623）",
                source=source,
                variable=f"{GIT_CONFIG_VALUE_ENV_PREFIX}{index}",
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
            env["PATH"] = path_override      # 沒設就不覆寫

    而它上面那一圈轉發迴圈當時**含 `PATH`**，所以「沒設」的實際後果分兩段：

    1. Manager 自己有 `PATH` ⇒ job 逐字沿用 **daemon 的** `PATH`。那份值是否含
       `<toolchain>/bin` 純看那台機器的 EnvironmentFile 被誰手動加過什麼，而且它帶著
       `<deploy_root>/venv/bin`——等於把 job 的 `python3` 綁回 Manager 的 venv。
    2. Manager 自己也沒有 `PATH` ⇒ spec 連這個鍵都沒有。job spec 的 `env` 就是 job 的
       **完整**環境（shim 以 `os.execvpe(command[0], command, spec["env"])` 整份換掉），
       少了 `PATH` 不是「用系統預設」，是 `execvpe` 退回 `os.defpath`＝`:/bin:/usr/bin`。

    兩段的終點相同：`claude`／`agy` rc=127（只存在於 toolchain），而 `codex` **靜默**
    解到 `/usr/bin/codex`——系統層 0.42.0，toolchain 那份是 0.147.0。不失敗、不報錯，
    只是每一筆產出都來自一支 operator 從未判讀過的 CLI。

    退回「permgen 導出的預設」（#679 的選項 (b)）看起來溫和，但那正是本 repo 已經
    否決過的形態：#453「registry 永不寫入預設值」。一個沒有宣告 `PSC_*_PATH` 的部署
    ＝operator 沒有對「job 解哪一份 CLI」做過決定，而那是**必須有人做**的決定；
    替他做一次、只在 spec 上留一行痕跡，等於把「未宣告」與「宣告成這樣」壓成同一種
    狀態，下一次漂移一樣看不見。

    **升級既有部署會痛，而那是對的**：現況是靜默跑錯版本，改完之後是下一次派工當場
    以可讀理由失敗。runbook 第 **5-5b** 步有逐字的升級程序（補三個變數、重新落檔六份
    模板 unit、重啟 Manager、跑反向不變式），`docs/onboarding/troubleshooting.md` 的
    `job-runner-path-undeclared` 一節是同一件事的簡版。
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


def _home_contract_hint(config: JobRoleConfig) -> str:
    flag = _UNIT_FLAG_HINT.get(config.role_id, "--job")
    return (
        "請在 Manager 的 root-owned EnvironmentFile 宣告（值由產生器導出，不要手打）："
        f"`python3 -m paulsha_cortex.trust_root unit four-way {flag} | grep "
        f"'^#      {config.home_env}='`；同一份 unit 的 `Environment=HOME=` 也必須逐字對齊"
    )


def _assess_home_path(
    value: str,
    *,
    expected_uid: int | None,
    require_existing: bool,
) -> str | None:
    if not value:
        return "undeclared"
    if not value.startswith("/"):
        return "not-absolute"
    try:
        exists = os.path.lexists(value)
    except OSError:
        return "unstatable"
    if not exists:
        return "missing" if require_existing else None
    try:
        stat_result = os.lstat(value)
    except OSError:
        return "unstatable"
    if stat.S_ISLNK(stat_result.st_mode):
        return "symlink"
    if not stat.S_ISDIR(stat_result.st_mode):
        return "not-directory"
    if expected_uid is not None and stat_result.st_uid != expected_uid:
        return "owner-mismatch"
    return None


def resolve_job_home(manager_env: Mapping[str, str], *, role: str = JOB_ROLE_BUILDER) -> str:
    """解析本角色的 job `HOME`。#692 起與 `PATH` 同樣 fail-closed。"""

    config = resolve_job_role(role)
    value = (manager_env.get(config.home_env) or "").strip()
    account = resolve_job_account(manager_env, role=config.role_id)
    account_ids = _account_ids(account)
    problem = _assess_home_path(
        value,
        expected_uid=account_ids[0] if account_ids is not None else None,
        require_existing=True,
    )
    if problem is None:
        return value
    hint = _home_contract_hint(config)
    if problem == "undeclared":
        detail = (
            f"{config.home_env} 未宣告——{config.role_id} job 會拿不到 HOME。job spec 的 env "
            "就是 job 的完整環境（shim 以 execvpe 整份換掉），少了 HOME 時模型會在更深處以 "
            "`$HOME is not defined`／`Not logged in` 收場。"
            f"{hint}"
        )
        reason = "job-runner-home-undeclared"
    elif problem == "not-absolute":
        detail = (
            f"{config.home_env} 必須是絕對路徑的 per-principal HOME；relative path 會讓 job "
            "state 落到不可稽核的位置。"
            f"{hint}"
        )
        reason = "job-runner-home-not-absolute"
    elif problem == "missing":
        detail = (
            f"{config.home_env} 指向的 HOME 目錄不存在——這代表 root-owned "
            "EnvironmentFile 與部署樹不同步。"
            f"{hint}"
        )
        reason = "job-runner-home-missing"
    elif problem == "unstatable":
        detail = (
            f"{config.home_env} 指向的 HOME 目前無法判定型態或 owner；在沒有這個判準前 "
            "job 不得起跑。"
            f"{hint}"
        )
        reason = "job-runner-home-unstatable"
    elif problem == "symlink":
        detail = (
            f"{config.home_env} 不得是 symlink；job 的 `$HOME` 必須直指 principal 自己的 "
            "真實目錄，不能外包到別的樹。"
            f"{hint}"
        )
        reason = "job-runner-home-symlink"
    elif problem == "not-directory":
        detail = (
            f"{config.home_env} 必須指向目錄，不得是普通檔或特殊檔。"
            f"{hint}"
        )
        reason = "job-runner-home-not-directory"
    else:
        detail = (
            f"{config.home_env} 的 owner 必須是 {account}；這一格 HOME 若屬於別人，job 的 "
            "state / credentials 就會落在錯帳號的樹。"
            f"{hint}"
        )
        reason = "job-runner-home-owner-mismatch"
    raise _fail(
        reason,
        detail,
        source="resolve_job_home",
        role=config.role_id,
        variable=config.home_env,
        account=account,
    )


def git_workspace_trust_env(*, role: str, workspace: str | Path | None) -> dict[str, str]:
    """該角色對**這一格工作區**的 git 放行 env；不需要就回空 dict（#712）。

    這是 #712 修法的本體，三個角色**共用同一支**——分岔只有一處，而且分岔的依據是
    `JOB_ROLE_CONFIG[...].git_workspace_trust`（＝`registry.JOB_GIT_WORKSPACE_TRUST`
    的成對契約），不是 `if role == …`：

    ``per-job-env``（builder／reviewer／planner）
        工作區由 **Manager** 建 ⇒ owner 是 Manager、job 是另一個 uid ⇒ git 的
        dubious-ownership 保護擋下**整個 repo**（權限全對也一樣：那是 owner 判準，
        不是權限判準）。放行走 `GIT_CONFIG_COUNT`／`GIT_CONFIG_KEY_0=safe.directory`
        ／`GIT_CONFIG_VALUE_0=<這一格的絕對路徑>`。

    ``owned-by-job``（gate）
        per-job 那一格由 job 自己 `copytree` 出來 ⇒ owner 就是自己 ⇒ **零動作**。

    ## `GIT_CONFIG_*` 真的設得動 `safe.directory` 嗎——0819 實測（git 2.43.0）

    這條必須實測，因為 `safe.directory` 是**受保護的鍵**：它刻意不吃 repo-local
    config（否則 untrusted repo 可以自我授信）。實測結果是**吃 command scope**——
    `GIT_CONFIG_*` 與 `git -c` 同屬該 scope，`git config --list --show-scope` 逐字回報
    `command\tsafe.directory=…`::

        # root-owned repo，以一般使用者執行
        $ git -C <repo> status
        fatal: detected dubious ownership in repository at '<repo>'
        $ GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory \
            GIT_CONFIG_VALUE_0=<repo> git -C <repo> status --porcelain --branch
        ## main                                                    # rc=0
        $ GIT_CONFIG_COUNT=1 … GIT_CONFIG_VALUE_0=<repoA> git -C <repoB> status
        fatal: detected dubious ownership in repository at '<repoB>'   # rc=128

    `git bundle create`（builder 真正會跑的那一支）在同一份 env 下 rc=0。

    ## 為什麼值取**已解析（physical）路徑**——這是支配性選擇，不是對 git 的斷言

    shim 在降權之後做的是 `os.chdir(spec["working_directory"])`，而 git 由 `getcwd()`
    取得 repo 路徑——`getcwd(2)` 回的**恆是** physical path。因此只要放行值與
    `working_directory` 取**同一個已解析的字串**，「我們放行的」與「git 問的」在任何
    git 版本上都是同一條。本函式一律 `Path(...).resolve()`，兩邊相等由
    :func:`build_job_spec` 斷言。

    ⚠️ **不要把「git 拒絕 symlink 路徑」寫成不變式。** 那是 git 的實作細節，**隨版本
    而異**：0819 本機 git 2.43.0 上 `safe.directory=<symlink 路徑>`（cwd 走該 symlink）
    **被拒**，而 PR #713 第一輪 CI 上較新的 git **接受**了同一組輸入——本 repo 曾有一條
    測試把前者寫成硬斷言，四個 python 版本一起紅。我們控制得了的只有「傳哪一條、
    `chdir` 到哪一條」；讓兩者都取 physical path 在**兩種 git 下都成立**，因此不必、
    也不該依賴 git 對 symlink 路徑的處置。

    ``workspace=None`` 是給**沒有工作區的呼叫端**用的（`launcher.executor_environment()`
    的 preflight：它報告的是 PATH／HOME／sandbox 剖面，那時還沒有任何 job 工作區）。
    真實派工路徑上「忘了傳」不可能發生：本參數在 :func:`build_job_env` 上是**必填**
    的具名參數，而 :func:`build_job_spec` 另外斷言「env 放行的那一格就是 spec 的
    `working_directory`」。
    """

    config = resolve_job_role(role)
    if config.git_workspace_trust != GIT_WORKSPACE_TRUST_PER_JOB_ENV:
        # 形態說「零動作」就是零動作——gate 的副本由它自己建，owner 就是自己。
        # 在這裡「順手也給一份」會讓一個**不成立的**放行看起來像是必要的。
        return {}
    if workspace is None:
        return {}
    resolved = str(Path(workspace).resolve())
    return {
        GIT_CONFIG_COUNT_ENV: "1",
        f"{GIT_CONFIG_KEY_ENV_PREFIX}0": sorted(ALLOWED_GIT_CONFIG_KEYS)[0],
        f"{GIT_CONFIG_VALUE_ENV_PREFIX}0": resolved,
    }


def build_job_env(
    *,
    manager_env: Mapping[str, str],
    job_id: str,
    slice_id: str,
    repo_root: str,
    workspace: str | Path | None,
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
    :func:`resolve_job_path` 直接 raise。

    **`HOME` 現在與 `PATH` 同樣 fail-closed（#692）。** 模板模式下 `cortex-job-shim`
    以 `os.execvpe(command[0], command, job_env)` 把環境**整份換掉**，unit 的
    `Environment=HOME=` 因此到不了模型行程；`HOME` 只能來自 spec 的 env，而那份 env
    只能由 Manager 端的 `PSC_*_HOME` 產生。0818 實機複驗：`PSC_REVIEWER_HOME`
    未宣告 ⇒ 降權 planning job 的 agy 死在 `getting home directory: $HOME is not
    defined`；補上該變數之後同一條呼叫 rc=0。#692 把它收成與 PATH 對稱：少了、空了、
    相對路徑、symlink、或 owner 不符，一律在起跑前 fail-closed。
    """

    config = resolve_job_role(role)
    path_declared = (manager_env.get(config.path_env) or "").strip()
    home_declared = (manager_env.get(config.home_env) or "").strip()
    if not path_declared and not home_declared:
        raise _fail(
            "job-runner-path-home-undeclared",
            (
                f"{config.path_env} 與 {config.home_env} 未宣告——{config.role_id} job 會同時拿不到 "
                "PATH 與 HOME。PATH 少了時 execvpe 會退回 os.defpath（:/bin:/usr/bin）；"
                "HOME 少了時模型會在更深處以 `$HOME is not defined`／`Not logged in` "
                "收場。請在 Manager 的 root-owned EnvironmentFile 同時宣告這兩條（值由產生器"
                "導出，不要手打）：`python3 -m paulsha_cortex.trust_root unit four-way "
                f"{_UNIT_FLAG_HINT.get(config.role_id, '--job')} | grep '^Environment=PATH='`；"
                f"`{config.home_env}=...` 必須與同一份 unit 的 `Environment=HOME=` 逐字對齊"
            ),
            source="build_job_env",
            role=config.role_id,
            path_variable=config.path_env,
            home_variable=config.home_env,
        )
    env: dict[str, str] = {}
    for forwarded in BUILDER_FORWARDED_ENV:
        value = manager_env.get(forwarded.name)
        if value:
            env[forwarded.name] = value
    env["PATH"] = resolve_job_path(manager_env, role=config.role_id)
    env["HOME"] = resolve_job_home(manager_env, role=config.role_id)
    if config.role_id in (JOB_ROLE_BUILDER, JOB_ROLE_REVIEW):
        from .spool_slot import canonical_job_slot, writable_surface

        principal_id = "reviewer" if config.role_id == JOB_ROLE_REVIEW else "builder"
        codex_row = writable_surface(f"{principal_id}-codex-home")
        cache_row = writable_surface(f"{principal_id}-runtime-cache")
        env["CODEX_HOME"] = str(canonical_job_slot(codex_row.surface_id, job_id))
        env["XDG_CACHE_HOME"] = str(canonical_job_slot(cache_row.surface_id, job_id))
    env["PSC_SLICE_ID"] = slice_id
    env["PSC_JOB_ID"] = job_id
    env["PSC_REPO_ROOT"] = repo_root
    if relay_target is not None:
        env["PSC_RELAY_TARGET"] = relay_target
    if config.role_id == JOB_ROLE_GATE:
        env.update(gate_declaration_env(manager_env))
    # #712：git 的 dubious-ownership 那一層。**值由 Manager 算**——job 拿到的是一份
    # 它改不動的 spec（#638／#639 的 spool 模型），因此這裡算出來的那一格就是它唯一
    # 拿得到的放行，改不了成 `*`、也改不了成別人的工作區。
    env.update(git_workspace_trust_env(role=config.role_id, workspace=workspace))
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
    workspace: str | Path | None,
    relay_target: str | None = None,
) -> dict[str, str]:
    """builder 角色的具名別名（既有呼叫端與測試直接用它）。"""

    return build_job_env(
        manager_env=manager_env,
        job_id=job_id,
        slice_id=slice_id,
        repo_root=repo_root,
        workspace=workspace,
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


def resolve_prompt_spec_spool(env: Mapping[str, str], *, role: str) -> str:
    """Resolve the prompt root's paired spec spool for a runtime role.

    Explicit per-role spool configuration wins.  When a transient lane has
    no spec channel of its own, derive the deployment path from the same
    ``PSC_AGENTS_ROOT``-aware resolver used by the trust-root layout instead
    of falling back to a fixed host path.
    """

    config = resolve_job_role(role)
    configured = (env.get(config.spec_spool_env) or "").strip()
    if configured:
        return configured
    return str(paths.job_spec_spool_for(config.log_spool_principal))


def resolve_job_shim(env: Mapping[str, str]) -> str:
    return (env.get(JOB_SHIM_ENV) or "").strip() or DEFAULT_JOB_SHIM


def job_spec_path(spool_dir: str, instance: str) -> str:
    """`<spool>/<instance>.json`——與 `job_shim.resolve_spec_path()` 同一條推導。"""

    return f"{spool_dir.rstrip('/')}/{instance}.json"


def job_prompt_path(spool_dir: str, instance: str) -> str:
    """Return the private per-job prompt path in a dedicated prompt slot.

    The launcher passes this path, never prompt bytes, through a template or
    transient job command.  ``spool_dir`` is a Manager-owned, per-job prompt
    directory; it is deliberately never a job-log slot.  A job-log parent has
    ``wx`` access for the job and therefore lets the job rename a child
    directory even when that child is sticky.
    """

    if not isinstance(spool_dir, str) or not spool_dir.startswith("/"):
        raise _fail(
            "job-runner-prompt-path-invalid",
            f"prompt spool 必須是絕對路徑: {spool_dir!r}",
            source="job_prompt_path",
        )
    if not isinstance(instance, str) or not instance_name_valid(instance):
        raise _fail(
            "job-runner-prompt-path-invalid",
            f"prompt instance 不合法: {instance!r}",
            source="job_prompt_path",
        )
    return f"{spool_dir.rstrip('/')}/.prompt-{instance}"


def job_prompt_spool_path(
    spec_spool: str, *, principal: str, instance: str
) -> str:
    """Purely derive the canonical Manager-owned prompt slot directory."""

    if not isinstance(spec_spool, str) or not spec_spool.startswith("/"):
        raise _fail(
            "job-runner-prompt-spool-invalid",
            f"spec spool 必須是絕對路徑: {spec_spool!r}",
            source="job_prompt_spool_path",
        )
    if not isinstance(principal, str) or not re.fullmatch(
        r"[a-z][a-z0-9-]*", principal
    ):
        raise _fail(
            "job-runner-prompt-spool-invalid",
            f"prompt principal 不合法: {principal!r}",
            source="job_prompt_spool_path",
        )
    if not isinstance(instance, str) or not instance_name_valid(instance):
        raise _fail(
            "job-runner-prompt-spool-invalid",
            f"prompt instance 不合法: {instance!r}",
            source="job_prompt_spool_path",
        )
    spool = Path(spec_spool)
    if spool.name not in {principal, paths.JOB_SPEC_SPOOL_DIRNAME}:
        raise _fail(
            "job-runner-prompt-spool-invalid",
            (
                "spec spool basename must be the typed principal or the standalone "
                f"{paths.JOB_SPEC_SPOOL_DIRNAME!r} root: {spec_spool!r}"
            ),
            source="job_prompt_spool_path",
            principal=principal,
        )
    # Production paths are ``<coordinator>/job-specs/<principal>``.  A
    # compatibility/test deployment may point a role at a standalone
    # ``.../job-specs`` directory; in that shape its parent is the coordinator
    # root.  Both forms remain below the Manager-selected coordinator root.
    coordinator_root = spool.parent.parent if spool.name == principal else spool.parent
    return str(coordinator_root / PRIVATE_PROMPT_ROOT_DIRNAME / principal / instance)


def job_prompt_spool_dir(
    spec_spool: str, *, principal: str, instance: str, account: str | None = None
) -> str:
    """Prepare the Manager-owned prompt directory for one job.

    Prompt state is a sibling of the per-principal spec spool, not a child of
    a writable log slot.  The spec spool's parent is the Manager-owned
    ``job-specs`` container, so the prompt root has no job-writable ancestor.
    Each instance gets its own directory; the job receives only ``r-x`` on
    that directory and ``r--`` on the eventual prompt file.  Consequently a
    job can consume a prompt but cannot rename the directory or replace the
    Manager inode.
    """

    root = Path(spec_spool)
    if root.is_symlink() or not root.is_dir():
        raise _fail(
            "job-runner-prompt-spool-invalid",
            f"spec spool 不存在或是 symlink: {root}",
            source="job_prompt_spool_dir",
        )
    current = root
    while current != current.parent:
        if current.is_symlink():
            raise _fail(
                "job-runner-prompt-spool-invalid",
                f"spec spool path contains symlink: {current}",
                source="job_prompt_spool_dir",
            )
        current = current.parent
    # Keep this derivation paired with config.paths.job_spec_spool_for(): the
    # configured per-principal spool is one directory below the coordinator
    # job-specs container, so the dedicated prompt root is its sibling.
    prompt_slot = Path(
        job_prompt_spool_path(spec_spool, principal=principal, instance=instance)
    )
    prompt_root = prompt_slot.parent
    current = prompt_root
    while current != current.parent:
        if current.is_symlink():
            raise _fail(
                "job-runner-prompt-spool-invalid",
                f"prompt path contains symlink: {current}",
                source="job_prompt_spool_dir",
            )
        current = current.parent
    try:
        parent_info = root.stat()
        if parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) & 0o022:
            raise _fail(
                "job-runner-prompt-spool-invalid",
                f"spec spool parent is not Manager-owned/non-writable: {root}",
                source="job_prompt_spool_dir",
            )
        prompt_root.parent.mkdir(mode=0o700, exist_ok=True)
        prompt_parent_info = prompt_root.parent.stat()
        if (
            prompt_parent_info.st_uid != os.geteuid()
            or stat.S_IMODE(prompt_parent_info.st_mode) & 0o022
        ):
            raise _fail(
                "job-runner-prompt-spool-invalid",
                f"prompt root parent is not Manager-owned/non-writable: {prompt_root.parent}",
                source="job_prompt_spool_dir",
            )
        if account and _account_ids(account) is not None:
            # The parent is shared by builder/reviewer roots. Grant only
            # execute traversal here; the per-principal child below carries
            # the named r-x ACL and remains the only enumerable prompt root.
            _grant_prompt_traverse(str(prompt_root.parent), account=account)
        if not prompt_root.exists():
            prompt_root.mkdir(mode=0o710)
        prompt_root = Path(
            prepare_private_prompt_spool(str(prompt_root), account=account)
        )
        instance_dir = prompt_slot
        instance_dir = Path(
            prepare_private_prompt_spool(str(instance_dir), account=account)
        )
    except OSError as exc:
        raise _fail(
            "job-runner-prompt-spool-write-failed",
            f"cannot prepare private prompt directory: {exc}",
            source="job_prompt_spool_dir",
            principal=principal,
            instance=instance,
        ) from exc
    return str(instance_dir)


def reap_orphaned_prompt_slots(
    spec_spool: str,
    *,
    principal: str,
    active_instances: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Reap durable prelaunch prompt directories from a Manager context.

    Directory creation happens before the systemd client is spawned, so the
    directory itself is the durable prelaunch record.  A Manager restart can
    scan the typed principal root and remove inactive regular/symlink leaves
    without following untrusted links.  Unexpected special/nested entries are
    retained and reported, making a crash-window leak visible instead of
    silently discarding it.
    """

    root = Path(spec_spool)
    if root.is_symlink() or not root.is_dir():
        raise _fail(
            "job-runner-prompt-janitor-invalid",
            f"spec spool 不存在或是 symlink: {root}",
            source="reap_orphaned_prompt_slots",
        )
    principal_root = Path(
        job_prompt_spool_path(spec_spool, principal=principal, instance="janitor")
    ).parent
    if principal_root.is_symlink() or not principal_root.is_dir():
        return ()
    info = principal_root.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise _fail(
            "job-runner-prompt-janitor-invalid",
            f"prompt root is not Manager-owned/non-writable: {principal_root}",
            source="reap_orphaned_prompt_slots",
        )
    diagnostics: list[str] = []
    for slot in sorted(principal_root.iterdir(), key=lambda item: item.name):
        if slot.name in active_instances:
            continue
        try:
            info = slot.lstat()
        except OSError as exc:
            diagnostics.append(f"{slot}: {type(exc).__name__}: {exc}")
            continue
        if stat.S_ISLNK(info.st_mode):
            slot.unlink()
            continue
        if not stat.S_ISDIR(info.st_mode) or not instance_name_valid(slot.name):
            diagnostics.append(f"unexpected prompt slot entry: {slot}")
            continue
        for child in sorted(slot.iterdir(), key=lambda item: item.name):
            try:
                child_info = child.lstat()
            except OSError as exc:
                diagnostics.append(f"{child}: {type(exc).__name__}: {exc}")
                continue
            if stat.S_ISREG(child_info.st_mode) or stat.S_ISLNK(child_info.st_mode):
                child.unlink()
            else:
                diagnostics.append(f"unexpected prompt slot child: {child}")
        try:
            slot.rmdir()
        except OSError as exc:
            diagnostics.append(f"prompt slot retained {slot}: {type(exc).__name__}: {exc}")
    return tuple(diagnostics)


def _prompt_acl_binary() -> str | None:
    binary = shutil.which("setfacl")
    if binary is None or Path(binary).name != "setfacl":
        fallback = "/usr/bin/setfacl"
        binary = fallback if Path(fallback).is_file() else None
    return binary


def _grant_prompt_traverse(directory: str, *, account: str) -> None:
    """Give one job only the execute bit needed to reach its prompt root."""

    target = Path(directory)
    if target.is_symlink() or not target.is_dir():
        raise _fail(
            "job-runner-prompt-spool-invalid",
            f"prompt traversal parent is malformed: {target}",
            source="_grant_prompt_traverse",
            account=account,
        )
    info = target.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise _fail(
            "job-runner-prompt-spool-invalid",
            f"prompt traversal parent is not Manager-owned/non-writable: {target}",
            source="_grant_prompt_traverse",
            account=account,
        )
    binary = _prompt_acl_binary()
    if binary is None:
        raise _fail(
            "job-runner-prompt-acl-unavailable",
            f"找不到 setfacl，無法讓 {account} traverse prompt root parent",
            source="_grant_prompt_traverse",
            account=account,
        )
    argv = (binary, "-m", f"u:{account}:--x", str(target))
    if os.spawnv(os.P_WAIT, binary, argv) != 0:
        raise _fail(
            "job-runner-prompt-acl-failed",
            f"無法讓 {account} traverse prompt root parent: {target}",
            source="_grant_prompt_traverse",
            account=account,
        )


def prepare_private_prompt_spool(
    directory: str, *, account: str | None = None
) -> str:
    """Create/validate one Manager-owned, non-renameable prompt directory.

    Sticky bit is not the boundary here: a process with ``wx`` on the parent
    can rename the sticky directory itself.  The directory is therefore
    normalized to owner ``rwx`` with no group/other write, and the job gets a
    named ``r-x`` ACL only.  Callers must place it below a Manager-owned
    parent; :func:`job_prompt_spool_dir` supplies the canonical placement.
    """

    target = Path(directory)
    if not target.is_absolute():
        raise _fail(
            "job-runner-prompt-spool-invalid",
            f"private prompt spool 必須是絕對路徑: {directory!r}",
            source="prepare_private_prompt_spool",
        )
    current = target
    while current != current.parent:
        if current.is_symlink():
            raise _fail(
                "job-runner-prompt-spool-invalid",
                f"private prompt spool path contains symlink: {current}",
                source="prepare_private_prompt_spool",
            )
        current = current.parent
    try:
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise _fail(
                    "job-runner-prompt-spool-invalid",
                    f"private prompt spool is malformed: {target}",
                    source="prepare_private_prompt_spool",
                )
            info = target.stat()
            mode = stat.S_IMODE(info.st_mode)
            if info.st_uid != os.geteuid() or mode & 0o022:
                raise _fail(
                    "job-runner-prompt-spool-invalid",
                    f"private prompt spool is not Manager-owned/non-writable: {target}",
                    source="prepare_private_prompt_spool",
                )
            os.chmod(target, 0o710)
        else:
            target.mkdir(mode=0o710)
            os.chmod(target, 0o710)
    except OSError as exc:
        raise _fail(
            "job-runner-prompt-spool-write-failed",
            f"cannot prepare private prompt spool {target}: {exc}",
            source="prepare_private_prompt_spool",
        ) from exc
    if account and _account_ids(account) is not None:
        binary = _prompt_acl_binary()
        if binary is None:
            raise _fail(
                "job-runner-prompt-acl-unavailable",
                f"找不到 setfacl，無法讓 {account} traverse private prompt spool",
                source="prepare_private_prompt_spool",
                account=account,
            )
        acl = f"u:{account}:r-x,m::r-x"
        argv = (binary, "-m", acl, str(target))
        if os.spawnv(os.P_WAIT, binary, argv) != 0:
            raise _fail(
                "job-runner-prompt-acl-failed",
                f"無法讓 {account} traverse private prompt spool: {target}",
                source="prepare_private_prompt_spool",
                account=account,
            )
    return str(target)


def write_job_prompt(
    prompt_path: str,
    prompt: str,
    *,
    account: str | None = None,
    max_bytes: int = MAX_JOB_PROMPT_BYTES,
) -> str:
    """Atomically publish a bounded, Manager-owned prompt file.

    A template unit has stdin connected to ``/dev/null`` by design.  Passing a
    long workflow envelope as a shell/CLI argument consequently fails twice:
    Linux rejects oversized argv elements, and a stdin workaround receives EOF.
    This channel is the single production path for isolated Claude/CG jobs.
    """

    if not isinstance(prompt_path, str) or not prompt_path.startswith("/"):
        raise _fail(
            "job-runner-prompt-path-invalid",
            f"private prompt path 必須是絕對路徑: {prompt_path!r}",
            source="write_job_prompt",
        )
    if not isinstance(prompt, str):
        raise _fail(
            "job-runner-prompt-invalid",
            "private prompt 必須是字串",
            source="write_job_prompt",
        )
    payload = prompt.encode("utf-8")
    if max_bytes <= 0 or len(payload) > max_bytes:
        raise _fail(
            "job-runner-prompt-too-large",
            f"private prompt 超過 {max_bytes} bytes bound（收到 {len(payload)}）",
            source="write_job_prompt",
            prompt_path=prompt_path,
            prompt_bytes=len(payload),
            max_bytes=max_bytes,
        )
    target = Path(prompt_path)
    if target.is_symlink():
        raise _fail(
            "job-runner-prompt-shape-invalid",
            f"private prompt 不得是 symlink: {target}",
            source="write_job_prompt",
        )
    directory = target.parent
    current = directory
    while current != current.parent:
        if current.is_symlink():
            raise _fail(
                "job-runner-prompt-spool-invalid",
                f"private prompt spool path contains symlink: {current}",
                source="write_job_prompt",
            )
        current = current.parent
    if not directory.is_dir() or directory.is_symlink():
        raise _fail(
            "job-runner-prompt-spool-invalid",
            f"private prompt spool 不存在或是 symlink: {directory}",
            source="write_job_prompt",
        )
    directory_info = directory.stat()
    if (
        directory_info.st_uid != os.geteuid()
        or stat.S_IMODE(directory_info.st_mode) & 0o022
    ):
        raise _fail(
            "job-runner-prompt-spool-invalid",
            f"private prompt parent is not Manager-owned/non-writable: {directory}",
            source="write_job_prompt",
        )
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".prompt-", suffix=".tmp", dir=str(directory))
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
        os.chmod(target, 0o600)
    except OSError as exc:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise _fail(
            "job-runner-prompt-write-failed",
            f"寫不進 private prompt {target}: {exc}",
            source="write_job_prompt",
            prompt_path=str(target),
        ) from exc

    if account and _account_ids(account) is not None:
        binary = _prompt_acl_binary()
        if binary is None:
            target.unlink(missing_ok=True)
            raise _fail(
                "job-runner-prompt-acl-unavailable",
                f"找不到 setfacl，無法把 private prompt 唯讀交給 {account}",
                source="write_job_prompt",
                prompt_path=str(target),
                account=account,
            )
        acl = f"u:{account}:r--,m::r--"
        argv = (binary, "-m", acl, str(target))
        if os.spawnv(os.P_WAIT, binary, argv) != 0:
            target.unlink(missing_ok=True)
            raise _fail(
                "job-runner-prompt-acl-failed",
                f"無法把 private prompt 唯讀交給 {account}: {target}",
                source="write_job_prompt",
                prompt_path=str(target),
                account=account,
            )
        ok, why = _spec_readable_by(str(target), account)
        if not ok:
            target.unlink(missing_ok=True)
            raise _fail(
                "job-runner-prompt-unreadable-by-job",
                f"private prompt 落地但 {account} 讀不到: {why}",
                source="write_job_prompt",
                prompt_path=str(target),
                account=account,
            )
    return str(target)


def forbidden_spec_keys(spec: Mapping[str, object]) -> list[str]:
    """spec 內出現的 :data:`SPEC_FORBIDDEN_KEYS`（排序後）。空 list＝乾淨。

    **寫端（`build_job_spec`）與讀端（`job_shim.load_spec`）呼叫的是這同一支**，
    兩邊只在「raise 哪一種例外」上不同。抽出來不是為了少寫一行，而是為了讓「兩端
    掃的是同一份判準」成為結構事實而非約定——這條在 #643 之後承載的東西更多了
    （不只身分，還有加固剖面）。
    """

    return sorted(key for key in SPEC_FORBIDDEN_KEYS if key in spec)


def malformed_job_command(command: object) -> str | None:
    """job spec 的 `command` 合法嗎？不合法時回一句可讀理由，合法回 `None`。

    **與 `forbidden_spec_keys()` 同一個模式**：寫端（:func:`build_job_spec`）與讀端
    （`job_shim.load_spec`）各呼叫一次**同一支**函式，兩端的判準因此不可能漂移。
    #679 買過一次「同一件事兩份實作」的單，這裡不再重演。

    ## 判準：`argv` 非空、`argv[0]` 非空、每個元素都是 `str`

    `argv[0]` 是要 exec 的程式名——空字串沒有任何意義，`execvpe("", …)` 的失敗訊息
    也不可讀，因此 fail-closed。**其餘元素可以是任何字串，包含空字串**：那是 POSIX
    argv 本來的語意。

    ## 為什麼「其餘元素也必須非空」這條要拿掉（#687／#672 票 F）

    原判準是 `not all(argv)`，寫在 `fe7d5f5`（三分 UID 定案）——當時 spec 的唯一
    產生者是 builder 的 `launcher`，它組出來的 argv 從來沒有空元素，因此這條判準
    **從未被任何真實 argv 證偽過**。票 E（#686）把 planning 接上同一條通道之後，
    `planning_runtime._planning_argv()` 對 `claude` 產出的
    `["claude", "-p", …, "--tools", "", …]` 在**每一次** define 都撞上它：

        job-runner-job-spec-invalid: spec 的 command 不得為空、且每個元素都必須是
        非空字串 (source=job_runner.build_job_spec)

    而 `--tools ""` 是 CLI 的成文 API（`claude --help` 逐字：`Use "" to disable all
    tools`），也是 #404 之後 planning「模型完全沒有工具可呼叫」的唯一保證，不能改。
    實測過的「等價寫法」`--tools=` **會讓模型拿回全部工具**（真實 unit 加固面下量到
    模型發出 Bash 呼叫），那是靜默放寬，比這條守衛壞得多。

    ## 放寬的安全論證

    這條**不是**信任控制，是 well-formedness 檢查。spec 上真正承重的三道是
    :func:`forbidden_spec_keys`（身分／加固剖面結構性禁止）、
    :func:`reject_unsafe_env`（憑證與 `LD_PRELOAD`）、以及
    `working_directory`／`log_path` 的絕對路徑要求——**沒有一道靠元素長度**。

    而 argv 裡**早就**有任意的、攻擊者可影響的字串：planning 的 prompt 逐字含
    untrusted issue 內容，它就是 `argv[2]`。在那個前提下多允許一個空字串換不到任何
    新能力——空字串命名不了程式、指不到路徑、夾帶不了 token。相對地，繼續禁止它換到
    的是「claude 這個 planning／reviewer 的預設 executor 結構性派不出 job」。
    """

    if not isinstance(command, (list, tuple)):
        return "spec 的 command 必須是字串陣列"
    if not command:
        return "spec 的 command 不得為空"
    if not all(isinstance(item, str) for item in command):
        return "spec 的 command 每個元素都必須是字串"
    if not command[0]:
        return "spec 的 command[0]（要 exec 的程式名）不得為空字串"
    return None


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

    #687：`command` 的合法性判準搬進 :func:`malformed_job_command`——與
    `forbidden_spec_keys()` 同一個模式，讀端（shim）呼叫**同一支**函式。
    先做 `str()` 正規化再驗，是因為本函式的型別契約是 `Sequence[str]`
    （呼叫端全部逐字給 str，見 `launcher`／`gate_runner`／`planning_job`）；
    shim 那端沒有型別契約可言（bytes 剛從磁碟讀回來），因此它驗的是原值。
    """

    argv = [str(item) for item in command]
    problem = malformed_job_command(argv)
    if problem is not None:
        raise _fail(
            "job-runner-job-spec-invalid",
            problem,
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
    # #712：git 放行**必須恰好是這個 job 的那一格**。
    #
    # 這條是「per-job 而非全域」的結構性保證，而不只是一句宣稱：env 是在別處算的
    # （`build_job_env(workspace=…)`），spec 的 `working_directory` 是在這裡定的，
    # 兩者一旦指向不同的路徑，放行的就不是這個 job 正要進去的那一格。判準是**逐字
    # 相等**，因為 git 比對 `safe.directory` 也是逐字相等（實測 git 2.43：多一個
    # 尾斜線就不算數）。
    #
    # 刻意不在這裡 `resolve()`：本函式的契約是「純資料，無 IO」。解析在
    # `git_workspace_trust_env()` 那一側做，呼叫端把**同一個已解析的字串**同時交給
    # 兩邊（見 `launcher`／`planning_job`／`gate_runner`）。
    for granted in git_config_safe_directories(env):
        if granted != str(working_directory):
            raise _fail(
                "job-runner-git-config-value-invalid",
                f"env 放行的 git 工作區 {granted!r} 不是 spec 的 working_directory "
                f"{str(working_directory)!r}——放行必須**逐 job**，指到別處的那一條"
                "既救不了這個 job，又擴大了放行面（#712）。"
                "兩邊必須由呼叫端以同一個**已解析**（physical）路徑字串同時給出：git "
                "比對的是 `getcwd()` 之後的真實路徑，逐字相等（實測 git 2.43）。",
                source="build_job_spec",
                instance=instance,
            )
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
    *, client_argv: Sequence[str], sentinel: str, cleanup_path: str | None = None
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
    if cleanup_path is not None and not cleanup_path.startswith("/"):
        raise _fail(
            "job-runner-exit-recorder-invalid",
            f"cleanup path 必須是絕對路徑: {cleanup_path!r}",
            source="build_manager_exit_recorder_argv",
            cleanup_path=cleanup_path,
        )
    cleanup = ""
    if cleanup_path is not None:
        # The Manager shell owns this cleanup.  The job only receives read ACL
        # on the prompt and cannot remove or replace the Manager inode.
        cleanup = f"; rm -f -- {shlex.quote(cleanup_path)} 2>/dev/null || :"
    script = (
        f"{shlex.join(list(client_argv))}; rc=$?; "
        f"printf %s \"$rc\" > {shlex.quote(sentinel)}{cleanup}; exit \"$rc\""
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
    job_log_path: str | None = None,
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
            f"`journalctl -u {unit}`）{read_shim_error(job_log_path)}{_log_tail(log_path)}"
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


def read_shim_error(job_log_path: str | None, *, limit: int = 400) -> str:
    """撿回 shim 在**接管 log 之前**留下的那一筆機器可讀失敗紀錄（#708 第 3 項）。

    在此之前，這一族失敗（instance 名非法、spec 缺席／schema 不合、`PATH` 兩層皆缺、
    **log 開不起來**）的逐字原因只進 unit journal，而 Manager 帳號讀不到那份 journal
    ——它看得到的只有 `systemctl exit=1`。shim 現在把同一段訊息另外寫進 job 自己那一格
    log spool（`job_shim.SHIM_ERROR_FILENAME`），本函式把它接回錯誤訊息。

    `job_log_path` 是 **job 端**的 log 路徑（spec 裡那一條），不是 Manager 的 harvest
    路徑——紀錄落在 job 寫得進去的那一格，而 Manager 恰好也是那一格的 owner。

    讀不到／格式不符一律回空字串：診斷用的補充資訊不該反過來變成新的失敗來源，
    而且這份紀錄由 job 帳號寫 ⇒ **可被偽造，不進任何採信路徑**，只用來指路。
    """

    if not job_log_path:
        return ""
    from .job_shim import SHIM_ERROR_FILENAME, SHIM_ERROR_SCHEMA

    try:
        raw = (Path(job_log_path).parent / SHIM_ERROR_FILENAME).read_text(
            encoding="utf-8", errors="replace"
        )
        record = json.loads(raw)
    except (OSError, ValueError):
        return ""
    if not isinstance(record, dict) or record.get("schema") != SHIM_ERROR_SCHEMA:
        return ""
    detail = str(record.get("error") or "").strip()
    if not detail:
        return ""
    return f"；shim 回報（job 端紀錄，僅供排查）: {detail[:limit]}"


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


# ---------------------------------------------------------------------------
# #710：工作區可達性——三個角色由 `registry.JOB_WORKSPACE_REACH` 一條規則導出
# ---------------------------------------------------------------------------

_PERM_BIT_BY_LETTER: Mapping[str, int] = MappingProxyType(
    {"r": _PERM_R, "w": _PERM_W, "x": _PERM_X}
)


def _required_perm_bits(perms: str) -> int:
    bits = 0
    for letter in perms.lower():
        bit = _PERM_BIT_BY_LETTER.get(letter)
        if bit is None:
            raise _fail(
                "job-workspace-required-perms-invalid",
                f"不認得的權限字母 {letter!r}（{perms!r}）",
                source="_required_perm_bits",
            )
        bits |= bit
    return bits


def workspace_acl_grants(
    env: Mapping[str, str], *, role: str
) -> tuple[job_workspace.WorkspaceAclGrant, ...]:
    """該角色的 per-job 工作區要下哪幾條具名 ACL（#710）。

    帳號**逐條由 `JOB_ROLE_CONFIG` 解**（`resolve_job_account(role=…)`），因此
    「gate 那條 `rX` 授給誰」與「gate job 以誰的身分執行」永遠是同一個部署決定的同
    一個值——兩邊各自解析就是 #657 的失效模式（ACL 產在一個帳號上、unit 以另一個
    帳號執行）。
    """

    config = resolve_job_role(role)
    grants: list[job_workspace.WorkspaceAclGrant] = []
    for spec in config.workspace_acl:
        grants.append(
            job_workspace.WorkspaceAclGrant(
                account=resolve_job_account(env, role=spec.role_id),
                access_perms=spec.access_perms,
                default_perms=spec.default_perms,
            )
        )
    return tuple(grants)


def _per_job_pool_root() -> Path | None:
    """本部署宣告的 per-job 工作區 pool 根；解不出來即 `None`（#710）。

    解不出來**不是**錯誤：`paths.worktree_root()` 在未宣告 `PSC_REPO_ROOT` 時
    fail-closed（#612／#630／#633），而那個情境在派工路徑上早就先炸過了——
    `seams.ScriptWorktreeCreator` 建 clone 用的是同一支。這裡回 `None` 的意思只有
    一個：「本呼叫端拿不到 pool 的位置，因此判定不了這一格是不是 pool 底下的 per-job
    那一格」，處置一律是**不動**（見 :func:`ensure_workspace_reachable`）。
    """

    try:
        return paths.worktree_root().resolve()
    except Exception:  # noqa: BLE001 - 解不出 pool 位置一律退化成「不判定」
        return None


def ensure_workspace_reachable(
    env: Mapping[str, str], *, role: str, workspace: str | Path
) -> str | None:
    """**派工之前**讓（並確認）這個 job 進得去自己的工作區；回傳實際執行的命令或 `None`。

    這是 #710 那條規則的執行期一側，三個角色**共用同一支**——分岔只有一處，而且
    分岔的依據是 `JOB_ROLE_CONFIG[...].workspace_reach`（＝
    `registry.JOB_WORKSPACE_REACH` 的成對契約），不是 `if role == …`：

    ``per-job-named-acl``（builder）
        Manager 對**那一格**遞迴下具名 ACL。`chown` 不是選項——它需要 `CAP_CHOWN`，
        而 Manager unit 帶 `CapabilityBoundingSet=`（空）。這一段就是 #710 的修法本體。

    ``inherited-default-acl``（reviewer／planner）／``pool-owned-by-job``（gate）
        **零動作**——可達性分別由 pool 根的 default ACL 與 owner 位供給，兩者都在
        部署當下就成立。

    三者**都要**通過同一道驗證：job 帳號對工作區的 **effective** 權限（mask 之後）
    必須涵蓋 `workspace_required_perms`。判準是 `mask::`／`#effective:` 而不是
    「ACL 行存在」——`setfacl -m u:x:rwX` 之後任何一次 `chmod` 都會把 mask 打成
    `---`，具名條目於是靜默失效（runbook 4e-2b 的陷阱）。

    **帳號不在 passwd 時整支略過**（回 `None`）：那代表這台機器沒有三分身分（單 UID
    的 `direct` 模式、開發機、CI），這條性質在那裡沒有可驗的語意。這與
    `_spec_readable_by()` 的既有處置逐字相同，且**不是** fail-open：真正的降權派工
    路徑在此之前已經由 `prepare_systemd_template()` 對帳號存在性 fail-closed。
    """

    config = resolve_job_role(role)
    if config.workspace_reach != WORKSPACE_REACH_PER_JOB_NAMED_ACL:
        # 形態說「執行期零動作」就是零動作。可達性在部署當下由 pool 根的 owner 位
        # （gate）或 default ACL（reviewer／planner）供給，而**那是權限計畫的性質**
        # ——它由 `permgen._assert_job_workspace_reach_matches_the_plan()` 在 import
        # 當下、由 `tests/test_per_job_workspace_acl_710.py` 在測試面、由
        # `trust_root workspace-probe` 在實機面各驗一次。在每一次派工上再驗一次不會
        # 讓它更真，只會把一個部署性質變成一個逐 job 的失敗面。
        return None
    target = str(workspace)
    resolved = Path(target).resolve()
    pool = _per_job_pool_root()
    if pool is not None and resolved == pool:
        # ⚠️ 本票兩個硬性注意事項的第一個。pool 根是三個 job 帳號共用的容器，把授權
        # 下在它身上會讓**每個** job 帳號進得去**每個** job 的目錄（裁決 10-2 當場
        # 歸零）。這是 fail-closed 而不是「往上一層也套一份」。
        raise _fail(
            "job-workspace-acl-target-is-the-pool-root",
            f"{target} 是 per-job 工作區 pool 的**根**，不是某個 job 的那一格——"
            "在 pool 根上授權會讓每個 job 帳號進得去每個 job 的目錄（#710）。",
            source="ensure_workspace_reachable",
            role=role,
            workspace=target,
            pool_root=str(pool),
        )
    if pool is None or resolved.parent != pool:
        # 這一格不在本部署宣告的 per-job pool 底下 ⇒ 它不是 `repo-worktree` 那個資產，
        # 本形態不適用。真實派工路徑上 builder 的工作區恆為
        # `job_workspace.workspace_path(paths.worktree_root(), job_id)`（`seams` 與
        # 本函式共用同一支推導），落到這一支的是**別的**東西：workflow lane 的 review
        # sandbox、注入合成路徑的測試、以及尚未宣告 pool 的部署。對它們套一條遞迴 ACL
        # 是「對不認識的路徑動手」，比不動危險。
        return None
    grants = workspace_acl_grants(env, role=role)
    account = resolve_job_account(env, role=role)
    if _account_ids(account) is None:
        # 本機沒有三分身分（單 UID 的 `direct` 模式、開發機、CI）——這條性質在那裡
        # 沒有可驗的語意。**不是 fail-open**：真正的降權派工在此之前已由
        # `prepare_systemd_template()` 對帳號存在性 fail-closed。
        return None
    applied: str | None = None
    try:
        applied = job_workspace.grant_workspace_acl(target, grants)
    except job_workspace.WorkspaceError as exc:
        raise _fail(
            "job-workspace-acl-grant-failed",
            str(exc),
            source="ensure_workspace_reachable",
            role=role,
            workspace=target,
            account=account,
        ) from exc
    bits = effective_perms_for_account(target, account)
    required = _required_perm_bits(config.workspace_required_perms)
    if bits is None:
        raise _fail(
            "job-workspace-unreachable",
            f"{target}：無法判定 {account} 的 effective 權限（stat 失敗？）",
            source="ensure_workspace_reachable",
            role=role,
            workspace=target,
            account=account,
        )
    if bits & required != required:
        raise _fail(
            "job-workspace-unreachable",
            (
                f"{target}：{account} 的 effective={_perm_str(bits)}，"
                f"不足 {config.workspace_required_perms}——shim 在降權之後 "
                f"`os.chdir()` 會 EACCES，job 死在它能記錄失敗之前（#710）。"
                f"已套用的 ACL：{applied}。"
                "ACL 套上去了卻沒有生效，最可能的原因是**之後又 chmod 了一次**——"
                "那會把 ACL mask 重寫成 mode 的 group 位，具名條目靜默失效"
                "（runbook 4e-2b）。判準永遠是 `getfacl` 的 `mask::` 與 "
                "`#effective:`，不是「ACL 行存在」。"
            ),
            source="ensure_workspace_reachable",
            role=role,
            workspace=target,
            account=account,
        )
    return applied
