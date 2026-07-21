from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


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
                "status": {"type": "string", "enum": ["verified"]},
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
                "reason": {"type": "string", "minLength": 1},
                "findings": {"type": "array", "items": finding},
                "reports": {"type": "array", "minItems": 1, "items": report},
            },
        }
    else:
        raise ValueError("Claude reviewer terminal contract kind invalid")
    return json.dumps(schema, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


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


def _git_scope_env() -> dict[str, str]:
    """Drop inherited Git repository/config selectors before scope binding."""

    return {
        key: value
        for key, value in os.environ.items()
        if key not in _GIT_REPOSITORY_ENV_KEYS and not key.startswith("GIT_CONFIG_")
    }


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
) -> list[str]:
    if (read_only or review_only) and allow_unsafe:
        raise ValueError("read-only Claude launcher cannot bypass permissions")
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
    if model is not None:
        argv += ["--model", model]
    if worktree is not None and not review_only:
        argv.extend(["--add-dir", worktree])
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


_ARGV_BUILDERS = {
    "copilot": build_copilot_argv,
    "claude": build_claude_argv,
    "codex": build_codex_argv,
    "agy": build_agy_argv,
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
    ) -> None:
        if executor not in _ARGV_BUILDERS:
            raise ValueError(f"unknown executor: {executor}")
        if executor == "agy" and allow_unsafe:
            raise ValueError("agy executor refuses unsafe mode")
        if (read_only or review_only) and executor == "copilot":
            raise ValueError("copilot executor has no enforced read-only planning mode")
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
        if self._executor in {"codex", "copilot"}:
            builder_kwargs["commit_required"] = self._commit_required
        if self._executor == "claude":
            builder_kwargs["review_terminal_kind"] = self._review_terminal_kind
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
                "PSC_REPO_ROOT": str(Path(__file__).resolve().parents[2]),
            }
            if self._relay_target is not None:
                env["PSC_RELAY_TARGET"] = self._relay_target
        log_path = str(Path(log_dir) / f"{slice_id}.jsonl")
        # 跨進程 durable 完成判定：以 bash -lc 包裝，子進程結束時把 $? 寫入 exit sentinel。
        # 用 shlex.join 安全嵌入內層 argv（prompt 含換行/空白仍為單一 token），
        # sentinel 路徑亦 shlex.quote。poll_headless_done 讀此 sentinel，不再靠 os.waitpid。
        sentinel = str(Path(log_dir) / f"{slice_id}.exit")
        # 重跑同一 slice_id 前先清掉上一輪殘留：移除舊 exit sentinel、log 以 wb 截斷。
        # 否則 poll_headless_done 會讀到上一輪的 sentinel / 末筆 JSONL，
        # 誤判「還沒開始就已完成」（fail-closed：每輪從乾淨狀態起跑）。
        Path(sentinel).unlink(missing_ok=True)
        script = f'{shlex.join(inner_argv)}; printf %s "$?" > {shlex.quote(sentinel)}'
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
