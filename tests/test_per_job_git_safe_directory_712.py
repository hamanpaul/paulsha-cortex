"""#712：per-job clone 跨 owner，git 的 dubious-ownership 擋死 builder。

`#710`／PR #711 把工作區 ACL 補上之後（實機 `getfacl` 逐字確認
`user:cortex-builder:rwx`／`mask::rwx`），builder job **真的跑起來了**，然後死在 git
自己那一層::

    fatal: detected dubious ownership in repository at '/var/lib/cortex/worktree/wf-…'
    fatal: Need a repository to create a bundle.

本檔釘住四件事：

1. **一條規則**——三個降權 principal 的 git 工作區信任由
   `registry.JOB_GIT_WORKSPACE_TRUST` 導出，且該表與 `JOB_WORKSPACE_REACH` 對「誰建
   那一格」的宣告**必須一致**（git 的判準只有 owner）。兩條 import 期斷言各驗一半。
2. **只放行一個鍵**——`GIT_CONFIG_*` 是與 `git -c` 同級的 command scope，`alias.*`／
   `core.fsmonitor` 經它塞進來**會執行外部命令**（本檔用真的 git 驗給你看）。寫端與
   讀端共用同一支守衛。
3. **放行是 per-job**——值由 Manager 算、綁死在 spec 的 `working_directory` 上。
4. **反向不變式**——用真的 git、真的 repo 驗：自己的工作區成功、**別的 job 的工作區
   失敗**。

## 為什麼這裡驗得到跨 owner 的語意（單 UID 的 CI 也能跑）

git 自己提供 `GIT_TEST_ASSUME_DIFFERENT_OWNER=1`（`setup.c:ensure_valid_ownership()`
的第一個條件），它讓 ownership 檢查**無條件視為失敗**，於是後續的 `safe.directory`
判定路徑與真正跨 owner 時**逐字相同**。0819 實測（git 2.43.0）三臂對照與 root-owned
repo 的實跑結果一致（見 `test_a_real_repo_...` 系列的 docstring）。

真正需要第二個 UID 的那一維（OS 層的 `User=`／ACL）走實機探針
`python3 -m paulsha_cortex.trust_root git-trust-probe`，並在本檔尾端留一條具名 skip。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import job_runner, job_shim
from paulsha_cortex.trust_root import permgen, registry
from paulsha_cortex.trust_root.__main__ import main

_JOB_PATH_ENV = {
    "PSC_BUILDER_PATH": "/opt/cortex/toolchain/bin:/usr/bin:/bin",
    "PSC_REVIEWER_PATH": "/opt/cortex/toolchain/bin:/usr/bin:/bin",
    "PSC_GATE_PATH": "/opt/cortex/toolchain/bin:/usr/bin:/bin",
}

_GIT_ENV_KEYS = ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")


def _build_env(role: str, workspace: str | None) -> dict[str, str]:
    return job_runner.build_job_env(
        manager_env=dict(_JOB_PATH_ENV),
        job_id="job-712",
        slice_id="slice-712",
        repo_root="/opt/cortex",
        workspace=workspace,
        role=role,
    )


# ---------------------------------------------------------------------------
# 1. 一條規則：三個 principal 一格都不能少，且與 #710 那張表一致
# ---------------------------------------------------------------------------


def test_every_downgraded_principal_has_exactly_one_git_trust_row() -> None:
    declared = [t.principal for t in registry.JOB_GIT_WORKSPACE_TRUST]
    assert declared == list(registry.DOWNGRADED_JOB_PRINCIPALS)
    assert len(declared) == len(set(declared))


def test_the_three_shapes_are_the_verified_ones_not_a_uniform_guess() -> None:
    """#712 逐字要求「三者形狀不同，不要假設一致」——查證結果就是這三格。"""

    shapes = {
        t.principal.value: t.trust for t in registry.JOB_GIT_WORKSPACE_TRUST
    }
    assert shapes == {
        # per-job clone（Manager 建）⇒ 跨 owner ⇒ 要
        "builder": registry.GitWorkspaceTrust.PER_JOB_ENV,
        # review worktree（Manager 建的 linked worktree）⇒ 同樣跨 owner ⇒ 也要
        "reviewer": registry.GitWorkspaceTrust.PER_JOB_ENV,
        # 副本由 gate 自己 copytree ⇒ owner 就是自己 ⇒ 不要
        "gate": registry.GitWorkspaceTrust.OWNED_BY_JOB,
    }


def test_the_rule_is_derived_from_who_creates_the_slot() -> None:
    """`OWNED_BY_JOB` ⟺ `POOL_OWNED_BY_JOB`——不是巧合，是同一個事實的兩個後果。"""

    for trust in registry.JOB_GIT_WORKSPACE_TRUST:
        reach = registry.job_workspace_reach_for(trust.principal)
        owned_by_job = reach.reach is registry.WorkspaceReach.POOL_OWNED_BY_JOB
        assert owned_by_job == (trust.trust is registry.GitWorkspaceTrust.OWNED_BY_JOB)


def test_job_runner_holds_the_paired_contract() -> None:
    """`job_runner` 刻意不 import `trust_root`；兩邊逐列相等由本條釘住。"""

    role_by_principal = {
        "builder": job_runner.JOB_ROLE_BUILDER,
        "reviewer": job_runner.JOB_ROLE_REVIEW,
        "gate": job_runner.JOB_ROLE_GATE,
    }
    for trust in registry.JOB_GIT_WORKSPACE_TRUST:
        role = role_by_principal[trust.principal.value]
        assert job_runner.JOB_ROLE_CONFIG[role].git_workspace_trust == trust.trust.value
    assert registry.GIT_SAFE_DIRECTORY_KEY in job_runner.ALLOWED_GIT_CONFIG_KEYS
    assert job_runner.ALLOWED_GIT_CONFIG_KEYS == {registry.GIT_SAFE_DIRECTORY_KEY}


def test_lookup_fails_closed_for_an_unregistered_principal() -> None:
    with pytest.raises(KeyError):
        registry.job_git_workspace_trust_for(registry.Principal.MANAGER)


# ---------------------------------------------------------------------------
# 2. import 期斷言：把「只修一格」變成結構上做不到
# ---------------------------------------------------------------------------


def _run_registry_assertion(rows) -> None:
    original = registry.JOB_GIT_WORKSPACE_TRUST
    registry.JOB_GIT_WORKSPACE_TRUST = rows
    try:
        registry._assert_every_downgraded_principal_has_a_git_workspace_trust()
    finally:
        registry.JOB_GIT_WORKSPACE_TRUST = original


def test_dropping_a_principal_makes_the_module_unloadable() -> None:
    rows = tuple(
        t for t in registry.JOB_GIT_WORKSPACE_TRUST
        if t.principal is not registry.Principal.REVIEWER
    )
    with pytest.raises(ValueError) as excinfo:
        _run_registry_assertion(rows)
    assert "沒有 git 工作區信任宣告" in str(excinfo.value)


def test_contradicting_the_workspace_reach_table_is_refused() -> None:
    """builder 若被宣告成「自己擁有」——那與 #710 的表直接矛盾，必須炸。"""

    rows = tuple(
        replace(t, trust=registry.GitWorkspaceTrust.OWNED_BY_JOB)
        if t.principal is registry.Principal.BUILDER
        else t
        for t in registry.JOB_GIT_WORKSPACE_TRUST
    )
    with pytest.raises(ValueError) as excinfo:
        _run_registry_assertion(rows)
    assert "互相矛盾" in str(excinfo.value)


def test_gate_declared_as_needing_env_is_also_refused() -> None:
    """反方向同樣要擋：gate 若被「順手也給一份」，那份放行沒有出處。"""

    rows = tuple(
        replace(t, trust=registry.GitWorkspaceTrust.PER_JOB_ENV)
        if t.principal is registry.Principal.GATE
        else t
        for t in registry.JOB_GIT_WORKSPACE_TRUST
    )
    with pytest.raises(ValueError) as excinfo:
        _run_registry_assertion(rows)
    assert "互相矛盾" in str(excinfo.value)


def test_an_empty_note_is_refused() -> None:
    """「這一格要不要放行」是查證結果，不是預設值。"""

    rows = tuple(
        replace(t, note="") if t.principal is registry.Principal.GATE else t
        for t in registry.JOB_GIT_WORKSPACE_TRUST
    )
    with pytest.raises(ValueError) as excinfo:
        _run_registry_assertion(rows)
    assert "note 為空" in str(excinfo.value)


def _run_permgen_assertion(layout) -> None:
    original = permgen._GIT_TRUST_PROBE_LAYOUT
    permgen._GIT_TRUST_PROBE_LAYOUT = layout
    try:
        permgen._assert_git_workspace_trust_matches_the_gitconfig()
    finally:
        permgen._GIT_TRUST_PROBE_LAYOUT = original


def test_the_current_plan_passes_the_permgen_assertion() -> None:
    permgen._assert_git_workspace_trust_matches_the_gitconfig()


def test_a_wildcard_in_the_static_gitconfig_makes_permgen_unloadable() -> None:
    """本票**最可能的壞修法**：「加個 `*` 讓它過」。那是 opt-out，不是授權。"""

    layout = replace(permgen._GIT_TRUST_PROBE_LAYOUT, source_repo_slugs=("*",))
    with pytest.raises(ValueError) as excinfo:
        _run_permgen_assertion(layout)
    assert "萬用字元" in str(excinfo.value)
    assert "opt-out" in str(excinfo.value)


def test_putting_a_workspace_pool_into_the_static_gitconfig_is_refused() -> None:
    """第二個壞修法：把 pool 根塞進靜態檔。

    對 per-job 那一格沒有效果（git 只認逐字相等的路徑），而真的生效的那部分是
    **整個 pool** 的放行——逐 job 語意當場歸零。
    """

    # 讓 worktree pool 恰好落在 `<repos>/<slug>`——於是靜態檔那一條與 pool 根逐字相同。
    probe = permgen._GIT_TRUST_PROBE_LAYOUT
    slug = probe.source_repo_slugs[0]
    layout = replace(probe, worktree_root=f"{probe.repo_source_root}/{slug}")
    with pytest.raises(ValueError) as excinfo:
        _run_permgen_assertion(layout)
    assert "工作區" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3. env 的形狀：值由 Manager 算，job 改不了自己的 spec
# ---------------------------------------------------------------------------


def test_builder_and_reviewer_get_exactly_one_safe_directory(tmp_path: Path) -> None:
    for role in (job_runner.JOB_ROLE_BUILDER, job_runner.JOB_ROLE_REVIEW):
        env = _build_env(role, str(tmp_path))
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
        assert env["GIT_CONFIG_VALUE_0"] == str(tmp_path.resolve())
        assert [k for k in env if k.startswith("GIT_CONFIG")] == sorted(_GIT_ENV_KEYS)


def test_gate_gets_nothing_at_all(tmp_path: Path) -> None:
    """「執行期零動作」必須在輸出上看得出來——多給一份就是一個沒有出處的放行。"""

    env = _build_env(job_runner.JOB_ROLE_GATE, str(tmp_path))
    assert not [k for k in env if k.startswith("GIT_CONFIG")]


def test_a_symlinked_workspace_is_resolved_to_its_physical_path(tmp_path: Path) -> None:
    """git 比對的是 `getcwd()` 之後的 **physical path**（0819 實測）。

    工作區經 symlink 進入時，`safe.directory=<symlink 路徑>` **仍被拒**——因此值一律
    解析。這條的實跑證據在 `test_a_real_repo_behind_a_symlink...`。
    """

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    env = _build_env(job_runner.JOB_ROLE_BUILDER, str(link))
    assert env["GIT_CONFIG_VALUE_0"] == str(real.resolve())


def test_a_caller_without_a_workspace_gets_nothing(tmp_path: Path) -> None:
    """preflight（`launcher.executor_environment()`）沒有工作區可言。

    這**不是** fail-open：真實派工那一支的 `workspace=` 是必填具名參數，而
    `build_job_spec()` 另外斷言「env 放行的那一格＝spec 的 `working_directory`」。
    """

    env = _build_env(job_runner.JOB_ROLE_BUILDER, None)
    assert not [k for k in env if k.startswith("GIT_CONFIG")]


def test_the_manager_environment_can_never_supply_these(tmp_path: Path) -> None:
    """值由 Manager 端的**產生器**算，不是從 Manager 的 environ 轉發過來的。"""

    poisoned = {
        **_JOB_PATH_ENV,
        "GIT_CONFIG_COUNT": "9",
        "GIT_CONFIG_KEY_0": "alias.pwn",
        "GIT_CONFIG_VALUE_0": "/evil",
    }
    env = job_runner.build_job_env(
        manager_env=poisoned,
        job_id="j",
        slice_id="s",
        repo_root="/opt/cortex",
        workspace=str(tmp_path),
        role=job_runner.JOB_ROLE_BUILDER,
    )
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == str(tmp_path.resolve())


# ---------------------------------------------------------------------------
# 4. 只放行 `safe.directory` 一個鍵——寫端與讀端同一支守衛
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["alias.pwn", "core.fsmonitor", "core.pager", "include.path", "Safe.Directory.x"],
)
def test_any_other_git_config_key_is_refused(key: str) -> None:
    env = {
        **_JOB_PATH_ENV,
        "PATH": "/usr/bin",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": key,
        "GIT_CONFIG_VALUE_0": "/tmp",
    }
    with pytest.raises(job_runner.JobRunnerError) as excinfo:
        job_runner.reject_unsafe_env(env, source="test")
    assert excinfo.value.diagnostic.reason == "job-runner-git-config-key-not-allowed"


def test_the_key_whitelist_is_case_insensitive() -> None:
    """git 的 config 鍵不區分大小寫；白名單若區分就等於留一個繞法。"""

    env = {
        "PATH": "/usr/bin",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "Safe.Directory",
        "GIT_CONFIG_VALUE_0": "/tmp",
    }
    job_runner.reject_unsafe_env(env, source="test")  # 不得 raise


@pytest.mark.parametrize(
    "name",
    [
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
    ],
)
def test_the_other_doors_into_git_config_are_denied(name: str) -> None:
    """同一扇門的另外五個把手。

    `GIT_CONFIG_GLOBAL` 會讓 root-owned 的 `$HOME/.gitconfig` 整份失效，
    `GIT_CONFIG_PARAMETERS` 是 `git -c` 的序列化管道、**不受單鍵白名單約束**。
    """

    assert name in job_runner.DENIED_ENV_NAMES
    with pytest.raises(job_runner.JobRunnerError) as excinfo:
        job_runner.reject_unsafe_env({"PATH": "/usr/bin", name: "x"}, source="test")
    assert excinfo.value.diagnostic.reason == "job-runner-credential-env-leak"


def test_an_unknown_git_config_variant_is_refused() -> None:
    with pytest.raises(job_runner.JobRunnerError) as excinfo:
        job_runner.reject_unsafe_env(
            {"PATH": "/usr/bin", "GIT_CONFIG_KEY_X": "safe.directory"}, source="test"
        )
    assert excinfo.value.diagnostic.reason == "job-runner-git-config-env-invalid"


def test_count_and_pairs_must_agree() -> None:
    """git 對缺項會整支 `fatal: unable to parse command-line config`（實測）。"""

    env = {
        "PATH": "/usr/bin",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "/tmp",
    }
    with pytest.raises(job_runner.JobRunnerError) as excinfo:
        job_runner.reject_unsafe_env(env, source="test")
    assert excinfo.value.diagnostic.reason == "job-runner-git-config-env-invalid"


def test_pairs_without_a_count_are_refused() -> None:
    """沒有 `GIT_CONFIG_COUNT` 時 git 完全忽略它們——放行看起來像生效了。"""

    env = {
        "PATH": "/usr/bin",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "/tmp",
    }
    with pytest.raises(job_runner.JobRunnerError) as excinfo:
        job_runner.reject_unsafe_env(env, source="test")
    assert excinfo.value.diagnostic.reason == "job-runner-git-config-env-invalid"


@pytest.mark.parametrize("value", ["*", "relative/path", "", "~/wt"])
def test_a_wildcard_or_relative_value_is_refused(value: str) -> None:
    env = {
        "PATH": "/usr/bin",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": value,
    }
    with pytest.raises(job_runner.JobRunnerError) as excinfo:
        job_runner.reject_unsafe_env(env, source="test")
    assert excinfo.value.diagnostic.reason == "job-runner-git-config-value-invalid"


def test_the_read_end_runs_the_same_guard(tmp_path: Path) -> None:
    """spec spool 被動過手腳時，shim 也拒絕 exec——不是只在寫端自律。"""

    spec = {
        "spec_version": job_runner.JOB_SPEC_VERSION,
        "instance": "j-deadbeef",
        "job_id": "j",
        "unit": "cortex-job@j-deadbeef.service",
        "command": ["/bin/true"],
        "working_directory": "/tmp",
        "log_path": "/tmp/j.jsonl",
        "env": {
            "PATH": "/usr/bin",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "/tmp",
            "GIT_CONFIG_KEY_1": "alias.pwn",
            "GIT_CONFIG_VALUE_1": "!echo pwned",
        },
    }
    (tmp_path / "j-deadbeef.json").write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(job_shim.ShimError) as excinfo:
        job_shim.load_spec("j-deadbeef", str(tmp_path))
    assert "job-runner-git-config-key-not-allowed" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5. 放行綁死在這個 job 的那一格上
# ---------------------------------------------------------------------------


def _spec_kwargs(**overrides):
    base = dict(
        job_id="j",
        instance="j-deadbeef",
        unit="cortex-job@j-deadbeef.service",
        command=["/bin/true"],
        working_directory="/var/lib/cortex/worktree/j",
        log_path="/tmp/j.jsonl",
        env={"PATH": "/usr/bin"},
    )
    base.update(overrides)
    return base


def test_a_spec_whose_grant_points_elsewhere_is_refused() -> None:
    with pytest.raises(job_runner.JobRunnerError) as excinfo:
        job_runner.build_job_spec(
            **_spec_kwargs(
                env={
                    "PATH": "/usr/bin",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "safe.directory",
                    "GIT_CONFIG_VALUE_0": "/var/lib/cortex/worktree/OTHER",
                }
            )
        )
    assert excinfo.value.diagnostic.reason == "job-runner-git-config-value-invalid"
    assert "逐 job" in str(excinfo.value)


def test_a_spec_whose_grant_matches_its_workspace_is_accepted() -> None:
    spec = job_runner.build_job_spec(
        **_spec_kwargs(
            env={
                "PATH": "/usr/bin",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": "/var/lib/cortex/worktree/j",
            }
        )
    )
    assert spec["env"]["GIT_CONFIG_VALUE_0"] == spec["working_directory"]


def test_a_spec_without_any_grant_is_still_accepted() -> None:
    """gate（`owned-by-job`）與單 UID 的既有路徑都走這一支——檢查是條件式的。"""

    spec = job_runner.build_job_spec(**_spec_kwargs())
    assert not [k for k in spec["env"] if k.startswith("GIT_CONFIG")]


# ---------------------------------------------------------------------------
# 6. 反向不變式：用**真的 git**驗
# ---------------------------------------------------------------------------

_ASSUME_FOREIGN = {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}


def _git(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(cwd),
            "GIT_CONFIG_NOSYSTEM": "1",
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture()
def two_job_workspaces(tmp_path: Path) -> tuple[Path, Path]:
    """兩格 per-job 工作區，各自是一棵真的 repo（＝builder 的 clone 形狀）。"""

    made = []
    for name in ("job-a", "job-b"):
        workspace = tmp_path / name
        workspace.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(workspace)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "commit", "-q", "--allow-empty", "-m", name],
            cwd=str(workspace),
            check=True,
            capture_output=True,
        )
        made.append(workspace)
    return made[0], made[1]


def test_a_real_repo_reproduces_the_712_symptom_without_the_grant(
    two_job_workspaces: tuple[Path, Path],
) -> None:
    """基線：跨 owner ＋ 無放行 ⇒ 逐字就是本票的原症狀。"""

    own, _other = two_job_workspaces
    status = _git(["status", "--porcelain"], cwd=own, env=dict(_ASSUME_FOREIGN))
    assert status.returncode != 0
    assert "detected dubious ownership" in status.stderr
    bundle = _git(
        ["bundle", "create", str(own / "x.bundle"), "--all"],
        cwd=own,
        env=dict(_ASSUME_FOREIGN),
    )
    assert bundle.returncode != 0
    assert "Need a repository to create a bundle" in bundle.stderr


def test_the_grant_makes_status_and_bundle_work_in_the_jobs_own_workspace(
    two_job_workspaces: tuple[Path, Path],
) -> None:
    """正向：`build_job_env()` 算出來的那份 env，讓 builder 真正會跑的兩支都成功。"""

    own, _other = two_job_workspaces
    env = {**_ASSUME_FOREIGN, **{k: _build_env(job_runner.JOB_ROLE_BUILDER, str(own))[k]
                                 for k in _GIT_ENV_KEYS}}
    status = _git(["status", "--porcelain", "--branch"], cwd=own, env=env)
    assert status.returncode == 0, status.stderr
    bundle = _git(
        ["bundle", "create", str(own / "commits.bundle"), "--all"], cwd=own, env=env
    )
    assert bundle.returncode == 0, bundle.stderr
    assert (own / "commits.bundle").is_file()


def test_the_same_env_still_fails_in_another_jobs_workspace(
    two_job_workspaces: tuple[Path, Path],
) -> None:
    """**反向不變式**：放行是 per-job，不是全域。

    這一半才是「只放行這一格」的證據。真正要排除的失敗是有人把值換成字面 `*`
    或換成 pool 根——那兩種寫法都會讓這條變成 rc=0。
    """

    own, other = two_job_workspaces
    env = {**_ASSUME_FOREIGN, **{k: _build_env(job_runner.JOB_ROLE_BUILDER, str(own))[k]
                                 for k in _GIT_ENV_KEYS}}
    status = _git(["status", "--porcelain"], cwd=other, env=env)
    assert status.returncode != 0
    assert "detected dubious ownership" in status.stderr


def test_the_gate_shape_really_is_zero_action(
    two_job_workspaces: tuple[Path, Path],
) -> None:
    """gate 的 env 是空的——本條把「不需要」與「忘了做」分開。

    gate **不需要**的理由是它的副本由自己 `copytree` 出來（owner 就是自己），因此
    在一個**真的**跨 owner 的樹上，gate 那份 env 當然救不了它——這正是形態不同的
    可觀察後果，不是缺陷。
    """

    own, _other = two_job_workspaces
    env = _build_env(job_runner.JOB_ROLE_GATE, str(own))
    assert not [k for k in env if k.startswith("GIT_CONFIG")]
    status = _git(["status", "--porcelain"], cwd=own, env=dict(_ASSUME_FOREIGN))
    assert status.returncode != 0


def test_a_real_repo_behind_a_symlink_needs_the_physical_path(tmp_path: Path) -> None:
    """實測：git 比對的是解析後的路徑，symlink 那條**不算數**。

    這是 `git_workspace_trust_env()` 一律 `resolve()` 的出處。
    """

    real = tmp_path / "real"
    real.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(real)], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "--allow-empty", "-m", "x"],
        cwd=str(real), check=True, capture_output=True,
    )
    link = tmp_path / "link"
    link.symlink_to(real)

    literal = {
        **_ASSUME_FOREIGN,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(link),
    }
    assert _git(["status", "--porcelain"], cwd=link, env=literal).returncode != 0

    produced = {**_ASSUME_FOREIGN,
                **{k: _build_env(job_runner.JOB_ROLE_BUILDER, str(link))[k]
                   for k in _GIT_ENV_KEYS}}
    assert _git(["status", "--porcelain"], cwd=link, env=produced).returncode == 0


def test_an_alias_through_this_channel_really_executes(tmp_path: Path) -> None:
    """單鍵白名單的**理由**，用真的 git 驗出來（0819，git 2.43.0）。

    `GIT_CONFIG_*` 與 `git -c` 同級，因此 `alias.*` 經它進來會執行外部命令——這正是
    三份 `.gitconfig` 必須 root-owned 的那條理由，本管道不得成為它的繞法。
    """

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True,
                   capture_output=True)
    result = _git(
        ["psc712"],
        cwd=tmp_path,
        env={
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "alias.psc712",
            "GIT_CONFIG_VALUE_0": "!echo PSC-712-EXTERNAL-COMMAND",
        },
    )
    assert "PSC-712-EXTERNAL-COMMAND" in result.stdout
    # …而同一組 env 交給守衛就是 fail-closed。
    with pytest.raises(job_runner.JobRunnerError):
        job_runner.reject_unsafe_env(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "alias.psc712",
                "GIT_CONFIG_VALUE_0": "!echo PSC-712-EXTERNAL-COMMAND",
            },
            source="test",
        )


def test_the_static_gitconfig_cannot_cover_a_linked_worktree(tmp_path: Path) -> None:
    """reviewer 那一格的查證證據：來源樹那兩條**蓋不到** review worktree。

    `prepare_review_worktree()` 開的是 `git worktree add --detach` 的 linked
    worktree；git 查的是**工作樹自己的路徑**，因此只給來源樹兩條時仍被拒。
    """

    source = tmp_path / "src"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "--allow-empty", "-m", "x"],
        cwd=str(source), check=True, capture_output=True,
    )
    worktree = source / ".psc-review-worktrees" / "rev-1"
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(worktree)],
        cwd=str(source), check=True, capture_output=True,
    )

    source_only = {
        **_ASSUME_FOREIGN,
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(source),
        "GIT_CONFIG_KEY_1": "safe.directory",
        "GIT_CONFIG_VALUE_1": str(source / ".git"),
    }
    blocked = _git(["status", "--porcelain"], cwd=worktree, env=source_only)
    assert blocked.returncode != 0
    assert "detected dubious ownership" in blocked.stderr

    per_job = {**_ASSUME_FOREIGN,
               **{k: _build_env(job_runner.JOB_ROLE_REVIEW, str(worktree))[k]
                  for k in _GIT_ENV_KEYS}}
    assert _git(["status", "--porcelain"], cwd=worktree, env=per_job).returncode == 0


@pytest.mark.skip(
    reason=(
        "OS 層語意需要第二個 UID：真正的 dubious-ownership 要 repo 的 owner uid ≠ "
        "當下的 euid，而本 CI 只有一個 UID。本檔以 git 自己的 "
        "`GIT_TEST_ASSUME_DIFFERENT_OWNER=1` 驗**判定路徑**（`ensure_valid_ownership()` "
        "的第一個條件），那與真正跨 owner 時逐字相同，且 0819 已在 root-owned repo 上"
        "以一般使用者實跑對照過。真正需要第二個 UID 的那一維（模板 unit 的 `User=`、"
        "ACL、mask、真實派工）走實機探針 "
        "`python3 -m paulsha_cortex.trust_root git-trust-probe`，記錄在 runbook 第 "
        "4e-2f 步——`psc_run_under` 複製的是加固面、不是派工路徑（#709 的 caveat），"
        "因此那一支的步驟 2／3 走真實 `systemctl start --wait`。"
    )
)
def test_a_real_cross_uid_dispatch_grants_only_this_jobs_workspace() -> None:  # pragma: no cover
    raise AssertionError("見 skip 理由：這一維由實機探針涵蓋")


# ---------------------------------------------------------------------------
# 7. 陳舊 note 的更正（#696 的教訓：陳舊宣稱會反向說謊）
# ---------------------------------------------------------------------------


def _dependency_note(dep_id: str) -> str:
    for dep in permgen.RUN_EXTERNAL_DEPENDENCIES:
        if dep.name == dep_id:
            return dep.note
    raise AssertionError(f"{dep_id} 不在 RUN_EXTERNAL_DEPENDENCIES 上")


@pytest.mark.parametrize("dep_id", ["builder-gitconfig", "reviewer-planner-gitconfig"])
def test_the_gitconfig_notes_no_longer_claim_to_cover_the_per_job_workspace(
    dep_id: str,
) -> None:
    """舊 note 逐字宣稱「per-job clone 的 `safe.directory`」，而產生器只出來源樹兩條。"""

    note = _dependency_note(dep_id)
    assert "來源樹" in note
    assert "不涵蓋" in note
    assert "#712" in note or "712" in note


def test_the_generated_gitconfig_states_its_own_boundary() -> None:
    layout = replace(permgen.DEFAULT_LAYOUT, source_repo_slugs=("psc-demo",))
    produced = permgen.build_account_gitconfig(
        permgen.DEFAULT_SCHEME, layout, registry.Principal.BUILDER
    )
    assert "涵蓋範圍**只有下列來源樹路徑**" in produced.content
    assert all("*" not in entry for entry in produced.safe_directories)


def test_the_template_units_describe_their_own_git_trust_shape() -> None:
    """三份 unit 的那一段必然不同，且各自等於它那一列宣告的機制。"""

    seen = {}
    for principal in permgen.downgraded_job_principals(permgen.DEFAULT_SCHEME):
        account = permgen.DEFAULT_SCHEME.resolve(principal)
        assert account is not None
        lines = permgen._job_unit_git_trust_lines(
            permgen.DEFAULT_LAYOUT, principal, account
        )
        text = "\n".join(lines)
        trust = registry.job_git_workspace_trust_for(principal)
        assert trust.trust.value in text
        seen[principal.value] = text
    assert "GIT_CONFIG_KEY_0=safe.directory" in seen["builder"]
    assert "GIT_CONFIG" not in seen["gate"]
    assert seen["builder"] != seen["gate"]


# ---------------------------------------------------------------------------
# 8. 實機探針
# ---------------------------------------------------------------------------


def test_the_probe_carries_no_hand_rolled_hardening(capsys) -> None:
    """D13：不得自組 `--property=`、不得自帶 `--setenv=PATH=`。"""

    lines = permgen.build_job_git_trust_probe(permgen.DEFAULT_SCHEME)
    assert permgen.path_probe_env_injections(lines) == ()
    assert not any("--property=" in line for line in lines if not line.strip().startswith("#"))


def test_the_probe_asserts_both_directions_and_uses_real_dispatch() -> None:
    text = "\n".join(permgen.build_job_git_trust_probe(permgen.DEFAULT_SCHEME))
    # 基線：缺陷本身（零額外 env、真實加固面）
    assert permgen.PATH_PROBE_HELPER in text
    assert "detected dubious ownership" in text
    # 正向：builder 真正會跑的那兩支
    assert "git bundle create" in text
    # 反向：別的 job 的工作區
    assert "$WS_OTHER" in text
    assert "exit 23" in text
    # 真實派工，而不是 psc_run_under 塞 env
    assert "systemctl start --wait" in text
    assert "build_job_env" in text
    # 工作區由真實 provisioning 產生
    assert "ScriptWorktreeCreator" in text
    assert "ensure_workspace_reachable" in text


def test_the_probe_cli_is_wired() -> None:
    assert main(["git-trust-probe", "four-way"]) == 0
    assert main(["git-trust-probe", "nonsense"]) == 2
