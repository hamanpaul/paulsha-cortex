from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence, runtime_checkable

from . import gate_ledger, terminal_contract


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

_CREDENTIAL_ENV_RE = re.compile(
    r"(?:^|_)(?:API_?KEY|AUTH|COOKIE|CREDENTIALS?|PASSWORD|PRIVATE_?KEY|SECRET|TOKEN)(?:$|_)",
    re.IGNORECASE,
)


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
) -> str:
    """組出 headless wrapper script（#261：模型結束後由 manager 產生 gate ledger）。

    三段皆以 ``;`` 串接，因此模型失敗時 sentinel 與 ledger 仍會產生：

    1. 模型 argv；
    2. 把 ``$?`` 寫入 exit sentinel（跨進程 durable 完成判定，早於 gate 階段，
       確保 gate 執行時間不會被算進模型的 exit code）；
    3. 由 manager 掌控的 gate ledger writer。

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
    script = f'{command}; printf %s "$?" > {shlex.quote(sentinel)}'
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
    verdict_spool_dir: str | None = None,
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
    elif read_only or review_only:
        argv += ["--sandbox", "read-only", "--skip-git-repo-check"]
    else:
        argv += ["--sandbox", "workspace-write"]
        if commit_required:
            for git_write_dir in _linked_worktree_git_write_dirs(worktree):
                argv += ["--add-dir", git_write_dir]
    for spool_dir in _verdict_spool_add_dirs(
        verdict_spool_dir, read_only=read_only, review_only=review_only
    ):
        argv += ["--add-dir", spool_dir]
    if model is not None:
        argv += ["--model", model]
    argv.extend(["-o", str(Path(log_dir) / "last.json")])
    if worktree is not None:
        argv.extend(["-C", worktree])
    return argv


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
            effort=self._effort,
            verdict_spool_dir=spool_dir,
        )

    def as_commit_required(self) -> "SubprocessLauncher":
        """Return a builder launcher explicitly allowed to update linked Git metadata."""

        if self._read_only or self._review_only:
            raise ValueError("commit-required launcher requires enforced workspace-write")
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

    def _should_run_gates(self, env: Mapping[str, str]) -> bool:
        """#261：只有會改動 candidate 的可寫入 card 才在 wrapper 內跑確定性 gate。

        read-only／review-only 的 reviewer 不改 candidate，也刻意在最小 env 下執行，
        不應在它的 sandbox 內跑 gate；operator 未宣告任何 ``PSC_GATE_CMD_*`` 時仍會
        寫出一份 ``gates: []`` 的 ledger，讓 harvest 能區分「沒宣告 gate」與
        「wrapper 根本沒跑完」。
        """

        if self._review_only or self._read_only:
            return False
        return env.get("PSC_REPO_ROOT") is not None

    def executor_environment(self, *, slice_id: str = "preflight"):
        """#262 D2：回報正式 job 會實際看到的 executor 環境。

        env 由與 `launch()` 相同的 `_review_scope_env()`／`_git_scope_env()` 產生，
        因此 preflight 檢查的 PATH／HOME／sandbox policy 與正式 job 一致；
        若在此另建一份 env，preflight 就只是安慰劑（見 design D2）。
        """

        from .runtime_preflight import ExecutorEnvironment

        if self._review_only:
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
        resolved_worktree = Path(worktree).resolve(strict=True)
        if not resolved_worktree.is_dir():
            raise ValueError("launcher worktree must be a directory")
        worktree = str(resolved_worktree)
        # log_dir resolve 成絕對：sentinel 由子進程的 bash wrapper 以 cwd=worktree 寫入，
        # 相對路徑會落到 worktree（poller 在他處找不到）→ 完成偵測對 worktree dispatch 失效。
        # 絕對化後 JSONL / sentinel / 回傳 log_path 皆與 cwd 無關，跨進程 poll 一致。
        log_dir = str(Path(log_dir).resolve())
        Path(log_dir).mkdir(parents=True, exist_ok=True)
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
        inner_argv = _ARGV_BUILDERS[self._executor](
            **builder_kwargs,
        )
        # PSC_REPO_ROOT 讓已安裝 hook 的 `${PSC_REPO_ROOT}/scripts/coordinator/psc-relay-hook.sh`
        # 在 cwd=worktree（≠repo）時仍可解（worktree 雖是 repo checkout，但 hook 為全域安裝、
        # 不可依賴相對 cwd；互動 session 亦不應因相對路徑找不到 script 而報錯）。
        if self._review_only:
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
        log_path = str(Path(log_dir) / f"{slice_id}.jsonl")
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
        )
        # Reviewer 不使用 login shell，避免 ~/.profile 等在最小 env 建立後重新匯入 secrets。
        argv = ["bash", "-c" if self._review_only else "-lc", script]
        with open(log_path, "wb") as logf:
            proc = subprocess.Popen(
                argv,
                cwd=worktree,
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        return LaunchHandle(
            executor=self._executor,
            model_id=self._model,
            session_name=slice_id,
            pid=proc.pid,
            log_path=log_path,
        )
