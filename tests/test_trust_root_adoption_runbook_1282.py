"""Runbook regressions from the first real legacy adoption (#1282).

The service lists and the restore function below are executed exactly as the
transactional-install runbook spells them, so a shell-level slip (an empty
``previously_active`` turning into one empty service name) fails here instead
of on a host in the middle of a maintenance window.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TRANSACTIONAL = ROOT / "docs/superpowers/runbooks/trust-root-transactional-install.md"
LEGACY = ROOT / "docs/superpowers/runbooks/trust-root-legacy-adoption.md"
BASH = shutil.which("bash") or "/bin/bash"
SERVICES = [
    "cortex-egress-proxy.service",
    "cortex-manager.service",
    "cortex-monitor.service",
]


def _block(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    stop = text.index(end, begin) + len(end)
    return text[begin:stop]


def _run_shell(script: str, *arguments: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "-c", script, "runbook", *arguments],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )


def _lease_result(present: list[str], active: list[str]) -> str:
    return json.dumps(
        {
            "maintenance_lease": True,
            "maintenance_token": "a" * 64,
            "plan_sha256": "b" * 64,
            "present_services": present,
            "previously_active": active,
            "receipt_path": "/var/lib/cortex-installer/receipt.json",
            "snapshot_path": "/var/lib/cortex-installer/maintenance-snapshot.json",
        }
    )


def _runbook_shell(body: str, *arguments: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    text = TRANSACTIONAL.read_text(encoding="utf-8")
    present = _block(text, "  mapfile -t cortex_present_services < <(", "\n  )\n")
    active = _block(text, "  mapfile -t cortex_previously_active < <(", "\n  )\n")
    restore = _block(text, "cortex_restore_active() {", "\n}\n")
    # The privileged tool is replaced by a recorder; nothing here runs sudo.
    restore = restore.replace("/usr/bin/sudo ", "cortex_fake_sudo ")
    assert "sudo" not in restore.replace("cortex_fake_sudo", "")
    script = "\n".join(
        [
            "set -euo pipefail",
            'cortex_maintenance_result=$1',
            'cortex_calls=$2',
            'cortex_fake_sudo() { printf "%s\\n" "$*" >>"$cortex_calls"; return 1; }',
            "cortex_present_services=()",
            "cortex_previously_active=()",
            restore,
            present,
            active,
            body,
        ]
    )
    return _run_shell(script, *arguments, tmp_path=tmp_path)


def _lists(tmp_path: Path, present: list[str], active: list[str]) -> tuple[list[str], list[str]]:
    completed = _runbook_shell(
        'for name in "${cortex_present_services[@]}"; do printf "P:%s\\n" "$name"; done\n'
        'for name in "${cortex_previously_active[@]}"; do printf "A:%s\\n" "$name"; done\n'
        'printf "N:%s:%s\\n" "${#cortex_present_services[@]}" "${#cortex_previously_active[@]}"',
        _lease_result(present, active),
        str(tmp_path / "calls"),
        tmp_path=tmp_path,
    )
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.splitlines()
    assert lines[-1] == f"N:{len(present)}:{len(active)}", lines
    return (
        [line[2:] for line in lines if line.startswith("P:")],
        [line[2:] for line in lines if line.startswith("A:")],
    )


def test_services_stopped_before_the_lease_parse_to_an_empty_list(tmp_path: Path) -> None:
    # Reference host: the services were already stopped, the lease reported
    # previously_active = [] and the old snippet produced one empty name.
    assert _lists(tmp_path, SERVICES, []) == (SERVICES, [])
    assert _lists(tmp_path, [], []) == ([], [])


def test_service_lists_keep_every_name(tmp_path: Path) -> None:
    assert _lists(tmp_path, SERVICES, SERVICES[1:]) == (SERVICES, SERVICES[1:])


def test_restore_with_nothing_previously_active_succeeds_without_a_start(
    tmp_path: Path,
) -> None:
    completed = _runbook_shell(
        "cortex_restore_active",
        _lease_result(SERVICES, []),
        str(tmp_path / "calls"),
        tmp_path=tmp_path,
    )

    assert completed.returncode == 0, completed.stderr
    assert not (tmp_path / "calls").exists(), "no systemctl start for an empty list"


def test_restore_starts_exactly_the_previously_active_services(tmp_path: Path) -> None:
    completed = _runbook_shell(
        "cortex_restore_active || printf 'restore failed\\n'",
        _lease_result(SERVICES, ["cortex-manager.service"]),
        str(tmp_path / "calls"),
        tmp_path=tmp_path,
    )

    assert completed.returncode == 0, completed.stderr
    assert "restore failed" in completed.stdout  # the recorder refuses every start
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[0] == "/usr/bin/systemctl start cortex-manager.service"
    assert calls[1] == "/usr/bin/systemctl stop cortex-manager.service"


def test_legacy_capture_stops_before_running_root_cli_on_a_sealed_tree_mismatch(
    tmp_path: Path,
) -> None:
    text = LEGACY.read_text(encoding="utf-8")
    root_cli = _block(
        TRANSACTIONAL.read_text(encoding="utf-8"), "cortex_root_cli() {", "\n}\n"
    )
    capture = _block(
        text,
        "cortex_capture_result=$(cortex_root_cli install trust-root legacy inventory \\",
        '/usr/bin/printf \'%s\\n\' "$cortex_capture_result"',
    )
    script = "\n".join(
        [
            "set -euo pipefail",
            'cortex_calls=$1',
            'cortex_install_config=/tmp/install-config.yaml',
            'cortex_bundle=/tmp/bundle.json',
            'cortex_cli_tree_sha() { printf "%s\\n" mismatch; }',
            'cortex_sealed_cli_tree_sha=expected',
            root_cli,
            capture,
        ]
    )

    completed = _run_shell(script, str(tmp_path / "calls"), tmp_path=tmp_path)

    assert completed.returncode != 0
    assert not (tmp_path / "calls").exists(), "sealed-tree mismatch must abort before root CLI"


def test_transactional_rollback_trap_stops_before_running_root_cli_on_a_sealed_tree_mismatch(
    tmp_path: Path,
) -> None:
    text = TRANSACTIONAL.read_text(encoding="utf-8")
    recovery = text.split(
        "cortex_recovery_sealed_cli_tree_sha=$(cortex_recovery_cli_tree_sha)", 1
    )[1]
    root_cli = _block(recovery, "cortex_root_cli() {", "\n}\n")
    abort_restore = _block(text, "cortex_abort_restore() {", "\n}\n")
    abort_restore = abort_restore.replace("/usr/bin/sudo ", "cortex_fake_sudo ")
    script = "\n".join(
        [
            "set -euo pipefail",
            'cortex_calls=$1',
            'cortex_apply_attempted=1',
            'cortex_receipt_path=/var/lib/cortex-installer/receipt.json',
            'cortex_maintenance_token=token',
            'cortex_recovery_cli_tree_sha() { printf "%s\\n" mismatch; }',
            'cortex_recovery_sealed_cli_tree_sha=expected',
            root_cli,
            'cortex_fake_sudo() {',
            '  if [ "$1" = "/usr/bin/test" ] && [ "$2" = "-f" ]; then',
            '    return 0',
            '  fi',
            '  return 0',
            '}',
            'cortex_restore_active() { return 0; }',
            'cortex_release_maintenance_lease() { return 0; }',
            abort_restore,
            'cortex_abort_restore 1',
        ]
    )

    completed = _run_shell(script, str(tmp_path / "calls"), tmp_path=tmp_path)

    assert completed.returncode == 1
    assert not (tmp_path / "calls").exists(), "sealed-tree mismatch must abort before rollback"


@pytest.mark.parametrize(
    "start",
    [
        "cortex_root_cli() {",
        'cortex_apply_attempted=1\n/usr/bin/sudo /usr/bin/env -i HOME=/root \\\n',
    ],
    ids=["sealed-root-cli", "apply"],
)
def test_root_installer_paths_include_the_system_sbin_directories(start: str) -> None:
    # Ubuntu keeps useradd/groupadd/visudo in /usr/sbin; the root PATH of the
    # sealed candidate CLI matches `cortex upgrade` (#1263) and the installer
    # itself also resolves those tools from fixed directories.
    text = TRANSACTIONAL.read_text(encoding="utf-8")
    block = _block(text, start, "PYTHONNOUSERSITE=1")
    assert 'PATH="$cortex_bootstrap_root/venv/bin:/usr/sbin:/usr/bin:/sbin:/bin"' in block


def test_recovery_root_installer_path_includes_the_system_sbin_directories() -> None:
    text = TRANSACTIONAL.read_text(encoding="utf-8")
    recovery = text.split(
        "cortex_recovery_sealed_cli_tree_sha=$(cortex_recovery_cli_tree_sha)", 1
    )[1]
    block = _block(recovery, "cortex_root_cli() {", "PYTHONNOUSERSITE=1")

    assert 'PATH="$cortex_bootstrap_root/venv/bin:/usr/sbin:/usr/bin:/sbin:/bin"' in block


def test_legacy_runbook_reviews_the_capture_preview_and_sudoers_as_root() -> None:
    text = LEGACY.read_text(encoding="utf-8")
    prerequisites = text.split("## 0. 前提", 1)[1].split("## 1.", 1)[0]
    capture = text.split("## 3. 擷取並審核 legacy inventory", 1)[1].split("## 4.", 1)[0]

    # Item 6: the sudoers preflight is a read-only check run with sudo, as part
    # of the root capture, before any plan or apply.
    assert "cortex_account_universal_nopasswd" in prerequisites
    assert "cortex_account_universal_nopasswd" in capture
    assert "plan_preview" in capture
    assert 'cortex_root_cli install trust-root legacy inventory' in capture
    # Items 3-5: what the installer now covers itself, so review lists are
    # only for what the preview still names.
    assert "home-top" in text
    assert "socket" in text and "FIFO" in text
    assert "census_exceptions" in text
