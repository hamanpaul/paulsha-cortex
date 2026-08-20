"""候選 git base 的曝光面（issue #731 (C)）。

## 這個模組要修的缺陷

run 的**候選 git base**是採信鏈的關鍵事實——Manager 的獨立 gate 重跑的是候選
自己那棵樹的測試套件，所以「這條 run 的基底停在哪個 commit」直接決定了「已經
merge 進 main 的修法進不進得來」。0819 深夜的現場逐字如下：

```
候選 worktree  git rev-parse HEAD        → 59a7a9b（0818）
mirror         refs/remotes/origin/main  → 7eb707b（落後 13 支 PR）
cortex status / work show 的任何欄位     → 看不到上面任何一個
```

也就是說：這個事實**只存在於檔案系統上**，operator 要看只能
`sudo git -C <候選 worktree> rev-parse HEAD`。而 run 上唯一顯眼的「版本」欄位
``source_revision`` 是 **64-hex 的 authority digest**（work item 來源材料的
sha256，見 `claim.semantic_source_revision`），**與 git base 無關**——它看起來
很像版本，於是 0819 那一晚它把診斷帶偏了兩次。命名因此是本模組的一部分：
新欄位叫 ``candidate_git_base``，字面就寫著「git base」，不與
``source_revision`` 共用任何詞彙。

## 權威來源：**不新造第二份**

候選基底這個事實已經存在，本模組只負責**把它接到曝光面**，一律不重算：

1. ``run.frozen_readiness["base_sha"]``——#211 pre-claim readiness 六道關卡通過
   後凍結的那一份（`claim_readiness.FrozenReadinessSet.base_sha`）。這是設計上
   的權威值，`manager._dispatch_workflow_card` 建首張 build 卡的工作區時就是拿
   它當 ``base_sha``。
2. 若 run 上沒有凍結集（實機 0820 逐字量測：29 個 run 的 ``frozen_readiness``
   **全為 null**——readiness transaction 尚未接進 production claim 路徑），則退回
   **該 run 第一張 build 卡的 ``job["dispatch_head"]``**。那正是 Manager 當時實際
   provision 工作區用的 base，而且後續每一張 build 卡都由
   `manager._dispatch_workflow_card` 以 ``builder_jobs[0]["dispatch_head"]``
   繼承同一個值——同一個欄位，不是另一份推導。

兩者都拿不到時回 ``sha=None`` ＋ 具名 reason，而不是靜默省略。

## 距離：唯讀，**絕不 fetch**

「落後 origin/main 幾個 commit」由 mirror（``PSC_REPO_ROOT``）上**現有的**
``refs/remotes/origin/main`` 算出：``git rev-list --count <base>..<ref>``。

- **status 路徑不得有副作用**：fetch 是 claim 的職責（`claim_readiness.
  base_sha_probe` 逐字「Fetch remote main once and freeze it」），呈現面跟著
  fetch 會讓「看一眼狀態」變成會改變狀態的動作。本模組因此永遠不 fetch，也不
  寫任何 git 物件；輸出上以 ``fetched: false`` ＋ ``measured_against`` 誠實標示
  比較基準是「mirror 上次 fetch 的 main」。
- 讀不到 mirror／算不出距離時 **fail-soft 但說得出口**：距離落
  ``<unresolved:…>``（沿用 `planning_runtime`／`planning_probe_cache` 既有的
  標記慣例），並附上具名 reason，不是靜默省略成 ``None``。

## 門檻與具名診斷

落後超過 :data:`CANDIDATE_BASE_STALE_THRESHOLD_COMMITS` 時給
:data:`CANDIDATE_BASE_STALE_REASON`——**機器可讀的 reason 碼**，不是塞進自由
文字的一句話（同 `diagnostics.DiagnosticReason` 的 ``reason`` 語意：operator 與
下游分支都 grep 得到）。門檻值只有這一處定義，`manager`／`manager_daemon`／
`monitor` 三個曝光面都讀它，不得各自寫死。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import verification

GitRunner = verification.GitRunner

#: 落後幾個 commit 起算「基底過舊」。
#:
#: 10 的來由：#731 現場逐字——0819 一天之內 main 前進 **13 支 PR**，而那條 run
#: 的基底停在前一天。門檻取「約一個工作天的 main 移動量」的保守下界：小於它的
#: 落後在長壽 run 上是常態（run 開跑後 main 本來就會前進），不該天天在 attention
#: 上叫；到達它就代表「已經有整整一輪的修法進不來」——#731 的 test-only 修復
#: 結構上救不了長壽 run，正是在這個量級上發生的。
#:
#: 可用 :data:`CANDIDATE_BASE_STALE_THRESHOLD_ENV` 覆寫（部署層調參，不必改碼）。
CANDIDATE_BASE_STALE_THRESHOLD_COMMITS = 10

#: 門檻的 env 覆寫鍵。沿用 `manager_daemon.PSC_MANAGER_RECENT_DONE_WINDOW_SECONDS`
#: 的形狀：常數是預設值，env 只是覆寫，未設或解析失敗一律退回常數。
CANDIDATE_BASE_STALE_THRESHOLD_ENV = "PSC_CANDIDATE_BASE_STALE_THRESHOLD_COMMITS"

#: 基底落後超過門檻。**具名診斷**——operator 一眼看出「這條 run 的基底過舊、
#: test-only 修復進不去」，下游也 grep 得到，不必解析自由文字。
CANDIDATE_BASE_STALE_REASON = "candidate-git-base-stale"

#: run 上完全找不到候選基底（既無凍結集，也還沒有任何 build 卡）。
#: 不是錯誤——define／plan 階段的 run 本來就還沒有基底——但必須說得出口，
#: 而不是讓欄位靜默消失。
CANDIDATE_BASE_ABSENT_REASON = "candidate-git-base-absent"

#: 有基底但距離算不出來（mirror 讀不到／base 不在 mirror 的 object store 裡）。
#: fail-soft：基底本身照常曝光，只有距離是 ``<unresolved:…>``。
CANDIDATE_BASE_DISTANCE_UNRESOLVED_REASON = "candidate-git-base-distance-unresolved"

#: ``sha_source`` 的兩個合法值——曝光面必須說得出「這個 SHA 是哪來的」。
CANDIDATE_BASE_SOURCE_FROZEN_READINESS = "frozen-readiness-base-sha"
CANDIDATE_BASE_SOURCE_BUILD_JOB = "first-build-job-dispatch-head"

#: 比較基準。刻意是 remote-tracking ref 而非 ``origin/main``：本模組不 fetch，
#: 讀的就是 mirror 上次 fetch 留下的那一格。
MIRROR_MAIN_REF = "refs/remotes/origin/main"

_UNRESOLVED_TEMPLATE = "<unresolved:{}>"

#: fail-soft 標記的分類名。刻意各自不同：「沒宣告 mirror」「mirror 的 main 讀不到」
#: 「base 不在 mirror 裡」是三件不同的事，塌縮成一個標記等於把診斷丟掉
#: （同 `planning_runtime` 對 `<absent>`／`<unresolved:X>` 的區分論證）。
UNRESOLVED_MIRROR_ROOT_UNSET = _UNRESOLVED_TEMPLATE.format("MirrorRootUnset")
UNRESOLVED_MIRROR_MAIN_UNREADABLE = _UNRESOLVED_TEMPLATE.format("MirrorMainUnreadable")
UNRESOLVED_BASE_NOT_IN_MIRROR = _UNRESOLVED_TEMPLATE.format("BaseNotInMirror")
UNRESOLVED_DISTANCE_UNPARSABLE = _UNRESOLVED_TEMPLATE.format("DistanceUnparsable")


def stale_threshold_commits(environ: Mapping[str, str] | None = None) -> int:
    """門檻值的單一讀取點。env 未設／非正整數一律退回常數。"""

    source = os.environ if environ is None else environ
    raw = source.get(CANDIDATE_BASE_STALE_THRESHOLD_ENV)
    if not raw:
        return CANDIDATE_BASE_STALE_THRESHOLD_COMMITS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return CANDIDATE_BASE_STALE_THRESHOLD_COMMITS
    if value <= 0:
        return CANDIDATE_BASE_STALE_THRESHOLD_COMMITS
    return value


def _normalized_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if verification.SAFE_SHA_RE.fullmatch(candidate) is None:
        return None
    return candidate


@dataclass(frozen=True)
class CandidateGitBase:
    """一條 run（或一張卡）的候選 git base 與它離 mirror 上 main 多遠。

    刻意**不叫** ``source_revision`` 家族的任何名字：``source_revision`` 是 work
    item 來源材料的 sha256（authority digest，64-hex），本欄位是 git commit SHA
    （40-hex）。#731 現場正是把兩者混為一談而誤判了兩次。
    """

    sha: str | None
    sha_source: str | None
    behind_origin_main: int | str | None
    mirror_origin_main: str | None
    threshold_commits: int
    reason: str | None

    @property
    def stale(self) -> bool:
        return self.reason == CANDIDATE_BASE_STALE_REASON

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "sha_source": self.sha_source,
            "behind_origin_main": self.behind_origin_main,
            "mirror_origin_main": self.mirror_origin_main,
            "threshold_commits": self.threshold_commits,
            "reason": self.reason,
            # 誠實標示比較基準：距離是相對「mirror 上次 fetch 到的 main」算的，
            # 不是相對 GitHub 此刻的 main。`fetched` 恆為 False——呈現面不得有
            # 網路／寫入副作用（有測試釘住），這個欄位就是那條約束的對外宣告。
            "measured_against": f"mirror:{MIRROR_MAIN_REF}",
            "fetched": False,
        }


class _Unset:
    """``None``（讀過但讀不到）與「還沒讀過」必須分得開，故需要第三個值。"""

    __slots__ = ()


_UNSET = _Unset()


class MirrorDistanceProbe:
    """對 mirror 的**唯讀**距離量測，量到的值在同一次快照內快取。

    只發兩種 git 子命令：``rev-parse``（讀 remote-tracking ref）與
    ``rev-list --count``（數 commit）。**沒有 fetch、沒有 worktree、沒有任何寫入**
    ——`cortex status` 是唯讀路徑，fetch 是 claim 的職責。

    快取的理由不是效能潔癖：一次 `cortex status` 會投影多筆 attention／in_flight
    條目，同一個 run 的多張卡共用同一個 base，逐條各發一次 git 等於把唯讀路徑的
    成本乘上條目數。
    """

    def __init__(
        self,
        *,
        mirror_root: str | Path | None,
        git_runner: GitRunner | None = None,
    ) -> None:
        self._mirror_root = None if mirror_root is None else str(mirror_root)
        self._git_runner = git_runner
        self._origin_main: str | None | object = _UNSET
        self._distances: dict[str, int | str] = {}

    @property
    def mirror_root(self) -> str | None:
        return self._mirror_root

    def origin_main(self) -> str | None:
        """mirror 上 ``refs/remotes/origin/main`` 現在指向哪裡（讀不到回 None）。"""

        if self._origin_main is _UNSET:
            self._origin_main = self._read_origin_main()
        return self._origin_main  # type: ignore[return-value]

    def _read_origin_main(self) -> str | None:
        if self._mirror_root is None:
            return None
        result = verification._run_git(
            ["-C", self._mirror_root, "rev-parse", MIRROR_MAIN_REF],
            self._git_runner,
        )
        if result["status"] != "ok":
            return None
        return _normalized_sha(result["stdout"])

    def behind(self, base_sha: str) -> int | str:
        """``base_sha`` 落後 mirror 上 main 幾個 commit；算不出回 ``<unresolved:…>``。"""

        cached = self._distances.get(base_sha)
        if cached is not None:
            return cached
        value = self._measure(base_sha)
        self._distances[base_sha] = value
        return value

    def _measure(self, base_sha: str) -> int | str:
        if self._mirror_root is None:
            return UNRESOLVED_MIRROR_ROOT_UNSET
        target = self.origin_main()
        if target is None:
            return UNRESOLVED_MIRROR_MAIN_UNREADABLE
        if target == base_sha:
            # 快路徑：基底就是 mirror 上的 main，不必再問 git 一次。
            return 0
        result = verification._run_git(
            ["-C", self._mirror_root, "rev-list", "--count", f"{base_sha}..{target}"],
            self._git_runner,
        )
        if result["status"] != "ok":
            # base 不在 mirror 的 object store 裡（例如基底來自一棵已被回收、
            # 從未 push 的樹）。這不是「距離是 0」，是「量不到」。
            return UNRESOLVED_BASE_NOT_IN_MIRROR
        raw = str(result["stdout"]).strip()
        try:
            return int(raw)
        except ValueError:
            return UNRESOLVED_DISTANCE_UNPARSABLE


def default_mirror_root() -> str | None:
    """mirror 根＝``PSC_REPO_ROOT``；未宣告回 ``None``（**不猜 cwd**）。

    刻意走 `config.paths.configured_repo_root()` 而不是 `repo_root()`：後者未宣告
    時會 fail-closed 拋例外（#612），而呈現面不該因為「這台機器沒宣告目標 repo」
    就整份 status 死掉——那條路徑的正確行為是 fail-soft 落
    ``<unresolved:MirrorRootUnset>``。
    """

    from ..config import paths

    try:
        configured = paths.configured_repo_root()
    except Exception:  # noqa: BLE001 - 呈現面不得因環境解析失敗而爆掉
        return None
    return None if configured is None else str(configured)


def frozen_base_sha(frozen_readiness: Mapping[str, Any] | None) -> str | None:
    """``frozen_readiness["base_sha"]`` 的**單一讀取點**（正規化 ＋ 40-hex 驗證）。

    #731 的 (A)（`work_actions._refreeze_base_action`，寫入端）與 (C)（本模組的
    曝光面，讀取端）都必須回答「這條 run 現在凍結在哪個 base」。那是**同一個
    事實**，因此只能有一支函式——本 repo 已經被「兩份表述」咬過很多次（#727 的
    第二份 `-o` 落點、#728 的兩份 `next_actions` 導出），不再重演。

    ``None`` 代表「run 沒有凍結集」，這在 production 是**常態**而非異常：
    `readiness_checker` 從未被接線，實機 0820 逐字量測 29 個 run 的
    ``frozen_readiness`` 全為 ``null``。兩側對這個 ``None`` 的後續處置**刻意不同**，
    因為問的是不同的問題：

    - (A) 問「下一張卡**會**用什麼基底」⇒ 退回來源樹本地 ``refs/heads/main``
      （`ScriptWorktreeCreator(base="main")` 實際會解析到的那一格）。
    - (C) 問「這條 run 的候選**已經**坐在哪個 commit 上」⇒ 退回第一張 build 卡
      記錄的 ``dispatch_head``，也就是當初 provisioning 時本地 ``main`` 解析出來
      的那個值的**落地紀錄**。0819 現場要診斷的正是這一格（59a7a9b）與 mirror
      main（7eb707b）的差距，而不是 status 讀取當下本地 main 恰好在哪。

    兩者不是矛盾，是同一條時間軸的前後兩點；(A) 重新凍結成功之後，凍結集就存在
    了，(C) 於是自動改讀 (1)、`sha_source` 變回 ``frozen-readiness-base-sha``
    ——有測試釘住這個接合。
    """

    if not isinstance(frozen_readiness, Mapping):
        return None
    return _normalized_sha(frozen_readiness.get("base_sha"))


def candidate_base_sha(
    *,
    frozen_readiness: Mapping[str, Any] | None,
    build_dispatch_heads: Sequence[str],
) -> tuple[str | None, str | None]:
    """取出候選基底與它的來源標記。**不重算任何東西**，只讀既有欄位。

    優先序見模組 docstring：凍結集 → 第一張 build 卡的 ``dispatch_head``。
    """

    frozen = frozen_base_sha(frozen_readiness)
    if frozen is not None:
        return frozen, CANDIDATE_BASE_SOURCE_FROZEN_READINESS
    for head in build_dispatch_heads:
        normalized = _normalized_sha(head)
        if normalized is not None:
            return normalized, CANDIDATE_BASE_SOURCE_BUILD_JOB
    return None, None


def resolve_candidate_git_base(
    *,
    frozen_readiness: Mapping[str, Any] | None,
    build_dispatch_heads: Sequence[str] = (),
    probe: MirrorDistanceProbe | None = None,
    threshold_commits: int | None = None,
) -> CandidateGitBase:
    """把「候選基底 ＋ 落後程度 ＋ 具名診斷」組成一份可投影的事實。"""

    threshold = (
        stale_threshold_commits() if threshold_commits is None else int(threshold_commits)
    )
    sha, sha_source = candidate_base_sha(
        frozen_readiness=frozen_readiness,
        build_dispatch_heads=build_dispatch_heads,
    )
    if sha is None:
        return CandidateGitBase(
            sha=None,
            sha_source=None,
            behind_origin_main=None,
            mirror_origin_main=None if probe is None else probe.origin_main(),
            threshold_commits=threshold,
            reason=CANDIDATE_BASE_ABSENT_REASON,
        )
    active = probe if probe is not None else MirrorDistanceProbe(mirror_root=default_mirror_root())
    behind = active.behind(sha)
    if isinstance(behind, int):
        reason = CANDIDATE_BASE_STALE_REASON if behind >= threshold else None
    else:
        reason = CANDIDATE_BASE_DISTANCE_UNRESOLVED_REASON
    return CandidateGitBase(
        sha=sha,
        sha_source=sha_source,
        behind_origin_main=behind,
        mirror_origin_main=active.origin_main(),
        threshold_commits=threshold,
        reason=reason,
    )


def build_dispatch_heads_from_jobs(
    jobs: Iterable[Mapping[str, Any]], *, run_id: str
) -> list[str]:
    """該 run 的 build 卡 ``dispatch_head``，依建立順序。

    與 `manager._dispatch_workflow_card` 讀的是**同一個欄位**：那裡對第二張起的
    build 卡取 ``builder_jobs[0]["dispatch_head"]`` 當 base，因此「第一張 build 卡
    的 dispatch_head」就是整條 run 的候選基底，不是另一份推導。

    排序鍵用 ``created_at``（ISO-8601，字典序即時間序）；缺欄位的 legacy job 落在
    最後，讓有時間戳的 job 先被採用。
    """

    rows: list[tuple[str, str]] = []
    for job in jobs:
        if not isinstance(job, Mapping):
            continue
        if job.get("workflow_run_id") != run_id:
            continue
        if job.get("workflow_phase") != "build":
            continue
        head = _normalized_sha(job.get("dispatch_head"))
        if head is None:
            continue
        created_at = job.get("created_at")
        rows.append((created_at if isinstance(created_at, str) else "￿", head))
    rows.sort(key=lambda item: item[0])
    return [head for _, head in rows]


def candidate_git_base_for_run(
    run: Any,
    registry: Any,
    *,
    probe: MirrorDistanceProbe | None = None,
    threshold_commits: int | None = None,
) -> CandidateGitBase:
    """`WorkflowRun` 物件版本的入口（`manager`／`manager_daemon` 用）。"""

    heads: list[str] = []
    list_jobs = getattr(registry, "list_jobs", None)
    if callable(list_jobs):
        try:
            heads = build_dispatch_heads_from_jobs(
                list_jobs(), run_id=str(getattr(run, "run_id", ""))
            )
        except Exception:  # noqa: BLE001 - 呈現面不得因曝光計算失敗而讓 status 死掉
            heads = []
    return resolve_candidate_git_base(
        frozen_readiness=getattr(run, "frozen_readiness", None),
        build_dispatch_heads=heads,
        probe=probe,
        threshold_commits=threshold_commits,
    )
