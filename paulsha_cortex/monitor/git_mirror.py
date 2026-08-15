"""#506 / D2：git 的資料走 git——把 repo 檔案讀取與 ancestry 判定移出 REST。

## 問題

``GitHubTerminalProvider`` 一輪掃描對 GitHub REST 發出的兩類請求，讀的全是本機
git checkout 裡本來就有（或 ``git fetch`` 一次就有）的東西：

- **``contents``**：每個 remote ``todo.md`` / archived ``tasks.md`` 各一次，實測一輪
  91 次——讀的是 default branch 上某個 blob 的內容。
- **``compare``**：每個 workflow-linked merged PR 各一次——判的是「merge commit 還在
  不在 default branch 上」，也就是一次 ancestry。

REST 有 primary／secondary rate limit；git 協定（fetch）不受它管轄。0813–0815 三度
進 REST 懲罰窗，這兩類是主要配額消耗之一。

## 本模組的契約

``LocalGitMirror`` 是「本機 git 當成遠端事實來源」的唯一入口：

1. **身分先驗**：``origin`` 必須真的指向宣稱的 ``owner/name``，否則 fail closed。
   monitor 掃的 workspace 目錄不保證是我們以為的那個 repo。
2. **sha 定址**：blob 一律用 REST tree 給的 blob sha 讀（``git cat-file --batch``），
   不用 path 讀。內容識別由 sha 本身保證，取代舊的
   ``remote Todo content identity mismatch`` 檢查。
3. **一輪最多一次 fetch**：``require()`` 先批次查缺（一次 ``--batch-check``），有缺才
   fetch，refspec 目的地一律落在私有 namespace ``refs/cortex/mirror/<hash>/*``，
   並以 ``--refmap=`` 關掉 configured refspec 的順帶更新，**不動**
   ``refs/remotes/origin/*``、工作區與使用者的任何分支。fetch 頻率因此沿用 monitor
   既有的 refresh 週期。
4. **fail closed**：ref 不存在、fetch 失敗、物件讀不到、shallow checkout 無法判
   ancestry——一律 raise :class:`GitMirrorError`，由 provider 轉成 degraded 快照、
   上層 ``_retain_last_good`` 保留上一份鏡像。**絕不**把讀不到靜默當成「檔案不存在」
   或「不是 ancestor」。唯一的例外是「default branch 已在本機、repo 非 shallow，而
   merge commit 仍不在本機」——這在 git 的可達性語意下就是「不是 ancestor」的定義，
   不是讀取失敗（見 :meth:`LocalGitMirror.is_ancestor`）。

:class:`GitMirrorError` 刻意繼承 ``OSError``，即使日後有人漏接這個新型別，
``GitHubTerminalProvider.scan()`` 既有的 catch-all 仍然接得住，只會退回通用診斷而不會
讓例外逸出 provider。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Mapping, Protocol, Sequence


# 與 ``monitor/work_api.py`` 的 ``_repo_identity`` 共用同一組樣式（該檔 import 本檔），
# 避免「provider 認定是 GitHub repo、mirror 卻認不出 origin」的分歧。
GITHUB_SSH_REMOTE = re.compile(
    r"^(?:ssh://)?git@github\.com[:/](?P<repo>[^/]+/[^/]+?)(?:\.git)?$"
)
GITHUB_HTTPS_REMOTE = re.compile(
    r"^https?://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$"
)

_OBJECT_ID = re.compile(r"[0-9a-fA-F]{40}")
# git ref component 的保守子集：不接受前導 ``-``（避免被當成 git 旗標）、``..``、
# ``.lock`` 結尾與空白。default branch 名稱來自 GraphQL，仍當成不可信輸入處理。
_SAFE_BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]*")

# ``relevant_pr_numbers=None`` 時 candidate 可能是整個 repo 的 merged PR。PR head
# refspec 只是選配（見 ``_fetch``），不值得為它把 fetch 撐爆。
_PULL_REF_LIMIT = 64


class GitMirrorError(OSError):
    """本機 git 鏡像不可用；呼叫端一律 fail closed。"""


class GitRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]: ...


class SubprocessGitRunner:
    """git 一律以 bytes 收；blob 內容的 utf-8 解碼由呼叫端顯式負責。"""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            input=b"" if stdin is None else stdin,
            timeout=timeout,
        )


def _validate_object_ids(values: Sequence[str]) -> tuple[str, ...]:
    ordered = tuple(dict.fromkeys(values))
    for value in ordered:
        if not isinstance(value, str) or _OBJECT_ID.fullmatch(value) is None:
            raise GitMirrorError("git mirror object id is invalid")
    return tuple(value.lower() for value in ordered)


class LocalGitMirror:
    """以本機 git objects 回答「default branch 上的檔案內容／ancestry」。"""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        repo: str,
        runner: GitRunner | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.repo = repo
        self.runner = runner or SubprocessGitRunner()
        self.timeout_seconds = float(timeout_seconds)
        # 私有 namespace 以 repo slug 的 hash 命名：永遠是合法 ref component，
        # 且兩個 repo 不會互撞。
        self._namespace = (
            "refs/cortex/mirror/"
            + hashlib.sha256(repo.encode("utf-8")).hexdigest()[:16]
        )
        self._verified = False
        self._fetched_refs: tuple[str, ...] = ()
        self._absent: frozenset[str] = frozenset()
        self._blob_reads = 0
        self._ancestry_checks = 0

    # -- 基礎設施 ---------------------------------------------------------

    def _run(
        self,
        args: Sequence[str],
        *,
        stdin: bytes | None = None,
        ok: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        argv = ("git", "--no-optional-locks", "-C", str(self.repo_root), *args)
        try:
            completed = self.runner.run(
                argv, timeout=self.timeout_seconds, stdin=stdin
            )
        except subprocess.TimeoutExpired as error:
            raise GitMirrorError(f"git mirror timeout: {args[0]}") from error
        except GitMirrorError:
            raise
        except OSError as error:
            raise GitMirrorError(f"git mirror unavailable: {error}") from error
        if completed.returncode not in ok:
            detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
            raise GitMirrorError(
                f"git {args[0]} failed in {self.repo_root}: {detail.splitlines()[-1] if detail else 'no stderr'}"
            )
        return completed

    def _verify_origin(self) -> None:
        if self._verified:
            return
        # 讀 raw config 而非 ``git remote get-url``：後者會套用 ``url.*.insteadOf``
        # 改寫，回傳的是 transport 目的地（企業 mirror／本機路徑），不是這個
        # checkout 宣告要追的 repo。身分判準要的是後者。
        completed = self._run(("config", "--get", "remote.origin.url"), ok=(0, 1))
        remote = (completed.stdout or b"").decode("utf-8", "replace").strip()
        if not remote:
            raise GitMirrorError(
                f"local checkout {self.repo_root} has no origin remote"
            )
        for pattern in (GITHUB_SSH_REMOTE, GITHUB_HTTPS_REMOTE):
            match = pattern.fullmatch(remote)
            if match is not None and match.group("repo").lower() == self.repo.lower():
                self._verified = True
                return
        raise GitMirrorError(
            f"local checkout {self.repo_root} does not track {self.repo}"
        )

    def _missing(self, oids: Sequence[str]) -> frozenset[str]:
        if not oids:
            return frozenset()
        payload = ("\n".join(oids) + "\n").encode("ascii")
        completed = self._run(("cat-file", "--batch-check"), stdin=payload)
        lines = (completed.stdout or b"").decode("utf-8", "replace").splitlines()
        if len(lines) != len(oids):
            raise GitMirrorError("git cat-file --batch-check output malformed")
        missing: set[str] = set()
        for oid, line in zip(oids, lines):
            fields = line.split()
            if len(fields) == 2 and fields[1] == "missing":
                missing.add(oid)
                continue
            if len(fields) != 3 or fields[0].lower() != oid:
                raise GitMirrorError("git cat-file --batch-check output malformed")
        return frozenset(missing)

    def _fetch(self, *, default_branch: str, pull_numbers: Sequence[int]) -> None:
        if _SAFE_BRANCH.fullmatch(default_branch) is None or ".." in default_branch:
            raise GitMirrorError("default branch name is not a safe git ref")
        branch_refspec = (
            f"+refs/heads/{default_branch}:{self._namespace}/default"
        )
        pull_refspecs = tuple(
            f"+refs/pull/{number}/head:{self._namespace}/pull/{number}"
            for number in sorted(set(pull_numbers))[:_PULL_REF_LIMIT]
        )
        # ``--refmap=`` 關掉 configured refspec 的「順帶更新」——沒有它，即使命令列
        # 給了顯式 refspec，git 仍會一併寫 ``refs/remotes/origin/*``。monitor 是在
        # operator 正在工作的 checkout 上跑，不該動它的 remote-tracking refs。
        base = ("fetch", "--refmap=", "--no-tags", "--quiet", "origin")
        try:
            self._run((*base, branch_refspec, *pull_refspecs))
        except GitMirrorError:
            if not pull_refspecs:
                raise
            # ``refs/pull/*`` 只有 GitHub 提供；fork mirror／自架 remote 沒有它時，
            # 讓整輪掃描因為一個**選配** refspec 而 degraded 是錯的。退回只 fetch
            # default branch——ancestry 的判準本來就只靠 default branch 的可達性。
            self._run((*base, branch_refspec))
            self._fetched_refs = (branch_refspec,)
            return
        self._fetched_refs = (branch_refspec, *pull_refspecs)

    def _is_shallow(self) -> bool:
        completed = self._run(("rev-parse", "--is-shallow-repository"))
        return (completed.stdout or b"").decode("utf-8", "replace").strip() == "true"

    # -- 對外契約 ---------------------------------------------------------

    def require(
        self,
        *,
        required: Sequence[str],
        ancestry: Sequence[tuple[int, str]] = (),
        default_branch: str,
    ) -> None:
        """確保本輪要讀的物件都在本機；缺就 fetch 一次（最多一次）。

        ``required``（default branch commit ＋ 要讀的 blob）缺一個就 fail closed。
        ``ancestry`` 是 ``(PR 編號, merge commit)``——merge commit 缺席本身是一項
        事實（見 :meth:`is_ancestor`），不是錯誤；但缺席的那幾個 PR 會把
        ``refs/pull/<n>/head`` 一併掛進同一次 fetch，讓本機真的握有那些 PR 的物件。
        """

        self._verify_origin()
        wanted_required = _validate_object_ids(required)
        pairs = tuple(
            (number, revision)
            for number, revision in ancestry
            if isinstance(number, int) and not isinstance(number, bool) and number > 0
        )
        if len(pairs) != len(tuple(ancestry)):
            raise GitMirrorError("pull request numbers must be positive integers")
        normalized_pairs = tuple(
            (number, _validate_object_ids((revision,))[0]) for number, revision in pairs
        )
        wanted_optional = tuple(
            oid
            for oid in dict.fromkeys(revision for _, revision in normalized_pairs)
            if oid not in set(wanted_required)
        )
        wanted = (*wanted_required, *wanted_optional)
        missing = self._missing(wanted)
        if missing:
            self._fetch(
                default_branch=default_branch,
                # 只替「merge commit 不在本機」的 PR 拉 head ref——已經握有 merge
                # commit 的 PR 不需要，多掛一條 refspec 只是徒增 fetch 失敗面。
                pull_numbers=tuple(
                    number
                    for number, revision in normalized_pairs
                    if revision in missing
                ),
            )
            missing = self._missing(wanted)
        unresolved = tuple(oid for oid in wanted_required if oid in missing)
        if unresolved:
            raise GitMirrorError(
                "git mirror is missing required objects after fetch: "
                + ", ".join(unresolved)
            )
        self._absent = missing

    def read_blobs(self, shas: Sequence[str]) -> dict[str, str]:
        """一次 ``git cat-file --batch`` 讀完整批 blob（取代逐檔 contents 請求）。"""

        order = _validate_object_ids(shas)
        if not order:
            return {}
        payload = ("\n".join(order) + "\n").encode("ascii")
        completed = self._run(("cat-file", "--batch"), stdin=payload)
        out = completed.stdout or b""
        result: dict[str, str] = {}
        offset = 0
        for oid in order:
            end = out.find(b"\n", offset)
            if end < 0:
                raise GitMirrorError("git cat-file --batch output is truncated")
            header = out[offset:end].decode("utf-8", "replace").split()
            offset = end + 1
            if len(header) == 2 and header[1] == "missing":
                raise GitMirrorError(f"git mirror blob is missing: {oid}")
            if len(header) != 3 or header[0].lower() != oid or header[1] != "blob":
                raise GitMirrorError("git cat-file --batch header is malformed")
            try:
                size = int(header[2])
            except ValueError as error:
                raise GitMirrorError(
                    "git cat-file --batch header is malformed"
                ) from error
            body = out[offset : offset + size]
            if len(body) != size:
                raise GitMirrorError("git cat-file --batch output is truncated")
            offset += size
            if out[offset : offset + 1] != b"\n":
                raise GitMirrorError("git cat-file --batch framing is malformed")
            offset += 1
            try:
                # 與舊 contents 路徑同語意：非 utf-8 是「內容無效」（ValueError），
                # 不是「鏡像不可用」。
                result[oid] = body.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(f"remote blob is not utf-8: {oid}") from error
            self._blob_reads += 1
        if offset != len(out):
            raise GitMirrorError("git cat-file --batch output has trailing data")
        return result

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """``git merge-base --is-ancestor``（取代逐 PR ``compare``）。"""

        (normalized_ancestor,) = _validate_object_ids((ancestor,))
        (normalized_descendant,) = _validate_object_ids((descendant,))
        self._ancestry_checks += 1
        if normalized_ancestor in self._absent:
            # descendant（default branch tip）已在本機且 repo 非 shallow 時，git 保證
            # 它的祖先全部在本機——物件不在本機就等價於「走不到」，也就是 REST
            # ``compare`` 回 ``behind``／``diverged`` 的那一格。shallow 破壞這個前提，
            # 此時無法判定，一律 fail closed。
            if self._is_shallow():
                raise GitMirrorError(
                    "shallow local checkout cannot decide merge ancestry"
                )
            return False
        completed = self._run(
            ("merge-base", "--is-ancestor", normalized_ancestor, normalized_descendant),
            ok=(0, 1),
        )
        return completed.returncode == 0

    @property
    def provenance(self) -> dict[str, object]:
        return {
            "transport": "git",
            "repo_root": str(self.repo_root),
            "remote": self.repo,
            "fetched_refs": list(self._fetched_refs),
            "absent_objects": sorted(self._absent),
            "blob_reads": self._blob_reads,
            "ancestry_checks": self._ancestry_checks,
        }


def unavailable_provenance(reason: str) -> Mapping[str, object]:
    """沒有動用鏡像（本輪沒有任何 remote 檔案／ancestry 要判）時的 provenance。"""

    return {
        "transport": "git",
        "repo_root": None,
        "remote": None,
        "fetched_refs": [],
        "absent_objects": [],
        "blob_reads": 0,
        "ancestry_checks": 0,
        "note": reason,
    }
