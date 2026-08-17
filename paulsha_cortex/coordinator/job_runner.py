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
  Manager-owned spool（登記表資產 `job-spec-spool`，job 帳號唯讀），
  `ExecStart=` 的 root-owned shim 讀完才 exec 真正的 job
  （見 :mod:`paulsha_cortex.coordinator.job_shim`）。
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
from typing import Callable, Mapping, Sequence

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
    "DEFAULT_JOB_SHIM",
    "DEFAULT_JOB_SPEC_SPOOL",
    "DEFAULT_START_TIMEOUT_MS",
    "DEFAULT_TEMPLATE_UNIT",
    "ForwardedEnvVar",
    "JOB_RUNNER_ENV",
    "JOB_SHIM_ENV",
    "JOB_SPEC_SPOOL_ENV",
    "JOB_SPEC_VERSION",
    "JobRunnerError",
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
    "TRANSIENT_UNIT_PROPERTIES",
    "UNIT_NAME_PREFIX",
    "build_builder_env",
    "build_job_spec",
    "build_manager_exit_recorder_argv",
    "build_systemctl_start_argv",
    "build_systemd_run_argv",
    "confirm_template_instance_started",
    "confirm_transient_unit_started",
    "instance_name_valid",
    "job_spec_path",
    "preflight_systemd_run",
    "preflight_systemd_template",
    "prepare_systemd_run",
    "prepare_systemd_template",
    "reject_unsafe_env",
    "template_instance_id",
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

#: builder 的 PATH 覆寫。未設時轉發 Manager 的 PATH（見 `BUILDER_FORWARDED_ENV`）；
#: Phase 2b 若把模型 CLI 裝在 builder 才讀得到的路徑，用這個覆寫。
BUILDER_PATH_ENV = "PSC_BUILDER_PATH"

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
TEMPLATE_UNIT_ENV = "PSC_JOB_TEMPLATE_UNIT"
TEMPLATE_UNIT_PREFIX = "cortex-job@"
TEMPLATE_UNIT_SUFFIX = ".service"
DEFAULT_TEMPLATE_UNIT = f"{TEMPLATE_UNIT_PREFIX}{TEMPLATE_UNIT_SUFFIX}"

#: Manager-owned 的 per-job spec spool（登記表資產 `job-spec-spool`）。
JOB_SPEC_SPOOL_ENV = "PSC_JOB_SPEC_SPOOL"
DEFAULT_JOB_SPEC_SPOOL = "/var/lib/cortex/coordinator/job-specs"

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

#: spec **絕不可**出現的欄位——身分只有一個來源：root-owned unit 檔的 `User=`。
#: 這是 B 案全部價值的所在，因此在寫端與讀端各擋一次。
SPEC_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {"user", "group", "uid", "gid", "User", "Group", "properties", "exec_start"}
)

#: systemd unit 實例名允許的字元。systemd 本身還允許更多（`/` 需 escape），這裡
#: 刻意更窄：instance 名會被 polkit 的 unit pattern 比對，也會被拼成 spec 檔名。
INSTANCE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")


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


def resolve_builder_account(env: Mapping[str, str]) -> str:
    """builder 的 OS 帳號名（`PSC_BUILDER_ACCOUNT`，預設 `cortex-builder`）。"""

    return _resolve_identity(
        env,
        key=BUILDER_ACCOUNT_ENV,
        default=DEFAULT_BUILDER_ACCOUNT,
        source="resolve_builder_account",
    )


def resolve_builder_group(env: Mapping[str, str]) -> str:
    """builder 的 primary group（`PSC_BUILDER_GROUP`，預設＝帳號名）。

    預設值沿用 `trust_root.permgen.UidScheme.group_of()`：每帳號一個同名 group。
    """

    return _resolve_identity(
        env,
        key=BUILDER_GROUP_ENV,
        default=resolve_builder_account(env),
        source="resolve_builder_group",
    )


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
        "PATH",
        # 沒有 PATH，transient unit 只會拿到 PID 1 的 manager PATH（通常是
        # /usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin），裡面沒有 npm global／
        # pipx shim，`claude`／`codex`／`copilot` 一律 127；wrapper 內的 `python3`
        # （gate ledger writer，見 launcher.build_wrapper_script）與 `git`（builder
        # 要 commit candidate）同樣靠它解析。可用 PSC_BUILDER_PATH 覆寫成 builder
        # 帳號讀得到的路徑。
        "模型 CLI（claude／codex／copilot）、wrapper 的 python3 gate writer 與 git 的解析路徑",
    ),
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
BUILDER_SYNTHESIZED_ENV = (
    "HOME",
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


def build_builder_env(
    *,
    manager_env: Mapping[str, str],
    job_id: str,
    slice_id: str,
    repo_root: str,
    relay_target: str | None = None,
) -> dict[str, str]:
    """算出 builder transient unit 的**完整**環境（白名單，非黑名單 scrub）。

    回傳值就是會逐項變成 `--setenv=` 的內容——沒列在這裡的名字不會出現在 job 的
    環境裡，因為 transient unit 本來就不繼承呼叫端的 environ。
    """

    env: dict[str, str] = {}
    for forwarded in BUILDER_FORWARDED_ENV:
        value = manager_env.get(forwarded.name)
        if value:
            env[forwarded.name] = value
    path_override = (manager_env.get(BUILDER_PATH_ENV) or "").strip()
    if path_override:
        env["PATH"] = path_override
    home = (manager_env.get(BUILDER_HOME_ENV) or "").strip()
    if home:
        env["HOME"] = home
    env["PSC_SLICE_ID"] = slice_id
    env["PSC_JOB_ID"] = job_id
    env["PSC_REPO_ROOT"] = repo_root
    if relay_target is not None:
        env["PSC_RELAY_TARGET"] = relay_target
    _reject_unsafe(env, source="build_builder_env")
    return env


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
            f"builder 帳號不存在: {account}（Phase 2b runbook 第 1 步尚未執行？）",
            source="preflight_systemd_run",
            account=account,
        )
    exists_group = group_exists or _group_exists
    if not exists_group(group):
        raise _fail(
            "job-runner-builder-group-missing",
            f"builder group 不存在: {group}",
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


def prepare_systemd_run(env: Mapping[str, str], *, job_id: str) -> SystemdRunPlan:
    """降權派工的前置：解析身分 config、跑靜態 preflight、算出 transient unit 名。

    **在任何副作用之前呼叫**——這裡的每一個 raise 都代表本次派工不該發生，呼叫端
    必須讓它往上傳（fail-closed），不得改走 direct。
    """

    account = resolve_builder_account(env)
    group = resolve_builder_group(env)
    binary = preflight_systemd_run(account=account, group=group)
    return SystemdRunPlan(
        binary=binary,
        unit=transient_unit_name(job_id),
        account=account,
        group=group,
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

    return bool(name) and INSTANCE_NAME_RE.fullmatch(name) is not None


def template_instance_id(job_id: str) -> str:
    """job_id → systemd 模板實例名（`cortex-job@<這個>.service`）。

    與 :func:`transient_unit_name` 同一套「可追蹤 ＋ 唯一」的推導，差別只在這裡
    產出的是**實例名**而不是完整 unit 名：消毒後的可讀片段 ＋ job_id 的 sha256
    前 8 碼（消毒後相同也不會撞名）。
    """

    raw = str(job_id).strip()
    if not raw:
        raise _fail(
            "job-runner-instance-name-invalid",
            "job_id 為空，無法組出可追蹤的模板實例名",
            source="template_instance_id",
        )
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", raw).strip("-")[:48]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    if not slug or not slug[0].isalnum():
        slug = f"job{slug}" if slug else "job"
    instance = f"{slug}-{digest}"
    if not instance_name_valid(instance):
        raise _fail(
            "job-runner-instance-name-invalid",
            f"推導出的模板實例名不合法: {instance!r}（job_id={raw!r}）",
            source="template_instance_id",
            job_id=raw,
        )
    return instance


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


def resolve_template_unit(env: Mapping[str, str]) -> str:
    return (env.get(TEMPLATE_UNIT_ENV) or "").strip() or DEFAULT_TEMPLATE_UNIT


def resolve_job_spec_spool(env: Mapping[str, str]) -> str:
    return (env.get(JOB_SPEC_SPOOL_ENV) or "").strip() or DEFAULT_JOB_SPEC_SPOOL


def resolve_job_shim(env: Mapping[str, str]) -> str:
    return (env.get(JOB_SHIM_ENV) or "").strip() or DEFAULT_JOB_SHIM


def job_spec_path(spool_dir: str, instance: str) -> str:
    """`<spool>/<instance>.json`——與 `job_shim.resolve_spec_path()` 同一條推導。"""

    return f"{spool_dir.rstrip('/')}/{instance}.json"


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
    leaked = sorted(SPEC_FORBIDDEN_KEYS & set(spec))
    if leaked:
        raise _fail(
            "job-runner-job-spec-invalid",
            f"spec 不得攜帶身分／特權欄位 {leaked}——身分只由 root-owned unit 的 User= 決定",
            source="build_job_spec",
            instance=instance,
        )
    return spec


def write_job_spec(spec_path: str, spec: Mapping[str, object]) -> str:
    """把 spec **原子**寫進 Manager-owned spool；失敗即 fail-closed。

    原子性（同目錄 temp ＋ `os.replace`）不是潔癖：spec 是 job 的命令列，一個被
    讀到一半的檔會變成「執行了半條命令」。`os.replace` 在同一個檔案系統上是
    rename(2)，讀端只會看到舊的或新的完整內容。

    mode 明確設 `0o640` 而不是靠 umask：Manager unit 的 `UMask=0077` 會讓新檔出生
    即 `0600`，而 group 位全關會**連帶把繼承下來的 ACL mask 也關掉**，job 帳號的
    唯讀 ACL 因此形同虛設（讀 spec 會 EACCES）。group 仍是 Manager 自己的 group，
    所以放寬 group-read 不會讓第三方讀到。
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
) -> str:
    """模板模式的靜態檢查；任一項不成立即 fail-closed，回傳 systemctl 絕對路徑。

    比 A 案多三條，對應「本模式生效需要 Phase 2b 安裝」的三個前置物：模板 unit 檔、
    root-owned shim、Manager-owned spec spool。**任何一條都不會退回其他模式**——
    「以為降權生效但其實沒有」正是這整條票要消除的失效模式。

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
            f"builder 帳號不存在: {account}（Phase 2b runbook 第 1 步尚未執行？）",
            source="preflight_systemd_template",
            account=account,
        )
    exists_group = group_exists or _group_exists
    if not exists_group(group):
        raise _fail(
            "job-runner-builder-group-missing",
            f"builder group 不存在: {group}",
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
            f"job spec spool 目錄不存在: {spool_dir}（Phase 2b 權限套用尚未執行？）",
            source="preflight_systemd_template",
            spool_dir=spool_dir,
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


def prepare_systemd_template(
    env: Mapping[str, str],
    *,
    job_id: str,
    unit_active: Callable[[str, str], bool] | None = None,
) -> SystemdTemplatePlan:
    """模板派工的前置：解析 config、靜態 preflight、算 instance／unit／spec 路徑，
    並確認**同名 instance 沒有正在跑**。

    最後一條特別重要：`systemctl start` 對一個**已經 active** 的 unit 會直接回 0，
    什麼都不做。少了這個檢查，Manager 會以為自己起了一個 job，實際上是掛在別人的
    unit 上等——而且 `--wait` 會一路等到那個別人的 job 結束。

    **在任何副作用之前呼叫**：這裡每一個 raise 都代表本次派工不該發生。
    """

    account = resolve_builder_account(env)
    group = resolve_builder_group(env)
    template = resolve_template_unit(env)
    shim = resolve_job_shim(env)
    spool_dir = resolve_job_spec_spool(env)
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
