"""#623：成果回收由「Manager 伸手進 builder 的 clone fetch」改為 **bundle ＋ append-only spool**。

`git -C <來源樹> fetch <builder 的 clone>` 在 trust-root Phase 2b 三分下結構性不成立
（operator 0817 實機驗證）：clone 是 builder-owned `0700`，Manager 進不去；而
`safe.directory` 實測不吃路徑 glob，per-job 路徑無法用一條設定涵蓋。

本檔覆蓋改法的五個面向，全部以**真 git repo**驗證（注入假 runner 只會驗到 stub 自己）：

1. base 錨點——provision 單一推導點，bundle 的 prerequisite 恆為來源樹已有的 commit
2. 產出與回收——builder 在自己的 clone 產 bundle，Manager 從那個**檔案** fetch
3. **不變式**——回收全程不存取 builder 的 clone（本次變更的全部價值）
4. fail-closed——bundle 缺席／不完整／帶錯 branch／非 fast-forward，訊息可操作
5. wrapper 與 `direct` 模式零回歸——段序、exit code、無 bundle 時逐字不變
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import job_workspace, launcher, manager, verification
from paulsha_cortex.coordinator.seams import ScriptWorktreeCreator

_BRANCH = "feature/623-bundle-harvest"
#: #645：工作區目錄名由 job id 導出（branch 名不變）。
_JOB_ID = "623-bundle-harvest"
_KEY = "job-623-0001"


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _try_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=False, capture_output=True, text=True
    )


def _source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repos" / "paulsha-cortex"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    _git(repo, "config", "user.email", "manager@example.invalid")
    _git(repo, "config", "user.name", "Cortex Manager")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _workspace(repo: Path, pool: Path) -> Path:
    return Path(
        ScriptWorktreeCreator(repo=repo, wt_root=pool, base="main").create(
            _BRANCH, job_id=_JOB_ID
        )
    )


def _builder_commit(workspace: Path, name: str = "builder.txt") -> str:
    (workspace / name).write_text("builder work\n", encoding="utf-8")
    _git(workspace, "add", name)
    _git(workspace, "commit", "-qm", f"builder: {name}")
    return _git(workspace, "rev-parse", "HEAD")


def _run_bundle_step(workspace: Path, bundle: Path) -> subprocess.CompletedProcess[str]:
    """以 **builder 身分**跑 wrapper 裡那一段真正的 bundle 命令。

    刻意不在測試裡自己組 `git bundle create`：要驗的就是 production wrapper 送出去
    的那一串字，另寫一份只會驗到測試自己。
    """

    return subprocess.run(
        ["bash", "-c", job_workspace.build_bundle_command(workspace=workspace, bundle=bundle)],
        cwd=str(workspace),
        check=False,
        capture_output=True,
        text=True,
    )


def _dispatch(tmp_path: Path, key: str = _KEY) -> Path:
    """Manager 在 dispatch 當下建立該 job 那一格，回傳 bundle 應該落地的路徑。"""

    return job_workspace.prepare_commit_spool(
        spool_key=key, coordinator_root=tmp_path / "coordinator"
    )


def _job(bundle: Path, *, branch: str = _BRANCH, workspace: Path | None = None) -> dict[str, object]:
    """一筆已 launch 的 job 記錄——spool key 由 `log_path` 的 stem 推導。"""

    return {
        "job_id": _KEY,
        "branch": branch,
        "worktree": str(workspace) if workspace is not None else "",
        "log_path": f"/logs/workflow/{bundle.parent.name}.jsonl",
    }


# ---------------------------------------------------------------------------
# 1. base 錨點
# ---------------------------------------------------------------------------

def test_provision_pins_the_bundle_base_inside_the_clone(tmp_path: Path) -> None:
    """`^<base>` 的推導只有一個點：provision 解出的 `exact_base`。

    bundle 要能被來源樹 fetch，它的 prerequisite 就必須是**來源樹已有**的 commit。
    `exact_base` 正是來源 repo 自己 `rev-parse --verify` 出來的，所以這條性質對每一
    條 lane 都成立——lane 之間唯一的差別是傳給 `create()` 的 `base_sha`，而那個值
    在同一個函式裡被同一個 `rev-parse --verify` 收斂。
    """

    repo = _source_repo(tmp_path)
    expected = _git(repo, "rev-parse", "main")

    workspace = _workspace(repo, tmp_path / "pool")

    assert _git(workspace, "rev-parse", job_workspace.BASE_REF) == expected
    # 與標記檔同源——兩邊都取自同一個 `exact_base`
    assert (job_workspace.read_marker(workspace) or {})["base"] == expected
    # 來源樹一定有它 → bundle 的 prerequisite 必然滿足
    assert _try_git(repo, "cat-file", "-e", f"{expected}^{{commit}}").returncode == 0
    # base pin 不得讓工作區一出生就是 dirty（dirty 是 verification／gc 的 fail-closed 條件）
    assert _git(workspace, "status", "--porcelain", "--untracked-files=all") == ""


def test_provision_pins_the_base_at_the_requested_base_sha(tmp_path: Path) -> None:
    """lane 指定 `base_sha` 時，pin 的是那一個 commit，不是 `main`。"""

    repo = _source_repo(tmp_path)
    pinned_at = _git(repo, "rev-parse", "main")
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "main advances")

    workspace = Path(
        ScriptWorktreeCreator(repo=repo, wt_root=tmp_path / "pool", base="main").create(
            _BRANCH, job_id=_JOB_ID, base_sha=pinned_at
        )
    )

    assert _git(workspace, "rev-parse", job_workspace.BASE_REF) == pinned_at


# ---------------------------------------------------------------------------
# 2. 產出與回收
# ---------------------------------------------------------------------------

def test_builder_bundle_round_trips_the_candidate_into_the_source_tree(
    tmp_path: Path,
) -> None:
    """bundle 產生 → 回收 → branch 落在來源樹且**內容**正確。"""

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    candidate = _builder_commit(workspace)

    assert _run_bundle_step(workspace, bundle).returncode == 0
    assert bundle.is_file()

    harvested = job_workspace.harvest_branch(
        source_repo=repo, bundle=bundle, branch=_BRANCH
    )

    assert harvested == candidate
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == candidate
    # object 也真的進來了——review 卡的 `worktree add --detach <candidate>` 靠這個
    assert _try_git(repo, "cat-file", "-e", f"{candidate}^{{commit}}").returncode == 0
    # 內容而不只是 ref：diff 必須逐字落地
    assert _git(repo, "show", f"{candidate}:builder.txt") == "builder work"


def test_bundle_carries_only_this_generation_not_the_whole_history(
    tmp_path: Path,
) -> None:
    """`^<base>` 真的收斂了範圍——bundle 只帶這一輪的 commit。"""

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    base = _git(workspace, "rev-parse", job_workspace.BASE_REF)
    candidate = _builder_commit(workspace)

    assert _run_bundle_step(workspace, bundle).returncode == 0

    heads = subprocess.run(
        ["git", "bundle", "list-heads", str(bundle)],
        check=True, capture_output=True, text=True,
    ).stdout
    assert heads.strip() == f"{candidate} refs/heads/{_BRANCH}"
    # base 是 prerequisite（bundle 不含它），不是 bundle 的一個 head
    assert base not in heads


def test_harvest_is_idempotent_across_repeated_ticks(tmp_path: Path) -> None:
    """同一份 bundle 被回收兩次（Manager 的 tick 可能重跑）不得失敗。"""

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    candidate = _builder_commit(workspace)
    _run_bundle_step(workspace, bundle)

    first = job_workspace.harvest_branch(source_repo=repo, bundle=bundle, branch=_BRANCH)
    job_workspace.seal_commit_spool(bundle)
    second = job_workspace.harvest_branch(source_repo=repo, bundle=bundle, branch=_BRANCH)

    assert first == second == candidate


# ---------------------------------------------------------------------------
# 3. 不變式：Manager 全程不碰 builder 的 clone
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受目錄權限限制，這條不變式驗不到")
def test_manager_never_touches_the_builder_clone_while_harvesting(
    tmp_path: Path,
) -> None:
    """**本次變更的全部價值**：回收路徑對 builder 的樹是零存取。

    以 `chmod 000` 重現 operator 實機看到的形狀（clone 為 builder-owned `0700`，
    Manager 連 `ls` 都 `Permission denied`）。舊做法在這裡必炸——它的第一個動作就是
    `git -C <來源樹> fetch <clone>`；新做法讀的是 spool 裡的一個**普通檔**，因此
    整條路徑（含 `manager._harvest_build_candidate`）照常完成。
    """

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    candidate = _builder_commit(workspace)
    assert _run_bundle_step(workspace, bundle).returncode == 0

    os.chmod(workspace, 0o000)
    try:
        # 前提成立：這棵樹現在真的進不去（等同實機的 Permission denied）
        assert _try_git(workspace, "status").returncode != 0
        assert not job_workspace.is_job_clone(workspace)

        harvested = job_workspace.harvest_branch(
            source_repo=repo, bundle=bundle, branch=_BRANCH
        )
        lane = manager._harvest_build_candidate(
            _job(bundle, workspace=workspace),
            run=SimpleNamespace(workspace_root=str(repo)),
            candidate=candidate,
            coordinator_root=tmp_path / "coordinator",
        )
    finally:
        os.chmod(workspace, 0o700)

    assert harvested == candidate
    assert lane == candidate
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == candidate


def test_manager_fetches_a_regular_file_never_a_repository(tmp_path: Path) -> None:
    """回收的來源必須是一個檔案。

    這是「Manager 不需要對 per-job 路徑設 `safe.directory`」那半個理由的機械形式：
    只要 fetch 對象是普通檔，dubious-ownership 與父鏈 traverse 都不會被觸發。
    """

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    _builder_commit(workspace)
    _run_bundle_step(workspace, bundle)

    assert bundle.is_file()
    assert not bundle.is_dir()
    assert not bundle.is_symlink()
    assert not (bundle / ".git").exists()


# ---------------------------------------------------------------------------
# 4. fail-closed
# ---------------------------------------------------------------------------

def test_harvest_fails_closed_when_the_bundle_is_absent(tmp_path: Path) -> None:
    """builder 沒有產出 bundle → 拒絕，且訊息說得出下一步。"""

    repo = _source_repo(tmp_path)
    bundle = _dispatch(tmp_path)

    with pytest.raises(job_workspace.WorkspaceError) as excinfo:
        job_workspace.harvest_branch(source_repo=repo, bundle=bundle, branch=_BRANCH)

    message = str(excinfo.value)
    assert "commit bundle missing" in message
    assert str(bundle) in message
    # 可操作：逐條列出成因，並指出逐字原因在哪裡看
    assert job_workspace.BASE_REF in message
    assert "log" in message


def test_harvest_fails_closed_on_an_incomplete_bundle(tmp_path: Path) -> None:
    """缺 base 的 bundle：`git fetch` 只吐一串裸 SHA，這裡必須包成可操作的說明。"""

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    _builder_commit(workspace, "first.txt")
    # base pin 被推到 builder 自己造的 commit 上 → bundle 的 prerequisite 來源樹沒有
    _git(workspace, "update-ref", job_workspace.BASE_REF, "HEAD")
    _builder_commit(workspace, "second.txt")
    assert _run_bundle_step(workspace, bundle).returncode == 0

    with pytest.raises(job_workspace.WorkspaceError) as excinfo:
        job_workspace.harvest_branch(source_repo=repo, bundle=bundle, branch=_BRANCH)

    message = str(excinfo.value)
    assert "commit bundle is incomplete" in message
    # git 的原文（裸 SHA 清單）保留，另外補上「該怎麼辦」
    assert "prerequisite" in message
    assert "重新 provision" in message
    # fail-closed：來源樹的 branch 沒有被動到
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == _git(repo, "rev-parse", "main")


def test_harvest_fails_closed_when_the_bundle_carries_another_branch(
    tmp_path: Path,
) -> None:
    """builder 換了 branch：bundle 帶的 ref 與 Manager 記錄的對不上 → 拒絕。"""

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    _git(workspace, "checkout", "-q", "-b", "feature/elsewhere")
    _builder_commit(workspace)
    assert _run_bundle_step(workspace, bundle).returncode == 0

    with pytest.raises(job_workspace.WorkspaceError, match="does not carry"):
        job_workspace.harvest_branch(source_repo=repo, bundle=bundle, branch=_BRANCH)


def test_harvest_rejects_a_rewritten_history_instead_of_absorbing_it(
    tmp_path: Path,
) -> None:
    """refspec 刻意不帶 `+`：Manager 不會靜默吸收被改寫過的歷史。"""

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    first = _builder_commit(workspace)
    _run_bundle_step(workspace, bundle)
    job_workspace.harvest_branch(source_repo=repo, bundle=bundle, branch=_BRANCH)

    _git(workspace, "reset", "-q", "--hard", "HEAD~1")
    _builder_commit(workspace, "rewritten.txt")
    rewritten = _dispatch(tmp_path, key="job-623-0002")
    _run_bundle_step(workspace, rewritten)

    with pytest.raises(job_workspace.WorkspaceError, match="not a fast-forward"):
        job_workspace.harvest_branch(source_repo=repo, bundle=rewritten, branch=_BRANCH)

    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == first


def test_harvest_refuses_a_symlinked_bundle(tmp_path: Path) -> None:
    """spool 那一格裡的 bundle 若是 symlink，回收路徑等於被外包出去。"""

    repo = _source_repo(tmp_path)
    bundle = _dispatch(tmp_path)
    elsewhere = tmp_path / "elsewhere.bundle"
    elsewhere.write_bytes(b"")
    bundle.symlink_to(elsewhere)

    with pytest.raises(job_workspace.WorkspaceError, match="is a symlink"):
        job_workspace.harvest_branch(source_repo=repo, bundle=bundle, branch=_BRANCH)


# ---------------------------------------------------------------------------
# 5. spool 的生命週期（pre-seed／seal）
# ---------------------------------------------------------------------------

def test_prepare_clears_a_preseeded_bundle_instead_of_inheriting_it(
    tmp_path: Path,
) -> None:
    """預埋一份 bundle 的人得到的是「自己的檔案被刪掉」，不是被繼承。"""

    bundle = _dispatch(tmp_path)
    bundle.write_bytes(b"pre-seeded")
    part = bundle.with_name(bundle.name + job_workspace.COMMIT_BUNDLE_PART_SUFFIX)
    part.write_bytes(b"half written")

    again = _dispatch(tmp_path)

    assert again == bundle
    assert not bundle.exists()
    assert not part.exists()


def test_prepare_reopens_a_sealed_spool_for_a_retry(tmp_path: Path) -> None:
    """同一個 key 被重派（retry-card／forced retry）時那一格必須重新可寫。"""

    bundle = _dispatch(tmp_path)
    bundle.write_bytes(b"harvested")
    job_workspace.seal_commit_spool(bundle)
    assert (bundle.parent.stat().st_mode & 0o777) == 0o500

    _dispatch(tmp_path)

    assert (bundle.parent.stat().st_mode & 0o777) == 0o700
    assert not bundle.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受目錄權限限制")
def test_seal_closes_the_spool_after_a_successful_harvest(tmp_path: Path) -> None:
    """append-only 的封口：落地後那一格再也建不了、刪不掉任何檔。

    封的是目錄不是檔案——bundle 由 builder 的 uid 建立，Manager `chmod` 不了它；
    但 Manager 是目錄的 owner，收掉目錄的 `w` 就足以讓那一格定版。
    """

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    _builder_commit(workspace)
    _run_bundle_step(workspace, bundle)

    job_workspace.harvest_if_spooled(
        source_repo=repo,
        job=_job(bundle),
        branch=_BRANCH,
        coordinator_root=tmp_path / "coordinator",
    )

    assert (bundle.parent.stat().st_mode & 0o777) == 0o500
    with pytest.raises(PermissionError):
        (bundle.parent / "smuggled").write_bytes(b"x")
    # 但證據還讀得到——bundle 就是 clone 被 rmtree 之後那些 commit 的 Manager-owned 副本
    assert bundle.read_bytes()[:4] == b"# v2"


def test_spool_key_rejects_a_path_traversing_identifier(tmp_path: Path) -> None:
    """key 會成為 Manager-owned 樹裡的一個目錄名，形狀守衛不得放寬。"""

    for bad in ("../escape", "a/b", "", ".hidden"):
        with pytest.raises(job_workspace.WorkspaceError, match="unsafe commit spool key"):
            job_workspace.commit_spool_dir(
                spool_key=bad, coordinator_root=tmp_path / "coordinator"
            )


def test_spool_key_is_derived_from_the_same_identifier_launch_used(
    tmp_path: Path,
) -> None:
    """launch 端與回收端**同一條**推導規則：`Path(log_path).stem`。

    canonical lane 的 launch key 是 job_id、slice lane 的是 slice_id；兩條 lane 若
    各自在回收端猜自己的 key，任何一邊改名都會退化成「找不到 spool → 靜默不回收」。
    """

    log_dir = tmp_path / "logs"
    for launch_key in ("job-abc123", "slice-2026-08-17-01"):
        log_path = str(log_dir / f"{launch_key}.jsonl")
        assert job_workspace.spool_key_for_job({"job_id": launch_key, "log_path": log_path}) == launch_key
    assert job_workspace.spool_key_for_job({}) is None
    assert job_workspace.spool_key_for_job({"job_id": "", "log_path": ""}) is None


def test_harvest_is_a_noop_for_a_job_without_a_spool_grant(tmp_path: Path) -> None:
    """零回歸掛點：升級前既存的工作區與測試裡的假 job 記錄完全不受影響。"""

    repo = _source_repo(tmp_path)
    coordinator_root = tmp_path / "coordinator"

    assert (
        job_workspace.harvest_if_spooled(
            source_repo=repo, job={}, branch=_BRANCH, coordinator_root=coordinator_root
        )
        is None
    )
    assert (
        job_workspace.harvest_if_spooled(
            source_repo=repo,
            job={"log_path": str(tmp_path / "logs" / "never-dispatched.jsonl")},
            branch=_BRANCH,
            coordinator_root=coordinator_root,
        )
        is None
    )


# ---------------------------------------------------------------------------
# 6. wrapper script（段序、exit code、`direct` 零回歸）
# ---------------------------------------------------------------------------

def _wrapper(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "inner_argv": ["true"],
        "sentinel": "/tmp/psc-nonexistent/s.exit",
        "ledger": "/tmp/psc-nonexistent/s.gates.json",
        "worktree": "/tmp/psc-nonexistent/wt",
        "repo_root": "/tmp/psc-nonexistent/repo",
        "run_gates": True,
    }
    kwargs.update(overrides)
    return launcher.build_wrapper_script(**kwargs)  # type: ignore[arg-type]


def test_wrapper_without_a_bundle_is_byte_identical_to_the_previous_shape() -> None:
    """`commit_bundle=None`（reviewer／planner）＝改動前逐字相同。"""

    assert _wrapper() == (
        "true; "
        'printf %s "$?" > /tmp/psc-nonexistent/s.exit; '
        "PYTHONPATH=/tmp/psc-nonexistent/repo python3 -m paulsha_cortex.coordinator.gate_ledger "
        "--out /tmp/psc-nonexistent/s.gates.json --worktree /tmp/psc-nonexistent/wt "
        ">/dev/null 2>&1"
    )


def test_wrapper_puts_the_bundle_before_the_sentinel() -> None:
    """sentinel 一出現，Manager 隨時可能開始回收——bundle 必須先落地。"""

    script = _wrapper(commit_bundle="/spool/k/commits.bundle")

    assert script.index("bundle create") < script.index("s.exit")
    # gate 排在 sentinel 之後（#261 的既有順序不變）
    assert script.index("s.exit") < script.index("gate_ledger")


def test_wrapper_preserves_the_model_exit_code_across_the_bundle_step(
    tmp_path: Path,
) -> None:
    """真的跑一次：bundle 段不得污染模型的 `$?`（降權模式下 unit 的 exit code 就是它）。"""

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    sentinel = log_dir / "job.exit"
    # 「模型」：做一次 commit 然後以 3 收場
    model = "git add -A && git commit -qm 'builder: model output' && exit 3"
    (workspace / "model.txt").write_text("from the model\n", encoding="utf-8")

    script = launcher.build_wrapper_script(
        inner_argv=["bash", "-c", model],
        sentinel=str(sentinel),
        ledger=str(log_dir / "job.gates.json"),
        worktree=str(workspace),
        repo_root=None,
        run_gates=False,
        commit_bundle=str(bundle),
    )
    proc = subprocess.run(
        ["bash", "-c", script], cwd=str(workspace), check=False, capture_output=True, text=True
    )

    assert proc.returncode == 3, proc.stderr
    assert sentinel.read_text(encoding="utf-8") == "3"
    # 失敗的模型也必須留下成果——採信與否由 Manager 判斷，不是由 exit code 決定搬不搬
    assert bundle.is_file()
    assert job_workspace.harvest_branch(
        source_repo=repo, bundle=bundle, branch=_BRANCH
    ) == _git(workspace, "rev-parse", "HEAD")


def test_degraded_wrapper_has_no_sentinel_but_still_bundles_and_exits_with_the_model_code(
    tmp_path: Path,
) -> None:
    """#604 的降權形狀：job 側不寫 sentinel／不跑 gate，但仍產 bundle。

    exit code 由 `exit "$rc"` 還原——降權模式下 Manager 側的記帳 shell 記的正是
    unit（＝這支 script）的 exit code。
    """

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    (workspace / "model.txt").write_text("from the model\n", encoding="utf-8")

    script = launcher.build_wrapper_script(
        inner_argv=["bash", "-c", "git add -A && git commit -qm 'builder: x' && exit 7"],
        sentinel="/tmp/psc-nonexistent/s.exit",
        ledger="/tmp/psc-nonexistent/s.gates.json",
        worktree=str(workspace),
        repo_root=str(repo),
        run_gates=False,
        write_sentinel=False,
        commit_bundle=str(bundle),
    )
    proc = subprocess.run(
        ["bash", "-c", script], cwd=str(workspace), check=False, capture_output=True, text=True
    )

    assert "s.exit" not in script
    assert "gate_ledger" not in script
    assert proc.returncode == 7, proc.stderr
    assert bundle.is_file()


def test_bundle_step_leaves_no_partial_file_when_it_fails(tmp_path: Path) -> None:
    """`.part` → `chmod` → `mv`：失敗時 spool 裡不得出現一個半成品 `commits.bundle`。"""

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    # 沒有任何新 commit → `git bundle create` 拒絕產生空 bundle
    proc = _run_bundle_step(workspace, bundle)

    assert proc.returncode != 0
    assert not bundle.exists()


def test_bundle_is_readable_by_the_manager_regardless_of_the_job_umask(
    tmp_path: Path,
) -> None:
    """降權 unit 常帶 `UMask=0077`；bundle 若落成 0600 builder-owned，Manager 就沒東西可讀。"""

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    _builder_commit(workspace)

    proc = subprocess.run(
        [
            "bash",
            "-c",
            "umask 0077; "
            + job_workspace.build_bundle_command(workspace=workspace, bundle=bundle),
        ],
        cwd=str(workspace), check=False, capture_output=True, text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert (bundle.stat().st_mode & 0o044) == 0o044


# ---------------------------------------------------------------------------
# 7. 兩條 lane 的掛點
# ---------------------------------------------------------------------------

def test_canonical_lane_harvests_the_accepted_candidate(tmp_path: Path) -> None:
    """掛在 `_verify_build_candidate_transition` **之後**：只搬運已被採信的 commit。"""

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    candidate = _builder_commit(workspace)
    _run_bundle_step(workspace, bundle)

    harvested = manager._harvest_build_candidate(
        _job(bundle, workspace=workspace),
        run=SimpleNamespace(workspace_root=str(repo)),
        candidate=candidate,
        coordinator_root=tmp_path / "coordinator",
    )

    assert harvested == candidate
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == candidate


def test_canonical_lane_fails_closed_when_the_harvested_head_is_not_the_candidate(
    tmp_path: Path,
) -> None:
    """回收後 branch 必須恰等於已採信的 candidate——這條守衛保留。"""

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    _builder_commit(workspace)
    _run_bundle_step(workspace, bundle)

    with pytest.raises(ValueError, match="harvest head mismatch"):
        manager._harvest_build_candidate(
            _job(bundle, workspace=workspace),
            run=SimpleNamespace(workspace_root=str(repo)),
            candidate="0" * 40,
            coordinator_root=tmp_path / "coordinator",
        )


def test_canonical_lane_accepts_a_card_that_produced_no_commit(tmp_path: Path) -> None:
    """沒有新 commit 就沒有 bundle（git 拒絕產空的）——candidate 早已在來源樹裡。"""

    repo = _source_repo(tmp_path)
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    baseline = _git(repo, "rev-parse", f"refs/heads/{_BRANCH}")

    harvested = manager._harvest_build_candidate(
        _job(bundle, workspace=workspace),
        run=SimpleNamespace(workspace_root=str(repo)),
        candidate=baseline,
        coordinator_root=tmp_path / "coordinator",
    )

    assert harvested == baseline
    # 但 branch 與 candidate 對不上時仍是 fail-closed，不得靜默放過
    with pytest.raises(job_workspace.WorkspaceError, match="commit bundle missing"):
        manager._harvest_build_candidate(
            _job(bundle, workspace=workspace),
            run=SimpleNamespace(workspace_root=str(repo)),
            candidate="0" * 40,
            coordinator_root=tmp_path / "coordinator",
        )


def test_canonical_lane_is_a_noop_for_a_job_without_a_spool_grant(
    tmp_path: Path,
) -> None:
    """既有部署零回歸：沒有 spool 那一格就完全不觸發回收。"""

    repo = _source_repo(tmp_path)
    run = SimpleNamespace(workspace_root=str(repo))

    assert (
        manager._harvest_build_candidate(
            {"worktree": str(repo), "branch": "main"},
            run=run, candidate="0" * 40, coordinator_root=tmp_path / "coordinator",
        )
        is None
    )
    assert (
        manager._harvest_build_candidate(
            {"branch": "", "log_path": "/logs/x.jsonl"},
            run=run, candidate="0" * 40, coordinator_root=tmp_path / "coordinator",
        )
        is None
    )


def test_slice_lane_verification_harvests_from_the_bundle_before_reading_the_branch(
    tmp_path: Path,
) -> None:
    """`verification` 以來源 repo 為根判讀；少了回收整段會以 `candidate-unreadable` 收場。"""

    repo = _source_repo(tmp_path)
    base = _git(repo, "rev-parse", "main")
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)
    candidate = _builder_commit(workspace)
    _run_bundle_step(workspace, bundle)

    result = verification.run_result_verification(
        slice_row={
            "slice_id": "bundle-623",
            "dispatch_base": base,
            "verification": {
                "contract": {
                    "docs_class": "trivial",
                    "review_policy": "not-required",
                    "required_artifacts": [],
                    "checks": [],
                    "tests": [],
                    "full_suite": {
                        "argv": ["true"], "cwd": ".", "timeout_seconds": 5,
                        "baseline": "no-regression",
                    },
                }
            },
        },
        job={
            "job_id": _KEY,
            "task": "bundle-623",
            "branch": _BRANCH,
            "worktree": str(workspace),
            "log_path": str(tmp_path / "logs" / f"{_KEY}.jsonl"),
        },
        repo_root=repo,
        coordinator_root=tmp_path / "coordinator",
    )

    assert result["payload"]["candidate"] == candidate
    assert _git(repo, "rev-parse", f"refs/heads/{_BRANCH}") == candidate


def test_slice_lane_reports_candidate_harvest_failed_with_an_actionable_detail(
    tmp_path: Path,
) -> None:
    """bundle 缺席時不得靜默略過——needs_human ＋ 逐字可操作的原因。"""

    repo = _source_repo(tmp_path)
    base = _git(repo, "rev-parse", "main")
    workspace = _workspace(repo, tmp_path / "pool")
    bundle = _dispatch(tmp_path)

    result = verification.run_result_verification(
        slice_row={
            "slice_id": "bundle-623",
            "dispatch_base": base,
            "verification": {
                "contract": {
                    "docs_class": "trivial",
                    "review_policy": "not-required",
                    "required_artifacts": [],
                    "checks": [],
                    "tests": [],
                    "full_suite": {
                        "argv": ["true"], "cwd": ".", "timeout_seconds": 5,
                        "baseline": "no-regression",
                    },
                }
            },
        },
        job={
            "job_id": _KEY,
            "task": "bundle-623",
            "branch": _BRANCH,
            "worktree": str(workspace),
            "log_path": str(tmp_path / "logs" / f"{bundle.parent.name}.jsonl"),
        },
        repo_root=repo,
        coordinator_root=tmp_path / "coordinator",
    )

    payload = result["payload"]
    assert payload["status"] == "needs_human"
    assert payload["summary"] == "candidate-harvest-failed"
    assert "commit bundle missing" in payload["details"]["candidate_harvest_error"]
