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

本模組只組字串與做唯讀探測，**不執行任何 root 操作、不建帳號、不寫 polkit**。
"""
from __future__ import annotations

import grp
import hashlib
import os
import pwd
import re
import shutil
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
    "DEFAULT_START_TIMEOUT_MS",
    "ForwardedEnvVar",
    "JOB_RUNNER_ENV",
    "JobRunnerError",
    "RUNNER_DIRECT",
    "RUNNER_MODES",
    "RUNNER_SYSTEMD_RUN",
    "START_TIMEOUT_ENV",
    "SystemdRunPlan",
    "TRANSIENT_UNIT_PROPERTIES",
    "UNIT_NAME_PREFIX",
    "build_builder_env",
    "build_systemd_run_argv",
    "confirm_transient_unit_started",
    "preflight_systemd_run",
    "prepare_systemd_run",
    "resolve_builder_account",
    "resolve_builder_group",
    "resolve_runner_mode",
    "resolve_start_timeout_ms",
    "transient_unit_name",
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
RUNNER_MODES = (RUNNER_DIRECT, RUNNER_SYSTEMD_RUN)

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
    which: Callable[[str], str | None] = shutil.which,
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

    resolved = which("systemd-run")
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

    deadline = monotonic() + max(timeout_ms, 0) / 1000.0
    while True:
        status = process.poll()
        if status is not None:
            if Path(sentinel).exists():
                # job 真的跑完了（且已寫下 exit sentinel）——不是起動失敗。
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
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        sleep(min(0.01, remaining))


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
