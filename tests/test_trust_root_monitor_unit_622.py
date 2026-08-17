"""issue #622：monitor 的 system-level unit（`trust_root unit three-way --monitor`）。

Phase 2b M1 之後 permgen 只產生 Manager unit，實機切換後 cortex instance **完全沒有
monitor**：舊的 `--user` monitor 以操作者身分跑、指向舊 `~/.agents`，起回來只會雙寫
且寫不進 `0700 cortex-manager` 的新樹；`monitor-event-spool` 因此只有生產端
（builder 的 `wx` ACL）沒有消費端。

本檔釘住四條，讓那個狀態不可能再出現，也不可能單邊漂移回去：

1. `User=` 是 durable_state_owner（UID 方案表：`cortex-manager`＝Manager ＋ monitor）；
2. 加固欄位與 Manager unit **集合相等**（任一邊加減一項就紅）；
3. `ReadWritePaths` 是 Manager 的**真子集**，且內容恰為 monitor persona 的登記表面；
4. `ExecStart` 指向的 CLI verb 真的存在（比照 `tests/test_service_run_verb.py`——
   #618 就是這條契約只存在於產生器端而斷掉）。
"""

from __future__ import annotations

import pytest

from paulsha_cortex import cli
from paulsha_cortex.trust_root import permgen
from paulsha_cortex.trust_root.permgen import (
    DEFAULT_LAYOUT,
    THREE_WAY_SCHEME,
    TWO_WAY_SCHEME,
    build_manager_unit,
    build_monitor_unit,
    generate_plan,
)
from paulsha_cortex.trust_root.registry import (
    ASSET_REGISTRY,
    IngressKind,
    Principal,
)

ALL_SCHEMES = [TWO_WAY_SCHEME, THREE_WAY_SCHEME]


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def _directives(content: str) -> dict[str, str]:
    """unit 內容 → `{key: value}`（略過註解與 section 標頭；重複 key 取最後一筆）。"""
    result: dict[str, str] = {}
    for line in content.splitlines():
        if not line or line.startswith("#") or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# 身分：與 Manager 同帳號，且永不是 root
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_unit_runs_as_the_durable_state_owner(scheme) -> None:
    """UID 方案表：`cortex-manager`＝Manager ＋ monitor。

    同帳號不是巧合而是必要條件——`/var/lib/cortex/monitor` 是 `0700
    durable_state_owner`，monitor 以任何其他身分跑都寫不進自己的 state 樹。
    """
    unit = build_monitor_unit(scheme, DEFAULT_LAYOUT)
    assert unit.account == scheme.durable_state_owner
    assert f"User={scheme.durable_state_owner}" in unit.content
    assert f"Group={scheme.group_of(scheme.durable_state_owner)}" in unit.content
    assert "User=root" not in unit.content
    assert unit.unit_name == "cortex-monitor.service"
    assert unit.install_path == "/etc/systemd/system/cortex-monitor.service"


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_and_manager_share_the_service_account(scheme) -> None:
    manager = build_manager_unit(scheme, DEFAULT_LAYOUT)
    monitor = build_monitor_unit(scheme, DEFAULT_LAYOUT)
    assert monitor.account == manager.account


def test_three_way_scheme_moves_the_monitor_account_too() -> None:
    """換 scheme 只換 config：monitor unit 的身分與 HOME 跟著走，程式碼零改動。"""
    assert build_monitor_unit(TWO_WAY_SCHEME, DEFAULT_LAYOUT).account == "cortex-svc"
    three = build_monitor_unit(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    assert three.account == "cortex-manager"
    assert "Environment=HOME=/var/lib/cortex-manager\n" in three.content
    assert "Environment=XDG_CACHE_HOME=/var/lib/cortex-manager/cache\n" in three.content


# ---------------------------------------------------------------------------
# 加固段：與 Manager unit 集合相等（不得單邊漂移）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_hardening_is_set_equal_to_manager(scheme) -> None:
    """集合比對而非逐項列舉：日後往 `_HARDENING` 加一項，兩邊必須同時拿到。

    只驗「monitor ⊇ manager」會漏掉 monitor 多開一項的情況，反之亦然，故取等式。
    """
    manager = _directives(build_manager_unit(scheme, DEFAULT_LAYOUT).content)
    monitor = _directives(build_monitor_unit(scheme, DEFAULT_LAYOUT).content)
    keys = {key for key, _value, _why in permgen._HARDENING}
    assert {k: v for k, v in monitor.items() if k in keys} == {
        k: v for k, v in manager.items() if k in keys
    }
    # 加固表本身也要完整落地（避免兩邊「一致地都缺」）。
    assert keys <= set(monitor)


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_unit_carries_every_hardening_directive_with_a_comment(scheme) -> None:
    """每項加固都要帶「為何」的註解——可審查性要求，與 Manager unit 同標準。"""
    lines = build_monitor_unit(scheme, DEFAULT_LAYOUT).content.splitlines()
    for key, value, why in permgen._HARDENING:
        directive = f"{key}={value}"
        assert directive in lines, directive
        index = lines.index(directive)
        assert lines[index - 1].startswith("# "), directive
        assert why.split("：")[0][:6] in lines[index - 1], directive


# ---------------------------------------------------------------------------
# ReadWritePaths：由登記表機械導出，且嚴格窄於 Manager
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_read_write_paths_are_strictly_narrower_than_manager(scheme) -> None:
    """核心驗收：真子集（是子集**且不相等**）。

    同帳號意味著單看帳號兩者可寫面相同；這條測試就是「persona 過濾真的有作用」
    的機械證據——拿掉 `principals=` 過濾即紅。
    """
    manager = set(build_manager_unit(scheme, DEFAULT_LAYOUT).read_write_paths)
    monitor = set(build_monitor_unit(scheme, DEFAULT_LAYOUT).read_write_paths)
    assert monitor < manager, (sorted(monitor), sorted(manager))


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_read_write_paths_exclude_the_manager_only_trees(scheme) -> None:
    """monitor 不擁有也不消費的樹一律不得出現（列舉 issue 點名的那幾棵）。"""
    unit = build_monitor_unit(scheme, DEFAULT_LAYOUT)
    for forbidden in (
        DEFAULT_LAYOUT.coordinator_root,
        DEFAULT_LAYOUT.specs_root,
        DEFAULT_LAYOUT.worktree_root,
        DEFAULT_LAYOUT.control_root,
        DEFAULT_LAYOUT.skill_registry_root,
        DEFAULT_LAYOUT.project_config_root,
        DEFAULT_LAYOUT.dispatch_log_root,
        DEFAULT_LAYOUT.deploy_root,
    ):
        assert not any(_within(forbidden, rwp) for rwp in unit.read_write_paths), (
            scheme.scheme_id, forbidden, unit.read_write_paths,
        )


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_read_write_paths_cover_every_monitor_asset(scheme) -> None:
    """無遺漏：登記表中每一項 monitor persona 需寫的資產都被某條 RWP 覆蓋。"""
    plan = generate_plan(scheme)
    unit = build_monitor_unit(scheme, DEFAULT_LAYOUT)
    targets = permgen.required_write_targets(
        plan, DEFAULT_LAYOUT, scheme.durable_state_owner,
        principals=permgen.MONITOR_PRINCIPALS,
    )
    assert set(targets) == {
        "runtime-run-tree",
        "monitor-state-tree",
        "monitor-work-items-snapshot",
        "monitor-github-sync-cursor",
        "monitor-event-spool",
    }, sorted(targets)
    for asset_id, target in targets.items():
        assert any(_within(target, rwp) for rwp in unit.read_write_paths), (asset_id, target)


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_read_write_paths_have_no_redundant_entry(scheme) -> None:
    """無多餘：拿掉任何一條登記表導出的條目就會有資產失去覆蓋。

    明示宣告的 extras（HOME cache）是唯一例外，且必須附理由。
    """
    plan = generate_plan(scheme)
    account = scheme.durable_state_owner
    unit = build_monitor_unit(scheme, DEFAULT_LAYOUT)
    targets = permgen.required_write_targets(
        plan, DEFAULT_LAYOUT, account, principals=permgen.MONITOR_PRINCIPALS
    )
    extras = {e.path for e in DEFAULT_LAYOUT.monitor_extra_write_paths(account)}
    assert all(e.reason for e in DEFAULT_LAYOUT.monitor_extra_write_paths(account))

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
def test_monitor_can_consume_the_event_spool(scheme) -> None:
    """#622 的契約面：spool 有 builder 的 `wx` 生產端，也必須有可 unlink 的消費端。

    消費＝讀完 unlink，unlink 需要**容器目錄**的寫入權；ProtectSystem=strict 下
    沒有涵蓋它的 RWP 就等於只累積不消費。
    """
    spool = DEFAULT_LAYOUT.asset_paths()["monitor-event-spool"]
    unit = build_monitor_unit(scheme, DEFAULT_LAYOUT)
    assert any(_within(spool, rwp) for rwp in unit.read_write_paths), unit.read_write_paths


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_read_write_paths_carry_the_registry_provenance_comment(scheme) -> None:
    """每條 RWP 上方保留「涵蓋哪些登記表資產」的註解（比照 Manager unit）。"""
    lines = build_monitor_unit(scheme, DEFAULT_LAYOUT).content.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("ReadWritePaths="):
            continue
        assert lines[index - 1].startswith("#   涵蓋："), line
        assert lines[index - 1].removeprefix("#   涵蓋：").strip() not in ("", "（無）"), line


def test_read_write_paths_shift_when_the_monitor_registry_face_shrinks() -> None:
    """機械性反證：把 monitor 需寫的一項從登記表拿掉，導出的 RWP 必須跟著變。

    取 `runtime-run-tree`（monitor 的 unix socket）而非 monitor state 族——後者四項
    共用同一個容器目錄，拿掉一項不會讓那條 RWP 消失，證不出機械性。
    """
    def _paths(assets) -> tuple[str, ...]:
        return permgen.read_write_paths(
            generate_plan(THREE_WAY_SCHEME, assets), DEFAULT_LAYOUT,
            THREE_WAY_SCHEME.durable_state_owner,
            principals=permgen.MONITOR_PRINCIPALS,
        )

    trimmed = tuple(a for a in ASSET_REGISTRY if a.asset_id != "runtime-run-tree")
    assert DEFAULT_LAYOUT.run_root in _paths(ASSET_REGISTRY)
    assert DEFAULT_LAYOUT.run_root not in _paths(trimmed)
    # 其餘 monitor 面不受影響（spool 的消費端不因此消失）。
    spool = DEFAULT_LAYOUT.asset_paths()["monitor-event-spool"]
    assert any(_within(spool, rwp) for rwp in _paths(trimmed)), _paths(trimmed)


def test_principal_filter_is_derived_from_the_registry_only() -> None:
    """導出規則只有兩條，且兩條都直接讀登記表欄位——沒有硬編碼的 asset_id 清單。"""
    monitor = permgen.MONITOR_PRINCIPALS
    for asset in ASSET_REGISTRY:
        expected = any(p in monitor for p in asset.writers) or (
            asset.ingress_kind is IngressKind.INTERPROCESS
            and any(p in monitor for p in asset.readers)
        )
        assert permgen.principal_needs_write(asset, monitor) is expected, asset.asset_id


def test_unfiltered_derivation_keeps_the_manager_behaviour() -> None:
    """`principals=None`＝帳號全集：Manager unit 的既有導出行為一位元都沒變。"""
    plan = generate_plan(THREE_WAY_SCHEME)
    account = THREE_WAY_SCHEME.durable_state_owner
    unfiltered = permgen.required_write_targets(plan, DEFAULT_LAYOUT, account)
    filtered = permgen.required_write_targets(
        plan, DEFAULT_LAYOUT, account, principals=permgen.MONITOR_PRINCIPALS
    )
    assert set(filtered) < set(unfiltered)
    assert set(unfiltered) > {"coordinator-root-tree", "dispatch-specs-tree"}


# ---------------------------------------------------------------------------
# ExecStart 契約鎖（比照 tests/test_service_run_verb.py）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_execstart_lives_in_the_root_owned_deploy_tree(scheme) -> None:
    unit = build_monitor_unit(scheme, DEFAULT_LAYOUT)
    assert unit.exec_start.startswith(DEFAULT_LAYOUT.deploy_root + "/")
    assert unit.exec_start == f"{DEFAULT_LAYOUT.venv_root}/bin/cortex monitor"
    assert f"ExecStart={unit.exec_start}" in unit.content
    assert f"WorkingDirectory={DEFAULT_LAYOUT.agents_root}" in unit.content


def test_monitor_execstart_shares_the_manager_entry_point_shape() -> None:
    """與 #618／PR #619 定下的形態一致：`<venv>/bin/cortex <verb>`，不是 `python -m`。"""
    manager = build_manager_unit(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    monitor = build_monitor_unit(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    prefix = f"{DEFAULT_LAYOUT.venv_root}/bin/cortex "
    assert manager.exec_start.startswith(prefix)
    assert monitor.exec_start.startswith(prefix)
    assert "python" not in monitor.exec_start
    assert " -m " not in monitor.exec_start


def test_monitor_execstart_verb_is_actually_routed_by_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """契約鎖：產生器的 ExecStart 與 CLI 實際提供的 verb 必須同步。

    #618 就是這條斷掉——unit 一 start 就以 `unsupported service command` 失敗。
    這裡不比字串常數，而是**真的走一次 `cli.main`**，確認該 verb 會抵達 monitor
    的進入點。
    """
    unit = build_monitor_unit(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    verb = unit.exec_start.split("/bin/cortex ", 1)[1].split()
    assert verb == ["monitor"]

    seen: dict[str, object] = {}

    def fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = list(argv or [])
        return 0

    monkeypatch.setattr(
        "paulsha_cortex.monitor.__main__.main", fake_main, raising=True
    )
    assert cli.main([*verb, "--once"]) == 0
    assert seen["argv"] == ["--once"]


def test_monitor_entry_point_module_exists_and_is_long_running_by_default() -> None:
    """進入點真的存在，且**不帶旗標時**是長駐服務（`Type=simple` 的前提）。"""
    from paulsha_cortex.monitor.__main__ import build_parser, main

    assert callable(main)
    parser = build_parser()
    assert parser.prog == "cortex monitor"
    # `--once` 才是單次掃描；unit 不帶它，因此走 `ProjectMonitorService.run_forever()`。
    assert parser.parse_args([]).once is False
    assert parser.parse_args(["--once"]).once is True


# ---------------------------------------------------------------------------
# env／fail-closed／產生器純度
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_environment_file_is_fail_closed(scheme) -> None:
    """無 `-` 前綴＝缺檔即拒絕啟動，不得靜默落回 `$HOME/.agents`（#622 的雙寫根因）。"""
    unit = build_monitor_unit(scheme, DEFAULT_LAYOUT)
    assert unit.environment_file == DEFAULT_LAYOUT.env_file
    assert f"EnvironmentFile={DEFAULT_LAYOUT.env_file}" in unit.content
    assert "EnvironmentFile=-" not in unit.content


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_shares_the_manager_environment_file(scheme) -> None:
    """同一份 instance-scoped env——兩個服務不可能解析到不同的 durable state 樹。"""
    assert (
        build_monitor_unit(scheme, DEFAULT_LAYOUT).environment_file
        == build_manager_unit(scheme, DEFAULT_LAYOUT).environment_file
    )


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_home_paths_come_from_the_layout_not_literals(scheme) -> None:
    """HOME／cache 由 `layout.home_of()`／`cache_of()` 導出，不得寫死字面量。"""
    account = scheme.durable_state_owner
    content = build_monitor_unit(scheme, DEFAULT_LAYOUT).content
    assert f"Environment=HOME={DEFAULT_LAYOUT.home_of(account)}\n" in content
    assert f"Environment=XDG_CACHE_HOME={DEFAULT_LAYOUT.cache_of(account)}\n" in content
    assert "/home/" not in content


def test_monitor_unit_is_deterministic() -> None:
    a = build_monitor_unit(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    b = build_monitor_unit(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    assert a.content == b.content
    assert a.to_dict() == b.to_dict()


def test_monitor_unit_needs_no_code_change_for_a_custom_layout() -> None:
    """換 layout 只換 config：unit 全面跟著走，產生器零改動。"""
    layout = permgen.PathLayout(
        agents_root="/srv/cx", worktree_root="/srv/cx/wt",
        deploy_root="/srv/deploy", instance="cx", home_root="/srv/home",
    )
    unit = build_monitor_unit(THREE_WAY_SCHEME, layout)
    assert unit.unit_name == "cx-monitor.service"
    assert unit.exec_start == "/srv/deploy/venv/bin/cortex monitor"
    assert unit.environment_file == "/srv/deploy/etc/cx-manager.env"
    assert "/srv/cx/monitor" in unit.read_write_paths
    assert "/var/lib" not in unit.content


def test_monitor_generator_is_strings_only() -> None:
    """產生器只回傳字串／結構化欄位——不得執行任何特權操作。"""
    unit = build_monitor_unit(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    assert isinstance(unit.content, str)
    assert all(isinstance(p, str) for p in unit.read_write_paths)
    assert all(isinstance(v, (str, list, type(None))) for v in unit.to_dict().values())


def test_cli_exposes_the_monitor_unit(capsys: pytest.CaptureFixture[str]) -> None:
    """`trust_root unit three-way --monitor` 是 runbook 引用的落檔來源。"""
    from paulsha_cortex.trust_root.__main__ import main

    assert main(["unit", "three-way", "--monitor"]) == 0
    out = capsys.readouterr().out
    assert out == build_monitor_unit(THREE_WAY_SCHEME, DEFAULT_LAYOUT).content
    assert "User=cortex-manager" in out
    # 預設仍是 manager（不打旗標不會意外拿到 monitor unit）。
    assert main(["unit", "three-way"]) == 0
    assert capsys.readouterr().out == build_manager_unit(
        THREE_WAY_SCHEME, DEFAULT_LAYOUT
    ).content


def test_monitor_principals_is_the_registry_persona_not_an_account() -> None:
    """過濾用的是 persona（`Principal.MONITOR`），不是帳號字串——換 scheme 不受影響。"""
    assert permgen.MONITOR_PRINCIPALS == frozenset({Principal.MONITOR})
