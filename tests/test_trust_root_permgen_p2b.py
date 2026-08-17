"""Phase 2b（systemd unit ＋ polkit 產生器）：路徑 layout、ReadWritePaths 等式、
降權授權面收窄、非執行保證。

驗收：
- layout 對登記表每一項給出真實絕對路徑（無遺漏、無多餘）；
- Manager unit 的 `ReadWritePaths` **由登記表機械導出**：覆蓋每一個 Manager 需寫的
  路徑（無遺漏），且每一條都是必要的（無多餘）；
- 降權 job 模板 unit 硬寫死 `User=<job 帳號>`，job 側 RWP 不含任何 Manager-owned；
- polkit 規則只放行「svc → job 模板實例的 start/stop」，transient unit／其他 verb／
  其他 unit／其他 action 一律拒（決策矩陣）；
- 兩者都只產生內容字串，絕不執行、不寫任何系統路徑。
"""
from __future__ import annotations

import inspect

import pytest

from paulsha_cortex.trust_root import permgen
from paulsha_cortex.trust_root.permgen import (
    DEFAULT_LAYOUT,
    THREE_WAY_SCHEME,
    TWO_WAY_SCHEME,
    OwnerClass,
    PathLayout,
    build_job_unit,
    build_manager_unit,
    PolkitPlan,
    build_polkit_rule,
    evaluate_polkit,
    generate_plan,
    read_write_paths,
    required_write_targets,
)
from paulsha_cortex.trust_root.registry import ASSET_REGISTRY, Principal

ALL_SCHEMES = [TWO_WAY_SCHEME, THREE_WAY_SCHEME]


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


# ---------------------------------------------------------------------------
# 路徑 layout：對登記表的雙向等式
# ---------------------------------------------------------------------------

def test_layout_covers_every_registry_asset_exactly() -> None:
    paths = DEFAULT_LAYOUT.asset_paths()
    registry_ids = {a.asset_id for a in ASSET_REGISTRY}
    assert set(paths) == registry_ids, "layout 與登記表必須逐項對齊（無遺漏、無多餘）"


def test_layout_paths_are_absolute_and_match_operator_decision() -> None:
    """0816 第二輪裁決：durable state 落 /var/lib/cortex、部署落 /opt/cortex。"""
    paths = DEFAULT_LAYOUT.asset_paths()
    for asset_id, path in paths.items():
        assert path.startswith("/"), (asset_id, path)
    assert DEFAULT_LAYOUT.agents_root == "/var/lib/cortex"
    assert DEFAULT_LAYOUT.worktree_root == "/var/lib/cortex/worktree"
    assert DEFAULT_LAYOUT.deploy_root == "/opt/cortex"
    assert paths["dispatch-worktree-pool"] == "/var/lib/cortex/worktree"
    # 部署面（enforcement plane）不得落在 durable state 樹內。
    assert paths["runtime-bootstrap-env"].startswith("/opt/cortex/")


def test_no_asset_path_remains_in_operator_home() -> None:
    """Phase 2b 的重點就是把 durable state 全數搬離 operator HOME。"""
    for asset_id, path in DEFAULT_LAYOUT.asset_paths().items():
        assert not path.startswith("/home/"), (asset_id, path)
        assert "~" not in path, (asset_id, path)


def test_per_job_assets_carry_the_job_segment() -> None:
    paths = DEFAULT_LAYOUT.asset_paths()
    for asset_id in ("repo-worktree", "review-verdict", "handoff-manifest", "work-items-yaml"):
        assert permgen.PER_JOB_SEGMENT in paths[asset_id], asset_id


def test_with_job_segment_substitutes_systemd_specifier() -> None:
    job_layout = DEFAULT_LAYOUT.with_job_segment("%i")
    assert job_layout.asset_paths()["repo-worktree"] == "/var/lib/cortex/worktree/%i"
    # 其餘欄位不變。
    assert job_layout.agents_root == DEFAULT_LAYOUT.agents_root


# ---------------------------------------------------------------------------
# ReadWritePaths 等式（本 PR 的核心驗收）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_manager_read_write_paths_cover_every_manager_writable_asset(scheme) -> None:
    """無遺漏：登記表中每一個 Manager 需寫的資產都被某條 ReadWritePaths 覆蓋。"""
    plan = generate_plan(scheme)
    unit = build_manager_unit(scheme, DEFAULT_LAYOUT)
    targets = required_write_targets(plan, DEFAULT_LAYOUT, scheme.durable_state_owner)
    assert targets, "Manager 至少要有可寫資產"
    for asset_id, target in targets.items():
        assert any(_within(target, rwp) for rwp in unit.read_write_paths), (
            scheme.scheme_id, asset_id, target, unit.read_write_paths,
        )


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_manager_read_write_paths_have_no_redundant_entry(scheme) -> None:
    """無多餘：拿掉任何一條登記表導出的條目，就會有資產失去覆蓋。

    明示宣告的 extras（job spool／HOME cache）是唯一例外，且每條必須附理由。
    """
    plan = generate_plan(scheme)
    unit = build_manager_unit(scheme, DEFAULT_LAYOUT)
    targets = required_write_targets(plan, DEFAULT_LAYOUT, scheme.durable_state_owner)
    extras = {
        e.path
        for e in DEFAULT_LAYOUT.manager_extra_write_paths(scheme.durable_state_owner)
    }

    for rwp in unit.read_write_paths:
        if rwp in extras:
            continue
        remaining = [p for p in unit.read_write_paths if p != rwp]
        uncovered = [
            asset_id
            for asset_id, target in targets.items()
            if not any(_within(target, other) for other in remaining)
        ]
        assert uncovered, (scheme.scheme_id, f"{rwp} 是多餘條目——移除後無資產失去覆蓋")


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_manager_read_write_paths_are_minimal_and_disjoint(scheme) -> None:
    """最小覆蓋：沒有任何一條被另一條包含（否則等於重複開放）。"""
    unit = build_manager_unit(scheme, DEFAULT_LAYOUT)
    rwp = unit.read_write_paths
    assert len(set(rwp)) == len(rwp)
    for a in rwp:
        for b in rwp:
            if a != b:
                assert not _within(a, b), (a, b)


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_deployment_tree_never_writable_by_manager_unit(scheme) -> None:
    """spec §R3：enforcement plane（/opt/cortex、env 檔、codex hooks）對服務唯讀。"""
    plan = generate_plan(scheme)
    unit = build_manager_unit(scheme, DEFAULT_LAYOUT)
    paths = DEFAULT_LAYOUT.asset_paths()
    for rwp in unit.read_write_paths:
        assert not _within(rwp, DEFAULT_LAYOUT.deploy_root), rwp
    for entry in plan.entries:
        if entry.owner_class is not OwnerClass.DEPLOYMENT:
            continue
        target = paths[entry.asset_id]
        assert not any(_within(target, rwp) for rwp in unit.read_write_paths), entry.asset_id


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_read_write_paths_are_consistent_with_protect_home(scheme) -> None:
    """`ProtectHome=yes` 會讓 /home、/root 不可見——RWP 落在那裡等於保證靜默失效。"""
    unit = build_manager_unit(scheme, DEFAULT_LAYOUT)
    for rwp in unit.read_write_paths:
        assert not rwp.startswith("/home/"), rwp
        assert not rwp.startswith("/root"), rwp


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_extra_write_paths_all_carry_a_reason(scheme) -> None:
    builder = scheme.resolve(Principal.BUILDER)
    assert builder is not None
    for extra in DEFAULT_LAYOUT.manager_extra_write_paths(
        scheme.durable_state_owner
    ) + DEFAULT_LAYOUT.job_extra_write_paths(builder):
        assert extra.path.startswith("/")
        assert extra.reason.strip(), extra.path


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_job_unit_read_write_paths_exclude_manager_owned(scheme) -> None:
    """降權 job 側：RWP 不得覆蓋任何 Manager-owned／deployment 資產。"""
    plan = generate_plan(scheme)
    job_layout = DEFAULT_LAYOUT.with_job_segment("%i")
    unit = build_job_unit(scheme, DEFAULT_LAYOUT)
    paths = job_layout.asset_paths()
    for entry in plan.entries:
        if entry.owner_class not in (OwnerClass.MANAGER_STATE, OwnerClass.DEPLOYMENT):
            continue
        target = paths[entry.asset_id]
        builder = scheme.resolve(Principal.BUILDER)
        if builder in plan.all_writable_accounts(entry):
            continue  # spool 之類以 ACL 明示授權者（見下一個測試）
        assert not any(_within(target, rwp) for rwp in unit.read_write_paths), (
            scheme.scheme_id, entry.asset_id, unit.read_write_paths,
        )


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_job_unit_read_write_paths_match_builder_writable_assets(scheme) -> None:
    """job 側同樣是機械導出：涵蓋 builder 需寫的每一項、且僅此。"""
    plan = generate_plan(scheme)
    job_layout = DEFAULT_LAYOUT.with_job_segment("%i")
    builder = scheme.resolve(Principal.BUILDER)
    assert builder is not None
    unit = build_job_unit(scheme, DEFAULT_LAYOUT)
    targets = required_write_targets(plan, job_layout, builder)
    assert targets
    for asset_id, target in targets.items():
        assert any(_within(target, rwp) for rwp in unit.read_write_paths), (asset_id, target)
    extras = {e.path for e in job_layout.job_extra_write_paths(builder)}
    for rwp in unit.read_write_paths:
        if rwp in extras:
            continue
        assert any(_within(t, rwp) for t in targets.values()), rwp


def test_read_write_paths_shift_when_registry_grows() -> None:
    """機械性反證：把 Manager 可寫資產拿掉一項，導出的 RWP 必須跟著變。"""
    plan = generate_plan(TWO_WAY_SCHEME)
    full = read_write_paths(plan, DEFAULT_LAYOUT, TWO_WAY_SCHEME.durable_state_owner)
    trimmed_assets = tuple(a for a in ASSET_REGISTRY if a.asset_id != "dispatch-specs-tree")
    trimmed_plan = generate_plan(TWO_WAY_SCHEME, trimmed_assets)
    trimmed = read_write_paths(trimmed_plan, DEFAULT_LAYOUT, TWO_WAY_SCHEME.durable_state_owner)
    assert "/var/lib/cortex/specs" in full
    assert "/var/lib/cortex/specs" not in trimmed


# ---------------------------------------------------------------------------
# unit 內容
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_manager_unit_runs_as_service_account_never_root(scheme) -> None:
    """裁決：cortex 任何元件永不具 root。"""
    unit = build_manager_unit(scheme, DEFAULT_LAYOUT)
    assert f"User={scheme.durable_state_owner}" in unit.content
    assert "User=root" not in unit.content
    assert unit.account == scheme.durable_state_owner
    assert "AmbientCapabilities=\n" in unit.content
    assert "CapabilityBoundingSet=\n" in unit.content


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_manager_unit_environment_file_is_fail_closed(scheme) -> None:
    """spec §R3：`EnvironmentFile=-` 的靜默容忍必須改為缺檔即拒絕啟動。"""
    unit = build_manager_unit(scheme, DEFAULT_LAYOUT)
    assert f"EnvironmentFile={DEFAULT_LAYOUT.env_file}" in unit.content
    assert "EnvironmentFile=-" not in unit.content


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_manager_unit_carries_every_hardening_directive_with_a_comment(scheme) -> None:
    """每一項加固都必須帶「為何」的註解——這是可審查性要求，不只是設定。"""
    unit = build_manager_unit(scheme, DEFAULT_LAYOUT)
    lines = unit.content.splitlines()
    for key, value, why in permgen._HARDENING:
        directive = f"{key}={value}"
        assert directive in lines, directive
        index = lines.index(directive)
        assert lines[index - 1].startswith("# "), directive
        assert why.split("：")[0][:6] in lines[index - 1], directive
    for required in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes",
                     "PrivateTmp=yes"):
        assert required in unit.content, required


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_manager_unit_execstart_lives_in_root_owned_deploy_tree(scheme) -> None:
    unit = build_manager_unit(scheme, DEFAULT_LAYOUT)
    assert unit.exec_start.startswith(DEFAULT_LAYOUT.deploy_root + "/")
    assert f"ExecStart={unit.exec_start}" in unit.content
    assert f"WorkingDirectory={DEFAULT_LAYOUT.agents_root}" in unit.content


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_job_unit_hardcodes_the_downgraded_identity(scheme) -> None:
    """降權/提權分界線：`User=` 寫死在 root-owned unit 檔，呼叫端選不了 UID。"""
    unit = build_job_unit(scheme, DEFAULT_LAYOUT)
    builder = scheme.resolve(Principal.BUILDER)
    assert unit.account == builder
    assert f"User={builder}" in unit.content
    assert "User=root" not in unit.content
    assert unit.install_path == "/etc/systemd/system/cortex-job@.service"
    assert "%i" in unit.content


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_job_unit_scrubs_github_token(scheme) -> None:
    """spec §R10 Phase 2 第 5 條：降權啟動器不傳遞 gh token。"""
    unit = build_job_unit(scheme, DEFAULT_LAYOUT)
    assert "Environment=GH_TOKEN=\n" in unit.content
    assert "Environment=GITHUB_TOKEN=\n" in unit.content


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_job_unit_command_comes_from_root_owned_shim_and_manager_owned_spec(scheme) -> None:
    """job 不得改寫自己的命令列。

    0816 第三輪 A+B 之後這條的形狀變了（比原本更緊）：`ExecStart=` 不再是 spool 裡
    的 `run.sh`，而是**固定**指向 root-owned 部署樹的 shim——連 Manager 都換不掉
    job 執行的第一支程式。per-job 參數改走 Manager-owned 的 spec spool，job 帳號
    對兩者皆零寫入。
    """
    unit = build_job_unit(scheme, DEFAULT_LAYOUT)
    assert unit.exec_start.startswith(DEFAULT_LAYOUT.job_shim + " ")
    assert unit.exec_start.startswith(DEFAULT_LAYOUT.deploy_root + "/")
    assert f"ExecStart={unit.exec_start}" in unit.content
    # spec spool 的位置由 root-owned unit 檔宣告給 shim，且 job 側不可寫。
    assert f"Environment=PSC_JOB_SPEC_SPOOL={DEFAULT_LAYOUT.job_spec_spool_root}" in unit.content
    for protected in (DEFAULT_LAYOUT.job_spec_spool_root, DEFAULT_LAYOUT.deploy_root):
        assert not any(_within(protected, rwp) for rwp in unit.read_write_paths), protected


def test_units_are_deterministic() -> None:
    a = build_manager_unit(TWO_WAY_SCHEME, DEFAULT_LAYOUT)
    b = build_manager_unit(TWO_WAY_SCHEME, DEFAULT_LAYOUT)
    assert a.content == b.content
    assert a.to_dict() == b.to_dict()


def test_three_way_scheme_changes_the_service_account() -> None:
    """保留三分彈性：只換 config，unit 的身分與 RWP 隨之收緊、程式碼零改動。"""
    two = build_manager_unit(TWO_WAY_SCHEME, DEFAULT_LAYOUT)
    three = build_manager_unit(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    assert two.account == "cortex-svc"
    assert three.account == "cortex-manager"
    # 三分下 reviewer/planner 不再是 durable state owner ⇒ verdict 不再落在
    # Manager 的可寫面（worktree pool 容器仍由 Manager 建子目錄）。
    plan = generate_plan(THREE_WAY_SCHEME)
    verdict = plan.by_id("review-verdict")
    assert THREE_WAY_SCHEME.durable_state_owner not in plan.all_writable_accounts(verdict)


# ---------------------------------------------------------------------------
# polkit 規則（A 方案 transient／B 方案 template，兩案都必須完整可用）
# ---------------------------------------------------------------------------

ALL_PLANS = [PolkitPlan.TRANSIENT, PolkitPlan.TEMPLATE]


@pytest.mark.parametrize("plan", ALL_PLANS, ids=lambda p: p.value)
@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_polkit_rule_binds_svc_to_job_units_only(scheme, plan) -> None:
    rule = build_polkit_rule(scheme, DEFAULT_LAYOUT, plan=plan)
    assert rule.plan is plan
    assert rule.subject_account == scheme.durable_state_owner
    assert rule.target_account == scheme.resolve(Principal.BUILDER)
    assert rule.install_path == "/etc/polkit-1/rules.d/49-cortex-downgrade.rules"
    assert rule.allowed_verbs == ("start", "stop")
    assert rule.unit_pattern.startswith("^cortex-job")
    assert rule.unit_pattern.endswith("$")


def test_transient_plan_unit_prefix_is_the_job_runner_contract() -> None:
    """A 方案的 unit 名前綴與 coordinator/job_runner.UNIT_NAME_PREFIX 是成對契約。"""
    rule = build_polkit_rule(TWO_WAY_SCHEME, DEFAULT_LAYOUT, plan=PolkitPlan.TRANSIENT)
    assert permgen.transient_unit_prefix(DEFAULT_LAYOUT) == "cortex-job-"
    assert rule.unit_pattern.startswith("^cortex-job-")
    assert "job_runner.UNIT_NAME_PREFIX" in rule.content


def test_template_plan_matches_the_generated_template_unit() -> None:
    """B 方案的 pattern 必須恰好認得 build_job_unit() 產出的模板實例。"""
    rule = build_polkit_rule(TWO_WAY_SCHEME, DEFAULT_LAYOUT, plan=PolkitPlan.TEMPLATE)
    unit = build_job_unit(TWO_WAY_SCHEME, DEFAULT_LAYOUT)
    assert unit.unit_name == "cortex-job@.service"
    assert rule.unit_pattern.startswith("^cortex-job@")
    assert evaluate_polkit(
        rule, user=rule.subject_account, action_id=permgen.POLKIT_ACTION,
        unit="cortex-job@abc.service", verb="start",
    ) == "YES"


@pytest.mark.parametrize("plan", ALL_PLANS, ids=lambda p: p.value)
def test_polkit_rule_has_exactly_one_grant(plan) -> None:
    """審查者的一眼結論：全檔只有一個 YES 出口。"""
    rule = build_polkit_rule(TWO_WAY_SCHEME, DEFAULT_LAYOUT, plan=plan)
    assert rule.content.count("polkit.Result.YES") == 1
    assert rule.content.count("polkit.addRule(") == 1
    assert 'action.id !== "org.freedesktop.systemd1.manage-units"' in rule.content
    assert "if (!unit || !verb)" in rule.content


def test_template_plan_documents_the_transient_closure() -> None:
    """B 方案必須明寫「為何 transient unit 一律拒」——那是提權的封口。"""
    rule = build_polkit_rule(TWO_WAY_SCHEME, DEFAULT_LAYOUT, plan=PolkitPlan.TEMPLATE)
    assert "StartTransientUnit" in rule.content
    for forbidden in permgen.POLKIT_FORBIDDEN_PROPERTIES:
        assert forbidden in rule.content, forbidden
    assert rule.residual_risks == (), "B 方案在 OS 層無殘餘（User= 由 root-owned 檔強制）"


def test_transient_plan_states_its_residual_risk_honestly() -> None:
    """A 方案的殘餘風險必須寫在規則檔裡：polkit 看不到 User=／--uid=。"""
    rule = build_polkit_rule(TWO_WAY_SCHEME, DEFAULT_LAYOUT, plan=PolkitPlan.TRANSIENT)
    assert rule.residual_risks, "A 方案必須明列殘餘風險"
    joined = " ".join(rule.residual_risks)
    assert "User=" in joined
    assert "同 UID" in joined
    # 二分方案下 reviewer／planner 與 Manager 併帳，故持有同一個 grant。
    assert "reviewer" in joined and "planner" in joined
    for risk in rule.residual_risks:
        assert risk in rule.content, "殘餘風險必須逐條出現在規則檔開頭"
    # 指出替代方案，operator 才有得選。
    assert "--template" in rule.content


def test_three_way_transient_risk_drops_the_model_persona_clause() -> None:
    """三分下 reviewer／planner 不再與 Manager 併帳——那條風險自動消失。"""
    rule = build_polkit_rule(THREE_WAY_SCHEME, DEFAULT_LAYOUT, plan=PolkitPlan.TRANSIENT)
    joined = " ".join(rule.residual_risks)
    assert "User=" in joined                 # polkit 的結構限制仍在
    assert "reviewer" not in joined          # 但併帳那條不再成立


def _matrix(prefix: str):
    ok = f"{prefix}abc-1.service"
    return [
        (ok, "start", "YES"),
        (ok, "stop", "YES"),
        (None, None, "NO"),                       # transient 無明細／明細缺席
        ("run-u1234.service", None, "NO"),
        ("cortex-manager.service", "start", "NO"),
        ("sshd.service", "start", "NO"),
        (f"evil-{prefix}x.service", "start", "NO"),
        (f"{ok}.evil", "start", "NO"),
        (ok, "reload", "NO"),
        (ok, "mask", "NO"),
    ]


@pytest.mark.parametrize("plan", ALL_PLANS, ids=lambda p: p.value)
def test_polkit_decision_matrix(plan) -> None:
    """polkit 無法本機執行——以規則常數的 Python 鏡像測產生邏輯。"""
    rule = build_polkit_rule(TWO_WAY_SCHEME, DEFAULT_LAYOUT, plan=plan)
    prefix = "cortex-job-" if plan is PolkitPlan.TRANSIENT else "cortex-job@"
    for unit, verb, expected in _matrix(prefix):
        got = evaluate_polkit(
            rule, user="cortex-svc", action_id=permgen.POLKIT_ACTION, unit=unit, verb=verb,
        )
        assert got == expected, (plan.value, unit, verb, got)
    # 其他 action：svc 的一切其他 polkit 面一律拒。
    for action_id in ("org.freedesktop.systemd1.reload-daemon",
                      "org.freedesktop.login1.power-off"):
        assert evaluate_polkit(
            rule, user="cortex-svc", action_id=action_id, unit="x.service", verb="start",
        ) == "NO"
    # 其他 subject 不受本規則干涉。
    for user in ("operator", "cortex-builder", "root"):
        assert evaluate_polkit(
            rule, user=user, action_id=permgen.POLKIT_ACTION,
            unit=f"{prefix}abc.service", verb="start",
        ) == "NOT_HANDLED"


@pytest.mark.parametrize("plan", ALL_PLANS, ids=lambda p: p.value)
def test_plans_never_authorise_the_other_plans_unit_shape(plan) -> None:
    """A 的規則不放行 B 的模板實例，反之亦然——兩案不得互相擴大授權面。"""
    rule = build_polkit_rule(TWO_WAY_SCHEME, DEFAULT_LAYOUT, plan=plan)
    other = "cortex-job@abc.service" if plan is PolkitPlan.TRANSIENT else "cortex-job-abc.service"
    assert evaluate_polkit(
        rule, user="cortex-svc", action_id=permgen.POLKIT_ACTION, unit=other, verb="start",
    ) == "NO"


@pytest.mark.parametrize("plan", ALL_PLANS, ids=lambda p: p.value)
def test_polkit_mirror_and_rule_text_share_the_same_constants(plan) -> None:
    """鏡像不可與規則檔漂移：verbs 與 unit regex 必須逐字出現在 JS 內。"""
    rule = build_polkit_rule(TWO_WAY_SCHEME, DEFAULT_LAYOUT, plan=plan)
    for verb in rule.allowed_verbs:
        assert f'verb !== "{verb}"' in rule.content
    assert f"/{rule.unit_pattern}/.test(unit)" in rule.content
    assert f'subject.user !== "{rule.subject_account}"' in rule.content


def test_transient_unit_properties_share_the_template_hardening() -> None:
    """A 方案的 --property= 清單與 B 方案模板 unit 同源（同一加固表＋同一份 RWP）。"""
    props = permgen.transient_unit_properties(TWO_WAY_SCHEME, DEFAULT_LAYOUT)
    unit = build_job_unit(TWO_WAY_SCHEME, DEFAULT_LAYOUT)
    for key, value, _why in permgen._HARDENING:
        assert f"--property={key}={value}" in props, key
    for rwp in unit.read_write_paths:
        assert f"--property=ReadWritePaths={rwp}" in props, rwp
    assert all(p.startswith("--property=") for p in props)


# ---------------------------------------------------------------------------
# 非執行保證（維持 permgen 的無特權靜態測試不變式）
# ---------------------------------------------------------------------------

def test_generators_return_strings_only() -> None:
    unit = build_manager_unit(TWO_WAY_SCHEME, DEFAULT_LAYOUT)
    job = build_job_unit(TWO_WAY_SCHEME, DEFAULT_LAYOUT)
    rule = build_polkit_rule(TWO_WAY_SCHEME, DEFAULT_LAYOUT)
    for blob in (unit.content, job.content, rule.content):
        assert isinstance(blob, str) and blob


def test_permgen_never_touches_the_filesystem() -> None:
    """靜態保證：新增的 unit／polkit 產生器仍不含任何 IO 或執行面。"""
    src = inspect.getsource(permgen)
    for forbidden in ("subprocess", "os.system", "os.chown", "os.chmod",
                      "open(", "write_text", "mkdir", "shutil"):
        assert forbidden not in src, forbidden


def test_scaffold_directories_are_command_strings_only() -> None:
    dirs = DEFAULT_LAYOUT.scaffold_directories(TWO_WAY_SCHEME)
    assert dirs
    seen = set()
    for path, owner, group, mode in dirs:
        assert path.startswith("/")
        assert path not in seen, f"骨架目錄重複：{path}"
        seen.add(path)
        assert owner and group
        assert 0 <= mode <= 0o777
    # 保護資產的父層一律 root 擁有（父目錄可寫者能 unlink/rename 子物件）。
    by_path = {p: (o, m) for p, o, _g, m in dirs}
    assert by_path[DEFAULT_LAYOUT.builder_home][0] == "root"
    assert by_path[f"{DEFAULT_LAYOUT.builder_home}/.codex"][0] == "root"
    assert by_path[DEFAULT_LAYOUT.home_of(TWO_WAY_SCHEME.durable_state_owner)][0] == "root"
    assert by_path[DEFAULT_LAYOUT.deploy_root][0] == "root"


def test_commands_with_real_layout_have_no_placeholder_left() -> None:
    """runbook 引用形式：帶 layout 後輸出不再有 <PATH:...> placeholder。"""
    # #626：命令輸出前必須先把部署決定型 principal 對應到真實帳號，否則 fail-closed。
    plan = generate_plan(TWO_WAY_SCHEME.with_principal_accounts({
        Principal.OPERATOR: "cortex-ops",
        Principal.EXTERNAL: "cortex-outbox",
    }))
    lines = permgen.plan_to_commands(plan, path_of=permgen.asset_paths())
    commands = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    assert commands
    assert not any("<PATH:" in ln for ln in commands)
    # per-job 資產以註解形式輸出（可讀、不會被誤執行）。
    for line in lines:
        if permgen.PER_JOB_SEGMENT in line:
            assert line.lstrip().startswith("#"), line


def test_custom_layout_needs_no_code_change() -> None:
    """layout 是純 config：換部署位置不必改產生器一行程式碼。"""
    alt = PathLayout(
        agents_root="/srv/cortex",
        worktree_root="/srv/cortex/wt",
        deploy_root="/usr/local/cortex",
        instance="alpha",
    )
    unit = build_manager_unit(TWO_WAY_SCHEME, alt)
    assert unit.unit_name == "alpha-manager.service"
    assert unit.exec_start == "/usr/local/cortex/venv/bin/cortex service run"
    assert all(p.startswith(("/srv/cortex", "/var/lib/cortex-svc")) for p in unit.read_write_paths)
    assert build_polkit_rule(TWO_WAY_SCHEME, alt, plan=PolkitPlan.TRANSIENT) \
        .unit_pattern.startswith("^alpha-job-")
    assert build_polkit_rule(TWO_WAY_SCHEME, alt, plan=PolkitPlan.TEMPLATE) \
        .unit_pattern.startswith("^alpha-job@")
