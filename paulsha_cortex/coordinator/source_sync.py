"""Advance a Manager-owned source checkout to its GitHub default branch.

The system Monitor sees this checkout through a read-only mount. The Manager
performs the fetch and fast-forward so new workstream files become visible to
Monitor without granting it write access to the repository.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Sequence

_GITHUB_REMOTE = re.compile(
    r"^(?:ssh://)?git@github\.com[:/][^/]+/[^/]+(?:\.git)?$|"
    r"^https?://github\.com/[^/]+/[^/]+(?:\.git)?/?$"
)
_SAFE_BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]*$")
_OBJECT_ID = re.compile(r"[0-9a-fA-F]{40}$")
_DEFAULT_REF = "refs/cortex/source-sync/default"


class SourceSyncError(RuntimeError):
    """The source checkout could not be safely advanced."""


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _subprocess_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), check=False, capture_output=True, text=True)


def sync_source_checkout(
    repo_root: str | Path,
    *,
    runner: CommandRunner | None = None,
) -> str:
    """Fetch the advertised GitHub default branch and fast-forward a clean checkout.

    The fetch updates one private ref and suppresses ``FETCH_HEAD``. Local changes,
    diverged branches, and non-GitHub remotes are left untouched.
    """
    root = Path(repo_root).resolve()
    run = runner or _subprocess_runner

    def git(*args: str, ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
        argv = ("git", "--no-optional-locks", "-C", str(root), *args)
        try:
            result = run(argv)
        except OSError as exc:
            raise SourceSyncError(f"git {args[0]} failed: {type(exc).__name__}") from exc
        if result.returncode not in ok:
            detail = (result.stderr or "").strip().splitlines()
            suffix = f": {detail[-1][:240]}" if detail else ""
            raise SourceSyncError(f"git {args[0]} failed{suffix}")
        return result

    remote_result = git("config", "--get", "remote.origin.url", ok=(0, 1))
    remote = (remote_result.stdout or "").strip()
    if not remote or _GITHUB_REMOTE.fullmatch(remote) is None:
        raise SourceSyncError("source checkout origin is not a GitHub repository")

    advertised = git("ls-remote", "--symref", "origin", "HEAD")
    branch = None
    for line in (advertised.stdout or "").splitlines():
        match = re.fullmatch(r"ref: refs/heads/([^\t]+)\tHEAD", line)
        if match:
            branch = match.group(1)
            break
    if (
        branch is None
        or _SAFE_BRANCH.fullmatch(branch) is None
        or ".." in branch
        or branch.endswith(".lock")
    ):
        raise SourceSyncError("GitHub origin did not advertise a safe default branch")

    git(
        "fetch",
        "--no-tags",
        "--no-write-fetch-head",
        "--refmap=",
        "origin",
        f"+refs/heads/{branch}:{_DEFAULT_REF}",
    )
    target = git("rev-parse", "--verify", _DEFAULT_REF).stdout.strip().lower()
    if _OBJECT_ID.fullmatch(target) is None:
        raise SourceSyncError("fetched default branch is not a commit")

    dirty = git("status", "--porcelain", "--untracked-files=no").stdout
    if dirty.strip():
        return "dirty"

    current_is_ancestor = git("merge-base", "--is-ancestor", "HEAD", target, ok=(0, 1))
    if current_is_ancestor.returncode == 1:
        return "ahead-or-diverged"
    if target == git("rev-parse", "HEAD").stdout.strip().lower():
        return "unchanged"
    git("merge", "--ff-only", "--no-edit", target)
    return "advanced"
