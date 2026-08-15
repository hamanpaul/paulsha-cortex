"""#506 / D2：git 的資料走 git——monitor 的 remote 讀取不再吃 REST 配額。

改動前，``GitHubTerminalProvider`` 一輪掃描對 GitHub REST 發出：

- 每個 remote ``todo.md`` / archived ``tasks.md`` 一次 ``contents``（實測 91 次／輪）
- 每個 workflow-linked merged PR 一次 ``compare``

兩者讀的都是本機 git 就有的東西。本檔的驗收樁把「一輪掃描的 REST contents/compare
呼叫數」釘死在 **0**，其餘測試釘住行為等價與 fail-closed 語意。

所有測試都用本機 tmp git repo（``git_origin`` fixture，見 ``tests/conftest.py``），
不打真實 GitHub API、不連網。
"""

from __future__ import annotations

import json
import subprocess

import pytest

from paulsha_cortex.monitor.git_mirror import GitMirrorError, LocalGitMirror
from paulsha_cortex.monitor.providers import GitHubTerminalProvider
from paulsha_cortex.monitor.work_api import _retain_last_good


class RecordingRunner:
    """依序回傳預備好的 ``gh`` 回應，並記下每一次 argv 供配額計數。"""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, timeout):
        self.calls.append(tuple(argv))
        if not self._payloads:
            raise AssertionError(f"unexpected extra gh call: {tuple(argv)}")
        payload = self._payloads.pop(0)
        return subprocess.CompletedProcess(
            args=tuple(argv),
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    @property
    def rest_contents_calls(self) -> int:
        return sum(1 for call in self.calls if any("/contents/" in arg for arg in call))

    @property
    def rest_compare_calls(self) -> int:
        return sum(1 for call in self.calls if any("/compare/" in arg for arg in call))


def _graph(*, default_revision: str, pulls=(), branch: str = "main") -> dict:
    return {
        "data": {
            "repository": {
                "defaultBranchRef": {
                    "name": branch,
                    "target": {"oid": default_revision},
                },
                "pullRequests": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": list(pulls),
                },
            }
        }
    }


def _merged_pull(number: int, merge_revision: str, head_revision: str) -> dict:
    return {
        "number": number,
        "body": "",
        "headRefName": f"feature/{number}-work",
        "headRefOid": head_revision,
        "state": "MERGED",
        "mergedAt": "2026-08-15T10:00:00Z",
        "mergeCommit": {"oid": merge_revision, "parents": {"totalCount": 2}},
        "closingIssuesReferences": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{"number": number, "state": "CLOSED"}],
        },
    }


def _tree(entries) -> dict:
    return {
        "truncated": False,
        "tree": [
            {"path": path, "type": "blob", "sha": sha} for path, sha in entries
        ],
    }


TODO_TEXT = "---\nwork_item: work\n---\n- [x] one\n- [x] two\n"
TODO_PATH = "docs/superpowers/workstreams/work/todo.md"


def test_remote_todo_content_comes_from_local_git_objects(git_origin):
    repo = git_origin()
    repo.commit({TODO_PATH: TODO_TEXT}, message="add todo")
    runner = RecordingRunner(
        [
            _graph(default_revision=repo.head()),
            _tree([(TODO_PATH, repo.blob_sha(TODO_PATH))]),
        ]
    )

    result = GitHubTerminalProvider(
        repo.repo, runner=runner, repo_root=repo.checkout
    ).scan()

    assert result.status == "ok"
    assert result.observations["remote_todos"] == [
        {
            "path": TODO_PATH,
            "revision": repo.blob_sha(TODO_PATH),
            "complete": True,
            "work_id": "work",
        }
    ]
    assert runner.rest_contents_calls == 0
    assert result.observations["remote_reads"]["transport"] == "git"
    assert result.observations["remote_reads"]["blob_reads"] == 1


def test_remote_archived_openspec_tasks_are_read_from_local_git(git_origin):
    repo = git_origin()
    tasks = "openspec/changes/archive/2026-08-15-canary/tasks.md"
    repo.commit({tasks: "- [x] task one\n- [x] task two\n"}, message="archive")
    runner = RecordingRunner(
        [
            _graph(default_revision=repo.head()),
            _tree([(tasks, repo.blob_sha(tasks))]),
        ]
    )

    result = GitHubTerminalProvider(
        repo.repo, runner=runner, repo_root=repo.checkout
    ).scan()

    assert result.status == "ok"
    assert result.observations["remote_todos"] == [
        {
            "path": tasks,
            "revision": repo.blob_sha(tasks),
            "complete": True,
            "openspec_ref": "canary",
        }
    ]
    assert runner.rest_contents_calls == 0


def test_merge_ancestry_uses_local_merge_base_not_compare(git_origin):
    repo = git_origin()
    repo.commit({"README.md": "# base\n"}, message="base")
    head = repo.branch_commit("feature/9-work", {"src.py": "x = 1\n"})
    merge = repo.merge("feature/9-work")
    runner = RecordingRunner(
        [
            _graph(
                default_revision=repo.head(),
                pulls=[_merged_pull(9, merge, head)],
            ),
            _tree([]),
        ]
    )

    result = GitHubTerminalProvider(
        repo.repo, runner=runner, repo_root=repo.checkout
    ).scan()

    assert result.status == "ok"
    assert result.observations["remote_prs"] == [
        {
            "source_id": f"github_pr:{repo.repo}#9",
            "candidate": head,
            "merge_revision": merge,
            "merged_with_merge_commit": True,
        }
    ]
    assert runner.rest_compare_calls == 0
    assert result.observations["remote_reads"]["ancestry_checks"] == 1


def test_merge_commit_off_default_branch_is_not_terminal(git_origin):
    """REST ``compare`` 回 ``diverged`` 的那一格，git 版必須答同一個 False。"""

    repo = git_origin()
    repo.commit({"README.md": "# base\n"}, message="base")
    head = repo.branch_commit("feature/9-work", {"src.py": "x = 1\n"})
    # merge commit 建在側枝上，main 完全走不到它。
    repo.git("checkout", "--quiet", "-b", "release", "main")
    repo.git("merge", "--quiet", "--no-ff", "-m", "side merge", "feature/9-work")
    merge = repo.head("release")
    repo.git("checkout", "--quiet", "main")
    repo.commit({"README.md": "# moved on\n"}, message="advance main")
    runner = RecordingRunner(
        [
            _graph(
                default_revision=repo.head("main"),
                pulls=[_merged_pull(9, merge, head)],
            ),
            _tree([]),
        ]
    )

    result = GitHubTerminalProvider(
        repo.repo, runner=runner, repo_root=repo.checkout
    ).scan()

    assert result.status == "ok"
    assert result.observations["remote_prs"][0]["merged_with_merge_commit"] is False
    assert runner.rest_compare_calls == 0


def test_scan_round_issues_zero_rest_contents_and_compare_calls(git_origin):
    """量化驗收：一輪掃描的 REST ``contents`` / ``compare`` 呼叫數 = 0。

    改動前這一輪會是 12 次 ``contents`` ＋ 3 次 ``compare`` = 15 次 REST（實測
    生產 workspace 一輪是 91 次 contents）；改動後同一輪的 REST 只剩 graphql
    與一次 git tree。
    """

    repo = git_origin()
    todo_paths = [
        f"docs/superpowers/workstreams/work-{index}/todo.md" for index in range(12)
    ]
    repo.commit(
        {
            path: f"---\nwork_item: work-{index}\n---\n- [x] done\n"
            for index, path in enumerate(todo_paths)
        },
        message="todos",
    )
    merges = []
    for number in (7, 8, 9):
        head = repo.branch_commit(f"feature/{number}-work", {f"src{number}.py": "x\n"})
        merges.append((number, repo.merge(f"feature/{number}-work"), head))
    runner = RecordingRunner(
        [
            _graph(
                default_revision=repo.head(),
                pulls=[
                    _merged_pull(number, merge, head)
                    for number, merge, head in merges
                ],
            ),
            _tree([(path, repo.blob_sha(path)) for path in todo_paths]),
        ]
    )

    result = GitHubTerminalProvider(
        repo.repo, runner=runner, repo_root=repo.checkout
    ).scan()

    assert result.status == "ok"
    assert len(result.observations["remote_todos"]) == 12
    assert all(
        row["merged_with_merge_commit"] for row in result.observations["remote_prs"]
    )
    assert runner.rest_contents_calls == 0
    assert runner.rest_compare_calls == 0
    # 一輪只剩 graphql（PR 分頁）＋ 1 次 git tree。
    assert len(runner.calls) == 2
    assert runner.calls[0][:3] == ("gh", "api", "graphql")
    assert "git/trees" in runner.calls[1][-1]


def test_stale_checkout_fetches_missing_revision_before_reading(git_origin):
    """本機落後時走 ``git fetch``（不吃 REST 配額），fetch 後再本機讀。"""

    repo = git_origin()
    repo.commit({TODO_PATH: TODO_TEXT}, message="add todo")
    repo.publish()
    empty = repo.detach()
    runner = RecordingRunner(
        [
            _graph(default_revision=repo.head()),
            _tree([(TODO_PATH, repo.blob_sha(TODO_PATH))]),
        ]
    )

    result = GitHubTerminalProvider(repo.repo, runner=runner, repo_root=empty).scan()

    assert result.status == "ok"
    assert result.observations["remote_todos"][0]["work_id"] == "work"
    assert result.observations["remote_reads"]["fetched_refs"] == [
        "+refs/heads/main:"
        + LocalGitMirror(empty, repo=repo.repo)._namespace
        + "/default"
    ]
    assert runner.rest_contents_calls == 0
    # fetch 不得動使用者的 remote-tracking refs。
    assert (
        subprocess.run(
            ("git", "-C", str(empty), "for-each-ref", "refs/remotes"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        == ""
    )


def test_pull_head_refspec_is_optional_when_origin_lacks_it(git_origin):
    """``refs/pull/<n>/head`` 是選配；remote 沒有它時仍要能判 ancestry。"""

    repo = git_origin()
    repo.commit({"README.md": "# base\n"}, message="base")
    head = repo.branch_commit("feature/9-work", {"src.py": "x = 1\n"})
    merge = repo.merge("feature/9-work")
    repo.publish()
    empty = repo.detach()
    runner = RecordingRunner(
        [
            _graph(
                default_revision=repo.head(),
                pulls=[_merged_pull(9, merge, head)],
            ),
            _tree([]),
        ]
    )

    result = GitHubTerminalProvider(repo.repo, runner=runner, repo_root=empty).scan()

    assert result.status == "ok"
    assert result.observations["remote_prs"][0]["merged_with_merge_commit"] is True
    assert runner.rest_compare_calls == 0


def test_pull_head_refspec_is_fetched_when_merge_commit_is_missing(git_origin):
    repo = git_origin()
    repo.commit({"README.md": "# base\n"}, message="base")
    head = repo.branch_commit("feature/9-work", {"src.py": "x = 1\n"})
    merge = repo.merge("feature/9-work")
    repo.publish()
    repo.publish_pull_head(9, head)
    empty = repo.detach()
    runner = RecordingRunner(
        [
            _graph(
                default_revision=repo.head(),
                pulls=[_merged_pull(9, merge, head)],
            ),
            _tree([]),
        ]
    )

    result = GitHubTerminalProvider(repo.repo, runner=runner, repo_root=empty).scan()

    assert result.status == "ok"
    namespace = LocalGitMirror(empty, repo=repo.repo)._namespace
    assert result.observations["remote_reads"]["fetched_refs"] == [
        f"+refs/heads/main:{namespace}/default",
        f"+refs/pull/9/head:{namespace}/pull/9",
    ]
    assert result.observations["remote_prs"][0]["merged_with_merge_commit"] is True


def test_unreadable_remote_blob_fails_closed_instead_of_absent(git_origin):
    """讀不到不得靜默當成「檔案不存在」——整支 provider degraded。"""

    repo = git_origin()
    repo.commit({TODO_PATH: TODO_TEXT}, message="add todo")
    repo.publish()
    runner = RecordingRunner(
        [
            _graph(default_revision=repo.head()),
            # tree 指向一個 origin 上根本不存在的 blob：fetch 成功，物件仍不在。
            _tree([(TODO_PATH, "0" * 40)]),
        ]
    )

    result = GitHubTerminalProvider(
        repo.repo, runner=runner, repo_root=repo.checkout
    ).scan()

    assert result.status == "degraded"
    assert result.observations == {}
    assert result.sources == ()
    assert len(result.diagnostics) == 1
    assert "git mirror" in result.diagnostics[0]
    assert "missing required objects" in result.diagnostics[0]


def test_missing_local_checkout_fails_closed(git_origin):
    repo = git_origin()
    repo.commit({TODO_PATH: TODO_TEXT}, message="add todo")
    runner = RecordingRunner(
        [
            _graph(default_revision=repo.head()),
            _tree([(TODO_PATH, repo.blob_sha(TODO_PATH))]),
        ]
    )

    result = GitHubTerminalProvider(repo.repo, runner=runner).scan()

    assert result.status == "degraded"
    assert result.diagnostics == (
        "github terminal git mirror unavailable: "
        "no local checkout configured for git-native remote reads",
    )


def test_checkout_tracking_another_repo_fails_closed(git_origin):
    repo = git_origin()
    repo.commit({TODO_PATH: TODO_TEXT}, message="add todo")
    runner = RecordingRunner(
        [
            _graph(default_revision=repo.head()),
            _tree([(TODO_PATH, repo.blob_sha(TODO_PATH))]),
        ]
    )

    result = GitHubTerminalProvider(
        "other/impostor", runner=runner, repo_root=repo.checkout
    ).scan()

    assert result.status == "degraded"
    assert "does not track other/impostor" in result.diagnostics[0]


def test_fetch_failure_is_degraded_and_previous_mirror_is_retained(git_origin):
    repo = git_origin()
    repo.commit({TODO_PATH: TODO_TEXT}, message="add todo")
    repo.publish()
    empty = repo.detach()
    # origin 宣告仍是 GitHub，但 transport 被改寫到一個不存在的路徑：fetch 必敗。
    subprocess.run(
        ("git", "-C", str(empty), "config", "--unset-all", f"url.{repo.origin}.insteadOf"),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(empty),
            "config",
            f"url.{repo.root / 'nonexistent.git'}.insteadOf",
            repo.url,
        ),
        check=True,
        capture_output=True,
    )
    runner = RecordingRunner(
        [
            _graph(default_revision=repo.head()),
            _tree([(TODO_PATH, repo.blob_sha(TODO_PATH))]),
        ]
    )

    result = GitHubTerminalProvider(repo.repo, runner=runner, repo_root=empty).scan()

    assert result.status == "degraded"
    assert "git mirror" in result.diagnostics[0]
    assert "rate limit" not in result.diagnostics[0]

    previous = GitHubTerminalProvider(
        repo.repo,
        runner=RecordingRunner(
            [
                _graph(default_revision=repo.head()),
                _tree([(TODO_PATH, repo.blob_sha(TODO_PATH))]),
            ]
        ),
        repo_root=repo.checkout,
    ).scan()
    retained = _retain_last_good(previous, result)

    assert retained.status == "degraded"
    assert retained.revision == previous.revision
    assert retained.sources == previous.sources


def test_shallow_checkout_cannot_decide_ancestry(git_origin):
    repo = git_origin()
    repo.commit({"README.md": "# base\n"}, message="base")
    head = repo.branch_commit("feature/9-work", {"src.py": "x = 1\n"})
    merge = repo.merge("feature/9-work")
    repo.commit({"README.md": "# moved on\n"}, message="advance")
    repo.publish()
    shallow = repo.root / "shallow"
    subprocess.run(
        (
            "git",
            "clone",
            "--quiet",
            "--depth=1",
            f"file://{repo.origin}",
            str(shallow),
        ),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "-C", str(shallow), "remote", "set-url", "origin", repo.url),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(shallow),
            "config",
            f"url.{repo.origin}.insteadOf",
            repo.url,
        ),
        check=True,
        capture_output=True,
    )
    runner = RecordingRunner(
        [
            _graph(
                default_revision=repo.head("main"),
                pulls=[_merged_pull(9, merge, head)],
            ),
            _tree([]),
        ]
    )

    result = GitHubTerminalProvider(
        repo.repo, runner=runner, repo_root=shallow
    ).scan()

    assert result.status == "degraded"
    assert "shallow" in result.diagnostics[0]


def test_mirror_rejects_unsafe_default_branch_names(git_origin):
    repo = git_origin()
    repo.commit({"README.md": "# base\n"}, message="base")
    mirror = LocalGitMirror(repo.checkout, repo=repo.repo)

    with pytest.raises(GitMirrorError, match="not a safe git ref"):
        mirror.require(
            required=("0" * 40,),
            default_branch="--upload-pack=touch /tmp/pwned",
        )


def test_mirror_rejects_malformed_object_ids(git_origin):
    repo = git_origin()
    repo.commit({"README.md": "# base\n"}, message="base")
    mirror = LocalGitMirror(repo.checkout, repo=repo.repo)

    with pytest.raises(GitMirrorError, match="object id is invalid"):
        mirror.require(required=("HEAD",), default_branch="main")


def test_mirror_reads_multiple_blobs_in_one_batch(git_origin):
    repo = git_origin()
    repo.commit({"a.md": "alpha\n", "b.md": "beta\n"}, message="two files")
    calls: list[tuple[str, ...]] = []

    class CountingRunner:
        def run(self, argv, *, timeout, stdin=None):
            calls.append(tuple(argv))
            return subprocess.run(
                list(argv), capture_output=True, input=b"" if stdin is None else stdin
            )

    mirror = LocalGitMirror(repo.checkout, repo=repo.repo, runner=CountingRunner())
    shas = (repo.blob_sha("a.md"), repo.blob_sha("b.md"))
    texts = mirror.read_blobs(shas)

    assert texts == {shas[0]: "alpha\n", shas[1]: "beta\n"}
    assert sum(1 for call in calls if "cat-file" in call) == 1
