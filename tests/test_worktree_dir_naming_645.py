"""issue #645：job 工作區目錄名 ＝ systemd 模板 instance 名（單一推導點）。

#645 的生產現場：`seams.ScriptWorktreeCreator.create()` 以 **branch slug**
（`feature/<slice_id>` → `feature-<slice_id>`）命名工作區，而模板 unit 的
`ReadWritePaths=<pool>/%i` 期望的是 `job_runner` 由 **job id** 算出的 instance 名。
兩條命名鏈各自導出、永遠差一個 `feature-` 前綴，於是 `ReadWritePaths` 指向不存在的
路徑，systemd 建 mount namespace 直接失敗（`226/NAMESPACE`）——降權派工從未經正式
路徑成功啟動過任何 job。

本檔的核心是**一條不變式**：兩個**真實**推導函式的輸出必須逐字相等。刻意不對常數
斷言——「兩邊各自算、剛好等於同一個字面量」正是 #645 復發的形狀。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import (
    autonomy,
    job_runner,
    job_workspace,
    worktree_reclaim,
)
from paulsha_cortex.coordinator.seams import ScriptWorktreeCreator
from paulsha_cortex.trust_root import permgen


#: 涵蓋幾種真實會出現的 slice/job id 形狀：純 slug、帶 issue 號、帶需要消毒的
#: 分隔字元（`/` 在目錄名裡必須被消掉，在 branch 名裡則是合法的一層）。
_JOB_IDS = (
    "dispatch1",
    "645-worktree-dir-naming",
    "645/sub-slice",
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


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


# ---------------------------------------------------------------------------
# 本票的全部價值：兩個真實推導函式的輸出必須相等
# ---------------------------------------------------------------------------

def test_provisioned_directory_name_equals_the_template_instance_name(
    tmp_path: Path,
) -> None:
    """`seams.create()` 產生的目錄名 == `job_runner` 算出的 instance 名。

    兩側都跑**真實**推導：左邊是真的在真 git repo 上 provision 出來的目錄，右邊是
    `prepare_systemd_template()` 內部逐字使用的 `template_instance_id()`。任何一側
    再度分岔（例如有人把目錄名改回 branch slug），這條就紅。
    """

    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"

    for job_id in _JOB_IDS:
        branch = autonomy._branch_for_slice(job_id)
        workspace = Path(
            ScriptWorktreeCreator(repo=repo, wt_root=pool, base="main").create(
                branch, job_id=job_id
            )
        )
        instance = job_runner.template_instance_id(job_id)

        assert workspace.name == instance, (
            f"job_id={job_id!r}：工作區目錄名 {workspace.name!r} 與模板 instance 名 "
            f"{instance!r} 不相等——模板 unit 的 ReadWritePaths=<pool>/%i 會指向不存在"
            "的路徑（#645 的 226/NAMESPACE）"
        )
        # unit 的 RWP 是 `<pool>/%i`：目錄的父層也必須就是 pool 本身。
        assert workspace.parent == pool
        assert workspace.is_dir()


def test_provisioning_never_produces_the_pre_645_branch_slug_directory(
    tmp_path: Path,
) -> None:
    """突變守衛：修法前的目錄名（branch slug）**不得**再出現在 pool 裡。

    沒有這一條，上面那條不變式有可能因為「兩個名字剛好都被改成 branch slug」而
    假通過——那正是 #645 的原狀。
    """

    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"
    job_id = "dispatch1"
    branch = autonomy._branch_for_slice(job_id)

    ScriptWorktreeCreator(repo=repo, wt_root=pool, base="main").create(
        branch, job_id=job_id
    )

    legacy = pool / job_workspace.legacy_branch_slug(branch)
    assert legacy.name == "feature-dispatch1", "legacy 形狀的定義本身漂移了"
    assert not legacy.exists()
    assert sorted(p.name for p in pool.iterdir()) == [
        job_runner.template_instance_id(job_id)
    ]


def test_prepare_systemd_template_agrees_with_provisioning(tmp_path: Path) -> None:
    """完整的 `prepare_systemd_template()` 與 provisioning 對齊（需 Phase 2b 部署）。

    上面那條不變式跑的是 `template_instance_id()`——它正是
    `prepare_systemd_template()` 內部唯一的 instance 來源，因此在任何機器上都測得到。
    這一條再往外包一層，連 preflight（帳號／unit 檔／shim／spool）一起跑，證明
    **正式派工路徑**上算出來的 instance 名也是同一個。

    這些前置物是 OS 層的（真的存在 `cortex-builder` 帳號、真的裝了模板 unit），
    單 UID 的開發機與 CI 都沒有。比照 #638 的教訓：**明確 skip 並說明理由**，
    不得靜默通過。
    """

    missing: list[str] = []
    account = job_runner.resolve_builder_account({})
    group = job_runner.resolve_builder_group({})
    template = job_runner.resolve_template_unit({})
    shim = job_runner.resolve_job_shim({})
    spool = job_runner.resolve_job_spec_spool({})
    if shutil.which("systemctl") is None:
        missing.append("PATH 上沒有 systemctl")
    if not job_runner._systemd_booted():
        missing.append("/run/systemd/system 不存在（本機未以 systemd 開機）")
    if not job_runner._account_exists(account):
        missing.append(f"builder 帳號 {account} 不存在")
    if not job_runner._group_exists(group):
        missing.append(f"builder group {group} 不存在")
    if not job_runner._unit_file_installed(template):
        missing.append(f"模板 unit {template} 未安裝")
    if not job_runner._is_executable(shim):
        missing.append(f"降權 shim {shim} 不存在或不可執行")
    if not Path(spool).is_dir():
        missing.append(f"job spec spool {spool} 不存在")
    if missing:
        pytest.skip(
            "本機沒有 trust-root Phase 2b 的降權前置物（"
            + "；".join(missing)
            + "）。#645 的不變式在 `template_instance_id()` 那條測試裡已被完整覆蓋，"
            "本條只多驗 preflight 這一層——刻意 skip 而非空過"
        )

    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"
    job_id = "dispatch1"
    workspace = Path(
        ScriptWorktreeCreator(repo=repo, wt_root=pool, base="main").create(
            autonomy._branch_for_slice(job_id), job_id=job_id
        )
    )
    plan = job_runner.prepare_systemd_template(
        {}, job_id=job_id, unit_active=lambda _binary, _unit: False
    )

    assert workspace.name == plan.instance
    assert plan.unit == f"cortex-job@{workspace.name}.service"


def test_slice_lane_passes_the_same_id_to_provisioning_and_to_launch(
    tmp_path: Path,
) -> None:
    """接線層：`autonomy._launcher_worktree()` 給 provisioning 的 id，就是
    `launcher.launch(slice_id=…)` 之後交給 `prepare_systemd_template(job_id=…)` 的
    那一個。

    不變式成立的前提是「兩邊拿到同一個 id」，這條就守在那個接縫上——只驗推導函式
    而不驗接線，一次傳錯參數就能讓 #645 原封不動地回來。
    """

    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"
    slice_id = "645-wiring"

    class _RecordingDispatcher:
        def __init__(self) -> None:
            self._worktree_creator = _RecordingCreator()

    class _RecordingCreator:
        def __init__(self) -> None:
            self.seen: list[tuple[str, str]] = []
            self._real = ScriptWorktreeCreator(repo=repo, wt_root=pool, base="main")

        def create(self, branch: str, *, job_id: str, base_sha: str | None = None) -> str:
            self.seen.append((branch, job_id))
            return self._real.create(branch, job_id=job_id, base_sha=base_sha)

    dispatcher = _RecordingDispatcher()
    worktree = autonomy._launcher_worktree(dispatcher, slice_id)

    assert dispatcher._worktree_creator.seen == [
        (autonomy._branch_for_slice(slice_id), slice_id)
    ]
    # `launcher.launch(slice_id=slice_id)` → `prepare_systemd_template(job_id=slice_id)`
    assert Path(worktree).name == job_runner.template_instance_id(slice_id)


# ---------------------------------------------------------------------------
# direct 模式零回歸：branch 名不變，成果回收面不受影響
# ---------------------------------------------------------------------------

def test_branch_naming_and_harvest_surface_are_unchanged(tmp_path: Path) -> None:
    """只有磁碟上的目錄名改，**branch 名一律不變**。

    direct 模式（與 `gc`／harvest）完全不看目錄名：它們讀來源 repo 的
    `refs/heads/<branch>`、讀工作區自己 checked-out 的 branch、以及 `log_path` 推導的
    spool key。這條把那三個面一次釘住。
    """

    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"
    slice_id = "645-direct-lane"
    branch = autonomy._branch_for_slice(slice_id)
    assert branch == "feature/645-direct-lane"

    workspace = Path(
        ScriptWorktreeCreator(repo=repo, wt_root=pool, base="main").create(
            branch, job_id=slice_id
        )
    )

    # 1. 來源 repo 的 branch 仍在原本的位置（dispatch baseline／gc 都直接讀它）
    assert _git(repo, "rev-parse", f"refs/heads/{branch}") == _git(repo, "rev-parse", "main")
    assert job_workspace.source_branch_head(repo, branch) is not None
    # 2. 工作區 checked-out 的仍是同一條 branch（gc 由此取 branch，不看目錄名）
    assert job_workspace.workspace_branch(workspace) == branch
    assert _git(workspace, "branch", "--show-current") == branch
    # 3. 標記檔記的 branch 不變
    marker = job_workspace.read_marker(workspace) or {}
    assert marker.get("branch") == branch
    # 4. spool key 仍由 log_path 推導，與目錄名無關（#637）
    job = {"log_path": f"/var/log/cortex/{slice_id}.jsonl"}
    assert job_workspace.spool_key_for_job(job) == slice_id


# ---------------------------------------------------------------------------
# 既有部署的殘留：兩種目錄形狀都要回收得掉，認不得的一律不刪
# ---------------------------------------------------------------------------

def test_reclaim_candidates_cover_both_directory_shapes() -> None:
    """反推候選必須同時涵蓋 #645 前後兩種形狀，且新形狀優先。"""

    pool = Path("/var/lib/cortex/worktree")
    candidates = job_workspace.reclaim_candidate_paths(
        pool, job_id="dispatch1", branch="feature/dispatch1"
    )

    assert candidates == [
        pool / job_workspace.job_segment("dispatch1"),
        pool / "feature-dispatch1",
    ]


def test_reclaim_collects_a_legacy_branch_slug_workspace(tmp_path: Path) -> None:
    """升級前 provision 的 `feature-<id>` 目錄仍必須被回收得掉。

    回收端若只認新形狀，磁碟上的舊殘留會被當成「不存在」而靜默略過，下一次
    provision 直接撞 `worktree target already exists`（#601 的生產現場）。
    """

    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"
    slice_id = "645-legacy"
    branch = autonomy._branch_for_slice(slice_id)
    provisioned = Path(
        ScriptWorktreeCreator(repo=repo, wt_root=pool, base="main").create(
            branch, job_id=slice_id
        )
    )
    # 搬成 #645 之前的形狀：per-job clone 是自足的，改目錄名即等價於舊部署的殘留。
    legacy = pool / job_workspace.legacy_branch_slug(branch)
    shutil.move(str(provisioned), str(legacy))
    assert job_workspace.is_job_clone(legacy)

    result = worktree_reclaim.reclaim_recorded_or_derived(
        recorded_path=None,
        pool_root=pool,
        job_id=slice_id,
        branch=branch,
        repo_root=repo,
        preserve_root=tmp_path / "evidence",
    )

    assert result is not None
    assert result.ok, result.detail
    assert result.status == worktree_reclaim.RECLAIM_RECLAIMED
    assert Path(result.path) == legacy
    assert not legacy.exists()


def test_reclaim_refuses_to_delete_an_unrecognised_directory(tmp_path: Path) -> None:
    """認不得的目錄一律 fail-closed——回收器不得變成「看到就刪」。

    pool 底下同名的目錄未必是 build 工作區（operator 的暫存、掛載點、別的專案）。
    #478 的安全閘在這條反推路徑上必須同樣生效。
    """

    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"
    slice_id = "645-not-a-workspace"
    branch = autonomy._branch_for_slice(slice_id)
    stranger = pool / job_workspace.legacy_branch_slug(branch)
    stranger.mkdir(parents=True)
    (stranger / "precious.json").write_text("{}", encoding="utf-8")

    result = worktree_reclaim.reclaim_recorded_or_derived(
        recorded_path=None,
        pool_root=pool,
        job_id=slice_id,
        branch=branch,
        repo_root=repo,
        preserve_root=tmp_path / "evidence",
    )

    assert result is not None
    assert not result.ok
    assert result.detail == "worktree-path-not-a-worktree"
    assert (stranger / "precious.json").is_file()


def test_reclaim_uses_the_recorded_path_verbatim_when_present(tmp_path: Path) -> None:
    """記錄有 `worktree` 時逐字回收那一條——與 #645 之前完全相同，不多掃 pool。"""

    repo = _source_repo(tmp_path)
    pool = tmp_path / "pool"
    slice_id = "645-recorded"
    branch = autonomy._branch_for_slice(slice_id)
    provisioned = Path(
        ScriptWorktreeCreator(repo=repo, wt_root=pool, base="main").create(
            branch, job_id=slice_id
        )
    )
    stranger = pool / job_workspace.legacy_branch_slug(branch)
    stranger.mkdir(parents=True)

    result = worktree_reclaim.reclaim_recorded_or_derived(
        recorded_path=provisioned,
        pool_root=pool,
        job_id=slice_id,
        branch=branch,
        repo_root=repo,
        preserve_root=tmp_path / "evidence",
    )

    assert result is not None and result.ok
    assert not provisioned.exists()
    # 反推不該被觸發：那個形狀不明的目錄一個位元組都不動。
    assert stranger.is_dir()


# ---------------------------------------------------------------------------
# 附帶：`CollectMode` 屬 [Unit]，放在 [Service] 會被 systemd 靜默忽略
# ---------------------------------------------------------------------------

def _section_of(content: str, key: str) -> str | None:
    section: str | None = None
    for raw in content.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif line.startswith(f"{key}="):
            return section
    return None


def test_collect_mode_is_declared_in_the_unit_section() -> None:
    """`CollectMode` 是 `[Unit]` 的鍵。

    放在 `[Service]` 時 systemd 只印
    `Unknown key name 'CollectMode' in section 'Service', ignoring.` 就繼續跑——
    語法不算錯，但「失敗的 instance 自動回收」這個用意整個沒生效。
    """

    unit = permgen.build_job_unit(permgen.DEFAULT_SCHEME, permgen.DEFAULT_LAYOUT)

    assert "CollectMode=inactive-or-failed" in unit.content
    assert _section_of(unit.content, "CollectMode") == "Unit"
    # 同段的 `Restart=` 仍屬 [Service]——這條確保上面不是整段被搬錯。
    assert _section_of(unit.content, "Restart") == "Service"
