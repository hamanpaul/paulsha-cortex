"""#710：per-job clone 建好之後沒有交給 job 帳號——工作區可達性的單一規則。

## 這一票在修什麼

`#708`／PR #709 讓 builder job 的 log 開得起來之後，`shim-error.json` 立刻交出下一個
逐字原因：

```json
{"schema": "cortex-job-shim-error/1",
 "error": "[Errno 13] Permission denied: '/var/lib/cortex/worktree/wf-…-2-be5433ea'"}
```

per-job clone 是 **Manager** 用 `git clone` 建的 ⇒ owner 是 Manager、mode `0700`、
零具名 ACL。模板 unit 的 `ReadWritePaths=<pool>/%i` 在 **mount 層**放行，**DAC 層擋死**
——兩層要同時成立。而 `cortex-reviewer-job@.service` 的註解逐字宣稱「整個 clone 由本
job 帳號擁有」，**全 `coordinator/` 零個 `chown`**，沒有任何程式實作那句話；
Manager 也結構上做不到（`chown` 給另一個使用者要 `CAP_CHOWN`，Manager unit 帶
`CapabilityBoundingSet=`）。**這不是漏寫一行，是方案與降權模型衝突。**

## 本檔釘住的四件事

1. **一條規則**（`registry.JOB_WORKSPACE_REACH`）覆蓋三個降權 principal，缺一格
   模組載不起來；`coordinator/job_runner.JOB_ROLE_CONFIG` 是它的成對契約，逐列相等。
2. ⚠️ **per-job 那一格，不是 pool 根**——pool 根的 default ACL 會讓**每個** job 帳號
   進得去**每個** job 的目錄。三個角度各釘一次：permgen 的 import 期斷言（突變驗證）、
   產生器輸出的落點、執行期把 pool 根當引數時 fail-closed。
3. ⚠️ **mask 陷阱**——判準是 `mask::` 與 `#effective:`，**不是「ACL 行存在」**。以一棵
   真的 ACL 樹驗，並示範 `chmod` 之後具名條目如何靜默失效。
4. **反向不變式**——工作區由**真實 provisioning** 產生（`seams.ScriptWorktreeCreator`
   ＋ `job_runner.ensure_workspace_reachable`），不手工前置（#645 逐字記錄過手工前置物
   會把 bug 繞過去；#709 也記了「`psc_run_under` 證明不了派工路徑」）。

## 環境相依的那一組

「別的 uid 進不進得去」是 **OS 層語意**。本檔**不需要**借第二個 uid 來執行——
`job_runner.effective_perms_for_account()` 是 POSIX.1e §Access Check Algorithm 的
直譯（含 mask 收斂），因此「那個帳號會拿到什麼」在不切換身分的情況下就算得出來。
需要的只有**一個真實存在的第二個帳號名**（`setfacl -m u:<名>:…` 在產生的當下就要
解析得到 uid，#626／#657 的同一條）與一個支援 POSIX ACL 的檔案系統；兩者任一缺席
時**具名 skip 並附理由**，絕不靜默通過（#638 的教訓）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import job_runner, job_workspace, seams
from paulsha_cortex.trust_root import permgen, registry
from paulsha_cortex.trust_root.permgen import DEFAULT_LAYOUT, generate_plan
from paulsha_cortex.trust_root.registry import Principal, WorkspaceReach

SCHEME = permgen.DEFAULT_SCHEME
ALL_SCHEMES = [permgen.SCHEMES[sid] for sid in ("two-way", "three-way", "four-way")]
SCHEME_IDS = [s.scheme_id for s in ALL_SCHEMES]

#: 借來當「第二個帳號」的候選。判準只有一個：`setfacl` 要解析得到它的 uid。
#: 刻意用發行版一定會有的系統帳號，而不是 `cortex-*`——後者只存在於實機部署，
#: 用它會讓本檔在 CI 上永遠 skip，而那正是 #638 那種「綠燈不承載語意」的形狀。
_PROBE_ACCOUNT_CANDIDATES = ("daemon", "nobody", "bin", "games", "www-data")


def _probe_accounts(count: int) -> list[str] | None:
    """`count` 個真實存在、且不是本行程 uid 的帳號名；湊不齊回 None。"""

    import pwd

    mine = os.getuid()
    found: list[str] = []
    for name in _PROBE_ACCOUNT_CANDIDATES:
        try:
            entry = pwd.getpwnam(name)
        except KeyError:
            continue
        if entry.pw_uid == mine:
            continue
        found.append(name)
        if len(found) == count:
            return found
    return None


def _acl_capable(root: Path) -> bool:
    """這個檔案系統吃不吃 POSIX ACL（且 `setfacl`／`getfacl` 在不在）。"""

    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        return False
    accounts = _probe_accounts(1)
    if accounts is None:
        return False
    probe = root / ".acl-probe"
    probe.mkdir()
    completed = subprocess.run(
        ["setfacl", "-m", f"u:{accounts[0]}:rx", str(probe)],
        check=False,
        capture_output=True,
    )
    return completed.returncode == 0


_NEEDS_SECOND_ACCOUNT = (
    "本組驗的是 **OS 層語意**（具名 ACL 的有效權限、以及「別的 job 帳號進不去」）。"
    "它需要 (a) 至少兩個 `setfacl` 解析得到 uid 的真實帳號名——`setfacl -m u:<名>:…` "
    "在產生的當下就要解得到，這是 #626 的既有前例；(b) 一個支援 POSIX ACL 的檔案系統。"
    "兩者任一缺席時**明確 skip 而非空過**（#638 的教訓：單 UID／無 ACL 環境下這條性質"
    "沒有可驗的語意，靜默通過等於假綠）。結構性保證（登記表、權限計畫、產生器輸出、"
    "執行期分岔）在任何環境都會跑。"
)


# ---------------------------------------------------------------------------
# 1. 一條規則覆蓋三個 principal（結構性——任何環境都跑）
# ---------------------------------------------------------------------------

def test_every_downgraded_principal_has_exactly_one_workspace_reach() -> None:
    """#698／#708 的第三次：三個降權 principal 一格都不能少，也不能多。"""

    declared = [reach.principal for reach in registry.JOB_WORKSPACE_REACH]
    assert declared == list(registry.DOWNGRADED_JOB_PRINCIPALS)
    for principal in registry.DOWNGRADED_JOB_PRINCIPALS:
        assert registry.job_workspace_reach_for(principal).principal is principal


def test_a_missing_row_makes_the_registry_refuse_to_load() -> None:
    """「只修一格」在**結構上做不到**：缺一列，import 期斷言當場翻紅。"""

    only_builder = tuple(
        r for r in registry.JOB_WORKSPACE_REACH if r.principal is Principal.BUILDER
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(registry, "JOB_WORKSPACE_REACH", only_builder)
        with pytest.raises(ValueError) as excinfo:
            registry._assert_every_downgraded_principal_has_a_workspace_reach()
    assert "reviewer" in str(excinfo.value) and "gate" in str(excinfo.value)


def test_the_three_shapes_are_not_assumed_identical() -> None:
    """三者現況**不同**，規則表講的就是那個不同（#710 issue body 逐字要求）。"""

    by_principal = {r.principal: r for r in registry.JOB_WORKSPACE_REACH}
    assert by_principal[Principal.BUILDER].reach is WorkspaceReach.PER_JOB_NAMED_ACL
    assert by_principal[Principal.REVIEWER].reach is WorkspaceReach.INHERITED_DEFAULT_ACL
    assert by_principal[Principal.GATE].reach is WorkspaceReach.POOL_OWNED_BY_JOB
    # 而「每個 job 都要進得去自己的工作區」這條**性質**三者共有，且寫得出可驗形式。
    for reach in registry.JOB_WORKSPACE_REACH:
        assert reach.required_perms
        assert set(reach.required_perms) <= {"r", "w", "x"}


def test_only_the_named_acl_shape_carries_a_per_job_asset() -> None:
    """`per_job_asset_id` 與形態必須相容——要下 setfacl 就要有落點。"""

    for reach in registry.JOB_WORKSPACE_REACH:
        has_leaf = reach.per_job_asset_id is not None
        assert has_leaf == (reach.reach is WorkspaceReach.PER_JOB_NAMED_ACL), reach


def test_job_runner_role_config_is_the_paired_contract() -> None:
    """`JOB_ROLE_CONFIG` ↔ `JOB_WORKSPACE_REACH` 逐列相等。

    `coordinator/` 刻意不 import `trust_root`（派工熱路徑對治理平面零依賴，與
    `log_spool_principal`／`DEFAULT_TEMPLATE_UNIT` 同一個既有模式）。兩份真相因此
    必須由一條測試釘住——不然它們會漂移，而漂移的方向不由人選（#679／#692）。
    """

    role_by_principal = {
        Principal.BUILDER: job_runner.JOB_ROLE_BUILDER,
        Principal.REVIEWER: job_runner.JOB_ROLE_REVIEW,
        Principal.GATE: job_runner.JOB_ROLE_GATE,
    }
    for reach in registry.JOB_WORKSPACE_REACH:
        config = job_runner.JOB_ROLE_CONFIG[role_by_principal[reach.principal]]
        assert config.workspace_reach == reach.reach.value, reach.principal
        assert config.workspace_required_perms == reach.required_perms, reach.principal
        if reach.reach is not WorkspaceReach.PER_JOB_NAMED_ACL:
            assert config.workspace_acl == (), reach.principal
            continue
        expected = [
            (role_by_principal[reach.principal], reach.access_perms, reach.default_perms)
        ]
        expected += [
            (role_by_principal[p], perms, perms) for p, perms in reach.extra_reader_perms
        ]
        assert [
            (s.role_id, s.access_perms, s.default_perms) for s in config.workspace_acl
        ] == expected


# ---------------------------------------------------------------------------
# 2. 三個形態各自與權限計畫一致（permgen 的 import 期斷言那一半）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=SCHEME_IDS)
def test_the_pool_owned_shape_really_owns_its_pool(scheme) -> None:
    """gate：pool 根的 owner 就是它自己（可達性唯一來源＝owner 位）。"""

    reach = registry.job_workspace_reach_for(Principal.GATE)
    account = scheme.resolve(Principal.GATE)
    if account is None:  # two-way／three-way 沒有 gate
        pytest.skip(f"{scheme.scheme_id} 沒有 gate 身分——本形態在該方案下不存在")
    entry = generate_plan(scheme).by_id(reach.pool_asset_ids[0])
    assert entry.owner == account
    assert entry.mode == 0o700
    assert entry.acls == ()


def test_the_inherited_shape_really_has_a_default_acl_on_every_pool() -> None:
    """reviewer／planner：兩棵樹的**根**都要帶該帳號的 default ACL。"""

    reach = registry.job_workspace_reach_for(Principal.REVIEWER)
    account = SCHEME.resolve(reach.persona)
    plan = generate_plan(SCHEME)
    for pool_id in reach.pool_asset_ids:
        defaults = [
            acl
            for acl in plan.by_id(pool_id).effective_default_acls()
            if acl.account == account
        ]
        assert defaults, pool_id
        assert set(reach.access_perms) <= set("".join(a.perms for a in defaults)), pool_id


def test_the_pool_root_of_the_named_acl_shape_has_zero_job_default_acl() -> None:
    """⚠️ **本票兩個硬性注意事項的第一個**：pool 根不得有任何 job 帳號的 default ACL。

    有的話，`<pool>` 底下**每一個** job 目錄都會繼承同一組授權 ⇒ 每個 job 帳號進得去
    每個 job 的目錄，裁決 10-2 的 per-job 隔離當場歸零。
    """

    reach = registry.job_workspace_reach_for(Principal.BUILDER)
    job_accounts = set(SCHEME.headless_accounts())
    for pool_id in reach.pool_asset_ids:
        entry = generate_plan(SCHEME).by_id(pool_id)
        leaked = [
            acl for acl in entry.effective_default_acls() if acl.account in job_accounts
        ]
        assert leaked == [], (pool_id, leaked)


def test_moving_the_grant_to_the_pool_root_makes_permgen_refuse_to_load() -> None:
    """突變驗證：把授權挪到 pool 根，import 期斷言必須當場翻紅。

    這一條是上一條的**強制力**來源。「順手往 pool 根加一條 default ACL 讓它過」正是
    這個缺陷最可能的**修法**，因此那個動作必須讓模組載不起來，而不是讓一條可以用
    `-k` 跳過的測試變紅（#698／#708 的同一條理由）。
    """

    real_build_entry = permgen.build_entry
    pool_id = registry.job_workspace_reach_for(Principal.BUILDER).pool_asset_ids[0]
    builder = SCHEME.resolve(Principal.BUILDER)

    def leaky_build_entry(asset, scheme, **kwargs):
        entry = real_build_entry(asset, scheme, **kwargs)
        if entry.asset_id != pool_id:
            return entry
        return replace(
            entry, acls=entry.acls + (permgen.AclEntry(builder, "rwx", default=True),)
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(permgen, "build_entry", leaky_build_entry)
        with pytest.raises(ValueError) as excinfo:
            permgen._assert_job_workspace_reach_matches_the_plan()
    message = str(excinfo.value)
    assert pool_id in message
    assert "per-job" in message


def test_dropping_the_named_acl_makes_permgen_refuse_to_load() -> None:
    """反向突變：把 per-job 那一格的具名 ACL 拿掉（＝回到 #710 的原症狀）也要翻紅。"""

    real_build_entry = permgen.build_entry
    leaf_id = registry.job_workspace_reach_for(Principal.BUILDER).per_job_asset_id

    def stripped_build_entry(asset, scheme, **kwargs):
        entry = real_build_entry(asset, scheme, **kwargs)
        return entry if entry.asset_id != leaf_id else replace(entry, acls=())

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(permgen, "build_entry", stripped_build_entry)
        with pytest.raises(ValueError) as excinfo:
            permgen._assert_job_workspace_reach_matches_the_plan()
    assert "沒有" in str(excinfo.value) and leaf_id in str(excinfo.value)


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=SCHEME_IDS)
def test_the_generated_setfacl_targets_the_per_job_slot_not_the_pool(scheme) -> None:
    """產生器輸出的落點：`<pool>/<job-id>`，而且 pool 根那一節零 setfacl。"""

    paths = DEFAULT_LAYOUT.asset_paths()
    pool = paths["dispatch-worktree-pool"]
    slot = paths["repo-worktree"]
    assert slot.startswith(pool + "/")
    assert permgen.PER_JOB_SEGMENT in slot

    accounts = permgen.DEFAULT_LAYOUT  # noqa: F841 - 只是讓 layout 這一行有出處
    deployment = {
        Principal.OPERATOR: "cortex-ops",
        Principal.EXTERNAL: "cortex-outbox",
    }
    lines = permgen.plan_to_commands(
        generate_plan(scheme.with_principal_accounts(deployment)),
        path_of=paths,
        scheme=scheme.with_principal_accounts(deployment),
    )
    stripped = [line.lstrip("#").strip() for line in lines]
    assert not [
        line
        for line in stripped
        if line.startswith("setfacl") and line.endswith(" " + pool)
    ]
    assert [line for line in stripped if line.startswith("setfacl") and slot in line]


def test_the_per_job_setfacl_lines_are_recursive() -> None:
    """遞迴不是風格：樹由 Manager 建，只在根下一條 ACL 的話 job 讀不到裡面任何東西。"""

    entry = generate_plan(SCHEME).by_id("repo-worktree")
    assert entry.acls
    for acl in entry.acls:
        assert acl.recursive, acl
        assert acl.render("/x").startswith("setfacl -R "), acl


def test_the_command_order_never_puts_chmod_after_setfacl() -> None:
    """⚠️ mask 陷阱的產生器面：`chmod` 一定排在 `setfacl` **之前**。

    反過來的話，`chmod` 會把 ACL mask 重寫成 mode 的 group 位，剛下的具名條目
    **靜默失效**——ACL 還在（`getfacl` 看得到），有效權限卻是零（runbook 4e-2b）。
    """

    entry = generate_plan(SCHEME).by_id("repo-worktree")
    cmds = entry.commands(DEFAULT_LAYOUT.asset_paths()["repo-worktree"])
    last_chmod = max(i for i, c in enumerate(cmds) if c.startswith("chmod "))
    first_setfacl = min(i for i, c in enumerate(cmds) if c.startswith("setfacl "))
    assert last_chmod < first_setfacl, cmds


# ---------------------------------------------------------------------------
# 3. unit 註解：陳舊的宣稱不得留著反向說謊（#696 的同型）
# ---------------------------------------------------------------------------

#: 那句陳舊宣稱的逐字內容。它**可以**出現在 unit 裡，但只能以被否定的形式出現
#: ——留著原句是刻意的：讀到這份 unit 的人最可能做的事就是「照 #623 的說法再寫一次
#: chown」，因此更正必須把舊句點名並說明它為什麼結構上做不到（#696 的教訓）。
_STALE_CLAIM = "整個 clone 由本 job 帳號擁有"


def test_no_job_unit_still_asserts_the_clone_is_owned_by_the_job() -> None:
    """三份模板 unit 都不得再**宣稱**「整個 clone 由本 job 帳號擁有」。"""

    for principal in registry.DOWNGRADED_JOB_PRINCIPALS:
        unit = permgen.build_job_unit(SCHEME, principal=principal)
        start = 0
        while True:
            hit = unit.content.find(_STALE_CLAIM, start)
            if hit < 0:
                break
            preceding = unit.content[max(0, hit - 24):hit]
            assert "不是" in preceding, (unit.unit_name, preceding)
            start = hit + len(_STALE_CLAIM)


def test_each_job_unit_describes_its_own_workspace_shape() -> None:
    """三份 unit 的工作區那一段**必然不同**，且各自等於它那一列宣告的機制。

    #710 之前那一段是三份 unit 逐字共用的一塊硬寫死註解，內容是 builder 的故事——
    對 reviewer 與 gate 每一個子句都是假的（工作區不在 pool 底下、也不在自己的
    `ReadWritePaths=` 裡）。
    """

    sections: dict[str, str] = {}
    for principal in registry.DOWNGRADED_JOB_PRINCIPALS:
        unit = permgen.build_job_unit(SCHEME, principal=principal)
        reach = registry.job_workspace_reach_for(principal)
        marker = f"JOB_WORKSPACE_REACH：{reach.reach.value}"
        assert marker in unit.content, (unit.unit_name, marker)
        body = unit.content.split(marker, 1)[1].split("# --- ", 1)[0]
        sections[unit.unit_name] = body
    assert len(set(sections.values())) == len(sections), sections.keys()


def test_the_builder_unit_says_who_owns_the_clone_and_why() -> None:
    """更正後的宣稱要講得出「為什麼不是 chown」——不然下一個人會再寫一次那句話。"""

    unit = permgen.build_job_unit(SCHEME, principal=Principal.BUILDER)
    assert "CAP_CHOWN" in unit.content
    assert "CapabilityBoundingSet" in unit.content
    assert "getfacl" in unit.content


def test_the_reviewer_unit_no_longer_claims_the_pool_is_in_its_rwp() -> None:
    """reviewer 的工作區不在 pool 底下，`ReadWritePaths` 也確實不含 pool。"""

    unit = permgen.build_job_unit(SCHEME, principal=Principal.REVIEWER)
    pool = DEFAULT_LAYOUT.worktree_root
    assert not [p for p in unit.read_write_paths if p == pool or p.startswith(pool + "/")]
    assert "已在下方 RWP 內" not in unit.content


# ---------------------------------------------------------------------------
# 4. `setfacl` 的相依（#666／PR #671 的雙向封閉盤點）
# ---------------------------------------------------------------------------

def test_setfacl_is_registered_as_a_run_dependency() -> None:
    """Manager 要執行 `setfacl` ⇒ 它必須在窮舉盤點內，兩張表都要有。"""

    system = {p.name: p for p in permgen.SYSTEM_PROGRAMS}
    assert "setfacl" in system
    assert "manager" in system["setfacl"].required_by
    rows = [d for d in permgen.RUN_EXTERNAL_DEPENDENCIES if d.name == "setfacl"]
    assert len(rows) == 1
    assert Principal.MANAGER in rows[0].principals
    assert permgen.RunStage.DISPATCH in rows[0].stages
    assert rows[0].covered_by == "SYSTEM_PROGRAMS"


def test_the_dependency_roster_is_still_two_way_closed() -> None:
    """#666 的雙向封閉：新增一項不得讓任何一邊出現孤兒。"""

    assert permgen.uncovered_run_dependencies() == ()
    assert permgen.unlisted_roster_entries() == ()


def test_the_runtime_resolves_setfacl_by_name_not_by_a_hardcoded_path() -> None:
    """執行期以名字解析（與 `systemctl` 同一條），解不到即 fail-closed 且訊息指得出來。"""

    assert job_workspace.SETFACL_PROGRAM == "setfacl"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(job_workspace.shutil, "which", lambda name: None)
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(job_workspace.WorkspaceError) as excinfo:
                job_workspace.grant_workspace_acl(
                    tmp, (job_workspace.WorkspaceAclGrant("daemon", "rwX", "rwx"),)
                )
    message = str(excinfo.value)
    assert "acl" in message and "PATH" in message


def test_setfacl_is_where_the_manager_path_can_reach_it() -> None:
    """實機面：`setfacl` 解得到（缺席時具名 skip——那是部署面，不是程式碼面）。"""

    resolved = shutil.which("setfacl")
    if resolved is None:
        pytest.skip(
            "本機沒有 `setfacl`（發行版的 `acl` 套件）。這是**部署決定**而不是程式碼"
            "缺陷，因此明確 skip 而非空過；缺它時 per-job 工作區的具名 ACL 套不上去，"
            "由 `grant_workspace_acl()` 的 fail-closed 在執行期指出來（上一條測試釘住"
            "那個訊息）。0818 trust-root Phase 2b 的三個部署陷阱之一就是這個套件缺席。"
        )
    # Manager unit 沒有自訂 PATH，走 systemd 的預設（`/usr/local/sbin:/usr/local/bin:
    # /usr/sbin:/usr/bin`）——與 `job_runner` 解 `systemctl` 的那一條逐字相同。
    assert Path(resolved).parent.as_posix() in {
        "/usr/bin", "/bin", "/usr/local/bin", "/usr/sbin", "/sbin"
    }, resolved


# ---------------------------------------------------------------------------
# 5. 執行期分岔：形態說零動作就是零動作，pool 根一律拒絕
# ---------------------------------------------------------------------------

def test_the_two_zero_action_shapes_do_nothing_at_dispatch(tmp_path: Path) -> None:
    """reviewer／gate 在派工路徑上不下任何 ACL——可達性是部署當下的性質。"""

    calls: list[object] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            job_workspace,
            "grant_workspace_acl",
            lambda *a, **k: calls.append((a, k)),
        )
        for role in (job_runner.JOB_ROLE_REVIEW, job_runner.JOB_ROLE_GATE):
            assert (
                job_runner.ensure_workspace_reachable(
                    {}, role=role, workspace=tmp_path
                )
                is None
            )
    assert calls == []


def test_handing_it_the_pool_root_is_fail_closed(tmp_path: Path) -> None:
    """⚠️ 硬性注意事項的第一個，在**執行期**這一側：pool 根一律拒絕。

    這是 permgen 那條 import 期斷言的執行期對應——宣告面擋掉「把授權寫成 pool 根的
    default ACL」，這一條擋掉「執行期把 pool 根當成那一格傳進來」。
    """

    pool = tmp_path / "worktree"
    pool.mkdir()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PSC_WORKTREE_ROOT", str(pool))
        with pytest.raises(job_runner.JobRunnerError) as excinfo:
            job_runner.ensure_workspace_reachable(
                {}, role=job_runner.JOB_ROLE_BUILDER, workspace=pool
            )
    assert "pool" in str(excinfo.value)
    assert "job-workspace-acl-target-is-the-pool-root" in str(excinfo.value)


def test_a_workspace_outside_the_declared_pool_is_left_alone(tmp_path: Path) -> None:
    """不在宣告的 pool 底下的路徑一律不動——對不認識的路徑遞迴 setfacl 比不動危險。"""

    pool = tmp_path / "worktree"
    pool.mkdir()
    stranger = tmp_path / "somewhere-else"
    stranger.mkdir()
    calls: list[object] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PSC_WORKTREE_ROOT", str(pool))
        mp.setattr(
            job_workspace,
            "grant_workspace_acl",
            lambda *a, **k: calls.append((a, k)),
        )
        assert (
            job_runner.ensure_workspace_reachable(
                {}, role=job_runner.JOB_ROLE_BUILDER, workspace=stranger
            )
            is None
        )
    assert calls == []


def test_grant_refuses_a_symlinked_workspace(tmp_path: Path) -> None:
    """`setfacl` 對**命令列上**的 symlink 引數預設跟著走 ⇒ 一律拒絕。"""

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(job_workspace.WorkspaceError) as excinfo:
        job_workspace.grant_workspace_acl(
            link, (job_workspace.WorkspaceAclGrant("daemon", "rwX", "rwx"),)
        )
    assert "symlink" in str(excinfo.value)


@pytest.mark.parametrize(
    "account,perms",
    [("root:evil", "rwX"), ("daemon", "rwXq"), ("daemon", ""), ("", "rwX")],
)
def test_grant_validates_the_acl_spec_before_building_the_command(
    tmp_path: Path, account: str, perms: str
) -> None:
    """帳號名／perms 會被逐字接進 `setfacl` 的 ACL spec ⇒ 形狀在組命令之前就驗。"""

    with pytest.raises(job_workspace.WorkspaceError):
        job_workspace.grant_workspace_acl(
            tmp_path, (job_workspace.WorkspaceAclGrant(account, perms, "rwx"),)
        )


# ---------------------------------------------------------------------------
# 6. OS 層語意：真的 ACL 樹、mask 判準、per-job 隔離
#
# 這一組需要第二個真實帳號名 ＋ 支援 ACL 的檔案系統；缺任一即**具名 skip**。
# ---------------------------------------------------------------------------

@pytest.fixture()
def acl_tree():
    """一棵真的、由 `tempfile.mkdtemp()` 建的樹（借來的帳號要 traverse 得進來）。"""

    root = Path(tempfile.mkdtemp(prefix="psc-710-"))
    os.chmod(root, 0o755)
    try:
        if not _acl_capable(root):
            pytest.skip(_NEEDS_SECOND_ACCOUNT)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_grant_reaches_every_inode_in_the_tree(acl_tree: Path) -> None:
    """遞迴那一半的實證：樹**裡面**的檔也拿得到有效權限，不只樹根。"""

    accounts = _probe_accounts(1)
    assert accounts is not None
    account = accounts[0]
    workspace = acl_tree / "job-710-0001"
    (workspace / "deep" / "nested").mkdir(parents=True)
    leaf = workspace / "deep" / "nested" / "object"
    leaf.write_text("x", encoding="utf-8")
    os.chmod(workspace, 0o700)
    os.chmod(leaf, 0o600)

    job_workspace.grant_workspace_acl(
        workspace, (job_workspace.WorkspaceAclGrant(account, "rwX", "rwx"),)
    )

    root_bits = job_runner.effective_perms_for_account(str(workspace), account)
    leaf_bits = job_runner.effective_perms_for_account(str(leaf), account)
    assert root_bits == 0o7, oct(root_bits or 0)
    # 檔案拿到 `rw` 而**不是** `rwx`——大寫 `X` 只給目錄與已可執行的檔。
    assert leaf_bits == 0o6, oct(leaf_bits or 0)


def test_the_verdict_is_the_mask_not_the_presence_of_an_acl_line(acl_tree: Path) -> None:
    """⚠️ 硬性注意事項的第二個：`chmod` 之後 ACL 行還在，有效權限卻是零。

    這一條同時是「為什麼判準必須是 `getfacl` 的 `mask::`／`#effective:`」的實證，
    也是「產生器與執行期都不得在 setfacl 之後再 chmod」的理由。
    """

    accounts = _probe_accounts(1)
    assert accounts is not None
    account = accounts[0]
    workspace = acl_tree / "job-710-0002"
    workspace.mkdir(mode=0o700)
    job_workspace.grant_workspace_acl(
        workspace, (job_workspace.WorkspaceAclGrant(account, "rwX", "rwx"),)
    )
    assert job_runner.effective_perms_for_account(str(workspace), account) == 0o7

    rendered = subprocess.run(
        ["getfacl", "-p", str(workspace)], check=True, capture_output=True, text=True
    ).stdout
    assert f"user:{account}:rwx" in rendered
    assert "mask::rwx" in rendered

    os.chmod(workspace, 0o700)  # ← 這一行就是陷阱本身

    after = subprocess.run(
        ["getfacl", "-p", str(workspace)], check=True, capture_output=True, text=True
    ).stdout
    assert f"user:{account}:rwx" in after          # ACL 行**還在**
    assert "mask::---" in after                    # 但 mask 被 chmod 重寫
    assert f"user:{account}:rwx\t#effective:---" in after
    assert job_runner.effective_perms_for_account(str(workspace), account) == 0


def test_one_job_cannot_reach_another_jobs_slot(acl_tree: Path) -> None:
    """⚠️ per-job 隔離的實證：授權下在 A 那一格，B 的帳號進不去。

    這一條就是「不能下在 pool 根」的**後果面**——真的下在 pool 根的話，B 會因為
    繼承而拿到 A 那一格的授權，本測試會當場翻紅。
    """

    accounts = _probe_accounts(2)
    if accounts is None:
        pytest.skip(_NEEDS_SECOND_ACCOUNT)
    first, second = accounts
    pool = acl_tree / "worktree"
    pool.mkdir()
    # issue #723 同族：`mkdir(mode=...)` 的 mode **會被 umask 遮罩**，`chmod` 不會。
    # 在 `UMask=0077` 的 unit（cortex-gate-job@.service）底下 `mkdir(mode=0o701)`
    # 實際建出來是 `0700`，pool 根的 other-execute（借來的帳號 traverse 得進去的
    # 那個位元）被靜默拿掉——本測試整個前提就架在它上面。`acl_tree` fixture 對
    # 自己的 root 用 `os.chmod(root, 0o755)` 正是同一個理由，pool 漏掉了。
    pool.chmod(0o701)
    slot_a = pool / "job-710-A"
    slot_b = pool / "job-710-B"
    slot_a.mkdir(mode=0o700)
    slot_b.mkdir(mode=0o700)

    job_workspace.grant_workspace_acl(
        slot_a, (job_workspace.WorkspaceAclGrant(first, "rwX", "rwx"),)
    )
    job_workspace.grant_workspace_acl(
        slot_b, (job_workspace.WorkspaceAclGrant(second, "rwX", "rwx"),)
    )

    assert job_runner.effective_perms_for_account(str(slot_a), first) == 0o7
    assert job_runner.effective_perms_for_account(str(slot_b), second) == 0o7
    # 交叉：各自進不去對方那一格。
    assert job_runner.effective_perms_for_account(str(slot_a), second) == 0
    assert job_runner.effective_perms_for_account(str(slot_b), first) == 0
    # pool 根本身也不得帶任何 job 帳號的 default ACL（那正是隔離歸零的機制）。
    listed = subprocess.run(
        ["getfacl", "-p", str(pool)], check=True, capture_output=True, text=True
    ).stdout
    assert "default:user:" not in listed, listed


# ---------------------------------------------------------------------------
# 7. 反向不變式：工作區由**真實 provisioning** 產生，不手工前置（#645／#709）
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def test_a_workspace_from_real_provisioning_is_reachable_by_its_job(
    acl_tree: Path,
) -> None:
    """真實 provisioning ＋ 真實派工前置 ⇒ job 帳號 `chdir` 得進去、寫得出檔。

    ⚠️ **工作區不是手工前置的**：它由 `seams.ScriptWorktreeCreator.create()`
    （產線那一支 `git clone`）產出，授權由 `job_runner.ensure_workspace_reachable()`
    （產線那一支）套上。#645 逐字記錄過手工前置物會把 bug 繞過去，而 #710 正是那一族
    的下一個成員：M1 當時 operator 手工挑路徑、恰好避開了這個缺陷。

    這一條**不需要**借第二個 uid 執行：`effective_perms_for_account()` 是 POSIX.1e
    存取檢查演算法的直譯（含 mask 收斂），因此「那個帳號會拿到什麼」在不切換身分的
    情況下就算得出來。實機那一維走 `trust_root workspace-probe`（#709 的 caveat：
    `psc_run_under` 複製的是加固面，不是派工路徑）。
    """

    accounts = _probe_accounts(2)
    if accounts is None:
        pytest.skip(_NEEDS_SECOND_ACCOUNT)
    builder_account, gate_account = accounts

    source = acl_tree / "source"
    source.mkdir()
    _git(["init", "--initial-branch=main", "."], source)
    _git(["config", "user.email", "probe@example.invalid"], source)
    _git(["config", "user.name", "probe"], source)
    (source / "README.md").write_text("probe\n", encoding="utf-8")
    _git(["add", "."], source)
    _git(["commit", "-m", "base"], source)

    pool = acl_tree / "worktree"
    pool.mkdir()
    # issue #723 同族：pool 根的 `0o701` 必須由 `chmod` 下（umask 不遮），不能靠
    # `mkdir(mode=...)`（會遮）。下面第 731 行才把 umask 切成 `0o077` 模擬 gate，
    # 但在 `UMask=0077` 的 unit 底下跑整套時，這一行 mkdir 在切換**之前**就已經
    # 被遮成 `0700` 了——pool 根與 workspace 兩層剛好一起失去 traverse。
    pool.chmod(0o701)
    creator = seams.ScriptWorktreeCreator(repo=source, wt_root=pool, base="main")
    # Manager unit 的 `UMask=0077` 是這棵樹長什麼樣的一半——沒有它，clone 出來是
    # `0755`，「job 進不去」在本機就**觀察不到**（而那正是本票要驗的那個缺陷）。
    # 這不是手工前置**工作區**，是把 provisioning 跑在它產線上真正的 umask 底下。
    previous_umask = os.umask(0o077)
    try:
        workspace = Path(creator.create("feature/probe-710", job_id="probe-710"))
    finally:
        os.umask(previous_umask)
    assert workspace.parent == pool
    assert (workspace.stat().st_mode & 0o777) == 0o700
    # provisioning 之後、授權之前：**這就是 #710 的原症狀**（連 traverse 都沒有）。
    assert job_runner.effective_perms_for_account(str(workspace), builder_account) == 0

    env = {
        "PSC_WORKTREE_ROOT": str(pool),
        job_runner.BUILDER_ACCOUNT_ENV: builder_account,
        job_runner.GATE_ACCOUNT_ENV: gate_account,
    }
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PSC_WORKTREE_ROOT", str(pool))
        applied = job_runner.ensure_workspace_reachable(
            env, role=job_runner.JOB_ROLE_BUILDER, workspace=workspace
        )
    assert applied is not None and applied.startswith(shutil.which("setfacl") or "setfacl")

    required = job_runner.JOB_ROLE_CONFIG[
        job_runner.JOB_ROLE_BUILDER
    ].workspace_required_perms
    bits = job_runner.effective_perms_for_account(str(workspace), builder_account)
    assert bits is not None
    for letter, bit in (("r", 0o4), ("w", 0o2), ("x", 0o1)):
        if letter in required:
            assert bits & bit, (letter, oct(bits))
    # 樹**裡面**（真實 clone 產出的檔）也要拿得到——只在根下一條 ACL 是不夠的。
    head = workspace / ".git" / "HEAD"
    assert job_runner.effective_perms_for_account(str(head), builder_account) == 0o6
    # #629 宣告的 gate 唯讀那條由**同一次** setfacl 落地，且沒有 `w`。
    gate_bits = job_runner.effective_perms_for_account(str(workspace), gate_account)
    assert gate_bits == 0o5, oct(gate_bits or 0)


def test_the_probe_generator_stays_a_pure_string_builder() -> None:
    """探針與 #679／#708 同一條 D13 規矩：不自組 `--property=`、不自帶 `--setenv=`。"""

    lines = permgen.build_job_workspace_probe(SCHEME)
    assert permgen.path_probe_env_injections(lines) == ()
    assert any(permgen.PATH_PROBE_HELPER in line for line in lines)
    # 工作區由產品程式碼建，不是探針自己 `install -d` 出來的。
    joined = "\n".join(lines)
    assert "seams.ScriptWorktreeCreator" in joined
    assert "ensure_workspace_reachable" in joined
    # 三個 principal 各有一段。
    for principal in registry.DOWNGRADED_JOB_PRINCIPALS:
        assert f"--- {principal.value}（" in joined


def test_the_probe_cli_verb_prints_it() -> None:
    """runbook 引用的是 CLI，不是函式——動詞不在就等於探針不存在。"""

    from paulsha_cortex.trust_root.__main__ import main

    assert main(["workspace-probe", "four-way"]) == 0
