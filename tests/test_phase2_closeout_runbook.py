"""Phase 2 closeout runbook authority regressions."""
from __future__ import annotations

from pathlib import Path
import subprocess

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


def test_current_runbook_uses_one_root_owned_candidate_cli_for_plan_and_apply() -> None:
    current = (ROOT / CURRENT).read_text(encoding="utf-8")

    assert "cortex_cli=" in current
    assert '"$cortex_cli" install trust-root plan' in current
    assert "/usr/bin/sudo /usr/bin/env -i HOME=/root" in current
    assert '"$cortex_cli" install trust-root apply' in current
    assert "PYTHONNOUSERSITE=1" in current
    assert "root-owned" in current
    assert "--no-index" in current
    assert "--no-deps --only-binary=:all: --require-hashes" in current
    assert '--requirement "$cortex_bootstrap_requirements"' in current
    assert "actual_wheelhouse != declared_wheelhouse" in current
    assert "--prior-receipt" in current
    assert "sudo cortex install trust-root" not in current
    assert "\ncortex install trust-root plan" not in current


def test_current_runbook_does_not_use_ambient_plan_review_tools() -> None:
    current = (ROOT / CURRENT).read_text(encoding="utf-8")

    assert "PATH=/usr/bin:/bin\nexport PATH" in current
    assert '$(/usr/bin/python3 -I -S - "$cortex_plan_result"' in current
    assert '$(/usr/bin/sha256sum "$cortex_plan_path" | /usr/bin/awk' in current
    assert '$(/usr/bin/python3 -I -S - "$cortex_plan_path"' in current
    assert '/usr/bin/python3 -I -S -m json.tool "$cortex_plan_path"' in current


def test_current_runbook_makes_sealed_cli_readable_without_accepting_symlinks() -> None:
    current = (ROOT / CURRENT).read_text(encoding="utf-8")

    assert '/usr/bin/sudo /usr/bin/chmod -R u=rwX,go=rX "$cortex_input_root"' in current
    assert '/usr/bin/sudo /usr/bin/chmod -R u=rwX,go=rX "$cortex_bootstrap_root/venv"' in current
    assert '/usr/bin/sudo /usr/bin/test -L "$cortex_bootstrap_root/venv/lib64"' in current
    assert '/usr/bin/sudo /usr/bin/readlink "$cortex_bootstrap_root/venv/lib64"' in current
    assert '/usr/bin/sudo /usr/bin/unlink "$cortex_bootstrap_root/venv/lib64"' in current
    assert 'paths = [root, *sorted(root.rglob("*")' in current
    assert "qualification input contains a symlink" in current
    assert "unsafe candidate CLI tree object" in current
    assert 'test -z "$(find ' not in current


def test_current_runbook_verifies_release_assets_before_extracting_input() -> None:
    current = (ROOT / CURRENT).read_text(encoding="utf-8")

    archive_digest = (
        '"$cortex_install_input_asset_sha256" "$cortex_install_input_archive"'
    )
    qualification_digest = (
        '"$cortex_qualification_asset_sha256" "$cortex_qualification_manifest"'
    )
    extract = '/usr/bin/sudo /usr/bin/tar --extract --gzip --file "$cortex_install_input_archive"'
    bundle_digest = '"$cortex_bundle_sha256" "$cortex_bundle"'
    assert "GitHub Releases REST asset metadata" in current
    assert archive_digest in current
    assert qualification_digest in current
    assert 'document.get("profile") != "release"' in current
    assert 'document.get("status") != "passed"' in current
    assert 'path.parts[0] != "qualification-input"' in current
    assert 'not (member.isdir() or member.isfile())' in current
    assert current.index(archive_digest) < current.index(extract)
    assert current.index(qualification_digest) < current.index(extract)
    assert current.index(extract) < current.index(bundle_digest)
    assert "install-config.yaml" in current[: current.index(extract)]


def test_current_runbook_stops_only_present_units_and_restores_on_stop_failure() -> None:
    current = (ROOT / CURRENT).read_text(encoding="utf-8")

    assert "cortex_present_services=()" in current
    assert 'present = document["present_services"]' in current
    assert "maintenance-snapshot.json" in current
    assert "cortex_restore_active()" in current
    assert 'if ! /usr/bin/sudo /usr/bin/systemctl stop "$cortex_service"; then' in current
    assert '/usr/bin/systemctl stop "${cortex_services[@]}"' not in current


def test_current_runbook_isolates_python_and_privileged_authority_tools(
    tmp_path: Path,
) -> None:
    current = (ROOT / CURRENT).read_text(encoding="utf-8")

    assert "/usr/bin/python3 - " not in current
    assert "/usr/bin/python3 -c " not in current
    assert "/usr/bin/python3 -m " not in current
    assert current.count("/usr/bin/python3") == current.count(
        "/usr/bin/python3 -I -S"
    )
    for command in ("test", "stat", "sha256sum", "install", "tar", "systemctl"):
        assert f"sudo {command}" not in current

    sentinel = tmp_path / "ambient-imported"
    (tmp_path / "json.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["/usr/bin/python3", "-I", "-S", "-c", "import json"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert not sentinel.exists()


def test_current_runbook_restores_services_on_every_pre_apply_abort() -> None:
    current = (ROOT / CURRENT).read_text(encoding="utf-8")

    trap = "trap 'cortex_abort_restore \"$?\"' EXIT"
    stop = '/usr/bin/sudo /usr/bin/systemctl stop "$cortex_service"'
    digest = 'test "$(cortex_cli_tree_sha)" = "$cortex_sealed_cli_tree_sha"'
    apply = '"$cortex_cli" install trust-root apply'
    verify = "cortex_root_cli install trust-root verify"
    release = "cortex_release_maintenance_lease"
    disarm = "trap - EXIT INT TERM"
    assert "cortex_apply_attempted=0" in current
    assert "cortex_apply_succeeded" not in current
    assert 'trap \'exit 130\' INT' in current
    assert 'trap \'exit 143\' TERM' in current
    assert current.index(trap) < current.index(stop) < current.rindex(digest)
    assert current.rindex(digest) < current.index(apply) < current.index(verify)
    assert current.index(verify) < current.rindex(release) < current.rindex(disarm)
    assert "cortex_root_cli install trust-root rollback" in current
    assert '--receipt "$cortex_receipt_path"' in current
    assert '--maintenance-token "$cortex_maintenance_token"' in current
    assert "--only-incomplete" not in current


def test_current_runbook_holds_maintenance_lease_before_service_snapshot() -> None:
    current = (ROOT / CURRENT).read_text(encoding="utf-8")

    lease = "cortex_root_cli install trust-root lease"
    ready = 'document["maintenance_lease"] is not True'
    snapshot = 'present = document["present_services"]'
    stop = '/usr/bin/sudo /usr/bin/systemctl stop "$cortex_service"'
    verify = "cortex_root_cli install trust-root verify"
    release = "cortex_release_maintenance_lease"
    assert current.index(lease) < current.index(ready) < current.index(snapshot)
    assert current.index(snapshot) < current.index(stop) < current.index(verify)
    assert current.index(verify) < current.rindex(release)
    assert "CORTEX_MAINTENANCE_LEASE_PID" in current
    assert "exec {cortex_maintenance_write_fd}>&-" in current
    for field in (
        "maintenance_lease",
        "maintenance_token",
        "plan_sha256",
        "present_services",
        "previously_active",
        "receipt_path",
        "snapshot_path",
    ):
        assert f'"{field}"' in current
    assert 'token = document["maintenance_token"]' in current
    # The optional builder/AGY import adds one plan-gated mutation command;
    # the default four credential imports remain unchanged.
    assert current.count('--maintenance-token "$cortex_maintenance_token"') == 9
    assert "cortex_acquire_maintenance_lease()" in current
    assert current.count("cortex_acquire_maintenance_lease") == 2
    receipt_binding = 'document["receipt_path"] != sys.argv[3]'
    assert current.index(ready) < current.index(receipt_binding) < current.index(snapshot)
    assert "若 coproc 先死亡" in current
    assert "/usr/bin/printf 'complete\\n'" in current
    assert "install trust-root lease-release" in current


def test_current_runbook_has_exact_plan_hard_crash_recovery() -> None:
    current = (ROOT / CURRENT).read_text(encoding="utf-8")

    assert "## 6. Hard-crash recovery" in current
    recovery = current.split("## 6. Hard-crash recovery", 1)[1].split(
        "## 7. Deployment canary", 1
    )[0]
    assert "不要重跑第 1–2 節" in recovery
    assert "重生 byte-identical plan" not in recovery
    assert "cortex_installer_root/plans/$cortex_confirmed_plan_sha.json" in recovery
    assert "durable reviewed plan digest mismatch" in recovery
    assert "cortex_recovery_cli_tree_sha" in recovery
    assert "cortex_root_cli install trust-root recover" in current
    assert '--plan "$cortex_plan_path"' in current
    assert '--confirm-sha256 "$cortex_confirmed_plan_sha"' in current
    assert "maintenance_recovered=true" in current
    assert "不得改用 tokenless rollback" in current
    assert "run-${cortex_receipt_nonce}.json" in current


def test_current_runbook_durably_publishes_reviewed_plan_before_lease() -> None:
    current = (ROOT / CURRENT).read_text(encoding="utf-8")

    confirmation = 'test "$cortex_confirmed_plan_sha" = "$cortex_reported_plan_sha"'
    durable = 'cortex_durable_plan_root="$cortex_installer_root/plans"'
    publication = "os.rename(staging_name, target.name"
    switch = "cortex_plan_path=$cortex_durable_plan_path"
    lease = "cortex_root_cli install trust-root lease"
    assert current.index(confirmation) < current.index(durable)
    assert current.index(durable) < current.index(publication) < current.index(switch)
    assert current.index(switch) < current.index(lease)
    assert "reviewed plan changed before durable publication" in current
    assert "existing durable plan does not match reviewed bytes" in current
    assert "os.fsync(staging_fd)" in current
    assert "os.fsync(root_fd)" in current


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
