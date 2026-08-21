"""#506 / D5：headless job 的 GitHub 物件事件 hook——D4 spool 的寫入端。

## 這個模組是什麼

D4（`paulsha_cortex.monitor.event_spool`）開了一條本機事件通道，但**沒有任何
producer**：monitor 每輪掃 spool，掃到的永遠是空目錄。本模組補上第一個 producer
——headless claude job 每跑完一次 ``Bash`` 工具，就由 launcher 注入的 PostToolUse
hook 呼叫 ``cortex headless-hook post-tool-use``，把「我剛動了哪個 GitHub 物件」
寫進 spool。monitor 下一輪對被點名的物件做 targeted 條件驗證，發現延遲從一個
refresh 週期（實務上 30 分鐘）壓到一輪。

## 只在 headless 觸發：兩道**結構性**保證

使用者的硬約束是「**不得影響正常的互動式 agent 使用**」。本模組不靠設定開關，
靠兩件同時成立才會有事件的結構條件：

1. **hook 只經 launcher 注入**：hook 宣告只存在於
   `SubprocessLauncher.launch()` 為每個 job 現場組出來的 ``--settings`` JSON
   （見 `coordinator/launcher.py` 的 `_claude_spool_hook_settings`），**從不寫入
   任何檔案**，尤其不寫 `~/.claude/settings.json`。互動 session 讀的是 operator
   自己的設定，那裡沒有這個 hook，因此互動 session **連呼叫本模組的機會都沒有**。
2. **`PSC_JOB_ID` 自守**：即使 hook 宣告以任何方式流到互動 session（例如有人手動
   複製那段 JSON），:func:`headless_job_id` 讀不到 ``PSC_JOB_ID`` 就直接回
   ``None``，:func:`emit_for_tool_use` 隨即 return——**不建 spool 目錄、不寫任何
   檔案、不跑任何 subprocess、不解析命令**。這個變數只由 launcher 為 cortex 派工
   的 job 設定。

兩道保證彼此獨立：任何一道成立，互動 session 就是完全的 no-op。

## fire-and-forget

hook 掛在**別人**（agent job）的工作路徑上，因此本模組的每一條路徑都是
fire-and-forget：:func:`emit_for_tool_use` 用一個總括的 ``except Exception``
把所有失敗吞成 debug log，CLI 一律 exit 0，launcher 注入的命令另外以
``|| true`` 兜底。掉一則 hint 的後果只是退回原本的 refresh 週期延遲——那正是 D3
每日 anti-entropy 的守備範圍；讓 hook 的例外炸掉一個跑到一半的 job 則是完全不成
比例的代價。

## hint 不是 authority

事件只帶「哪個 repo 的哪個編號被動了」，**不帶新狀態**。命令解析因此可以（也應該）
往「寧可漏報」的方向失準：解不出編號就不發事件，發現延遲退回輪詢週期；反之發出
錯誤的物件只是讓 monitor 多花一次條件請求（多半 304），也不會污染鏡像——鏡像只寫
GitHub 自己回的內容。``action`` 純屬診斷。

## #536／#488 心跳（本次只預留信封）

同一條 hook 也是 job 心跳的天然訊號源（每次 tool call 都會觸發）。本次**只發
``github_object`` 事件**，但每一則都帶 ``job_id``——D4 信封的 ``job_id`` 欄位與
``RESERVED_EVENT_TYPES`` 裡的 ``job`` 型別因此已經備妥，心跳 consumer
（#536 define 停滯、#488 stale-progress）落地時不需要改寫入端契約。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from paulsha_cortex.monitor.event_spool import EventSpool

from . import COMMANDS, PorcelainCommand, register


logger = logging.getLogger(__name__)


#: launcher 為 cortex 派工的 job 設定的自守標記。**沒有它就沒有任何事件**。
JOB_MARKER_ENV = "PSC_JOB_ID"

#: 寫進事件信封的 ``source``；與 D4 消費端測試使用的字面值一致。
EVENT_SOURCE = "agent-hook:claude"

#: 解析 GitHub 物件時只認這兩種 kind（與 `IssueEntry.kind`／D4 同語彙）。
KIND_ISSUE = "github_issue"
KIND_PR = "github_pr"

#: git remote 解析出來的 owner/name 之外，其餘一律視為解析失敗。
_REPO_SLUG = re.compile(r"\A[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")

# `gh issue`／`gh pr` 底下**會改動遠端物件**的動詞（封閉列舉）。
# 只列會落在 `repos/{repo}/issues/{number}` 這個端點上看得到的動作；未知動詞一律
# 不發事件——漏報退回輪詢週期，誤報則是白花一次條件請求。
_GH_ISSUE_MUTATIONS = frozenset(
    {
        "close",
        "comment",
        "delete",
        "develop",
        "edit",
        "lock",
        "pin",
        "reopen",
        "transfer",
        "unlock",
        "unpin",
    }
)
_GH_PR_MUTATIONS = frozenset(
    {
        "close",
        "comment",
        "edit",
        "lock",
        "merge",
        "ready",
        "reopen",
        "review",
        "unlock",
        "update-branch",
    }
)

# `gh api` 的請求方法：沒指定時，帶欄位參數即隱含 POST（gh 自己的行為）。
_GH_API_FIELD_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})
_GH_API_FIELD_PREFIXES = ("--field=", "--raw-field=", "--input=")
_READ_ONLY_METHODS = frozenset({"GET", "HEAD"})

# `repos/{owner}/{repo}/{issues|pulls}/{number}`：編號必須是數字，因此
# `issues/comments/{comment_id}`（改的是留言不是 issue）不會被誤認。
# `{owner}`／`{repo}` 是 gh 自己的 placeholder（由 gh 依 cwd 展開），解析端
# 認得它但不當成 repo 名，改走 cwd 的 git remote。
_API_PLACEHOLDER = frozenset({"{owner}", "{repo}"})
_API_OBJECT = re.compile(
    r"repos/(?P<owner>\{owner\}|[A-Za-z0-9._-]+)/(?P<repo>\{repo\}|[A-Za-z0-9._-]+)"
    r"/(?P<collection>issues|pulls)/(?P<number>\d+)(?![0-9])"
)
_HTML_OBJECT = re.compile(
    r"https?://[^/\s]*github\.com/(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)"
    r"/(?P<collection>issues|pull|pulls)/(?P<number>\d+)(?![0-9])"
)
_BARE_NUMBER = re.compile(r"\A#?(?P<number>\d+)\Z")

# 命令前綴：只有這些包裝詞（與 `VAR=value` 賦值）之後的 `gh` 才算 gh 呼叫，
# 避免把 `echo gh issue comment 1` 這種提到但沒執行的字串當成 mutation。
_COMMAND_PREFIXES = frozenset({"command", "env", "nohup", "sudo"})
_ENV_ASSIGNMENT = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*=")

# shlex 的 punctuation_chars 會把 `&& || ; | ( ) < >` 切成獨立 token；
# 由這些字元組成的 token 即為命令分隔點。
_PUNCTUATION = set("();<>|&")

_GIT_REMOTE_TIMEOUT_SECONDS = 5.0

_UNRESOLVED = object()


def register_commands() -> None:
    if "headless-hook" in COMMANDS:
        return
    register(
        PorcelainCommand(
            name="headless-hook",
            help="headless job 的事件 hook：把被動過的 GitHub 物件寫進 monitor spool",
            run=main,
        )
    )


# ---------------------------------------------------------------------------
# 自守標記
# ---------------------------------------------------------------------------


def headless_job_id(env: Mapping[str, str] | None = None) -> str | None:
    """回傳這個行程所屬的 cortex job id；不是 headless job 就回 ``None``。

    這是「只在 headless 觸發」的機制保證：``PSC_JOB_ID`` 只由
    `SubprocessLauncher.launch()` 為 cortex 派工的 job 注入，互動 session 的環境
    裡不存在，因此呼叫端讀到 ``None`` 就該原地返回，**不做任何其他事**。
    """

    source = os.environ if env is None else env
    try:
        value = source.get(JOB_MARKER_ENV)
    except Exception:  # noqa: BLE001 - env 可能是任意 Mapping 實作
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


# ---------------------------------------------------------------------------
# 命令解析
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitHubObjectRef:
    """命令裡被動到的一個 GitHub 物件。``repo`` 為 ``None`` 代表命令沒寫，待 cwd 補。"""

    kind: str
    number: int
    action: str
    repo: str | None = None


def _split_segments(command: str) -> tuple[tuple[str, ...], ...]:
    """把一行 shell 命令切成「以 ``&&``／``||``／``;``／``|`` 分隔」的命令段。

    job 常常一行內串好幾個命令（``gh issue comment ... && gh issue edit ...``），
    只看第一個動詞會漏掉後面的物件。解析失敗（引號不對稱等）回空——寧可漏報。
    """

    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return ()
    segments: list[tuple[str, ...]] = []
    current: list[str] = []
    for token in tokens:
        if token and all(char in _PUNCTUATION for char in token):
            if current:
                segments.append(tuple(current))
                current = []
            continue
        current.append(token)
    if current:
        segments.append(tuple(current))
    return tuple(segments)


def _gh_arguments(segment: Sequence[str]) -> tuple[str, ...] | None:
    """段落是 ``gh`` 呼叫時回傳 ``gh`` 之後的參數，否則回 ``None``。"""

    for index, token in enumerate(segment):
        if PurePosixPath(token).name == "gh":
            return tuple(segment[index + 1 :])
        if _ENV_ASSIGNMENT.match(token) or token in _COMMAND_PREFIXES:
            continue
        return None
    return None


def _positional_tokens(tokens: Sequence[str]) -> tuple[str, ...]:
    """取出位置參數，並把旗標**與其值**一起跳過。

    保守到「把每個旗標都當成吃一個值」：``gh pr merge --squash 45`` 因此會漏掉
    編號。這是刻意的方向——漏報只是退回輪詢週期，而把 ``--add-label`` 的值誤認成
    issue 編號會讓 monitor 去查一個不存在的物件。
    """

    positionals: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("--"):
            skip_next = "=" not in token
            continue
        if token.startswith("-") and len(token) > 1:
            skip_next = True
            continue
        positionals.append(token)
    return tuple(positionals)


def _flag_value(tokens: Sequence[str], names: tuple[str, ...]) -> str | None:
    """讀 ``--name value``／``--name=value``／``-Xvalue`` 形式的旗標值。"""

    for index, token in enumerate(tokens):
        for name in names:
            if token == name:
                if index + 1 < len(tokens):
                    return tokens[index + 1]
                return None
            if token.startswith(f"{name}="):
                return token[len(name) + 1 :]
            if name.startswith("-") and not name.startswith("--") and token.startswith(name):
                remainder = token[len(name) :]
                if remainder:
                    return remainder
    return None


def _repo_from_match(match: "re.Match[str]") -> str | None:
    owner = match.group("owner")
    name = match.group("repo")
    if owner in _API_PLACEHOLDER or name in _API_PLACEHOLDER:
        return None
    slug = f"{owner}/{name}"
    return slug if _REPO_SLUG.fullmatch(slug) else None


def _parse_gh_object(group: str, arguments: Sequence[str]) -> tuple[GitHubObjectRef, ...]:
    """解析 ``gh issue <verb> ...``／``gh pr <verb> ...``。"""

    if not arguments:
        return ()
    verb = arguments[0]
    mutations = _GH_ISSUE_MUTATIONS if group == "issue" else _GH_PR_MUTATIONS
    if verb not in mutations:
        return ()
    rest = arguments[1:]
    kind = KIND_ISSUE if group == "issue" else KIND_PR
    repo = _flag_value(rest, ("--repo", "-R"))
    if repo is not None and not _REPO_SLUG.fullmatch(repo):
        repo = None
    for token in _positional_tokens(rest):
        url = _HTML_OBJECT.search(token)
        if url is not None:
            return (
                GitHubObjectRef(
                    kind=KIND_ISSUE if url.group("collection") == "issues" else KIND_PR,
                    number=int(url.group("number")),
                    action=f"{group}-{verb}",
                    repo=_repo_from_match(url) or repo,
                ),
            )
        bare = _BARE_NUMBER.fullmatch(token)
        if bare is not None:
            number = int(bare.group("number"))
            if number <= 0:
                return ()
            return (
                GitHubObjectRef(kind=kind, number=number, action=f"{group}-{verb}", repo=repo),
            )
    # 沒有編號可解（例如 `gh pr comment` 靠 cwd 的分支推斷當前 PR）：不猜。
    return ()


def _parse_gh_api(arguments: Sequence[str]) -> tuple[GitHubObjectRef, ...]:
    """解析 ``gh api``：只認會改狀態的方法＋單物件路徑。"""

    method = _flag_value(arguments, ("--method", "-X"))
    if method is None:
        has_field = any(
            token in _GH_API_FIELD_FLAGS or token.startswith(_GH_API_FIELD_PREFIXES)
            for token in arguments
        )
        method = "POST" if has_field else "GET"
    if method.strip().upper() in _READ_ONLY_METHODS:
        return ()
    action = f"api-{method.strip().lower()}"
    for token in _positional_tokens(arguments):
        match = _API_OBJECT.search(token)
        if match is None:
            continue
        number = int(match.group("number"))
        if number <= 0:
            continue
        return (
            GitHubObjectRef(
                kind=KIND_ISSUE if match.group("collection") == "issues" else KIND_PR,
                number=number,
                action=action,
                repo=_repo_from_match(match),
            ),
        )
    return ()


def parse_bash_command(command: str) -> tuple[GitHubObjectRef, ...]:
    """從一行 Bash 命令解析出被動過的 GitHub 物件；**永不 raise**。

    同一行內同一個物件被點名多次只回一筆（monitor 端也會再收斂一次，這裡先省掉
    重複的事件檔）。
    """

    if not isinstance(command, str) or not command.strip():
        return ()
    try:
        segments = _split_segments(command)
    except Exception as error:  # noqa: BLE001 - 解析失敗不得影響 job
        logger.debug("headless hook could not lex a command: %s", error)
        return ()
    found: dict[tuple[str | None, str, int], GitHubObjectRef] = {}
    for segment in segments:
        arguments = _gh_arguments(segment)
        if not arguments:
            continue
        group = arguments[0]
        if group == "api":
            refs = _parse_gh_api(arguments[1:])
        elif group in {"issue", "pr"}:
            refs = _parse_gh_object(group, arguments[1:])
        else:
            refs = ()
        for ref in refs:
            found.setdefault((ref.repo, ref.kind, ref.number), ref)
    return tuple(found.values())


# ---------------------------------------------------------------------------
# repo 補值
# ---------------------------------------------------------------------------


def repo_from_remote_url(url: str) -> str | None:
    """由 git remote URL 取 ``owner/name``；認不出來回 ``None``。"""

    if not isinstance(url, str) or not url.strip():
        return None
    text = url.strip()
    for prefix in ("git+", "ssh://", "git://", "https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    if "@" in text and "://" not in text:
        text = text.split("@", 1)[1]
    text = text.replace(":", "/", 1) if ":" in text.split("/", 1)[0] else text
    if text.endswith(".git"):
        text = text[: -len(".git")]
    parts = [part for part in text.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    slug = f"{parts[-2]}/{parts[-1]}"
    return slug if _REPO_SLUG.fullmatch(slug) else None


def resolve_repo_from_worktree(
    cwd: object,
    *,
    runner: Callable[..., Any] | None = None,
) -> str | None:
    """命令沒寫 ``--repo`` 時，從 job worktree 的 ``origin`` 補；失敗回 ``None``。

    只讀本機 git 設定（不打網路、不碰 GitHub），並帶超時；任何失敗一律回 ``None``
    ——沒有 repo 就沒有事件，發現延遲退回輪詢週期。
    """

    if not isinstance(cwd, str) or not cwd.strip():
        return None
    execute = subprocess.run if runner is None else runner
    try:
        completed = execute(
            ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=_GIT_REMOTE_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception as error:  # noqa: BLE001 - git 不在／不是 repo／逾時
        logger.debug("headless hook could not read the job remote: %s", error)
        return None
    if getattr(completed, "returncode", 1) != 0:
        return None
    return repo_from_remote_url(getattr(completed, "stdout", "") or "")


# ---------------------------------------------------------------------------
# 寫入端
# ---------------------------------------------------------------------------


def emit_for_tool_use(
    payload: object,
    *,
    env: Mapping[str, str] | None = None,
    spool: EventSpool | None = None,
    runner: Callable[..., Any] | None = None,
) -> tuple[str, ...]:
    """處理一次 PostToolUse 事件；回傳寫出去的物件 ref。**永不 raise**。

    第一件事就是查 ``PSC_JOB_ID``：不是 headless job 就在**碰到 spool、cwd、
    甚至解析命令之前**返回空 tuple。互動 session 走到這裡（照設計走不到，見模組
    docstring）也只是這一行的成本。
    """

    try:
        job_id = headless_job_id(env)
        if job_id is None:
            # 互動 session 的唯一結局：什麼都沒發生——沒有 spool 目錄、沒有檔案、
            # 沒有 subprocess，也沒有任何行為改變。
            return ()
        if not isinstance(payload, Mapping):
            return ()
        if payload.get("tool_name") != "Bash":
            return ()
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
        if not isinstance(command, str):
            return ()
        refs = parse_bash_command(command)
        if not refs:
            return ()
        fallback_repo: object = _UNRESOLVED
        # The hook is a job-side producer.  Bind its default writer to the same
        # Manager-authored identity that is carried in the event envelope; the
        # unbound EventSpool is reserved for the monitor's shared-root reader.
        target = spool if spool is not None else EventSpool(job_id=job_id)
        emitted: list[str] = []
        for ref in refs:
            repo = ref.repo
            if repo is None:
                if fallback_repo is _UNRESOLVED:
                    fallback_repo = resolve_repo_from_worktree(
                        payload.get("cwd"), runner=runner
                    )
                repo = fallback_repo if isinstance(fallback_repo, str) else None
            if repo is None:
                continue
            path = target.emit_github_object(
                repo=repo,
                kind=ref.kind,
                number=ref.number,
                source=EVENT_SOURCE,
                action=ref.action,
                job_id=job_id,
            )
            if path is not None:
                emitted.append(f"{repo}#{ref.number}")
        return tuple(emitted)
    except Exception as error:  # noqa: BLE001 - fire-and-forget 的全部意義
        logger.debug("headless hook dropped a tool-use event: %s", error)
        return ()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_stdin_payload(stream: Any) -> dict[str, Any]:
    try:
        raw = stream.read()
    except Exception:  # noqa: BLE001 - stdin 關閉／不可讀
        return {}
    if not raw or not str(raw).strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def main(argv: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="cortex headless-hook")
    sub = parser.add_subparsers(dest="command", required=True)
    post_tool_use = sub.add_parser(
        "post-tool-use",
        help="讀 stdin 的 PostToolUse payload，把被動過的 GitHub 物件寫進 spool",
    )
    post_tool_use.add_argument(
        "--spool-root",
        default=None,
        help="覆寫 monitor event spool 目錄（測試/fixture 注入用）",
    )

    args = parser.parse_args(list(argv))
    if args.command != "post-tool-use":
        parser.error(f"unsupported headless-hook command: {args.command}")
        return 2

    payload = _read_stdin_payload(sys.stdin)
    job_id = headless_job_id(os.environ)
    spool = (
        EventSpool(args.spool_root, job_id=job_id)
        if args.spool_root and job_id is not None
        else None
    )
    emit_for_tool_use(payload, spool=spool)
    # stdout 一律空、exit code 一律 0：PostToolUse 的非零 exit 會讓 Claude Code
    # 把 stderr 回報成 hook 失敗（甚至回饋給模型），而這個 hook 對 job 本體
    # 沒有任何話要說——它只是把一則 hint 丟進本機目錄。
    return 0
