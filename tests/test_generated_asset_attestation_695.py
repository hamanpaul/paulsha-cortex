"""#695（RED）：generated asset attestation 必須補齊四分 gate 與 gh 憑證面。"""

from __future__ import annotations

import hashlib
import grp
import os
import pwd
from pathlib import Path

from paulsha_cortex.trust_root import permgen
from paulsha_cortex.trust_root.permgen import (
    DEFAULT_LAYOUT,
    FOUR_WAY_SCHEME,
    JIT_PROFILE,
    Principal,
    THREE_WAY_SCHEME,
    build_job_unit,
    build_manager_unit,
    build_monitor_unit,
    generate_plan,
)

SOURCE_LAYOUT = DEFAULT_LAYOUT.with_source_repo_slugs(("paulsha-cortex",))
RUNTIME_PLACEHOLDERS = {
    "manager-gh-credential": "oauth_token=gho_local_test\n",
    "manager-gh-config": "git_protocol: https\neditor: vim\n",
}


class _LocalAttestationScheme:
    def __init__(self, *, account: str, group: str) -> None:
        self.scheme_id = "local-attestation"
        self.deploy_account = account
        self.durable_state_owner = account
        self.operator_account = account
        self.external_reader_account = account
        self._group = group
        self._accounts = {
            principal: account for principal in FOUR_WAY_SCHEME.account_of
        }

    def resolve(self, principal: Principal) -> str | None:
        if principal is Principal.INSTALLER:
            return self.deploy_account
        option = permgen.PRINCIPAL_ACCOUNT_OPTION_BY_PRINCIPAL.get(principal)
        if option is not None:
            return getattr(self, option.field_name)
        return self._accounts.get(principal)

    def group_of(self, account: str) -> str:
        return self._group

    def declared_accounts(self) -> frozenset[str]:
        return frozenset(
            {
                self.deploy_account,
                self.durable_state_owner,
                self.operator_account,
                self.external_reader_account,
                *self._accounts.values(),
            }
        )


def _field(record: object, name: str) -> object:
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def _mode_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return format(value, "04o")
    return str(value)


def _attestation_inventory(scheme) -> dict[str, object]:
    builder = getattr(permgen, "build_attestation_inventory", None)
    assert builder is not None, (
        "missing build_attestation_inventory(): generated asset attestation has no "
        "machine-readable inventory for Manager/Monitor units, four-way gate "
        "templates, or Manager GitHub credential surfaces"
    )
    records = builder(scheme=scheme, layout=SOURCE_LAYOUT)
    assert records, "attestation inventory must not be empty"
    return {_field(record, "install_path"): record for record in records}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _local_inventory(tmp_path: Path) -> tuple[tuple[object, ...], permgen.PathLayout]:
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    scheme = _LocalAttestationScheme(account=account, group=group)
    layout = permgen.PathLayout(
        agents_root=str(tmp_path / "agents"),
        home_root=str(tmp_path / "home"),
        deploy_root=str(tmp_path / "deploy"),
    ).with_source_repo_slugs(("paulsha-cortex",))
    deduped_by_path = {
        record.install_path: record
        for record in permgen.build_attestation_inventory(scheme=scheme, layout=layout)
    }
    return tuple(deduped_by_path.values()), layout


def _materialize_runtime(records: tuple[object, ...], *, install_root: Path) -> None:
    for record in records:
        path = install_root / Path(str(_field(record, "install_path"))).relative_to("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        content = _field(record, "content")
        if content is None:
            content = RUNTIME_PLACEHOLDERS[str(_field(record, "asset_id"))]
        path.write_text(str(content), encoding="utf-8")
        mode = _field(record, "mode")
        os.chmod(path, int(str(mode), 8) if not isinstance(mode, int) else mode)


def test_attestation_inventory_promotes_four_way_gate_units_but_keeps_three_way_gate_free() -> None:
    """#695：由三分升到四分後，attestation inventory 不能再少 gate 那兩份模板 unit。"""

    four_way = _attestation_inventory(FOUR_WAY_SCHEME)
    three_way = _attestation_inventory(THREE_WAY_SCHEME)
    expected_units = (
        build_manager_unit(FOUR_WAY_SCHEME, SOURCE_LAYOUT),
        build_monitor_unit(FOUR_WAY_SCHEME, SOURCE_LAYOUT),
        build_job_unit(FOUR_WAY_SCHEME, SOURCE_LAYOUT, Principal.GATE),
        build_job_unit(FOUR_WAY_SCHEME, SOURCE_LAYOUT, Principal.GATE, profile=JIT_PROFILE),
    )

    for unit in expected_units:
        record = four_way.get(unit.install_path)
        assert record is not None, unit.install_path
        assert _field(record, "sha256") == _sha256(unit.content), unit.install_path

    gate_paths = {
        expected_units[-2].install_path,
        expected_units[-1].install_path,
    }
    assert gate_paths.isdisjoint(three_way), "three-way inventory must stay gate-free"


def test_attestation_inventory_lists_manager_github_surfaces_without_emitting_raw_content() -> None:
    """#695：gh 兩個面都要進 inventory，但只准出 metadata / hash，不准出內容。"""

    inventory = _attestation_inventory(FOUR_WAY_SCHEME)
    plan = generate_plan(FOUR_WAY_SCHEME)
    expected = {
        "manager-gh-credential": SOURCE_LAYOUT.gh_credential_of(SOURCE_LAYOUT.manager_account),
        "manager-gh-config": SOURCE_LAYOUT.gh_settings_of(SOURCE_LAYOUT.manager_account),
    }

    for asset_id, install_path in expected.items():
        record = inventory.get(install_path)
        assert record is not None, asset_id
        entry = plan.by_id(asset_id)
        assert _field(record, "owner") == entry.owner, asset_id
        assert _mode_text(_field(record, "mode")) == format(entry.mode, "04o"), asset_id
        assert _field(record, "content") in (None, ""), asset_id


def test_attestation_inventory_json_projection_redacts_generated_asset_content() -> None:
    """#695：inventory JSON 只能保留 metadata / mode / hash，不能帶 generated 內容。"""

    inventory = _attestation_inventory(FOUR_WAY_SCHEME)
    manager_unit = build_manager_unit(FOUR_WAY_SCHEME, SOURCE_LAYOUT)
    record = inventory[manager_unit.install_path]

    assert _field(record, "content") == manager_unit.content
    rendered = record.to_dict()
    assert rendered["sha256"] == _sha256(manager_unit.content)
    assert "content" not in rendered


def test_attestation_runtime_compare_passes_and_keeps_runtime_surfaces_redacted(
    tmp_path: Path,
) -> None:
    inventory, layout = _local_inventory(tmp_path)
    install_root = tmp_path / "runtime"
    _materialize_runtime(inventory, install_root=install_root)

    result = permgen.compare_attestation_runtime(inventory, install_root=install_root)

    assert result.passed
    assert result.errors == ()
    assert result.warnings == ()
    observed = {record.install_path: record for record in result.observed}
    for asset_id in ("manager-gh-credential", "manager-gh-config"):
        record = observed[layout.asset_paths()[asset_id]]
        assert _field(record, "sha256")
        assert _field(record, "content") in (None, ""), asset_id


def test_attestation_runtime_compare_warns_on_comment_only_drift(tmp_path: Path) -> None:
    inventory, _ = _local_inventory(tmp_path)
    install_root = tmp_path / "runtime"
    _materialize_runtime(inventory, install_root=install_root)

    manager_unit = install_root / Path(str(_field(inventory[0], "install_path"))).relative_to("/")
    manager_unit.write_text(
        "# local operator comment\n" + manager_unit.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = permgen.compare_attestation_runtime(inventory, install_root=install_root)

    assert result.passed
    assert result.errors == ()
    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert warning.kind == "comment-only-drift"
    assert warning.install_path == str(_field(inventory[0], "install_path"))


def test_attestation_runtime_compare_fails_closed_when_manager_gh_surface_missing(
    tmp_path: Path,
) -> None:
    inventory, layout = _local_inventory(tmp_path)
    install_root = tmp_path / "runtime"
    _materialize_runtime(inventory, install_root=install_root)
    (install_root / Path(layout.asset_paths()["manager-gh-credential"]).relative_to("/")).unlink()

    result = permgen.compare_attestation_runtime(inventory, install_root=install_root)

    assert not result.passed
    assert any(
        issue.kind == "missing-file"
        and issue.install_path == layout.asset_paths()["manager-gh-credential"]
        for issue in result.errors
    )


def test_attestation_runtime_compare_stops_at_decode_failed_but_keeps_observed_hash(
    tmp_path: Path,
) -> None:
    inventory, _ = _local_inventory(tmp_path)
    install_root = tmp_path / "runtime"
    _materialize_runtime(inventory, install_root=install_root)
    generated_record = next(record for record in inventory if _field(record, "content") is not None)
    install_path = str(_field(generated_record, "install_path"))
    target = install_root / Path(install_path).relative_to("/")
    payload = b"\xff\xfe\xfdnot-utf8"
    target.write_bytes(payload)

    result = permgen.compare_attestation_runtime(inventory, install_root=install_root)

    assert not result.passed
    assert any(
        issue.kind == "decode-failed" and issue.install_path == install_path
        for issue in result.errors
    )
    assert not any(
        issue.kind == "content-mismatch" and issue.install_path == install_path
        for issue in result.errors
    )
    observed = {record.install_path: record for record in result.observed}[install_path]
    assert _field(observed, "sha256") == hashlib.sha256(payload).hexdigest()
    assert _field(observed, "content") in (None, "")
