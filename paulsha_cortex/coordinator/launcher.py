from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence, runtime_checkable

from . import gate_ledger, job_runner, job_workspace, spool_slot, terminal_contract


_GIT_REPOSITORY_ENV_KEYS = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_INTERNAL_SUPER_PREFIX",
        "GIT_NAMESPACE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_QUARANTINE_PATH",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)

# 憑證形狀的 env 名稱。定義搬到 `job_runner`（Phase 2a 降權啟動器的 env 白名單守衛
# 需要同一份判準），這裡保留原名別名——reviewer sandbox 政策與 builder transient unit
# 的憑證判準永遠是同一條 pattern，不會兩處漂移。
_CREDENTIAL_ENV_RE = job_runner.CREDENTIAL_ENV_RE


def _claude_review_json_schema(kind: str) -> str:
    """Bind Claude StructuredOutput to the Manager terminal contract."""

    report = {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "body"],
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "body": {"type": "string", "minLength": 1},
        },
    }
    common = {
        "type": "object",
        "additionalProperties": False,
    }
    if kind == "workflow-verification-result":
        schema = {
            **common,
            "required": [
                "schema_version", "kind", "status", "summary", "details", "reports",
            ],
            "properties": {
                "schema_version": {"type": "integer", "enum": [1]},
                "kind": {"type": "string", "enum": [kind]},
                # #261 R1：三種終局狀態在契約上對等可達。只允許成功形狀會逼模型
                # 在 gate 已失敗時仍輸出成功 card（fail-open）；非通過狀態由
                # manager.terminalize_workflow_job fail closed 為可操作錯誤。
                "status": {
                    "type": "string",
                    "enum": ["verified", "failed", "needs_human"],
                },
                "summary": {"type": "string", "minLength": 1},
                "details": {"type": "object"},
                "reports": {"type": "array", "minItems": 1, "items": report},
            },
        }
    elif kind == "workflow-review-result":
        evidence = {
            "type": "object",
            "additionalProperties": False,
            "required": ["path", "line", "detail"],
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "line": {"type": ["integer", "null"], "minimum": 1},
                "detail": {"type": "string", "minLength": 1},
            },
        }
        finding = {
            "type": "object",
            "additionalProperties": False,
            "required": ["category", "severity", "summary", "evidence", "recommendation"],
            "properties": {
                "category": {
                    "type": "string",
                    "description": (
                        "Use blocking categories only for defects in the Candidate or its "
                        "acceptance. A report-only wording or enumeration inaccuracy that does "
                        "not change the Candidate verdict is style; correct it in this report."
                    ),
                    "enum": [
                        "acceptance", "correctness", "data-loss", "pre-existing-out-of-scope",
                        "race", "scope-bypass", "security", "style", "verification-bypass",
                    ],
                },
                "severity": {"type": "string", "enum": ["critical", "important", "minor"]},
                "summary": {"type": "string", "minLength": 1},
                "evidence": {"type": "array", "items": evidence},
                "recommendation": {"type": "string", "minLength": 1},
            },
        }
        schema = {
            **common,
            "required": ["schema_version", "kind", "reason", "findings", "reports"],
            "properties": {
                "schema_version": {"type": "integer", "enum": [1]},
                "kind": {"type": "string", "enum": [kind]},
                # #261 R1：選填的終局狀態欄位。review verdict 本身仍由 findings
                # 決定；status 讓 reviewer 能誠實表達「這張 card 自己沒跑完」。
                "status": {
                    "type": "string",
                    "enum": ["passed", "failed", "needs_human"],
                },
                "reason": {"type": "string", "minLength": 1},
                "findings": {"type": "array", "items": finding},
                "reports": {"type": "array", "minItems": 1, "items": report},
                # #315 補遺 3（#219 attestation 缺口）：manager 驗證器在 input
                # snapshot 含 planning-authority 列時「要求」authority_hashes，
                # 但本工具 schema additionalProperties:false 之前沒有此屬性——
                # 模型再遵循 prompt 也交不出來（工具層拒收），review terminal
                # 恆 schema invalid。是否必填由 manager 依 context 驗證，工具
                # schema 僅開放屬性。
                "authority_hashes": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                    },
                },
            },
        }
    else:
        raise ValueError("Claude reviewer terminal contract kind invalid")
    return json.dumps(schema, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def build_wrapper_script(
    *,
    inner_argv: Sequence[str],
    sentinel: str,
    ledger: str,
    worktree: str,
    repo_root: str | None,
    run_gates: bool,
    stdin_prompt: str | None = None,
    write_sentinel: bool = True,
    commit_bundle: str | None = None,
    verdict_file: str | None = None,
) -> str:
    """組出 headless wrapper script（#261：模型結束後由 manager 產生 gate ledger）。

    三段皆以 ``;`` 串接，因此模型失敗時 sentinel 與 ledger 仍會產生：

    1. 模型 argv；
    2. 把 ``$?`` 寫入 exit sentinel（跨進程 durable 完成判定，早於 gate 階段，
       確保 gate 執行時間不會被算進模型的 exit code）；
    3. 由 manager 掌控的 gate ledger writer。

    ``commit_bundle``（#623）：成果 bundle 的落點。非 None 時 script 改成**先把
    模型的 ``$?`` 存進 shell 變數**，接著產 bundle，最後才寫 sentinel／跑 gate，
    並以存下來的值收場。兩個理由：

    - **順序**——sentinel 一出現，Manager 隨時可能在下一個 tick 判定完成並開始
      回收。bundle 必須在 sentinel **之前**落地，否則回收會撞上一個還沒寫完的 spool。
      降權模式沒有 job 側 sentinel，但 Manager 側的記帳 shell 等的是整個 unit
      （``systemctl start --wait``），bundle 同樣先完成。
    - **exit code 不得被污染**——降權模式下 unit 的 exit code 就是這支 script 的
      exit code，而 Manager 的記帳 shell 記的正是它（#604）。多接一段 bundle 之後
      若不還原 ``$?``，模型明明失敗卻會被記成成功（或反過來）。

    ``verdict_file``（#638 缺陷 2）：reviewer verdict spool 的落點。非 None 時在
    模型之後追加一段 ``chmod``，把 reviewer 寫出來的 verdict 放寬到 Manager 讀得到
    ——那個檔由 **reviewer 的 uid** 建立、又常帶降權 unit 的 ``UMask=0077``，
    Manager 是**目錄**的 owner 但那不給檔案內容的讀取權，consumer 讀不到整條
    verdict 通道就不成立。與 bundle 段的 ``chmod`` 是**同一個修法的兩個實例**
    （共用 :data:`spool_slot.PUBLISHED_FILE_MODE`），差別只在 bundle 的 producer
    是 Manager 組出來的 ``git`` 命令、verdict 的 producer 是模型本身——模型不會
    自己 chmod，所以那一步必須由 wrapper 在它結束後補上。段序同樣排在 sentinel
    **之前**，理由與 bundle 一致。

    ``commit_bundle`` 與 ``verdict_file`` 皆為 None 時（planner，以及所有既有測試
    路徑）script **逐字**與改動前相同。

    ``write_sentinel=False`` / ``run_gates=False``（#604，降權模式）：這支 script
    在降權模式下是以 **job 帳號**（`cortex-builder`）執行的，而 sentinel 與 ledger
    的落點屬登記表資產 ``gate-ledger``（Manager 的 dispatch log 目錄，`0700
    cortex-manager`，且不在 job 模板 unit 的 ``ReadWritePaths=`` 內）。讓 job 去寫
    那兩個檔在信任面上是「被隔離的一方自證 exit code 與 gate 結果」，在可行性上則
    是必定 EROFS。降權模式因此把兩段都拿掉：sentinel 改由 Manager 側的 exit 記帳
    shell 寫（見 :func:`job_runner.build_manager_exit_recorder_argv`），gate 的重跑
    與 ledger 產生留待 Manager 側的 gate 執行面（見本檔 :meth:`SubprocessLauncher.
    _should_run_gates` 的說明），在那之前 ``require_ledger`` 會照既有規則 fail
    closed——**不會**因為少了這一段而讓任何 build 卡靜默通過。

    gate 階段的 stdout/stderr 一律導向 /dev/null：JSONL log 是 terminal evidence 的
    來源，混入 gate 輸出會讓 ``_extract_terminal_json`` 讀到非 terminal 的內容。

    ``stdin_prompt``（issue #442）：既有 copilot/claude/codex/agy 皆把 prompt 當作
    argv 的一個元素；`cg`（見 `build_cg_argv`）改走「prompt 經 stdin」的介面
    （`cg --headless --stdin`），argv 本身不含 prompt。非 None 時改以
    ``printf %s <prompt> | <inner argv>`` 組出管線，把 prompt 經標準輸入餵給內層
    命令；``$?``（bash 未開 pipefail 時）取自管線最後一個命令，即內層命令本身，
    語意與既有「$? 為模型 exit code」不變。內層命令顯式 ``2>/dev/null``：cg 的
    stderr 是人類可讀 summary banner（非 terminal evidence），Popen 層雖以
    ``stderr=STDOUT`` 併入同一份 log fd，但那只重導向這個 bash 進程自己的 fd 2；
    管線內命令的顯式重導向可覆寫，藉此讓 banner 不混入 JSONL log（下游
    `_extract_terminal_json` 只能安全解析乾淨 stdout）。
    """

    if stdin_prompt is None:
        command = shlex.join(inner_argv)
    else:
        command = (
            f"printf %s {shlex.quote(stdin_prompt)} | {shlex.join(inner_argv)} 2>/dev/null"
        )
    if commit_bundle is not None or verdict_file is not None:
        return _publishing_wrapper_script(
            command=command,
            sentinel=sentinel,
            ledger=ledger,
            worktree=worktree,
            repo_root=repo_root,
            run_gates=run_gates,
            write_sentinel=write_sentinel,
            commit_bundle=commit_bundle,
            verdict_file=verdict_file,
        )
    if write_sentinel:
        script = f'{command}; printf %s "$?" > {shlex.quote(sentinel)}'
    else:
        script = command
    if not run_gates or not repo_root:
        return script
    gate_argv = [
        "python3",
        "-m",
        "paulsha_cortex.coordinator.gate_ledger",
        "--out",
        ledger,
        "--worktree",
        worktree,
    ]
    # PYTHONPATH 指向 repo root，讓 wrapper 在 worktree cwd 下仍能 import 套件。
    return (
        f"{script}; PYTHONPATH={shlex.quote(repo_root)} "
        f"{shlex.join(gate_argv)} >/dev/null 2>&1"
    )


#: 存放模型 exit code 的 shell 變數名（#623 的 bundle 段用）。刻意帶 `__psc_` 前綴，
#: 不與模型或 gate 階段可能設定的任何變數撞名。
_RC_VAR = "__psc_rc"


def _gate_segment(*, ledger: str, worktree: str, repo_root: str) -> str:
    gate_argv = [
        "python3",
        "-m",
        "paulsha_cortex.coordinator.gate_ledger",
        "--out",
        ledger,
        "--worktree",
        worktree,
    ]
    return (
        f"PYTHONPATH={shlex.quote(repo_root)} {shlex.join(gate_argv)} >/dev/null 2>&1"
    )


def _publishing_wrapper_script(
    *,
    command: str,
    sentinel: str,
    ledger: str,
    worktree: str,
    repo_root: str | None,
    run_gates: bool,
    write_sentinel: bool,
    commit_bundle: str | None,
    verdict_file: str | None,
) -> str:
    """帶「成果發表」段的 wrapper。

    段序＝模型 → 存 `$?` → bundle（#623）→ verdict 放寬（#638）→ sentinel →
    gate → 還原 `$?`。

    兩個發表段都排在 sentinel **之前**：sentinel 一出現，Manager 隨時可能在下一個
    tick 判定完成並開始收割，成果必須先落地且已經是 consumer 讀得到的形狀。

    為什麼 bundle 段用 `git` 而不是像 gate 那樣呼叫一個 python module：降權模式下
    builder 看到的是白名單 env、且它未必讀得到 Manager 的 repo root
    （`ProtectHome=yes` 之後 `/home` 整個不可見，#623 缺口 1），`PYTHONPATH=<repo>`
    這條路在那裡不成立。`git` 是 job 本來就必須有的工具；verdict 段只用 `chmod`，
    同理。
    """

    segments = [command, f"{_RC_VAR}=$?"]
    if commit_bundle is not None:
        segments.append(
            job_workspace.build_bundle_command(workspace=worktree, bundle=commit_bundle)
        )
    if verdict_file is not None:
        segments.append(spool_slot.publish_file_command(verdict_file))
    if write_sentinel:
        segments.append(f'printf %s "${_RC_VAR}" > {shlex.quote(sentinel)}')
    if run_gates and repo_root:
        segments.append(_gate_segment(ledger=ledger, worktree=worktree, repo_root=repo_root))
    segments.append(f'exit "${_RC_VAR}"')
    return "; ".join(segments)


def _srt_runtime_root() -> Path | None:
    """Resolve only the installed official sandbox-runtime package root."""

    executable = shutil.which("srt")
    if executable is None:
        return None
    resolved = Path(executable).resolve()
    for parent in resolved.parents:
        metadata = parent / "package.json"
        if not metadata.is_file() or metadata.is_symlink():
            continue
        try:
            payload = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if payload.get("name") == "@anthropic-ai/sandbox-runtime":
            return parent
    return None


def _claude_review_settings(worktree: str) -> str:
    """Build a CLI-only sandbox policy for a headless Claude reviewer."""

    candidate = (Path(worktree).resolve() / "candidate").resolve()
    home = Path.home().resolve()
    runtime_paths = tuple(
        dict.fromkeys(
            path.resolve()
            for path in (
                Path("/run/user"),
                Path("/run/docker.sock"),
                Path("/var/run/docker.sock"),
            )
        )
    )
    credential_paths = (
        home / ".aws",
        home / ".claude",
        home / ".claude.json",
        home / ".config" / "gh",
        home / ".config" / "gcloud",
        home / ".kube",
        home / ".ssh",
        *runtime_paths,
    )
    credential_env = sorted(
        name for name in os.environ if _CREDENTIAL_ENV_RE.search(name) is not None
    )
    tool_read_paths = [candidate]
    tool_read_paths.extend(
        path.resolve()
        for path in sorted(home.glob(".local/lib/python*/site-packages"))
        if path.is_dir() and not path.is_symlink()
    )
    srt_root = _srt_runtime_root()
    if srt_root is not None:
        tool_read_paths.append(srt_root)
    read_denials = [
        f"Read(/{path.as_posix()}{'/**' if path.suffix == '' else ''})"
        for path in credential_paths
    ]
    settings = {
        "permissions": {"deny": read_denials},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "denyWrite": [str(candidate)],
                "denyRead": [str(home), *(str(path) for path in runtime_paths)],
                "allowRead": [str(path) for path in tool_read_paths],
            },
            "credentials": {
                "files": [
                    {"path": str(path), "mode": "deny"}
                    for path in credential_paths
                ],
                "envVars": [
                    {"name": name, "mode": "deny"}
                    for name in credential_env
                ],
            },
        },
    }
    return json.dumps(settings, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


# #506 / D5：headless job 的事件 hook 命令。`cortex headless-hook post-tool-use`
# 讀 stdin 的 PostToolUse payload，把被動過的 GitHub 物件寫進 monitor 的 D4 event
# spool（見 `porcelain/headless_hook.py`）。
#
# `|| true`：hook 掛在別人（job）的工作路徑上。CLI 本身已一律 exit 0，這一段兜的是
# CLI 之外的失敗——`cortex` 不在 PATH（127）、套件安裝損壞、python 起不來。任何情況
# 下 hook 都不得讓 job 看到非零 exit（PostToolUse 的非零 exit 會被回報成 hook 失敗，
# 甚至把 stderr 回饋給模型）。
_CLAUDE_SPOOL_HOOK_COMMAND = "cortex headless-hook post-tool-use || true"

# 逾時上限：hook 只寫一個本機檔案（外加最多一次本機 `git config` 讀取），秒級都嫌多；
# 設上限是為了「hook 永不阻塞 job」這條硬約束，不是為了正常路徑。
_CLAUDE_SPOOL_HOOK_TIMEOUT_SECONDS = 10


def _claude_spool_hook_settings() -> str:
    """Build the per-job PostToolUse hook settings for a headless Claude builder.

    #506 / D5，使用者硬約束「**hook 不得影響正常的互動式 agent 使用**」的第一道
    結構保證：這份宣告是每次 `launch()` 現場組出來、經 argv 的 `--settings` 交給
    這一個 job 的行程，**從不寫入任何檔案**——尤其不寫 `~/.claude/settings.json`。
    operator 自己開的互動 session 讀的是 operator 的設定，那裡永遠沒有這個 hook。

    刻意只宣告 `hooks`：`--settings` 是與其他設定來源合併的一層 overlay，因此
    builder job 既有的 operator 設定（permissions allowlist 等）原封不動——換成
    hermetic `CLAUDE_CONFIG_DIR`（#404 為 planning 的純 JSON 回聲任務所做的選擇）
    會把那些設定一併抽掉，對 builder 是遠超出 D5 範圍的行為變更（例如失去
    permissions allowlist 會讓 headless job 卡在無人可核可的授權提示）。

    第二道保證在 hook 自己身上：`porcelain/headless_hook.py` 沒有 `PSC_JOB_ID`
    就是完全的 no-op。兩道保證彼此獨立。
    """

    settings = {
        "hooks": {
            "PostToolUse": [
                {
                    # 只掛 Bash：GitHub 物件的 mutation 一律經 `gh`（CLI 或 `gh api`）
                    # 發生。其餘工具（Read/Edit/Task…）不可能動到遠端物件，掛上去
                    # 只會讓每次 tool call 都多跑一個沒事做的行程。
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": _CLAUDE_SPOOL_HOOK_COMMAND,
                            "timeout": _CLAUDE_SPOOL_HOOK_TIMEOUT_SECONDS,
                        }
                    ],
                }
            ]
        }
    }
    return json.dumps(settings, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _git_scope_env() -> dict[str, str]:
    """Drop inherited Git repository/config selectors before scope binding."""

    return {
        key: value
        for key, value in os.environ.items()
        if key not in _GIT_REPOSITORY_ENV_KEYS and not key.startswith("GIT_CONFIG_")
    }


# #396 item 2(b)：copilot executor 的 credential 注入契約——依優先序列出 copilot
# CLI 認得的既有 env var 名稱（見 porcelain/bootstrap.py `_executor_status` 的
# login_fix 訊息："請設定 COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN"）。
_COPILOT_TOKEN_ENV_VARS: tuple[str, ...] = (
    "COPILOT_GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


def _copilot_credential_env(env: Mapping[str, str]) -> dict[str, str]:
    """把既有可設定來源的 token 正規化成 copilot CLI 優先讀取的 env var。

    背景（issue #396 item 2）：copilot builder job 派出即失敗，因為 job env 沒有
    COPILOT_GITHUB_TOKEN／GH_TOKEN，唯一有效 token 只在 gh CLI 的 OS keyring
    ——headless daemon 本就讀不到桌面 keyring，此函式刻意不去碰它，避免多一個
    平台相依的失敗模式。

    這裡只做「這個 process 的 env 裡已經有的三個候選名稱之一，正規化成
    copilot CLI 優先序最高的那個」；至於 token 怎麼進到 process env——沿用本
    repo 既有的可設定來源（daemon 自身的 systemd EnvironmentFile／
    `~/.agents/core/runtime/<instance>.env`，與 porcelain/bootstrap.py
    `_instance_runtime_env_path` 同一份機制，PSC_MANAGER_EXECUTOR 也是這樣佈署
    的）——留給 operator 決定，不是本函式的責任。

    COPILOT_GITHUB_TOKEN 已存在時視為 operator 明確指定，不覆寫；三個候選皆缺
    （或皆為空字串）時回傳空 dict，呼叫端不改動 env（fail-soft：沒有 token 不
    是這個函式該擋的錯，讓 job 依現行行為派出、由 copilot CLI 自己的登入態訊息
    或 #369 的 executor_auth 探測回報）。
    """

    if env.get("COPILOT_GITHUB_TOKEN"):
        return {}
    token = next(
        (env[name] for name in _COPILOT_TOKEN_ENV_VARS[1:] if env.get(name)),
        None,
    )
    if token is None:
        return {}
    return {"COPILOT_GITHUB_TOKEN": token}


def _review_scope_env() -> dict[str, str]:
    """Keep only non-secret process basics for an untrusted read-only reviewer."""

    allowed = {
        "HOME",
        "LANG",
        "LC_ADDRESS",
        "LC_ALL",
        "LC_COLLATE",
        "LC_CTYPE",
        "LC_IDENTIFICATION",
        "LC_MEASUREMENT",
        "LC_MESSAGES",
        "LC_MONETARY",
        "LC_NAME",
        "LC_NUMERIC",
        "LC_PAPER",
        "LC_TELEPHONE",
        "LC_TIME",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TMPDIR",
        "USER",
        "VIRTUAL_ENV",
    }
    return {
        key: value
        for key, value in _git_scope_env().items()
        if key in allowed
    }


@dataclass(frozen=True)
class LaunchHandle:
    executor: str
    model_id: str | None
    session_name: str
    pid: int
    log_path: str


def _linked_worktree_git_write_dirs(worktree: str | None) -> tuple[str, ...]:
    """Resolve only the external Git directories required for a branch commit."""

    if worktree is None:
        return ()
    root = Path(worktree).resolve()
    marker = root / ".git"
    if marker.is_symlink():
        raise ValueError("worktree .git marker must not be a symlink")
    if not marker.exists():
        return ()
    if not marker.is_file() and not marker.is_dir():
        raise ValueError("worktree .git marker must be a regular file or directory")
    if marker.is_dir():
        return ()
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--path-format=absolute",
                "--absolute-git-dir",
                "--git-common-dir",
                "--show-toplevel",
                "--symbolic-full-name",
                "HEAD",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=_git_scope_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("linked worktree git metadata is unavailable") from exc
    rows = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or len(rows) != 4:
        raise ValueError("linked worktree git metadata is invalid")
    unresolved_git_dir = Path(rows[0])
    unresolved_common_dir = Path(rows[1])
    unresolved_toplevel = Path(rows[2])
    branch_ref = rows[3]
    if (
        unresolved_git_dir.is_symlink()
        or not unresolved_git_dir.is_dir()
        or unresolved_common_dir.is_symlink()
        or not unresolved_common_dir.is_dir()
        or unresolved_git_dir.absolute() != unresolved_git_dir.resolve()
        or unresolved_common_dir.absolute() != unresolved_common_dir.resolve()
    ):
        raise ValueError("linked worktree git metadata is invalid")
    git_dir = unresolved_git_dir.resolve()
    common_dir = unresolved_common_dir.resolve()
    if (
        unresolved_toplevel.resolve() != root
        or git_dir.parent != common_dir / "worktrees"
        or not branch_ref.startswith("refs/heads/")
    ):
        raise ValueError("linked worktree gitdir escapes common metadata root")
    relative_ref = Path(branch_ref)
    if relative_ref.is_absolute() or ".." in relative_ref.parts:
        raise ValueError("linked worktree branch ref is invalid")

    objects_dir = common_dir / "objects"
    refs_root = common_dir / "refs" / "heads"
    logs_root = common_dir / "logs" / "refs" / "heads"
    ref_parent = (common_dir / relative_ref).parent
    reflog_parent = (common_dir / "logs" / relative_ref).parent
    required = (git_dir, objects_dir, ref_parent, reflog_parent)
    if any(
        path.is_symlink()
        or not path.is_dir()
        or path.absolute() != path.resolve()
        for path in required
    ):
        raise ValueError("linked worktree required git write directory is invalid")
    try:
        objects_dir.resolve().relative_to(common_dir)
        ref_parent.resolve().relative_to(refs_root)
        reflog_parent.resolve().relative_to(logs_root)
    except ValueError as exc:
        raise ValueError("linked worktree git write directory escapes branch scope") from exc
    return tuple(dict.fromkeys(str(path.resolve()) for path in required))


def _verdict_spool_add_dirs(
    verdict_spool_dir: str | None,
    *,
    read_only: bool,
    review_only: bool,
) -> tuple[str, ...]:
    """trust-root Phase 2a：reviewer 專屬 verdict spool 的 `--add-dir` 放行清單。

    verdict 落點搬出 worktree 之後（spec §R2），executor 自己的 sandbox 會把
    `<coordinator_root>/review-verdicts/<job_id>/` 擋在工作區之外——codex
    `--sandbox workspace-write` 只放行 cwd、claude `acceptEdits` 只覆蓋工作目錄。
    這裡沿用既有的 `--add-dir` 機制**只**放行那一個 per-job 目錄（不是整棵
    coordinator 樹），與 `_linked_worktree_git_write_dirs()` 的窄放行同一個模式。

    read-only／review-only 契約下不放行任何寫入路徑：那些 persona 依契約不寫檔，
    verdict 走終局 JSON 契約（workflow lane），不需要也不該開這個洞。
    """

    if verdict_spool_dir is None:
        return ()
    if read_only or review_only:
        raise ValueError("read-only launcher cannot be granted a verdict spool write path")
    path = Path(verdict_spool_dir)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("verdict spool directory must be an absolute non-symlink path")
    return (str(path.resolve()),)


def build_copilot_argv(
    *,
    prompt: str,
    slice_id: str,
    log_dir: str,
    worktree: str | None = None,
    remote: str | None = None,
    allow_unsafe: bool = False,
    model: str | None = None,
    read_only: bool = False,
    review_only: bool = False,
    commit_required: bool = False,
    verdict_spool_dir: str | None = None,
) -> list[str]:
    if commit_required and (read_only or review_only or allow_unsafe):
        raise ValueError("commit-required Copilot builder requires enforced workspace-write")
    if read_only or review_only:
        raise ValueError("copilot executor has no enforced read-only planning mode")
    if commit_required:
        if worktree is None:
            raise ValueError("commit-required Copilot builder requires a worktree")
        worktree = str(Path(worktree).resolve())
    # allow_unsafe（明確 opt-in）才放開 copilot 的全自動授權 --allow-all；
    # 預設關閉 → 由 executor 自身的互動授權把關（manager 自主派工請設 allow_unsafe=True）。
    argv = [
        "copilot",
        "-p",
        prompt,
        "--remote",
        "--name",
        slice_id,
        "--log-dir",
        log_dir,
        "--output-format",
        "json",
    ]
    if model is not None:
        argv += ["--model", model]
    if commit_required:
        argv.append("--allow-all-tools")
        argv += ["--add-dir", worktree]
        for git_write_dir in _linked_worktree_git_write_dirs(worktree):
            argv += ["--add-dir", git_write_dir]
    elif allow_unsafe:
        argv.append("--allow-all")
    for spool_dir in _verdict_spool_add_dirs(
        verdict_spool_dir, read_only=read_only, review_only=review_only
    ):
        argv += ["--add-dir", spool_dir]
    return argv


def build_claude_argv(
    *,
    prompt: str,
    slice_id: str,
    log_dir: str,
    worktree: str | None = None,
    remote: str | None = None,
    allow_unsafe: bool = False,
    model: str | None = None,
    read_only: bool = False,
    review_only: bool = False,
    review_terminal_kind: str | None = None,
    commit_required: bool = False,
    verdict_spool_dir: str | None = None,
) -> list[str]:
    if (read_only or review_only) and allow_unsafe:
        raise ValueError("read-only Claude launcher cannot bypass permissions")
    if commit_required and (read_only or review_only or allow_unsafe):
        raise ValueError("commit-required Claude builder requires enforced workspace-write")
    if review_only and worktree is None:
        raise ValueError("read-only Claude reviewer requires a Candidate checkout")
    if review_only:
        if review_terminal_kind is None:
            raise ValueError("Claude reviewer terminal contract kind missing")
        review_schema = _claude_review_json_schema(review_terminal_kind)
    else:
        if review_terminal_kind is not None:
            raise ValueError("Claude terminal contract requires reviewer mode")
        review_schema = None
    # allow_unsafe（明確 opt-in）→ bypassPermissions（不再逐筆授權）；
    # 預設用 acceptEdits（仍受權限模式把關，最小放權）。
    argv = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",  # smoke 實證：claude -p + --output-format stream-json 必須帶 --verbose
        "--name",
        slice_id,
        "--permission-mode",
        (
            "plan"
            if read_only
            else (
                "dontAsk"
                if review_only
                else ("bypassPermissions" if allow_unsafe else "acceptEdits")
            )
        ),
    ]
    if not review_only:
        argv.append("--remote-control")
    if read_only:
        argv += ["--tools", ""]
    elif review_only:
        argv += [
            "--tools",
            "Bash",
            "--setting-sources",
            "",
            "--settings",
            _claude_review_settings(worktree),
            "--mcp-config",
            '{"mcpServers":{}}',
            "--strict-mcp-config",
            "--json-schema",
            str(review_schema),
            "--safe-mode",
            "--disable-slash-commands",
            "--no-chrome",
            "--no-session-persistence",
        ]
    else:
        # #506 / D5：只有 builder（可寫入、有 Bash、會動 GitHub 物件的那一種 job）
        # 掛事件 hook。
        # - read_only planner 走 `--tools ""`，連 Bash 都沒有，掛了也永遠不會觸發；
        # - review_only reviewer 是 read-only 契約，且它的 `--settings` 是那份
        #   sandbox 政策（deny 掉 $HOME 讀寫），事件根本寫不出去。
        # 這一行是 hook 的**唯一**注入點：per-job、走 argv、不落地任何檔案。
        argv += ["--settings", _claude_spool_hook_settings()]
    if model is not None:
        argv += ["--model", model]
    if worktree is not None and not review_only:
        argv.extend(["--add-dir", worktree])
        # #396 item 3：linked worktree 的 .git 只是個指向 repo 外部（objects／
        # refs／index）的檔案；builder 完成後對這些外部路徑 git add/commit 若不在
        # sandbox 的放行清單內，會被擋下回 requires approval（headless 無人可
        # approve）→ candidate-worktree-dirty。比照 build_copilot_argv／
        # build_codex_argv 既有的 commit_required 分支，把同一份 linked-worktree
        # git 寫入目錄透過 --add-dir 放行；非 commit-required（例如 planner）維持
        # 原行為不放寬。
        if commit_required:
            for git_write_dir in _linked_worktree_git_write_dirs(worktree):
                argv += ["--add-dir", git_write_dir]
    for spool_dir in _verdict_spool_add_dirs(
        verdict_spool_dir, read_only=read_only, review_only=review_only
    ):
        argv += ["--add-dir", spool_dir]
    return argv


def build_codex_argv(
    *,
    prompt: str,
    slice_id: str,
    log_dir: str,
    worktree: str | None = None,
    remote: str | None = "psc",
    allow_unsafe: bool = False,
    model: str | None = None,
    read_only: bool = False,
    review_only: bool = False,
    commit_required: bool = False,
    write_forbidden: bool = False,
    verdict_spool_dir: str | None = None,
    last_message_path: str | None = None,
) -> list[str]:
    if (read_only or review_only) and allow_unsafe:
        raise ValueError("read-only Codex planning cannot bypass sandbox")
    if commit_required and (read_only or review_only or allow_unsafe):
        raise ValueError("commit-required Codex builder requires enforced workspace-write")
    if worktree is not None:
        worktree = str(Path(worktree).resolve())
    # smoke 實證：`codex exec` 不接受 `--remote`（unexpected argument）。codex 的 remote
    # 是獨立的 `remote-control` 子命令/app-server，非 exec 旗標；故 headless exec 不帶 remote。
    argv = [
        "codex",
        "exec",
        prompt,
        "--json",
    ]
    # 高風險：--dangerously-bypass-approvals-and-sandbox 同時關掉核可「與」沙箱。
    # 僅在明確 opt-in（allow_unsafe=True，例如 manager 自主全自動派工）時才加入；
    # 預設關閉，讓 codex 自身的核可/沙箱機制把關。
    if allow_unsafe:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
        # smoke 實證：headless codex exec 帶（未持久信任的）relay hook 時，會卡在 hook
        # 信任閘等待輸入 → timeout。autonomous 派工須一併 bypass hook trust 才不會掛住。
        argv.append("--dangerously-bypass-hook-trust")
    else:
        # #716：**sandbox mode 由登記表導出，這裡不做第二次決定。**
        #
        # 舊形態是一條 `if/elif/else`，而那條 `else` 對 builder **一律**發
        # `workspace-write`——`commit_policy` 完全不看，因為 `read_only` 是 launcher
        # 維度（`as_read_only()`）。於是一張 `commit_policy=forbidden` 且
        # `declared_outputs` 為空的唯讀 build 卡拿到的是寫入授權，那是**獨立成立的
        # 最小權限缺陷**（#716 選項 F）；#714 的 legacy landlock 只是讓它從靜默變成
        # `linux_run_main.rs:318` 的 panic。
        #
        # 導出規則與需要哪個 mode 的理由都住在 `registry.SANDBOX_MODE_DERIVATION`，
        # 在 registry import 當下被全覆蓋斷言強制（缺一格模組載不起來）。lazy import
        # 的理由與 `_codex_inner_sandbox_argv()` 逐字相同。
        argv += ["--sandbox", _codex_sandbox_mode(
            allow_unsafe=allow_unsafe,
            read_only=read_only,
            review_only=review_only,
            commit_required=commit_required,
            write_forbidden=write_forbidden,
        )]
        if read_only or review_only:
            # planner／reviewer 的既有旗標，**逐字不變**：它們的工作區可能根本不是
            # repo（reviewer 的 planning scratch 就是空目錄，見
            # `registry.JOB_GIT_WORKSPACE_TRUST` 的 reviewer 那一列）。
            # write-forbidden 的 build 卡刻意**不**帶它——那張卡跑在 per-job clone 裡，
            # 它與今天唯一的差別就是 mode 這一個 token。
            argv.append("--skip-git-repo-check")
        if commit_required:
            for git_write_dir in _linked_worktree_git_write_dirs(worktree):
                argv += ["--add-dir", git_write_dir]
    if not allow_unsafe:
        # #714：codex 的**內層沙箱形態**。預設是 bubblewrap，而 bwrap 在本系統的
        # 加固面下要付四條放寬（`ProcSubset`／`RestrictNamespaces`／
        # `RestrictAddressFamilies`／`SystemCallFilter` 加 `@mount`），其中兩條放寬的
        # 正是 user namespace 與 mount——外層加固面存在的理由本身。改走
        # landlock ＋ seccomp 之後外層一條都不必動（見 `permgen.CODEX_LEGACY_LANDLOCK`）。
        #
        # **形態由登記表導出，不在這裡寫死**：`permgen.EXECUTOR_TOOLS` 那一列同時是
        # 「需要放行哪些 syscall 群組」的來源，而那條需求在 permgen import 當下被強制
        # （`_validate_inner_sandbox_support()`）。兩邊各寫一份就會出現「argv 換了形態、
        # 加固面沒跟上」的靜默組合。
        #
        # `allow_unsafe` 那一支刻意不帶：`--dangerously-bypass-approvals-and-sandbox`
        # 已經整個關掉內層沙箱，再選形態沒有意義。
        argv += list(_codex_inner_sandbox_argv())
    for spool_dir in _verdict_spool_add_dirs(
        verdict_spool_dir, read_only=read_only, review_only=review_only
    ):
        argv += ["--add-dir", spool_dir]
    if model is not None:
        argv += ["--model", model]
    # #714 缺陷 2：`-o` 的落點必須是**這個 job 寫得進去、而且帶 job id** 的那一格。
    # 呼叫端沒給時退回 `<log_dir>/<slice>.last.json`——仍然帶 slice id（共用
    # `last.json` 會讓並行的兩個 job 互相蓋掉），只是落在 Manager 的 dispatch log
    # 目錄，那是 direct 模式的既有落點。
    argv.extend(
        ["-o", last_message_path or str(Path(log_dir) / f"{slice_id}.last.json")]
    )
    if worktree is not None:
        argv.extend(["-C", worktree])
    return argv


def _codex_inner_sandbox_argv() -> tuple[str, ...]:
    """codex 的內層沙箱形態 argv（`permgen.EXECUTOR_TOOLS` 是唯一真相，#714）。

    lazy import 與 `planning_job`／`planning_probe_cache` 既有的做法一致：`trust_root`
    是產生器面，不該進 `coordinator` 的模組載入圖，但**登記表的內容必須只有一份**。

    ## 涵蓋範圍的邊界（刻意的，不是漏的）

    這一支只餵給 `build_codex_argv`——也就是**經 `SubprocessLauncher` 派出去**的 codex
    job（builder／reviewer／slice-lane planner 都算）。`planning_runtime._planning_argv`
    那條路**刻意不動**：它是 Manager 行程內（或 `planning_job` 那一格）的 planning 呼叫，
    輸出由 `_extract_json()` **從 stdout 直接解 JSON**，而本形態的旗標會讓 codex 在串流
    最前面多印一筆 deprecation 的 `item.type=error`。jsonl 那一端由尾端往回找
    `agent_message`，多一筆開頭的雜訊無害；stdout 直接解 JSON 那一端沒有這個保護，
    為了一個目前跑得通的路徑去冒那個險不划算。

    ⚠️ 這是**範圍**判斷，不是「planning 不需要內層沙箱」的宣稱——planning 真的跑起命令
    時會撞上與本票逐字相同的牆。要動那一條路，得先量它的 JSON 抽取吃不吃得下那筆 error。
    """

    from ..trust_root import permgen

    spec = permgen.executor_inner_sandbox("codex")
    return () if spec is None else tuple(spec.argv)


def _codex_sandbox_mode(
    *,
    allow_unsafe: bool,
    read_only: bool,
    review_only: bool,
    commit_required: bool,
    write_forbidden: bool,
) -> str:
    """codex `--sandbox` 的值（`registry.SANDBOX_MODE_DERIVATION` 是唯一真相，#716）。

    lazy import 的理由與 :func:`_codex_inner_sandbox_argv` 逐字相同：`trust_root` 是
    產生器面，不該進 `coordinator` 的模組載入圖，但**規則的內容必須只有一份**——
    `permgen.build_inner_sandbox_probe()` 的 per-mode 矩陣、本函式、以及測試裡的窮舉
    對照都消費同一張表。

    這裡不再有任何 `if <persona> then <mode>`：導出由
    `registry.derive_job_write_contract()` ＋ `registry.sandbox_mode_for()` 兩步完成，
    兩者在 registry import 當下被全覆蓋斷言釘死。回傳值恆為字串——`unsafe-bypass`
    那一格根本進不到這裡（呼叫端在 `allow_unsafe` 分支就走掉了），若真的進來，
    `sandbox_mode_for()` 回 `None` 會在這裡顯性失敗，而不是靜默發出一個 `None` token。
    """

    from ..trust_root import registry

    contract = registry.derive_job_write_contract(
        allow_unsafe=allow_unsafe,
        read_only=read_only,
        review_only=review_only,
        commit_required=commit_required,
        write_forbidden=write_forbidden,
    )
    mode = registry.sandbox_mode_for(contract)
    if mode is None:
        raise ValueError(
            f"寫入契約 {contract.value} 不發 --sandbox，不該走到 argv 的沙箱分支（#716）"
        )
    return mode


def build_agy_argv(
    *,
    prompt: str,
    slice_id: str,
    log_dir: str,
    worktree: str | None = None,
    remote: str | None = None,
    allow_unsafe: bool = False,
    model: str | None = None,
    read_only: bool = False,
    review_only: bool = False,
) -> list[str]:
    """Build the only supported Antigravity invocation: headless plan+sandbox.

    Antigravity exposes ``--dangerously-skip-permissions`` but cortex never
    emits it.  The planner peer is evidence-only, so it has no reason to run
    with write permissions even when another executor was explicitly granted
    unsafe mode.
    """
    if allow_unsafe:
        raise ValueError("agy executor does not support unsafe mode")
    argv = ["agy", "--print", prompt, "--mode", "plan", "--sandbox"]
    if model is not None:
        argv.extend(["--model", model])
    return argv


# cg（copilot API／glm-5.2 經 llm-share 巷道）的預設身分——operator 的
# `$HOME/.local/bin/cg` thin wrapper 已固定 `HIPPO_COPILOT_ENV_FILE` 指向
# llm-share env（含 `COPILOT_MODEL=glm-5.2`），這裡的常數只是 argv 沒收到明確
# `model` 時的落地預設，實際身分仍由 operator 的 env file 決定。
_CG_DEFAULT_MODEL = "glm-5.2"
_CG_VALID_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
_CG_DEFAULT_EFFORT = "medium"


def build_cg_argv(
    *,
    prompt: str,
    slice_id: str,
    log_dir: str,
    worktree: str | None = None,
    remote: str | None = None,
    allow_unsafe: bool = False,
    model: str | None = None,
    read_only: bool = False,
    review_only: bool = False,
    commit_required: bool = False,
    effort: str | None = None,
) -> list[str]:
    """Build the headless `cg`（copilot API／glm-5.2 via llm-share）invocation.

    Operator-provided、smoke-verified 契約（issue #442）：
    ``cg --model {MODEL} --effort {low|medium|high|xhigh} --headless --stdin``
    ——prompt 經 stdin 傳入（不是 argv 參數，見 `SubprocessLauncher.launch` 的
    stdin plumbing）、乾淨 response 寫到 stdout、summary banner 寫到 stderr、
    exit 0 表示成功。`cg` wrapper 自帶 ``--available-tools=__none__`` ＋
    ``--disable-builtin-mcps`` ＋ throwaway HOME：zero-tool，不能跑任何 tool、
    不能寫檔、不能 commit。因此 cg 只能服務 read-only 的 planner／reviewer
    persona，絕不可當 builder——``prompt``／``slice_id``／``log_dir``／
    ``worktree``／``remote`` 是為了滿足與其餘 builder 共用的呼叫介面而接收，
    實際不會進入回傳的 argv（prompt 由呼叫端經 stdin 餵入）。

    比照 `build_agy_argv` 對 `allow_unsafe` 的拒絕模式：agy 與 cg 都是 read-only
    planner，這裡同樣 fail-closed，而非靜默降級成安全形狀——commit_required／
    unsafe／非 read-only-或-review-only 的「builder 語境」一律 raise，讓誤用在
    建構期就顯性失敗，不會把一個從未被授權寫入的 executor 悄悄放進 builder 角色。
    """
    if allow_unsafe:
        raise ValueError("cg executor does not support unsafe mode")
    if commit_required:
        raise ValueError("cg executor is zero-tool and cannot commit")
    if not (read_only or review_only):
        raise ValueError("cg executor requires read-only or review-only mode")
    resolved_effort = effort or _CG_DEFAULT_EFFORT
    if resolved_effort not in _CG_VALID_EFFORTS:
        raise ValueError(
            f"cg executor effort must be one of {sorted(_CG_VALID_EFFORTS)}, got {resolved_effort!r}"
        )
    return [
        "cg",
        "--model",
        model or _CG_DEFAULT_MODEL,
        "--effort",
        resolved_effort,
        "--headless",
        "--stdin",
    ]


@runtime_checkable
class AgentLauncher(Protocol):
    def launch(
        self,
        *,
        slice_id: str,
        prompt: str,
        worktree: str,
        log_dir: str,
    ) -> LaunchHandle: ...


# 目前支援的 headless executor 家族——這是唯一真相來源：`cli.py` 的
# `--executor`/`--review-executor` choices 與 `SubprocessLauncher.__init__` 的
# 建構驗證都直接消費這個字典的 key（不重複列舉），新增一筆即兩處自動同步。
#
# 新增 executor 前的必要條件：先確認其 CLI 介面（prompt 怎麼傳、輸出格式、
# sandbox/approval 旗標怎麼關），寫一個對應的 `build_<executor>_argv`，仿照本檔
# 其餘 builder 附近散落的「smoke 實證」註解方式留下驗證依據，再加進這個字典。
#
# `cg`（copilot API／glm-5.2 巷道）自 issue #442 起已支援：#396 item 1 當時找不到
# CLI 介面文件或 smoke 紀錄而刻意 out-of-scope；operator 已提供並 smoke 驗證介面
# 契約（見 `build_cg_argv` docstring），故補上 `build_cg_argv` 並在此登記。cg 是
# zero-tool（無法跑任何 tool／寫檔／commit），`build_cg_argv` 與
# `SubprocessLauncher.__init__` 對 commit_required／allow_unsafe／非
# read-only-或-review-only 的建構請求一律 raise，只服務 planner／reviewer 角色。
_ARGV_BUILDERS = {
    "copilot": build_copilot_argv,
    "claude": build_claude_argv,
    "codex": build_codex_argv,
    "agy": build_agy_argv,
    "cg": build_cg_argv,
}


class SubprocessLauncher:
    """真實作：headless subprocess 啟動。測試 MUST 注入 fake，不實體化。"""

    def __init__(
        self,
        executor: str = "copilot",
        *,
        relay_target: str | None = None,
        codex_remote: str = "psc",
        allow_unsafe: bool = False,
        model: str | None = None,
        read_only: bool = False,
        review_only: bool = False,
        commit_required: bool = False,
        write_forbidden: bool = False,
        review_terminal_kind: str | None = None,
        effort: str | None = None,
        verdict_spool_dir: str | None = None,
    ) -> None:
        if executor not in _ARGV_BUILDERS:
            raise ValueError(f"unknown executor: {executor}")
        if executor == "agy" and allow_unsafe:
            raise ValueError("agy executor refuses unsafe mode")
        if executor == "cg" and allow_unsafe:
            raise ValueError("cg executor refuses unsafe mode")
        if (read_only or review_only) and executor == "copilot":
            raise ValueError("copilot executor has no enforced read-only planning mode")
        # cg 是 zero-tool（見 build_cg_argv docstring）：與 copilot 相反，這裡要求
        # 而非禁止 read-only/review-only——builder 語境（both False）在建構期即
        # 顯性拒絕，不留給呼叫端在 launch() 時才踩空。
        if executor == "cg" and not (read_only or review_only):
            raise ValueError("cg executor requires read-only or review-only mode")
        if read_only and review_only:
            raise ValueError("launcher cannot be both planner-read-only and reviewer-read-only")
        if (read_only or review_only) and allow_unsafe:
            raise ValueError("read-only launcher cannot enable unsafe mode")
        if commit_required and (read_only or review_only or allow_unsafe):
            raise ValueError("commit-required launcher requires enforced workspace-write")
        # #716：`write_forbidden` 與 `commit_required` 是同一張卡的 `commit_policy` 的
        # 兩個互斥值（`forbidden` vs `required`），同時成立代表呼叫端算錯了契約；
        # 與 `allow_unsafe`（沙箱整個關掉）同時成立同理。**顯性失敗，不靜默選一個。**
        # 導出規則本身在 `registry.derive_job_write_contract()`，這裡的守衛與它同型、
        # 同理由——建構期擋掉的東西不必等到 argv 才發現。
        if write_forbidden and (commit_required or allow_unsafe):
            raise ValueError("write-forbidden launcher contradicts commit-required/unsafe")
        if review_only and review_terminal_kind not in {
            "workflow-verification-result", "workflow-review-result",
        }:
            raise ValueError("reviewer launcher terminal contract kind invalid")
        if not review_only and review_terminal_kind is not None:
            raise ValueError("reviewer terminal contract requires reviewer mode")
        # trust-root Phase 2a：verdict spool 放行只對「可寫入的 slice-lane
        # reviewer」有意義；read-only／review-only 契約下顯性拒絕（見
        # `_verdict_spool_add_dirs`），不靜默降級成「開了洞卻寫不進去」。
        if verdict_spool_dir is not None and (read_only or review_only):
            raise ValueError("read-only launcher cannot be granted a verdict spool write path")
        self._executor = executor
        self._relay_target = relay_target
        self._codex_remote = codex_remote
        # allow_unsafe（明確 opt-in）：放開各 executor 的全自動授權/沙箱旁路旗標
        # （codex --dangerously-bypass-approvals-and-sandbox、copilot --allow-all、
        # claude bypassPermissions）。預設 False，採最小放權，避免無意間關掉沙箱。
        self._allow_unsafe = allow_unsafe
        self._model = model
        self._read_only = read_only
        self._review_only = review_only
        self._commit_required = commit_required
        # #716：卡片契約明確宣告「不 commit 且無 declared_outputs」⇒ 這張 build 卡依
        # 契約不寫工作區 ⇒ 不該拿到 `workspace-write`。**它不改變 job 角色**——
        # `_is_review_persona()` 的三個判準一個都沒動，write-forbidden 的 build 卡仍以
        # `cortex-builder` 起跑（它就是 builder，只是這一張卡不寫檔）。
        self._write_forbidden = write_forbidden
        self._review_terminal_kind = review_terminal_kind
        # cg-only：`--effort low|medium|high|xhigh`。其餘 executor 沒有對應概念
        # （不同於 `model`，本 repo 目前沒有既有的「effort 來源」可直接映射），
        # 存下不驗證——合法值集合在 `build_cg_argv` 驗證，未指定時落地預設
        # `_CG_DEFAULT_EFFORT`；非 cg 的 executor 忽略此欄位。
        self._effort = effort
        # trust-root Phase 2a：本 job 專屬的 verdict spool 目錄（唯一額外放行的
        # 寫入路徑）。None ＝ 不放行任何 worktree 之外的寫入（既有行為）。
        self._verdict_spool_dir = verdict_spool_dir

    @property
    def executor(self) -> str:
        """公開 executor CLI 家族（copilot/claude/codex/agy/cg）。

        #381：spawn admission limiter 需要在啟動前依 provider 分桶節流；
        沒有 per-slice identity 可查時，這是唯一能從已注入的 launcher
        自報「實際會是哪個 provider」的管道（見 spawn_admission.resolve_provider）。
        """
        return self._executor

    def as_read_only(self) -> "SubprocessLauncher":
        """Return an equivalent launcher with the executor's strict planning contract."""

        return SubprocessLauncher(
            executor=self._executor,
            relay_target=self._relay_target,
            codex_remote=self._codex_remote,
            allow_unsafe=False,
            model=self._model,
            read_only=True,
            review_only=False,
            commit_required=False,
            effort=self._effort,
        )

    def as_review_only(self, *, terminal_kind: str) -> "SubprocessLauncher":
        """Return a launcher that can inspect, but cannot mutate, a Candidate checkout."""

        return SubprocessLauncher(
            executor=self._executor,
            relay_target=self._relay_target,
            codex_remote=self._codex_remote,
            allow_unsafe=False,
            model=self._model,
            read_only=False,
            review_only=True,
            commit_required=False,
            review_terminal_kind=terminal_kind,
            effort=self._effort,
        )

    def as_verdict_spool_writer(self, spool_dir: str) -> "SubprocessLauncher":
        """Return an equivalent launcher that may also write this job's verdict spool.

        trust-root Phase 2a（spec §R2）：review verdict 的權威落點搬到
        `<coordinator_root>/review-verdicts/<reviewer_job_id>/`，不再是 reviewer
        worktree 內的檔案。executor 的 sandbox 預設把那裡視為工作區之外，因此
        Manager 在派工當下用這個特化把**該 job 的那一格**（不是整棵 coordinator
        樹）加進放行清單；其餘契約（allow_unsafe／model／commit_required）一律
        原封不動。
        """

        if self._read_only or self._review_only:
            raise ValueError("read-only launcher cannot be granted a verdict spool write path")
        if self._verdict_spool_dir == spool_dir:
            return self
        return SubprocessLauncher(
            executor=self._executor,
            relay_target=self._relay_target,
            codex_remote=self._codex_remote,
            allow_unsafe=self._allow_unsafe,
            model=self._model,
            read_only=False,
            review_only=False,
            commit_required=self._commit_required,
            write_forbidden=self._write_forbidden,
            effort=self._effort,
            verdict_spool_dir=spool_dir,
        )

    def as_commit_required(self) -> "SubprocessLauncher":
        """Return a builder launcher explicitly allowed to update linked Git metadata."""

        if self._read_only or self._review_only:
            raise ValueError("commit-required launcher requires enforced workspace-write")
        # #716：`commit_policy` 不可能同時是 `forbidden` 與 `required`；先降級成
        # write-forbidden 再要求 commit 代表呼叫端算錯了契約，fail-closed。
        if self._write_forbidden:
            raise ValueError("write-forbidden launcher cannot become commit-required")
        if self._allow_unsafe or self._commit_required:
            return self
        return SubprocessLauncher(
            executor=self._executor,
            relay_target=self._relay_target,
            codex_remote=self._codex_remote,
            allow_unsafe=False,
            model=self._model,
            read_only=False,
            review_only=False,
            commit_required=True,
            effort=self._effort,
        )

    def as_write_forbidden(self) -> "SubprocessLauncher":
        """Return a launcher whose card contract forbids any workspace write（#716）。

        **判準在卡片契約上，不在 persona 上**：呼叫端（`manager._specialize_workflow_
        launcher()`）先用 `registry.card_contract_forbids_workspace_write()` 從
        `commit_policy` ＋ `declared_outputs` 機械算出來，再套用這個特化。builder
        persona 底下同時有唯讀卡與寫入卡，persona 一刀切正是被否決的形態。

        **read-only 族原樣回傳，不是靜默降級**：planner／reviewer 的契約已經**至少
        一樣嚴**（`registry.SANDBOX_MODE_DERIVATION` 上兩格都是 `read-only`，且
        `derive_job_write_contract()` 的優先序刻意讓它們壓過 `write_forbidden`），
        再包一層只會改變它們其餘的既有旗標。它們今天就是好的，本票一個位元都不動。

        `commit_required` 是**契約矛盾**（同一張卡的 `commit_policy` 不可能同時是
        `forbidden` 與 `required`），顯性拒絕；`allow_unsafe` 是 operator 的明確
        opt-in bypass，本票**不觸碰**——那一格連沙箱都沒有，降不降 mode 沒有意義，
        而把它悄悄改掉會是一個超出本票射程的行為變更。
        """

        if self._read_only or self._review_only:
            return self
        if self._allow_unsafe:
            return self
        if self._commit_required:
            raise ValueError("commit-required launcher cannot become write-forbidden")
        if self._write_forbidden:
            return self
        return SubprocessLauncher(
            executor=self._executor,
            relay_target=self._relay_target,
            codex_remote=self._codex_remote,
            allow_unsafe=False,
            model=self._model,
            read_only=False,
            review_only=False,
            commit_required=False,
            write_forbidden=True,
            effort=self._effort,
            verdict_spool_dir=self._verdict_spool_dir,
        )

    def _should_run_gates(self, env: Mapping[str, str]) -> bool:
        """#261：只有會改動 candidate 的可寫入 card 才在 wrapper 內跑確定性 gate。

        read-only／review-only 的 reviewer 不改 candidate，也刻意在最小 env 下執行，
        不應在它的 sandbox 內跑 gate；operator 未宣告任何 ``PSC_GATE_CMD_*`` 時仍會
        寫出一份 ``gates: []`` 的 ledger，讓 harvest 能區分「沒宣告 gate」與
        「wrapper 根本沒跑完」。

        **#604：降權模式一律不在 job wrapper 內跑 gate。** 理由有兩層——

        1. 信任面：wrapper 跑在 builder 的 uid 底下，ledger 因此是「被驗方在自己的
           進程裡產生自己的驗收證據」。#540 的 gate acceptance chain 要求 model 既
           不能自證成功、也不能自證失敗。
        2. 可行性：ledger 的落點是登記表資產 ``gate-ledger``（`0700 cortex-manager`，
           不在 job 模板 unit 的 ``ReadWritePaths=`` 內），job 寫進去必然 EROFS。

        **為什麼不直接把 gate 搬進 Manager 進程**（＝本票只做到「不再由 job 自寫」
        這一步的原因）：宣告出來的 gate 命令（`PSC_GATE_CMD_PYTEST` 之類）是在
        **builder 完全掌控內容的 worktree** 裡執行的；`pytest` 會載入該 worktree 的
        `conftest.py`／plugin，等於讓 builder 在 `cortex-manager` 身分下取得任意程式
        碼執行——那比本票要修的缺口更嚴重。正確的落點是一個**既非 builder、也非
        Manager** 的 gate 執行身分（Manager 起、以獨立降權帳號跑、產出直接落在
        Manager-owned 樹），那需要動 permgen 的 UID scheme 與 unit 產生器，屬後續票。
        在那之前，降權模式下的 build 卡會因 ``require_ledger`` 而 fail closed——這是
        刻意的：沒有獨立證據就不採信，而不是採信一份 builder 自己寫的。
        """

        if self._review_only or self._read_only:
            return False
        if self._degraded_runner(os.environ):
            return False
        return env.get("PSC_REPO_ROOT") is not None

    def _is_review_persona(self) -> bool:
        """本 launcher 派出去的是 reviewer／planner（而不是 builder）嗎。

        **三個判準，缺一不可**——這是 #615 實作時發現的一個真缺口：

        1. `review_only`＝workflow lane 的 reviewer（`as_review_only()`）；
        2. `read_only`＝**workflow lane 的 planner 卡**（`as_read_only()`）；
        3. `verdict_spool_dir is not None`＝**slice lane 的 foreign reviewer**。

        **第 2 條的範圍要看清楚（#672／#687）**：它涵蓋的是 workflow lane 上
        `persona == "planner"` 的那幾張卡（`manager._specialize_workflow_launcher()`
        呼叫 `as_read_only()`）。**define／brainstorm 的 planning 不在其中**——那條路
        根本不建立 `SubprocessLauncher`，它走 `planning_runtime._invoke_json()`。
        這個 docstring 曾經是「planner 已經降權了」這個假直覺的來源之一：讀完三個
        判準，很自然會以為 planner 全部路徑都在本函式的射程內。它不是。
        planning 那一條由 `planning_runtime._select_planning_invoker()` 決定
        （#686 接上 `JobPlanningInvoker`、#687 切換）。

        第 3 條容易漏掉，而漏掉的後果最嚴重。slice lane 的 foreign reviewer 走的是
        `manager._spool_writable_launcher()` → `as_verdict_spool_writer()`，而那支
        工廠產出的 launcher `read_only` 與 `review_only` **都是 False**（見它自己的
        `__init__` 守衛：verdict spool 放行與 read-only 契約互斥，因為 read-only 的
        executor 連 `--add-dir` 都拿不到）。若只看前兩條，這個 job 會被判成 builder
        並以 `cortex-builder` 起跑——**而它正是寫 verdict 的那一個**。那等於把
        verdict 通道交還給 builder 帳號，把 #638／#639 剛修好的東西整個抵銷掉。

        換句話說：**「被授予了 verdict spool」本身就是 reviewer 的標記**，而那個授予
        是 Manager 在 dispatch 當下做的決定（`as_verdict_spool_writer(spool_dir)`），
        job 側完全碰不到。
        """

        return bool(self._review_only or self._read_only or self._verdict_spool_dir)

    def _job_role(self) -> str:
        """本 launcher 的降權 job 角色（#615 M2）——**launcher 這條路徑的唯一決定點**。

        reviewer 與 planner 同屬 `review` 角色，因為三分方案把它們映到**同一個**
        OS 帳號（`cortex-reviewer-planner`）；其餘為 `builder`。

        **「唯一」的範圍限定（#687）**：本函式原本寫「唯一決定點」，那在 #686 之後
        不再是全稱——`planning_job.JobPlanningInvoker` 是第二個消費者，它把角色**在
        建構期固定**成 `JOB_ROLE_REVIEW`（不經本函式，也不從 spec 導出）。兩處共用
        同一組角色常數與同一支 `resolve_runner_mode()`，但「哪些程式碼決定角色」
        的答案是**兩個地方**。這不是缺陷，是兩條 code path 的事實；把它寫清楚，是
        因為 #672 的三個月盲區正是由「大家都在讀 launcher」造成的。

        **角色由 launcher 的建構契約導出，不由 job 導出**：三個判準
        （見 :meth:`_is_review_persona`）在 `__init__` 就固定，此後 immutable。
        prompt、worktree 內容、job spec 都在這之後才產生，而 spec 連提身分欄位都不准
        （`job_runner.SPEC_FORBIDDEN_KEYS`）——job 影響不到自己會以哪個 UID 起跑。
        """

        if self._is_review_persona():
            return job_runner.JOB_ROLE_REVIEW
        return job_runner.JOB_ROLE_BUILDER

    def _downgraded_mode(self, env: Mapping[str, str]) -> str | None:
        """本次 launch 要走哪一種降權啟動器（皆非時回 None＝direct，行為不變）。

        唯一條件：`PSC_JOB_RUNNER` ∈ {`systemd-run`, `systemd-template`}（部署期
        設定；預設 `direct`＝現行行為不變）。

        **#615（M2）移除了第二個條件。** 在此之前這裡對 `review_only`／`read_only`
        回 `None`，於是 reviewer／planner 仍在 Manager 行程內以 Manager 帳號執行
        ——A+B 裁決的核心論述「injection 可達的進程皆無 spawn 授權」因此**只對
        builder 成立**，而 reviewer 正是寫 verdict 的那一個。M2 之後**經過本
        launcher 的**派工，persona 只決定**哪一個角色**（:meth:`_job_role`），
        不再決定「降不降權」。

        **原文寫的是「M2 之後三個會跑模型的 persona 全部離開 Manager 的 UID」，
        #687 更正了它。** 那句話的射程被誤讀成全稱：define／brainstorm 的 planning
        不經過本 launcher，因此 #615 對它零效果，它到 #672 票 A～F（#682-#687）才
        離開 Manager 的 UID。本函式今天能宣稱的，逐字是「**走本 launcher 的**派工
        全部降權」。全稱要合上 `planning_runtime._select_planning_invoker()` 那一半
        才成立。

        `systemd-run`（A 案）與 `systemd-template`（B 案，0816 第三輪裁決）在
        launcher 這一層共用**完全相同**的 env 白名單與 `bash -c` 決定，差別只在
        「怎麼把這條命令交給 systemd」——A 案經 `systemd-run` 的 argv，B 案經
        Manager-owned spec 檔 ＋ root-owned 模板 unit。

        `PSC_JOB_RUNNER` 的值即使非法，也在這裡 fail-closed（見
        `job_runner.resolve_runner_mode`），不會被靜默當成 direct。
        """

        mode = job_runner.resolve_runner_mode(env)
        if mode not in (job_runner.RUNNER_SYSTEMD_RUN, job_runner.RUNNER_SYSTEMD_TEMPLATE):
            return None
        return mode

    def _degraded_runner(self, env: Mapping[str, str]) -> bool:
        """是否走任一種降權啟動器（`executor_environment` 的 env 分支用）。"""

        return self._downgraded_mode(env) is not None

    def executor_environment(self, *, slice_id: str = "preflight"):
        """#262 D2：回報正式 job 會實際看到的 executor 環境。

        env 由與 `launch()` 相同的 `_review_scope_env()`／`_git_scope_env()` 產生，
        因此 preflight 檢查的 PATH／HOME／sandbox policy 與正式 job 一致；
        若在此另建一份 env，preflight 就只是安慰劑（見 design D2）。
        """

        from .runtime_preflight import ExecutorEnvironment

        # 降權判定**排在 review_only 之前**（#615 M2）：reviewer 也走降權之後，
        # `_review_scope_env()` 那份「從 daemon environ 篩出來的最小集」對它已經
        # 不再是實情——job 看到的是 unit 的白名單 env（HOME 是自己帳號的，不是
        # daemon 的）。順序寫反的話 preflight 又會變回安慰劑，而且是**只有
        # reviewer 會錯**的那一種安慰劑。
        if self._degraded_runner(os.environ):
            # 降權模式下 job 實際看到的是 unit 的白名單 env，不是 daemon 的
            # environ；preflight 若仍回報 daemon env，它報的 PATH／HOME 就與正式 job
            # 無關（見本方法 docstring 的「不然只是安慰劑」）。
            env = job_runner.build_job_env(
                manager_env=os.environ,
                job_id=slice_id,
                slice_id=slice_id,
                repo_root=str(Path(__file__).resolve().parents[2]),
                # #712：preflight **沒有工作區**——它報告的是 PATH／HOME／sandbox
                # 剖面，而那時還沒有任何 job 工作區可言。逐 job 的 git 放行因此在
                # 這裡是空的，而這不是 fail-open：真實派工那一支（`launch()`）的
                # `workspace=` 是必填具名參數，且 `build_job_spec()` 另外斷言
                # 「env 放行的那一格＝spec 的 working_directory」。
                workspace=None,
                relay_target=self._relay_target,
                role=self._job_role(),
            )
        elif self._review_only:
            env = _review_scope_env()
        else:
            env = {
                **_git_scope_env(),
                "PSC_SLICE_ID": slice_id,
                "PSC_JOB_ID": slice_id,
                "PSC_REPO_ROOT": str(Path(__file__).resolve().parents[2]),
            }
            if self._relay_target is not None:
                env["PSC_RELAY_TARGET"] = self._relay_target
        # interpreter：job 以 `bash -lc` 啟動，其 python 由 env 的 PATH 決定，
        # 因此以同一份 env 解析，而非用 manager 自己的 sys.executable。
        interpreter = shutil.which("python3", path=env.get("PATH", "")) or shutil.which(
            "python", path=env.get("PATH", "")
        )
        if self._review_only:
            mode = "review-only"
        elif self._read_only:
            mode = "read-only"
        elif self._write_forbidden:
            # #716：preflight 報的必須是**正式 job 會實際看到的**剖面（design D2 的
            # 「不然只是安慰劑」逐字適用）。這張卡的契約宣告不寫工作區，argv 上發的
            # 是 `read-only`——名字沿用契約值而不是 mode 值，因為 `read-only` 那個名字
            # 已經被 planner 佔著，兩者的其餘旗標並不相同（見 `as_write_forbidden`）。
            mode = "write-forbidden"
        else:
            mode = "workspace-write"
        return ExecutorEnvironment(
            name=f"{self._executor}:{mode}",
            interpreter=(interpreter,) if interpreter else (sys.executable,),
            path=env.get("PATH", ""),
            home=env.get("HOME", ""),
            provider_identity=f"{self._executor}/{self._model}" if self._model else self._executor,
        )

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str) -> LaunchHandle:
        # Phase 2a 降權啟動器（#584 未決 1 裁決＝systemd-run transient unit）。
        # 這一行在**任何**副作用（mkdir／清 sentinel／Popen）之前求值：`PSC_JOB_RUNNER`
        # 非法或 builder 帳號不存在時，本次派工必須在還沒改動任何狀態前就 fail-closed，
        # 而不是先做一半再退回 direct。
        runner_plan: job_runner.SystemdRunPlan | None = None
        template_plan: job_runner.SystemdTemplatePlan | None = None
        runner_mode = self._downgraded_mode(os.environ)
        # #615 M2：角色（＝哪個 job 帳號、哪一份 root-owned 模板）在這裡定案，與
        # 加固剖面同一個位置、同一個時機——都在**任何** per-job 產物之前。
        job_role = self._job_role()
        if runner_mode == job_runner.RUNNER_SYSTEMD_RUN:
            runner_plan = job_runner.prepare_systemd_run(
                os.environ, job_id=slice_id, role=job_role
            )
        elif runner_mode == job_runner.RUNNER_SYSTEMD_TEMPLATE:
            # B 案（0816 第三輪裁決）：模板 unit／shim／spec spool 三個前置物任一
            # 缺席都在這裡 fail-closed，且**在寫任何 spec 之前**。
            #
            # #643：加固剖面在這裡定案。唯一的輸入是 `self._executor`——它由
            # `SubprocessLauncher.__init__` 收下（Manager 的 dispatch 決定，且已對
            # `_ARGV_BUILDERS` 驗過），此後 immutable。**job 影響不到它**：prompt、
            # worktree 內容、spec 都在這行之後才產生，而 spec 連提剖面都不准
            # （`job_runner.SPEC_FORBIDDEN_KEYS`）。未登記的 executor 在這裡
            # fail-closed，不會落到放寬的那一份剖面。
            template_plan = job_runner.prepare_systemd_template(
                os.environ, job_id=slice_id, executor=self._executor, role=job_role
            )
        degraded = runner_mode is not None
        resolved_worktree = Path(worktree).resolve(strict=True)
        if not resolved_worktree.is_dir():
            raise ValueError("launcher worktree must be a directory")
        worktree = str(resolved_worktree)
        # log_dir resolve 成絕對：sentinel 由子進程的 bash wrapper 以 cwd=worktree 寫入，
        # 相對路徑會落到 worktree（poller 在他處找不到）→ 完成偵測對 worktree dispatch 失效。
        # 絕對化後 JSONL / sentinel / 回傳 log_path 皆與 cwd 無關，跨進程 poll 一致。
        log_dir = str(Path(log_dir).resolve())
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log_path = str(Path(log_dir) / f"{slice_id}.jsonl")
        # #714 缺陷 2：**job 端 log 的落點在這裡就定案**（純路徑推導、零副作用），
        # 因為 executor 的 argv 在下面幾行就要用到它——`-o` 的落點是「這個 job 自己那
        # 份 log 的兄弟檔」。真正把那一格建出來的仍是下面 `prepare_job_log_spool()`
        # 那一步（#708 的順序不變：先備好 log，再寫 spec），且它回傳的路徑會與這裡算
        # 出來的**逐字比對**——兩處推導漂移時當場 fail-closed，不會出現「argv 指著 A、
        # shim 開的是 B」這種只在實機上看得見的錯位。
        job_log_path = log_path
        if template_plan is not None:
            job_log_path = str(
                job_workspace.job_log_spool_dir(
                    principal_id=job_runner.JOB_ROLE_CONFIG[job_role].log_spool_principal,
                    spool_key=slice_id,
                )
                / job_workspace.JOB_LOG_FILENAME
            )
        last_message_path = str(job_workspace.job_last_message_path(job_log_path))
        builder_kwargs = {
            "prompt": prompt,
            "slice_id": slice_id,
            "log_dir": log_dir,
            "worktree": worktree,
            "remote": self._codex_remote,
            "allow_unsafe": self._allow_unsafe,
            "model": self._model,
            "read_only": self._read_only,
            "review_only": self._review_only,
        }
        # #396 item 3：claude 併入 commit_required 傳遞——builder-persona 的
        # as_commit_required() 轉換（autonomy.dispatch_ready）對三個 executor
        # 一視同仁，build_claude_argv 缺這個 kwarg 會讓轉換對 claude 變 no-op。
        # cg 併入同一份 kwarg（issue #442）：self._commit_required 對任何成功建構
        # 的 cg launcher 恆為 False（見 __init__ 的 cg 專屬不變量），這裡顯式傳遞
        # 只是與其餘 builder 的呼叫形狀一致、defense-in-depth，不改變行為。
        if self._executor in {"codex", "copilot", "claude", "cg"}:
            builder_kwargs["commit_required"] = self._commit_required
        # #716：只有 codex 的 argv 上有 `--sandbox <mode>` 這個維度可表達。其餘 executor
        # 沒有對應旗標（`build_claude_argv` 走 `--permission-mode`、`build_copilot_argv`
        # 走 `--allow-all`／`--deny-tool`、agy／cg 是 plan-only／zero-tool），傳過去只會
        # 是一個沒人接的 kwarg——形狀與既有的 `verdict_spool_dir`／`effort`／
        # `last_message_path` 逐條一致：能力有差異就顯式分岔，不塞給接不住的那幾支。
        #
        # ⚠️ 這是**範圍**判斷，不是「其餘 executor 的最小權限已經對了」的宣稱。
        # `claude` 的 `--permission-mode` 有沒有對應的降級形態**沒有量過**（#716 comment
        # 記過 `EXECUTOR_TOOLS` 的 `inner_sandbox=None` 同時代表「沒有」與「還沒量」，
        # 那是同一族的錯）。要動那幾支，得先各自量一次。
        if self._executor == "codex":
            builder_kwargs["write_forbidden"] = self._write_forbidden
        # trust-root Phase 2a：只有支援 `--add-dir` 的三個 executor 能表達「額外
        # 放行一個目錄」。agy／cg 是 zero-tool／plan-only，本來就寫不了檔，也不會
        # 被指派成 slice-lane reviewer；對它們宣告 spool 放行是設定錯誤，顯性拒絕。
        if self._verdict_spool_dir is not None:
            if self._executor not in {"codex", "copilot", "claude"}:
                raise ValueError(
                    f"executor {self._executor} cannot be granted a verdict spool write path"
                )
            builder_kwargs["verdict_spool_dir"] = self._verdict_spool_dir
        if self._executor == "claude":
            builder_kwargs["review_terminal_kind"] = self._review_terminal_kind
        if self._executor == "cg":
            builder_kwargs["effort"] = self._effort
        # #714 缺陷 2：只有 codex 有 `--output-last-message`。其餘 executor 沒有這個
        # 落點，傳過去只會是一個沒人接的 kwarg（形狀與既有的 `verdict_spool_dir`／
        # `effort` 逐條一致：能力有差異就顯式分岔，不塞 None 給接不住的那幾支）。
        if self._executor == "codex":
            builder_kwargs["last_message_path"] = last_message_path
        inner_argv = _ARGV_BUILDERS[self._executor](
            **builder_kwargs,
        )
        # PSC_REPO_ROOT 讓已安裝 hook 的 `${PSC_REPO_ROOT}/scripts/coordinator/psc-relay-hook.sh`
        # 在 cwd=worktree（≠repo）時仍可解（worktree 雖是 repo checkout，但 hook 為全域安裝、
        # 不可依賴相對 cwd；互動 session 亦不應因相對路徑找不到 script 而報錯）。
        # 順序：**降權優先於 review_only**（#615 M2，與 `executor_environment()`
        # 逐字一致）。`_review_scope_env()` 是「從 daemon environ 篩」的模型——它在
        # 同 UID 下是唯一能做的事，但降權之後 job 根本不繼承 daemon 的 environ，
        # 繼續用它只會把 daemon 的 HOME／PATH／VIRTUAL_ENV 硬塞進一個跑在別的 UID
        # 上、根本進不去那些路徑的行程。
        if degraded:
            # #588 第 1 點的結構性解法：降權 unit **不繼承呼叫端的 environ**，
            # 因此 job 的環境就是這份白名單本身（不是「daemon environ 減去黑名單」）。
            # gh token、daemon 的 CLAUDE_CONFIG_DIR 都不在白名單上，因此不會出現在
            # job 裡——包括 `_copilot_credential_env()` 也因此自然回傳空 dict（它讀的是
            # 這份 env，裡面沒有任何 token 候選），不必為降權模式另設特例。
            env = job_runner.build_job_env(
                manager_env=os.environ,
                job_id=slice_id,
                slice_id=slice_id,
                repo_root=str(Path(__file__).resolve().parents[2]),
                # #712：git 的 dubious-ownership 那一層。`worktree` 在上面已經被
                # `Path(...).resolve(strict=True)` 換成**已解析的**絕對路徑字串，而
                # 下面 `build_job_spec()` 的 `working_directory=` 用的是**同一個變數**
                # ——兩者必須逐字相同（git 比對 `safe.directory` 是逐字相等，而 shim
                # `chdir` 之後 git 由 `getcwd()` 取路徑，那**恆是** physical path）。
                # 這條相等性由 `build_job_spec()` 斷言。
                workspace=worktree,
                relay_target=self._relay_target,
                role=job_role,
            )
        elif self._review_only:
            env = _review_scope_env()
        else:
            env = {
                **_git_scope_env(),
                "PSC_SLICE_ID": slice_id,
                # #506 / D5：cortex 派工的 headless job 標記。事件 hook 以它自守
                # ——沒有這個變數就是完全的 no-op（見 `porcelain/headless_hook.py`）。
                # 互動 session 的環境裡不存在，因此 hook 即使被誤裝也不會有事件。
                # reviewer 分支刻意不設：它走 read-only 契約、不掛 hook，marker 與
                # 注入點成對出現才不會出現「有標記卻沒 hook」的半套狀態。
                "PSC_JOB_ID": slice_id,
                "PSC_REPO_ROOT": str(Path(__file__).resolve().parents[2]),
            }
            if self._relay_target is not None:
                env["PSC_RELAY_TARGET"] = self._relay_target
            if self._executor == "copilot":
                env.update(_copilot_credential_env(env))
        # （`log_path` 已在 argv 之前算好——#714：`-o` 的落點由它導出。）
        # 跨進程 durable 完成判定：以 bash -lc 包裝，子進程結束時把 $? 寫入 exit sentinel。
        # 用 shlex.join 安全嵌入內層 argv（prompt 含換行/空白仍為單一 token），
        # sentinel 路徑亦 shlex.quote。poll_headless_done 讀此 sentinel，不再靠 os.waitpid。
        sentinel = str(Path(log_dir) / f"{slice_id}.exit")
        # 重跑同一 slice_id 前先清掉上一輪殘留：移除舊 exit sentinel、log 以 wb 截斷。
        # 否則 poll_headless_done 會讀到上一輪的 sentinel / 末筆 JSONL，
        # 誤判「還沒開始就已完成」（fail-closed：每輪從乾淨狀態起跑）。
        Path(sentinel).unlink(missing_ok=True)
        # #261：同理清掉上一輪的 gate ledger，避免 harvest 讀到前一次的 gate 結果。
        ledger = terminal_contract.gate_ledger_path(log_path)
        Path(ledger).unlink(missing_ok=True)
        # #623：成果 bundle 的 per-job spool。`prepare_commit_spool()` 同樣負責清掉
        # 上一輪殘留（與 sentinel／ledger 逐條一致），並把上一輪 harvest 之後的封存
        # 解開。判準是 **persona**，不是 `PSC_JOB_RUNNER`：reviewer／planner 不產生
        # commit，給它們一格 spool 只會多一個沒人寫的空目錄；builder 兩種模式走完全
        # 相同的路徑（#634 的「以形狀判斷、不依旗標分支」原則）。
        #
        # #615：判準改用 `_is_review_persona()`——它多涵蓋 slice lane 的 foreign
        # reviewer（`read_only`／`review_only` 皆為 False，但持有 verdict spool）。
        # 修正前那個 job 會拿到一格 commit spool 並在 wrapper 裡跑 `git bundle
        # create`；它從來不 commit，所以那一格永遠是空的（direct 模式下是浪費），
        # 而降權之後 reviewer 帳號對 commit-spool **零寫入權**，那一段會逐 job 失敗。
        commit_bundle: str | None = None
        if not self._is_review_persona():
            commit_bundle = str(job_workspace.prepare_commit_spool(spool_key=slice_id))
        # #638 缺陷 2：reviewer 寫出來的 verdict 由 **reviewer 的 uid** 建立
        # （降權 unit 常帶 `UMask=0077`），Manager 是那一格目錄的 owner 但那不給
        # 檔案內容的讀取權——不補這一步，verdict 通道在三分下讀不到任何東西。
        # 落點由 Manager 在 dispatch 當下決定（`as_verdict_spool_writer()` 帶進來
        # 的就是那一格），因此這裡不需要、也不該讓模型自述路徑。
        verdict_file: str | None = None
        if self._verdict_spool_dir is not None:
            verdict_file = str(
                Path(self._verdict_spool_dir) / spool_slot.REVIEW_VERDICT_FILENAME
            )
        # cg（issue #442）走 stdin 傳 prompt，不是 argv 參數（見 build_cg_argv）：
        # 其餘 executor 維持既有「prompt 為 argv 一個元素」路徑，stdin_prompt=None
        # 時 build_wrapper_script 的行為與改動前逐字相同（零影響）。
        stdin_prompt = prompt if self._executor == "cg" else None
        script = build_wrapper_script(
            inner_argv=inner_argv,
            sentinel=sentinel,
            ledger=str(ledger),
            worktree=worktree,
            repo_root=env.get("PSC_REPO_ROOT"),
            run_gates=self._should_run_gates(env),
            stdin_prompt=stdin_prompt,
            # #604：降權模式下 sentinel 改由 Manager 側的 exit 記帳 shell 寫；
            # job wrapper 內不得再出現任何指向 Manager log 目錄的寫入。
            write_sentinel=not degraded,
            commit_bundle=commit_bundle,
            verdict_file=verdict_file,
        )
        # Reviewer 不使用 login shell，避免 ~/.profile 等在最小 env 建立後重新匯入 secrets。
        # 降權模式的 builder 同理（#588 第 2 點）：login shell 會在 transient unit 的
        # 白名單 env 建立完成之後重新 source ~/.profile，把 env 約束整個覆寫掉。
        # direct 模式的 builder 維持 `-lc` 不動——那是既有行為，本票不改。
        argv = ["bash", "-c" if (self._review_only or degraded) else "-lc", script]
        popen_kwargs: dict[str, object] = {
            "cwd": worktree,
            "env": env,
            "stderr": subprocess.STDOUT,
        }
        if runner_plan is not None:
            argv = job_runner.build_systemd_run_argv(
                systemd_run=runner_plan.binary,
                unit=runner_plan.unit,
                account=runner_plan.account,
                group=runner_plan.group,
                working_directory=worktree,
                env=env,
                command=argv,
            )
            # #604：client 跑在 Manager 這一側且存活到 unit 結束，由它記 exit code。
            argv = job_runner.build_manager_exit_recorder_argv(
                client_argv=argv, sentinel=sentinel
            )
            # systemd-run **client 自己**的環境。它不會流進 transient unit——unit 的
            # 環境只有 PID 1 的 manager environment 加上 `--setenv` 白名單（這正是
            # #588 第 1 點在此模式下結構性成立的原因）。client 端保留完整 env 是刻意
            # 的：polkit 授權可能要查呼叫端的 session（XDG_SESSION_ID 等），砍掉會讓
            # 授權在某些部署下無故失敗。
            popen_kwargs["env"] = _git_scope_env()
            # FD：Popen 預設 close_fds=True，因此只有 0/1/2 會經 `--pipe` 進到 unit；
            # stdin 顯式接 /dev/null（direct 模式今天仍把 daemon 的 stdin 交給 job，
            # 降權模式在這點上比 direct 更緊）。
            popen_kwargs["stdin"] = subprocess.DEVNULL
        log_mode = "wb"
        if template_plan is not None:
            # #708：**先把 job 端的 log 落點準備好，再寫 spec。**
            #
            # 在此之前 spec 的 `log_path` 就是 Manager 的 dispatch log 路徑
            # （`<log_dir>/<slice>.jsonl`），而那個目錄是 `0700 cortex-manager`、
            # 零具名 ACL ⇒ shim 在**接管 stdio 之前**的 `os.open()` 直接 EACCES，
            # job 連一行 log 都寫不出來就以 `78/CONFIG` 收場（實機 0819）。
            # 那一層刻意不開放：gate ledger 與 exit sentinel 住在同一個目錄，開放
            # 它等於把 #604 的作者性保證賣掉。
            #
            # 改成寫進 builder 自己的 log spool（登記表資產 `build-job-log-spool`，
            # 掛在既有的 `commit-spool` 底下 ⇒ 模板 unit 的 `ReadWritePaths=` 逐字
            # 不變），Manager 那條路徑則以 **hard link** 指向同一個 inode——
            # `log_path` 這個字面量、sentinel／gate ledger／spool key 的推導、
            # harvest 與 usage 抽取因此**一個位元組都沒有變**。
            #
            # **落點由角色決定，不是寫死 builder**：launcher 同時派 builder 與
            # reviewer 兩種 job（`_job_role()`），兩者走不同的模板 unit、不同的帳號，
            # 因此也是不同的一條既有輸出通道。對應關係在
            # `job_runner.JobRoleConfig.log_spool_principal`（與
            # `registry.JOB_LOG_SPOOLS` 成對），不在這裡推導。
            prepared_log_path = str(
                job_workspace.prepare_job_log_spool(
                    principal_id=job_runner.JOB_ROLE_CONFIG[job_role].log_spool_principal,
                    spool_key=slice_id,
                    manager_log_path=log_path,
                )
            )
            # #714：argv 上的 `-o` 是由**上面那個純路徑推導**算出來的，這裡是真的建出
            # 那一格的那一步。兩者漂移＝「codex 寫到 A、shim 開的是 B」，而那種錯位
            # 只有在實機上才看得見（一個空的 last message ＋ 一個沒人讀的檔）。
            # 因此逐字比對，不相等就當場停下來。
            if prepared_log_path != job_log_path:
                raise RuntimeError(
                    "job log 落點推導漂移："
                    f"argv 用的是 {job_log_path}，實際建出來的是 {prepared_log_path}"
                    "（#714）"
                )
            job_log_path = prepared_log_path
            # #710：**再把工作區的可達性準備好**，同樣在寫 spec 之前。
            #
            # shim 在降權之後、exec 之前 `os.chdir(spec["working_directory"])`——那一步
            # 走不進去，job 就死在它做任何事之前（實機 `[Errno 13] Permission denied:
            # '/var/lib/cortex/worktree/wf-…'`）。#708 修好 log 之後露出的就是這一票：
            # per-job clone 是 Manager 建的、owner 因此是 Manager，而模板 unit 註解裡
            # 那句「整個 clone 由本 job 帳號擁有」**沒有任何程式實作**，且 Manager
            # 結構上做不到（`chown` 要 `CAP_CHOWN`，Manager 的 CapabilityBoundingSet
            # 是空的）。
            #
            # **與 log 那一格逐條同型**：形態由角色決定、對應關係在
            # `job_runner.JobRoleConfig.workspace_reach`（與
            # `registry.JOB_WORKSPACE_REACH` 成對），不在這裡推導；builder 走具名 ACL，
            # reviewer／planner 的工作區靠 pool 根的 default ACL 繼承（零動作）。
            # 三者共用同一支，且**都**在派工前以 mask-aware 的有效權限複驗一次。
            # env 取 `os.environ`（**Manager 的**環境）而不是上面組給 job 的那一份：
            # 帳號名來自 `PSC_*_ACCOUNT`，那是 Manager 端 root-owned EnvironmentFile 的
            # 部署決定；`prepare_systemd_template()` 上一步解身分用的也是同一份。
            job_runner.ensure_workspace_reachable(
                os.environ, role=job_role, workspace=worktree
            )
            # B 案：per-job 參數走 Manager-owned spec 檔（job 帳號唯讀），不走 argv
            # ——模板 unit 的 ExecStart= 是固定的，Manager 給不了命令列。
            # `User=` 刻意**不在** spec 內：身分只有 root-owned unit 檔一個來源。
            spec = job_runner.build_job_spec(
                job_id=slice_id,
                instance=template_plan.instance,
                unit=template_plan.unit,
                command=argv,
                working_directory=worktree,
                log_path=job_log_path,
                env=env,
            )
            job_runner.write_job_spec(
                template_plan.spec_path, spec, account=template_plan.account
            )
            argv = job_runner.build_systemctl_start_argv(
                systemctl=template_plan.binary, unit=template_plan.unit
            )
            # #604：同 A 案——sentinel 由 Manager 側這層 shell 寫，job 不參與。
            argv = job_runner.build_manager_exit_recorder_argv(
                client_argv=argv, sentinel=sentinel
            )
            # systemctl **client 自己**的環境（同 A 案的理由：polkit 授權可能要查
            # 呼叫端 session）。它不會流進 unit——unit 的環境來自 root-owned 模板檔，
            # job 的環境則來自 spec，由 shim 在 exec 時直接指定。
            popen_kwargs["env"] = _git_scope_env()
            # systemctl client 不進 worktree：job 的 cwd 由 shim 依 spec 的
            # working_directory 設定。三分方案下 per-job worktree 已 chown 給 job
            # 帳號，Manager 未必進得去，硬把 client 的 cwd 指過去只會多一個
            # PermissionError 失敗面。
            popen_kwargs["cwd"] = None
            popen_kwargs["stdin"] = subprocess.DEVNULL
            # log 由 shim 在降權後以 O_APPEND 接管（unit 沒有 --pipe、也刻意不用
            # StandardOutput=append:，理由見 job_runner 模組 docstring）。上一輪的
            # 殘留已經由 `prepare_job_log_spool()` 整格重建掉（新 inode、新 hard
            # link），因此這裡不再需要 `write_bytes(b"")`——**而且不能再截一次**：
            # 那會在 job 端與 Manager 端之間多一個「誰先動 offset」的競態。
            # 以 append 開檔的理由不變：systemctl client 的非 O_APPEND fd 會在 shim
            # 已經寫了幾 KB 之後從 offset 0 覆蓋回去。
            log_mode = "ab"
        with open(log_path, log_mode) as logf:
            popen_kwargs["stdout"] = logf
            proc = subprocess.Popen(argv, **popen_kwargs)
        if template_plan is not None:
            # polkit 拒絕／模板未安裝／shim 讀 spec 失敗只在起動當下才知道；
            # 確認不到就 fail-closed，**絕不**退回其他模式。
            job_runner.confirm_template_instance_started(
                process=proc,
                sentinel=sentinel,
                unit=template_plan.unit,
                account=template_plan.account,
                log_path=log_path,
                # #708 第 3 項：shim 在**接管 log 之前**的失敗只進 unit journal，
                # 而 Manager 讀不到那份 journal。它改為在 job 自己的 log spool 那一格
                # 留一筆機器可讀的紀錄，這裡把它撿回錯誤訊息裡。
                job_log_path=job_log_path,
                timeout_ms=job_runner.resolve_start_timeout_ms(os.environ),
                manager_authored_sentinel=True,
            )
        if runner_plan is not None:
            # polkit 拒絕／unit 名衝突只在起動當下才知道；確認不到就 fail-closed，
            # **絕不**退回 direct（見 job_runner.confirm_transient_unit_started）。
            job_runner.confirm_transient_unit_started(
                process=proc,
                sentinel=sentinel,
                unit=runner_plan.unit,
                account=runner_plan.account,
                log_path=log_path,
                timeout_ms=job_runner.resolve_start_timeout_ms(os.environ),
                manager_authored_sentinel=True,
            )
        return LaunchHandle(
            executor=self._executor,
            model_id=self._model,
            session_name=slice_id,
            pid=proc.pid,
            log_path=log_path,
        )
