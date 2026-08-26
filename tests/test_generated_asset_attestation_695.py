"""#695（RED）：generated asset attestation 必須補齊四分 gate 與 gh 憑證面。"""

from __future__ import annotations

import hashlib

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
