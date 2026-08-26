"""Phase 2 closeout runbook authority regressions."""
from __future__ import annotations

from pathlib import Path

from paulsha_cortex.trust_root import permgen


ROOT = Path(__file__).resolve().parents[1]
CURRENT = "docs/superpowers/runbooks/trust-root-transactional-install.md"
LEGACY = "docs/superpowers/runbooks/trust-root-phase2b-setup.md"


def test_current_runbook_requires_three_way_operator_confirmation() -> None:
    current = (ROOT / CURRENT).read_text(encoding="utf-8")

    assert "status: executable" in current
    assert "cortex_reported_plan_sha" in current
    assert "cortex_observed_plan_sha" in current
    assert "cortex_confirmed_plan_sha" in current
    assert 'read -r -p "Type the reviewed plan SHA-256: "' in current
    assert '"$cortex_reported_plan_sha" = "$cortex_observed_plan_sha"' in current
    assert '"$cortex_confirmed_plan_sha" = "$cortex_reported_plan_sha"' in current
    assert "sudo rm -rf /opt/cortex" not in current
    assert "sudo cp -a" not in current


def test_legacy_manual_runbook_is_explicitly_non_executable() -> None:
    legacy = (ROOT / LEGACY).read_text(encoding="utf-8")

    assert "status: historical" in legacy
    assert f"superseded_by: {CURRENT}" in legacy
    assert "不可執行" in legacy[:1600]


def test_generated_units_link_only_to_current_install_runbook() -> None:
    units = (
        permgen.build_manager_unit(permgen.FOUR_WAY_SCHEME).content,
        permgen.build_monitor_unit(permgen.FOUR_WAY_SCHEME).content,
        permgen.build_egress_proxy_unit(permgen.FOUR_WAY_SCHEME).content,
    )

    for content in units:
        assert f"Documentation=file://{CURRENT}" in content
        assert LEGACY not in content
