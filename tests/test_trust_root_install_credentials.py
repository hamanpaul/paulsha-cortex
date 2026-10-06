"""Phase 2 trust-root installer RED contract: credentials and activation."""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.trust_root.install import (
    ActivationError,
    CredentialImportError,
    InstallError,
    InstallReceipt,
    activate_receipt,
    apply_plan,
    import_credential,
    inherit_prior_credentials,
    new_install_receipt,
    plan_sha256,
    rollback_receipt,
)
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install.backend import LocalInstallBackend
from paulsha_cortex.trust_root.install.core import credential_source_basename


@pytest.fixture(autouse=True)
def _synthetic_credential_plans_skip_complete_authority_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credential seam tests use an empty typed apply order by design."""

    monkeypatch.setattr(
        install_core, "_validate_repo_identity", lambda plan: plan["repo_identity"]
    )
    monkeypatch.setattr(
        install_core, "_validate_apply_account_inventories", lambda _plan: {}
    )
    monkeypatch.setattr(
        install_core, "_validate_required_credentials", lambda _plan: None
    )
    monkeypatch.setattr(
        install_core, "_validate_candidate_venv", lambda _plan, _steps: None
    )
    monkeypatch.setattr(
        install_core, "_validate_account_step_bijection", lambda _rows, _steps: None
    )
    monkeypatch.setattr(
        install_core,
        "_validate_repository_step_bijection",
        lambda _plan, _steps, _identity: None,
    )
    monkeypatch.setattr(
        install_core, "_validate_canonical_receipt_path", lambda _plan: None
    )
    monkeypatch.setattr(
        install_core,
        "_validate_finalized_apply_surfaces",
        lambda _plan, _steps: None,
    )


def _plan(*, required_credentials=None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "scheme": "four-way",
        "repo_identity": {"commit": "a" * 40},
        "candidate": {
            "wheel_sha256": "b" * 64,
            "bundle_sha256": "c" * 64,
        },
        "accounts": [],
        "roots": {
            "deploy": "/opt/cortex",
            "state": "/var/lib/cortex",
            "systemd": "/etc/systemd/system",
            "polkit": "/etc/polkit-1/rules.d",
        },
        "apply_order": [],
        "required_credentials": required_credentials or [],
    }


class CredentialBackend:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.fail_start: str | None = None
        self.fail_stop: str | None = None
        self.credential_rollbacks = 0

    def preflight_facts(self, _plan):
        return {
            "systemd": True,
            "polkit": True,
            "cgroup_v2": True,
            "acl": True,
            "disk_free_bytes": 2 * 1024 * 1024 * 1024,
            "cortex_account_universal_nopasswd": {"accounts": [], "unproven": None},
            "in_flight_jobs": 0,
            "services": {
                "cortex-egress-proxy.service": "inactive",
                "cortex-manager.service": "inactive",
                "cortex-monitor.service": "inactive",
            },
            "accounts": {},
            "paths": {},
        }

    def inspect_step(self, _step):  # pragma: no cover - empty plan contract
        raise AssertionError("empty apply plan must not inspect a step")

    def apply_step(self, _step):  # pragma: no cover - empty plan contract
        raise AssertionError("empty apply plan must not mutate a step")

    def start_service(self, name: str) -> None:
        self.started.append(name)
        if self.fail_start == name:
            raise RuntimeError(f"injected start failure: {name}")

    def stop_service(self, name: str) -> None:
        self.stopped.append(name)
        if self.fail_stop == name:
            raise RuntimeError(f"injected stop failure: {name}")

    def rollback_credentials(self, _receipt):
        self.credential_rollbacks += 1
        return ()

    def list_unknown_state(self, _receipt):
        return ()


def _applied_receipt(*, required_credentials=None):
    plan = _plan(required_credentials=required_credentials)
    backend = CredentialBackend()
    receipt = new_install_receipt(plan)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    return plan, receipt, backend


def test_codex_import_accepts_only_the_allowlisted_regular_filename(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "auth.json"
    secret = "test-secret-must-never-be-rendered"
    source.write_text(json.dumps({"OPENAI_API_KEY": secret}), encoding="utf-8")
    destination_root = tmp_path / "installed-credentials"

    imported = import_credential(
        receipt,
        principal="builder",
        provider="codex",
        source=source,
        destination_root=destination_root,
    )

    metadata = imported.to_dict()
    assert metadata == {
        "principal": "builder",
        "provider": "codex",
        "mode": "0600",
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    rendered = json.dumps(
        {"result": metadata, "receipt": receipt.to_dict()}, sort_keys=True
    )
    assert secret not in rendered
    assert str(source) not in rendered

    installed = [path for path in destination_root.rglob("*") if path.is_file()]
    assert len(installed) == 1
    assert installed[0].read_bytes() == source.read_bytes()
    assert os.stat(installed[0], follow_symlinks=False).st_mode & 0o777 == 0o600
    assert not list(destination_root.rglob("*.tmp")), "atomic temp must not be stranded"


def test_credential_adapter_rejects_a_non_allowlisted_source_name(tmp_path: Path) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "token.json"
    source.write_text('{"token":"test-secret"}', encoding="utf-8")

    with pytest.raises(CredentialImportError, match="allowlist|auth.json"):
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=tmp_path / "installed",
        )
    assert not (tmp_path / "installed").exists()


def test_agy_import_writes_physical_cache_target_without_following_home_symlink(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / credential_source_basename("reviewer-planner", "agy")
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    home = tmp_path / "cortex-reviewer-planner"
    target = home / "cache/gemini/antigravity-cli"
    target.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".gemini").symlink_to(outside, target_is_directory=True)

    import_credential(
        receipt,
        principal="reviewer-planner",
        provider="agy",
        source=source,
        destination_root=home,
    )

    assert (target / "antigravity-oauth-token").read_bytes() == source.read_bytes()
    assert not (outside / "antigravity-cli").exists()


def test_builder_agy_import_is_scoped_to_the_builder_cache(tmp_path: Path) -> None:
    """The explicit builder/AGY adapter must not discover reviewer state."""

    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / credential_source_basename("builder", "agy")
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    builder_home = tmp_path / "cortex-builder"
    builder_cache = builder_home / "cache/gemini/antigravity-cli"
    builder_cache.mkdir(parents=True)
    reviewer_home = tmp_path / "cortex-reviewer-planner"
    reviewer_target = reviewer_home / "cache/gemini/antigravity-cli"
    reviewer_target.mkdir(parents=True)
    (reviewer_target / "antigravity-oauth-token").write_text(
        '{"token":"reviewer-state"}', encoding="utf-8"
    )

    import_credential(
        receipt,
        principal="builder",
        provider="agy",
        source=source,
        destination_root=builder_home,
    )

    assert (builder_cache / "antigravity-oauth-token").read_bytes() == source.read_bytes()
    assert (reviewer_target / "antigravity-oauth-token").read_text(encoding="utf-8") == (
        '{"token":"reviewer-state"}'
    )
    assert not (builder_home / ".gemini" / "antigravity-oauth-token").exists()


def test_agy_adapter_is_derived_from_the_permgen_credential_row() -> None:
    """AGY 的來源 basename 與落點只有 permgen 一份真相（#716 canary refresh）。"""

    from pathlib import PurePosixPath

    from paulsha_cortex.trust_root import permgen

    layout = permgen.DEFAULT_LAYOUT
    for principal in ("builder", "reviewer-planner"):
        credential = permgen.credential_for(principal, "agy")
        account = layout.credential_accounts()[principal]
        adapter = install_core._credential_adapter_for(principal, "agy")
        assert adapter is not None
        assert credential_source_basename(principal, "agy") == (
            PurePosixPath(credential.token_leaf).name
        )
        expected = PurePosixPath(layout.credential_target_of(account, credential)).joinpath(
            credential.token_leaf
        )
        assert PurePosixPath(layout.home_of(account)).joinpath(
            *adapter.destination_parts
        ) == expected
        assert adapter.account_owned_dirs == len(
            PurePosixPath(credential.token_leaf).parts
        ) - 1
    assert credential_source_basename("reviewer-planner", "agy") != "oauth_creds.json"


def test_copilot_adapter_uses_the_config_file_the_manager_projects() -> None:
    from paulsha_cortex.coordinator import spool_slot

    assert install_core.COPILOT_CONFIG_FILENAME == spool_slot._COPILOT_CONFIG_FILENAME
    adapter = install_core._credential_adapter_for("reviewer-planner", "copilot")
    assert adapter is not None
    assert adapter.source_name == install_core.COPILOT_CONFIG_FILENAME
    assert adapter.destination_parts == (".copilot", install_core.COPILOT_CONFIG_FILENAME)
    # #666：job 帳號不得有 gh 設定目錄，Copilot 憑證不可落到 gh 的 hosts.yml。
    assert ".config" not in adapter.destination_parts


def test_copilot_import_uses_config_json_shape_and_actual_cli_path(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / credential_source_basename("reviewer-planner", "copilot")
    secret = "copilot-source-secret-sentinel"
    source.write_text(
        json.dumps(
            {
                "loggedInUsers": [{"host": "https://github.com", "login": "fixture"}],
                "lastLoggedInUser": {"host": "https://github.com", "login": "fixture"},
                "copilotTokens": {"https://github.com:fixture": secret},
            }
        ),
        encoding="utf-8",
    )
    home = tmp_path / "cortex-reviewer-planner"
    home.mkdir()

    metadata = import_credential(
        receipt,
        principal="reviewer-planner",
        provider="copilot",
        source=source,
        destination_root=home,
        destination_uid=os.getuid(),
        destination_gid=os.getgid(),
    )

    installed = home / ".copilot/config.json"
    assert installed.read_bytes() == source.read_bytes()
    assert oct(installed.stat().st_mode & 0o777) == "0o600"
    state_dir = (home / ".copilot").stat()
    assert state_dir.st_uid == os.getuid()
    assert oct(state_dir.st_mode & 0o777) == "0o700"
    assert not (home / ".config").exists()
    assert secret not in str(metadata.to_dict())


@pytest.mark.parametrize(
    "content",
    [
        "github.com:\n  user: do-not-render-source-content\n  oauth_token: x\n",
        '{"copilotTokens": {}, "note": "do-not-render-source-content"}',
        '{"copilotTokens": {"host": ""}, "note": "do-not-render-source-content"}',
        '["do-not-render-source-content"]',
    ],
)
def test_copilot_import_rejects_wrong_config_shape_without_echoing_content(
    tmp_path: Path, content: str
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / credential_source_basename("reviewer-planner", "copilot")
    source.write_text(content, encoding="utf-8")

    with pytest.raises(CredentialImportError, match="Copilot credential source") as error:
        import_credential(
            receipt,
            principal="reviewer-planner",
            provider="copilot",
            source=source,
            destination_root=tmp_path / "installed",
        )

    assert "do-not-render-source-content" not in str(error.value)
    assert not (tmp_path / "installed").exists()


def test_agy_import_creates_its_state_directory_for_the_account(tmp_path: Path) -> None:
    """`antigravity-cli/` 由匯入者建立時必須屬於該帳號，AGY 才讀得到自己的登入檔。"""

    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / credential_source_basename("reviewer-planner", "agy")
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    home = tmp_path / "cortex-reviewer-planner"
    (home / "cache/gemini").mkdir(parents=True)

    import_credential(
        receipt,
        principal="reviewer-planner",
        provider="agy",
        source=source,
        destination_root=home,
        destination_uid=os.getuid(),
        destination_gid=os.getgid(),
    )

    state_dir = home / "cache/gemini/antigravity-cli"
    assert state_dir.stat().st_uid == os.getuid()
    assert oct(state_dir.stat().st_mode & 0o777) == "0o700"
    assert (state_dir / "antigravity-oauth-token").read_bytes() == source.read_bytes()


def test_credential_import_rejects_state_directory_owned_by_another_account(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / credential_source_basename("reviewer-planner", "agy")
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    home = tmp_path / "cortex-reviewer-planner"
    (home / "cache/gemini/antigravity-cli").mkdir(parents=True)

    with pytest.raises(CredentialImportError, match="destination preparation failed"):
        import_credential(
            receipt,
            principal="reviewer-planner",
            provider="agy",
            source=source,
            destination_root=home,
            destination_uid=os.getuid() + 1,
            destination_gid=os.getgid(),
        )

    assert not (home / "cache/gemini/antigravity-cli/antigravity-oauth-token").exists()


def test_credential_adapter_rejects_a_symlink_before_reading_content(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    real = tmp_path / "real-auth.json"
    real.write_text('{"token":"test-secret"}', encoding="utf-8")
    source = tmp_path / "auth.json"
    source.symlink_to(real)

    with pytest.raises(CredentialImportError, match="symlink"):
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=tmp_path / "installed",
        )
    assert not (tmp_path / "installed").exists()


def test_credential_import_rejects_source_ancestor_rename_with_same_leaf_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source_parent = tmp_path / "operator-source"
    source_parent.mkdir()
    source = source_parent / "auth.json"
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    displaced = tmp_path / "displaced-operator-source"
    destination_root = tmp_path / "installed"
    original_fdopen = os.fdopen
    swapped = False

    def rename_ancestor_once() -> None:
        nonlocal swapped
        if swapped:
            return
        source_parent.rename(displaced)
        source_parent.mkdir()
        (displaced / source.name).rename(source)
        swapped = True

    def fdopen_and_rename_ancestor(*args, **kwargs):
        stream = original_fdopen(*args, **kwargs)
        rename_ancestor_once()
        return stream

    monkeypatch.setattr(install_core.os, "fdopen", fdopen_and_rename_ancestor)

    with pytest.raises(CredentialImportError, match="source.*changed"):
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=destination_root,
        )

    assert swapped
    assert not destination_root.exists()


def test_credential_import_rejects_same_inode_same_size_source_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "auth.json"
    original = b'{"token":"AAAA"}'
    rewritten = b'{"token":"BBBB"}'
    assert len(original) == len(rewritten)
    source.write_bytes(original)
    initial = source.stat()
    destination_root = tmp_path / "installed"
    original_read = install_core._read_fd_bytes
    original_fstat = install_core.os.fstat
    mutated = False

    def fstat_with_forged_times(descriptor: int):
        observed = original_fstat(descriptor)
        if (observed.st_dev, observed.st_ino) != (initial.st_dev, initial.st_ino):
            return observed
        return SimpleNamespace(
            st_dev=observed.st_dev,
            st_ino=observed.st_ino,
            st_mode=observed.st_mode,
            st_nlink=observed.st_nlink,
            st_uid=observed.st_uid,
            st_gid=observed.st_gid,
            st_size=observed.st_size,
            st_mtime_ns=initial.st_mtime_ns,
            st_ctime_ns=initial.st_ctime_ns,
        )

    def read_then_rewrite(descriptor: int) -> bytes:
        nonlocal mutated
        content = original_read(descriptor)
        if not mutated:
            with source.open("r+b") as stream:
                stream.write(rewritten)
                stream.flush()
                os.fsync(stream.fileno())
            os.utime(
                source,
                ns=(initial.st_atime_ns, initial.st_mtime_ns),
                follow_symlinks=False,
            )
            mutated = True
        return content

    monkeypatch.setattr(install_core, "_read_fd_bytes", read_then_rewrite)
    monkeypatch.setattr(install_core.os, "fstat", fstat_with_forged_times)

    with pytest.raises(CredentialImportError, match="source.*changed"):
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=destination_root,
        )

    final = source.stat()
    assert mutated
    assert (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) == (
        initial.st_dev,
        initial.st_ino,
        initial.st_size,
        initial.st_mtime_ns,
    )
    assert source.read_bytes() == rewritten
    assert not destination_root.exists()


def test_provider_principal_pair_is_fail_closed(tmp_path: Path) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "hosts.yml"
    source.write_text("github.com:\n  oauth_token: test-secret\n", encoding="utf-8")

    with pytest.raises(CredentialImportError, match="builder|github"):
        import_credential(
            receipt,
            principal="builder",
            provider="github",
            source=source,
            destination_root=tmp_path / "installed",
        )


def test_credential_replace_then_receipt_persist_crash_recovers_without_orphan(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "auth.json"
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    destination_root = tmp_path / "installed"
    durable_snapshots: list[dict[str, object]] = []
    persist_count = 0

    def crash_after_replace() -> None:
        nonlocal persist_count
        persist_count += 1
        if persist_count == 1:
            durable_snapshots.append(receipt.to_dict())
            return
        raise RuntimeError("injected receipt persist crash")

    receipt._persist = crash_after_replace  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="receipt persist crash"):
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=destination_root,
        )

    installed = destination_root / ".codex/auth.json"
    assert installed.read_bytes() == source.read_bytes()
    assert receipt.to_dict()["credentials"] == []
    assert receipt.to_dict()["credential_journal"] == durable_snapshots[0][
        "credential_journal"
    ]
    assert durable_snapshots[0]["credentials"] == []
    assert durable_snapshots[0]["credential_journal"] == [
        {
            "principal": "builder",
            "provider": "codex",
            "mode": "0600",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "status": "prepared",
        }
    ]

    recovered = InstallReceipt(durable_snapshots[0])
    metadata = import_credential(
        recovered,
        principal="builder",
        provider="codex",
        source=source,
        destination_root=destination_root,
    )
    assert recovered.to_dict()["credentials"] == [metadata.to_dict()]
    assert recovered.to_dict()["credential_journal"] == []


def test_credential_source_os_error_does_not_disclose_source_path(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "private-source-name" / "auth.json"

    with pytest.raises(CredentialImportError) as exc:
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=tmp_path / "installed",
        )

    assert str(source) not in str(exc.value)
    assert "private-source-name" not in str(exc.value)
    assert str(exc.value) == "credential source is not readable"


def test_credential_cli_stderr_redacts_source_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    receipt._document["plan"]["accounts"] = [  # type: ignore[index]
        {
            "name": "cortex-builder",
            "home": str(tmp_path / "cortex-builder"),
            "uid": 991,
            "gid": 991,
        }
    ]
    source = tmp_path / "private-source-name" / "auth.json"
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(
        install_cli.InstallReceipt,
        "load",
        classmethod(lambda _cls, _path, **_kwargs: receipt),
    )
    monkeypatch.setattr(
        install_cli,
        "_install_transaction_lock",
        lambda _plan, **_kwargs: nullcontext(),
    )

    assert install_cli.main(
        [
            "credentials",
            "import",
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--principal",
            "builder",
            "--provider",
            "codex",
            "--source",
            str(source),
        ]
    ) == 1
    error = capsys.readouterr().err
    assert str(source) not in error
    assert "private-source-name" not in error
    assert "credential source is not readable" in error


def test_credential_destination_os_error_uses_fixed_redacted_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "auth.json"
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    destination_root = tmp_path / "private-destination-name"

    def fail_replace(_source, destination, **_kwargs):
        raise OSError(f"cannot replace private path {destination}")

    monkeypatch.setattr(
        install_core, "_open_unnamed_credential_tmpfile", lambda _parent_fd: None
    )
    monkeypatch.setattr(install_core.os, "replace", fail_replace)

    with pytest.raises(CredentialImportError) as exc:
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=destination_root,
        )

    assert str(destination_root) not in str(exc.value)
    assert "private-destination-name" not in str(exc.value)
    assert str(exc.value) == "credential destination write failed"


def test_activation_refuses_missing_required_credential_without_starting_services(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, backend = _applied_receipt(
        required_credentials=[{"principal": "builder", "provider": "codex"}]
    )

    with pytest.raises(ActivationError, match="builder.*codex|codex.*builder"):
        activate_receipt(receipt, backend=backend)
    assert backend.started == []
    assert backend.stopped == []


def test_manager_start_failure_reverse_stops_egress_and_never_starts_monitor(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, backend = _applied_receipt(
        required_credentials=[{"principal": "builder", "provider": "codex"}]
    )
    source = tmp_path / "auth.json"
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    import_credential(
        receipt,
        principal="builder",
        provider="codex",
        source=source,
        destination_root=tmp_path / "installed",
    )
    backend.fail_start = "cortex-manager.service"

    with pytest.raises(ActivationError, match="cortex-manager.service"):
        activate_receipt(receipt, backend=backend)

    assert backend.started == [
        "cortex-egress-proxy.service",
        "cortex-manager.service",
    ]
    assert backend.stopped == [
        "cortex-manager.service",
        "cortex-egress-proxy.service",
    ]
    doc = receipt.to_dict()
    assert doc["activated"] is False
    assert doc["qualified"] is False


def test_activation_surfaces_reverse_stop_failure_and_records_remaining_service(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, backend = _applied_receipt()
    backend.fail_start = "cortex-manager.service"
    backend.fail_stop = "cortex-egress-proxy.service"

    with pytest.raises(ActivationError, match="stop.*cortex-egress-proxy"):
        activate_receipt(receipt, backend=backend)

    assert backend.started == [
        "cortex-egress-proxy.service",
        "cortex-manager.service",
    ]
    assert backend.stopped == [
        "cortex-manager.service",
        "cortex-egress-proxy.service",
    ]
    document = receipt.to_dict()
    assert document["services_started"] is True
    assert document["running_services"] == ["cortex-egress-proxy.service"]
    assert document["activation_failure"]["compensation_failures"] == [
        "cortex-egress-proxy.service"
    ]


def test_activation_compensation_continues_when_every_forget_persist_fails() -> None:
    _plan_doc, receipt, backend = _applied_receipt()
    backend.fail_start = "cortex-manager.service"
    durable: dict[str, object] = {}

    def fail_compensation_persist() -> None:
        if backend.stopped:
            raise OSError("injected compensation receipt failure")
        durable.clear()
        durable.update(receipt.to_dict())

    receipt._persist = fail_compensation_persist  # type: ignore[method-assign]

    with pytest.raises(ActivationError, match="persist.*manager.*egress"):
        activate_receipt(receipt, backend=backend)

    assert backend.stopped == [
        "cortex-manager.service",
        "cortex-egress-proxy.service",
    ]
    assert [row["service"] for row in receipt.to_dict()["activation_journal"]] == [
        "cortex-egress-proxy.service",
        "cortex-manager.service",
    ]
    assert durable["activation_journal"] == receipt.to_dict()["activation_journal"]

    recovery_backend = CredentialBackend()
    recovered = InstallReceipt(durable)
    report = rollback_receipt(recovered, backend=recovery_backend)

    assert report.retained_drift == ()
    assert recovery_backend.stopped == [
        "cortex-manager.service",
        "cortex-egress-proxy.service",
    ]


def test_final_activation_checkpoint_failure_compensates_all_services_and_replays() -> None:
    _plan_doc, receipt, backend = _applied_receipt()
    durable: dict[str, object] = {}
    final_failure_started = False

    def fail_final_and_compensation_persists() -> None:
        nonlocal final_failure_started
        document = receipt.to_dict()
        journal = document.get("activation_journal", [])
        if final_failure_started or (
            document.get("services_started") is True
            and len(journal) == 3
            and all(row.get("status") == "completed" for row in journal)
        ):
            final_failure_started = True
            raise OSError("injected final activation checkpoint failure")
        durable.clear()
        durable.update(document)

    receipt._persist = fail_final_and_compensation_persists  # type: ignore[method-assign]

    with pytest.raises(ActivationError, match="final.*checkpoint|checkpoint.*final"):
        activate_receipt(receipt, backend=backend)

    assert backend.started == [
        "cortex-egress-proxy.service",
        "cortex-manager.service",
        "cortex-monitor.service",
    ]
    assert backend.stopped == [
        "cortex-monitor.service",
        "cortex-manager.service",
        "cortex-egress-proxy.service",
    ]
    assert [row["service"] for row in receipt.to_dict()["activation_journal"]] == [
        "cortex-egress-proxy.service",
        "cortex-manager.service",
        "cortex-monitor.service",
    ]
    assert durable["activation_journal"] == receipt.to_dict()["activation_journal"]

    recovery_backend = CredentialBackend()
    recovered = InstallReceipt(durable)
    report = rollback_receipt(recovered, backend=recovery_backend)

    assert report.retained_drift == ()
    assert recovery_backend.stopped == [
        "cortex-monitor.service",
        "cortex-manager.service",
        "cortex-egress-proxy.service",
    ]


def test_activation_persists_prepared_and_completed_entry_around_each_start(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, backend = _applied_receipt()
    snapshots: list[list[dict[str, object]]] = []
    original_persist = receipt._persist

    def capture_persist() -> None:
        snapshots.append(
            list(receipt.to_dict().get("activation_journal", []))
        )
        original_persist()

    receipt._persist = capture_persist  # type: ignore[method-assign]

    activate_receipt(receipt, backend=backend)

    transitions = [snapshot for snapshot in snapshots if snapshot]
    assert transitions[:6] == [
        [{"service": "cortex-egress-proxy.service", "status": "prepared"}],
        [{"service": "cortex-egress-proxy.service", "status": "completed"}],
        [
            {"service": "cortex-egress-proxy.service", "status": "completed"},
            {"service": "cortex-manager.service", "status": "prepared"},
        ],
        [
            {"service": "cortex-egress-proxy.service", "status": "completed"},
            {"service": "cortex-manager.service", "status": "completed"},
        ],
        [
            {"service": "cortex-egress-proxy.service", "status": "completed"},
            {"service": "cortex-manager.service", "status": "completed"},
            {"service": "cortex-monitor.service", "status": "prepared"},
        ],
        [
            {"service": "cortex-egress-proxy.service", "status": "completed"},
            {"service": "cortex-manager.service", "status": "completed"},
            {"service": "cortex-monitor.service", "status": "completed"},
        ],
    ]


def test_loaded_prepared_activation_rolls_back_attempted_service_after_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan()
    backend = CredentialBackend()
    receipt_path = (tmp_path / "activation-receipt.json").absolute()
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )
    receipt = new_install_receipt(plan, path=receipt_path)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    original_persist = receipt._persist

    def crash_after_start_before_completed_persist() -> None:
        journal = receipt.to_dict().get("activation_journal", [])
        if journal and journal[-1].get("status") == "completed":
            raise SystemExit("simulated SIGKILL persistence boundary")
        original_persist()

    receipt._persist = crash_after_start_before_completed_persist  # type: ignore[method-assign]

    with pytest.raises(SystemExit, match="SIGKILL"):
        activate_receipt(receipt, backend=backend)

    assert backend.started == ["cortex-egress-proxy.service"]
    recovered = InstallReceipt.load(receipt_path)
    assert recovered.to_dict()["activation_journal"] == [
        {"service": "cortex-egress-proxy.service", "status": "prepared"}
    ]

    report = rollback_receipt(recovered, backend=backend)

    assert report.retained_drift == ()
    assert backend.stopped == ["cortex-egress-proxy.service"]
    assert recovered.to_dict()["activation_journal"] == []


def test_rollback_removes_hash_bound_prepared_credential(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    home = tmp_path / "cortex-builder"
    destination = home / ".codex/auth.json"
    destination.parent.mkdir(parents=True)
    destination.write_text('{"token":"test-secret"}', encoding="utf-8")
    destination.chmod(0o600)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    receipt._document["plan"]["accounts"] = [  # type: ignore[index]
        {
            "name": "cortex-builder",
            "home": str(home),
            "uid": os.getuid(),
            "gid": os.getgid(),
        }
    ]
    receipt._document["credential_journal"] = [
        {
            "principal": "builder",
            "provider": "codex",
            "mode": "0600",
            "sha256": digest,
            "status": "prepared",
        }
    ]

    report = rollback_receipt(
        receipt, backend=LocalInstallBackend(require_root=False)
    )

    assert report.retained_drift == ()
    assert not destination.exists()
    assert receipt.to_dict()["credential_journal"] == []


def test_live_credential_validation_redacts_destination_path(tmp_path: Path) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    private_home = tmp_path / "private-destination-name"
    receipt._document["plan"]["accounts"] = [  # type: ignore[index]
        {
            "name": "cortex-builder",
            "home": str(private_home),
            "uid": os.getuid(),
            "gid": os.getgid(),
        }
    ]
    receipt._document["credentials"] = [
        {
            "principal": "builder",
            "provider": "codex",
            "mode": "0600",
            "sha256": "0" * 64,
        }
    ]

    failures = LocalInstallBackend(require_root=False).validate_credentials(receipt)

    assert failures == ("builder/codex unavailable",)
    assert str(private_home) not in " ".join(failures)


def test_credential_import_does_not_follow_swapped_destination_ancestor(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "auth.json"
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    destination_root = tmp_path / "credential-home"
    destination_root.mkdir()
    displaced = tmp_path / "displaced-credential-home"
    external_root = tmp_path / "external-home"
    external_root.mkdir()
    original_persist = receipt._persist
    swapped = False

    def persist_and_swap() -> None:
        nonlocal swapped
        original_persist()
        if not swapped:
            destination_root.rename(displaced)
            destination_root.symlink_to(external_root, target_is_directory=True)
            swapped = True

    receipt._persist = persist_and_swap  # type: ignore[method-assign]

    with pytest.raises(CredentialImportError, match="changed|destination"):
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=destination_root,
        )

    assert swapped
    assert list(external_root.rglob("*")) == []
    assert not (displaced / ".codex/auth.json").exists()


def test_credential_unnamed_tmpfile_crash_leaves_no_readable_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "auth.json"
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    destination_root = tmp_path / "installed"

    def crash_before_publish(*_args, **_kwargs):
        raise KeyboardInterrupt("simulated SIGKILL boundary")

    monkeypatch.setattr(
        install_core,
        "_publish_credential_tmpfile",
        crash_before_publish,
        raising=False,
    )

    with pytest.raises(KeyboardInterrupt, match="SIGKILL"):
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=destination_root,
        )

    credential_parent = destination_root / ".codex"
    assert credential_parent.is_dir()
    assert list(credential_parent.iterdir()) == []
    pending = receipt.to_dict()["credential_journal"]
    assert len(pending) == 1
    assert "temp_name" not in pending[0]


def test_credential_named_temp_fallback_is_journaled_and_replayable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "auth.json"
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    destination_root = tmp_path / "installed"
    real_replace = os.replace
    publish_calls = 0

    monkeypatch.setattr(
        install_core,
        "_open_unnamed_credential_tmpfile",
        lambda _parent_fd: None,
        raising=False,
    )

    def crash_first_publish(source_name, destination_name, *args, **kwargs):
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 1:
            raise KeyboardInterrupt("simulated fallback publish crash")
        return real_replace(source_name, destination_name, *args, **kwargs)

    monkeypatch.setattr(install_core.os, "replace", crash_first_publish)

    with pytest.raises(KeyboardInterrupt, match="fallback publish crash"):
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=destination_root,
        )

    pending = receipt.to_dict()["credential_journal"]
    assert len(pending) == 1
    temp_name = pending[0]["temp_name"]
    assert isinstance(temp_name, str)
    assert (destination_root / ".codex" / temp_name).is_file()

    metadata = import_credential(
        receipt,
        principal="builder",
        provider="codex",
        source=source,
        destination_root=destination_root,
    )

    assert receipt.to_dict()["credentials"] == [metadata.to_dict()]
    assert receipt.to_dict()["credential_journal"] == []
    assert not (destination_root / ".codex" / temp_name).exists()


def test_rollback_cleans_hash_bound_named_credential_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "auth.json"
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    destination_root = tmp_path / "cortex-builder"
    receipt._document["plan"]["accounts"] = [  # type: ignore[index]
        {
            "name": "cortex-builder",
            "home": str(destination_root),
            "uid": os.getuid(),
            "gid": os.getgid(),
        }
    ]
    monkeypatch.setattr(
        install_core,
        "_open_unnamed_credential_tmpfile",
        lambda _parent_fd: None,
    )
    monkeypatch.setattr(
        install_core.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            KeyboardInterrupt("simulated fallback publish crash")
        ),
    )

    with pytest.raises(KeyboardInterrupt, match="fallback publish crash"):
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=destination_root,
            destination_uid=os.getuid(),
            destination_gid=os.getgid(),
        )

    temp_name = receipt.to_dict()["credential_journal"][0]["temp_name"]
    temp_path = destination_root / ".codex" / temp_name
    assert temp_path.is_file()

    report = rollback_receipt(
        receipt, backend=LocalInstallBackend(require_root=False)
    )

    assert report.retained_drift == ()
    assert not temp_path.exists()
    assert receipt.to_dict()["credential_journal"] == []


def test_successful_start_order_is_still_unverified_not_qualified() -> None:
    _plan_doc, receipt, backend = _applied_receipt()

    activate_receipt(receipt, backend=backend)

    assert backend.started == [
        "cortex-egress-proxy.service",
        "cortex-manager.service",
        "cortex-monitor.service",
    ]
    doc = receipt.to_dict()
    assert doc["activated"] is False
    assert doc["qualified"] is False
    assert doc["services_started"] is True


def test_activation_revalidates_imported_credential_bytes(tmp_path: Path) -> None:
    _plan_doc, receipt, _backend = _applied_receipt(
        required_credentials=[{"principal": "builder", "provider": "codex"}]
    )
    source = tmp_path / "auth.json"
    source.write_text('{"token":"original"}', encoding="utf-8")
    home = tmp_path / "cortex-builder"
    import_credential(
        receipt,
        principal="builder",
        provider="codex",
        source=source,
        destination_root=home,
    )
    installed = home / ".codex/auth.json"
    installed.write_text('{"token":"tampered"}', encoding="utf-8")

    class ValidatingBackend(CredentialBackend):
        def validate_credentials(self, _receipt):
            return ("builder/codex hash mismatch",)

    backend = ValidatingBackend()
    with pytest.raises(ActivationError, match="hash mismatch"):
        activate_receipt(receipt, backend=backend)
    assert backend.started == []


_PRIOR_RECEIPT_PATH = Path("/var/lib/cortex-install-receipts/prior.json")


def _inherited_row(
    digest: str,
    prior_id: str,
    *,
    principal: str = "builder",
    provider: str = "codex",
) -> dict[str, str]:
    return {
        "principal": principal,
        "provider": provider,
        "mode": "0600",
        "sha256": digest,
        "inherited_from": prior_id,
    }


def _write_credential(home: Path, relative: str, content: bytes) -> str:
    destination = home / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    destination.chmod(0o600)
    return hashlib.sha256(content).hexdigest()


def _credential_accounts(
    tmp_path: Path, *, builder_uid: int | None = None
) -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "home": str(tmp_path / name),
            "uid": (
                builder_uid
                if builder_uid is not None and name == "cortex-builder"
                else os.getuid()
            ),
            "gid": os.getgid(),
        }
        for name in ("cortex-builder", "cortex-reviewer-planner", "cortex-manager")
    ]


def _successor_link(prior_id: str) -> dict[str, str]:
    return {
        "path": str(_PRIOR_RECEIPT_PATH),
        "receipt_id": prior_id,
        "plan_sha256": "d" * 64,
    }


def test_receipt_load_accepts_inherited_credential_linked_to_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(install_core, "_validate_receipt_file", lambda _o, _p: None)
    path = (tmp_path / "receipts" / "successor.json").absolute()
    receipt = new_install_receipt(_plan(), path=path)
    receipt._document["parent_receipt"] = _successor_link("prior-receipt")
    receipt._document["credentials"] = [_inherited_row("e" * 64, "prior-receipt")]
    receipt._persist()

    loaded = InstallReceipt.load(path)

    assert loaded.to_dict()["credentials"] == [
        _inherited_row("e" * 64, "prior-receipt")
    ]


@pytest.mark.parametrize(
    ("parent", "inherited_from"),
    [
        (_successor_link("prior-receipt"), "another-receipt"),
        (_successor_link("prior-receipt"), ""),
        (None, "prior-receipt"),
    ],
)
def test_receipt_load_rejects_inherited_credential_not_linked_to_its_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent: dict[str, str] | None,
    inherited_from: str,
) -> None:
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(install_core, "_validate_receipt_file", lambda _o, _p: None)
    path = (tmp_path / "receipts" / "successor.json").absolute()
    receipt = new_install_receipt(_plan(), path=path)
    if parent is not None:
        receipt._document["parent_receipt"] = parent
    receipt._document["credentials"] = [_inherited_row("e" * 64, inherited_from)]
    receipt._persist()

    with pytest.raises(InstallError, match="credential metadata is invalid"):
        InstallReceipt.load(path)


def test_rollback_keeps_the_prior_credential_and_its_inherited_record(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    receipt._document["plan"]["accounts"] = _credential_accounts(tmp_path)  # type: ignore[index]
    receipt._document["parent_receipt"] = _successor_link("prior-receipt")
    inherited = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    owned = _write_credential(
        tmp_path / "cortex-manager", ".config/gh/hosts.yml", b"github.com: {}\n"
    )
    receipt._document["credentials"] = [
        _inherited_row(inherited, "prior-receipt"),
        {
            "principal": "manager",
            "provider": "github",
            "mode": "0600",
            "sha256": owned,
        },
    ]

    report = rollback_receipt(
        receipt, backend=LocalInstallBackend(require_root=False)
    )

    assert report.retained_drift == ()
    assert (tmp_path / "cortex-builder/.codex/auth.json").read_bytes() == (
        b'{"token":"prior"}'
    )
    assert not (tmp_path / "cortex-manager/.config/gh/hosts.yml").exists()
    document = receipt.to_dict()
    assert document["state"] == "rolled-back"
    assert document["credentials"] == [_inherited_row(inherited, "prior-receipt")]
    assert install_cli._receipt_restore_safe(document) is True


def test_restore_safe_refuses_an_owned_credential_left_after_rollback() -> None:
    document = {
        "state": "rolled-back",
        "journal": [],
        "activation_journal": [],
        "credential_journal": [],
        "services_started": False,
        "rollback": {"retained_unknown": [], "retained_drift": []},
        "credentials": [
            {"principal": "builder", "provider": "codex", "mode": "0600", "sha256": "e" * 64}
        ],
    }

    assert install_cli._receipt_restore_safe(document) is False
    document["credentials"] = [_inherited_row("e" * 64, "prior-receipt")]
    assert install_cli._receipt_restore_safe(document) is True


def _handoff(
    tmp_path: Path,
    *,
    required: tuple[tuple[str, str], ...] = (("builder", "codex"),),
    prior_rows: list[dict[str, str]] | None = None,
    prior_builder_uid: int | None = None,
    new_builder_uid: int | None = None,
) -> tuple[InstallReceipt, InstallReceipt]:
    rows = [{"principal": principal, "provider": provider} for principal, provider in required]
    prior_plan = _plan(required_credentials=rows)
    prior_plan["accounts"] = _credential_accounts(tmp_path, builder_uid=prior_builder_uid)
    prior_plan["candidate"] = {**prior_plan["candidate"], "wheel_sha256": "1" * 64}
    seed = new_install_receipt(prior_plan).to_dict()
    seed.update(state="applied", qualified=True, credentials=deepcopy(prior_rows or []))
    prior = InstallReceipt(seed, path=_PRIOR_RECEIPT_PATH)
    plan = deepcopy(prior_plan)
    plan["accounts"] = _credential_accounts(
        tmp_path,
        builder_uid=new_builder_uid if new_builder_uid is not None else prior_builder_uid,
    )
    plan["candidate"]["wheel_sha256"] = "2" * 64
    receipt = new_install_receipt(plan)
    receipt._document["state"] = "applied"
    receipt._document["parent_receipt"] = {
        "path": str(_PRIOR_RECEIPT_PATH),
        "receipt_id": seed["receipt_id"],
        "plan_sha256": seed["plan_sha256"],
    }
    return prior, receipt


def _prior_row(digest: str, *, principal: str = "builder", provider: str = "codex") -> dict[str, str]:
    return {"principal": principal, "provider": provider, "mode": "0600", "sha256": digest}


def test_inherit_prior_credentials_marks_rows_and_activation_counts_them(
    tmp_path: Path,
) -> None:
    digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    prior, receipt = _handoff(tmp_path, prior_rows=[_prior_row(digest)])
    prior_id = prior.to_dict()["receipt_id"]

    rows = inherit_prior_credentials(
        receipt, prior, backend=LocalInstallBackend(require_root=False)
    )

    assert rows == (_inherited_row(digest, prior_id),)
    assert receipt.to_dict()["credentials"] == [_inherited_row(digest, prior_id)]

    class LiveValidation(CredentialBackend):
        def validate_credentials(self, current):
            return LocalInstallBackend(require_root=False).validate_credentials(current)

    backend = LiveValidation()
    activate_receipt(receipt, backend=backend)
    assert backend.started == [
        "cortex-egress-proxy.service",
        "cortex-manager.service",
        "cortex-monitor.service",
    ]


class _LiveCredentialBackend(CredentialBackend):
    """Activation double whose credential check is the real live validation."""

    def validate_credentials(self, current):
        return LocalInstallBackend(require_root=False).validate_credentials(current)


def _rewrite_in_place(path: Path, content: bytes) -> str:
    """Rewrite like a token refresh: same inode, owner, mode and link count."""

    before = path.lstat()
    descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    try:
        os.write(descriptor, content)
    finally:
        os.close(descriptor)
    after = path.lstat()
    assert (after.st_ino, after.st_mode, after.st_uid, after.st_nlink) == (
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_nlink,
    )
    return hashlib.sha256(content).hexdigest()


def test_inherit_records_the_current_digest_of_a_credential_its_account_rewrote(
    tmp_path: Path,
) -> None:
    prior_digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    current = _rewrite_in_place(
        tmp_path / "cortex-builder/.codex/auth.json", b'{"token":"refreshed"}'
    )
    assert current != prior_digest
    prior, receipt = _handoff(tmp_path, prior_rows=[_prior_row(prior_digest)])
    prior_id = prior.to_dict()["receipt_id"]

    rows = inherit_prior_credentials(
        receipt, prior, backend=LocalInstallBackend(require_root=False)
    )

    assert rows == (_inherited_row(current, prior_id),)
    assert receipt.to_dict()["credentials"] == [_inherited_row(current, prior_id)]
    # Provenance stays in the chain: the prior receipt still records its digest.
    assert prior.to_dict()["credentials"] == [_prior_row(prior_digest)]

    backend = _LiveCredentialBackend()
    activate_receipt(receipt, backend=backend)
    assert backend.started == [
        "cortex-egress-proxy.service",
        "cortex-manager.service",
        "cortex-monitor.service",
    ]
    assert receipt.to_dict()["services_started"] is True


def test_activation_binds_the_inherited_row_to_the_digest_recorded_at_inheritance(
    tmp_path: Path,
) -> None:
    prior_digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    destination = tmp_path / "cortex-builder/.codex/auth.json"
    _rewrite_in_place(destination, b'{"token":"refreshed"}')
    prior, receipt = _handoff(tmp_path, prior_rows=[_prior_row(prior_digest)])
    inherit_prior_credentials(
        receipt, prior, backend=LocalInstallBackend(require_root=False)
    )
    _rewrite_in_place(destination, b'{"token":"after-inheritance"}')

    backend = _LiveCredentialBackend()
    with pytest.raises(ActivationError, match="builder/codex metadata or hash mismatch"):
        activate_receipt(receipt, backend=backend)
    assert backend.started == []


@pytest.mark.parametrize(
    ("principal", "provider"),
    [
        ("builder", "codex"),
        ("reviewer-planner", "codex"),
        ("builder", "agy"),
        ("reviewer-planner", "agy"),
        ("reviewer-planner", "copilot"),
        ("manager", "github"),
    ],
)
def test_inherit_follows_an_owner_rewrite_for_every_credential_shape(
    tmp_path: Path, principal: str, provider: str
) -> None:
    account = {
        "builder": "cortex-builder",
        "reviewer-planner": "cortex-reviewer-planner",
        "manager": "cortex-manager",
    }[principal]
    adapter = install_core._credential_adapter_for(principal, provider)
    assert adapter is not None
    relative = "/".join(adapter.destination_parts)
    prior_digest = _write_credential(tmp_path / account, relative, b"prior-content\n")
    current = _rewrite_in_place(tmp_path / account / relative, b"refreshed-content\n")
    prior, receipt = _handoff(
        tmp_path,
        required=((principal, provider),),
        prior_rows=[_prior_row(prior_digest, principal=principal, provider=provider)],
    )
    prior_id = prior.to_dict()["receipt_id"]

    rows = inherit_prior_credentials(
        receipt, prior, backend=LocalInstallBackend(require_root=False)
    )

    expected = _inherited_row(current, prior_id, principal=principal, provider=provider)
    assert rows == (expected,)
    assert LocalInstallBackend(require_root=False).validate_credentials(receipt) == ()


def _drift_then(tmp_path: Path, unsafe: str) -> dict[str, object]:
    """Rewrite the builder credential, then break one metadata property."""

    home = tmp_path / "cortex-builder"
    prior_digest = _write_credential(home, ".codex/auth.json", b'{"token":"prior"}')
    destination = home / ".codex/auth.json"
    _rewrite_in_place(destination, b'{"token":"refreshed"}')
    handoff: dict[str, object] = {"prior_rows": [_prior_row(prior_digest)]}
    if unsafe == "mode-0640":
        destination.chmod(0o640)
    elif unsafe == "hard-link":
        os.link(destination, tmp_path / "second-link")
    elif unsafe == "symlink":
        target = tmp_path / "elsewhere.json"
        target.write_bytes(b'{"token":"refreshed"}')
        target.chmod(0o600)
        destination.unlink()
        destination.symlink_to(target)
    elif unsafe == "foreign-uid":
        handoff["prior_builder_uid"] = os.getuid() + 1
    elif unsafe == "moved-destination":
        handoff["new_builder_uid"] = os.getuid() + 1
    elif unsafe == "unrecorded-pair":
        handoff["required"] = (("builder", "codex"), ("reviewer-planner", "copilot"))
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(unsafe)
    return handoff


@pytest.mark.parametrize(
    ("unsafe", "refusal"),
    [
        ("mode-0640", "builder/codex metadata mismatch"),
        ("hard-link", "builder/codex metadata mismatch"),
        ("symlink", "builder/codex unavailable"),
        ("foreign-uid", "builder/codex metadata mismatch"),
        ("moved-destination", "destination changed"),
        ("unrecorded-pair", "never recorded: reviewer-planner/copilot"),
    ],
)
def test_inherit_still_refuses_unsafe_metadata_after_an_owner_rewrite(
    tmp_path: Path, unsafe: str, refusal: str
) -> None:
    prior, receipt = _handoff(tmp_path, **_drift_then(tmp_path, unsafe))  # type: ignore[arg-type]

    with pytest.raises(CredentialImportError, match=refusal):
        inherit_prior_credentials(
            receipt, prior, backend=LocalInstallBackend(require_root=False)
        )
    assert receipt.to_dict()["credentials"] == []


def test_inherit_refuses_a_destination_the_new_plan_moved(tmp_path: Path) -> None:
    digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    prior, receipt = _handoff(
        tmp_path, prior_rows=[_prior_row(digest)], new_builder_uid=os.getuid() + 1
    )

    with pytest.raises(CredentialImportError, match="destination changed"):
        inherit_prior_credentials(
            receipt, prior, backend=LocalInstallBackend(require_root=False)
        )


def test_inherit_refuses_a_file_owned_by_another_uid(tmp_path: Path) -> None:
    digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    prior, receipt = _handoff(
        tmp_path, prior_rows=[_prior_row(digest)], prior_builder_uid=os.getuid() + 1
    )

    with pytest.raises(CredentialImportError, match="builder/codex metadata mismatch"):
        inherit_prior_credentials(
            receipt, prior, backend=LocalInstallBackend(require_root=False)
        )


def test_inherit_refuses_a_hard_linked_destination(tmp_path: Path) -> None:
    digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    os.link(tmp_path / "cortex-builder/.codex/auth.json", tmp_path / "second-link")
    prior, receipt = _handoff(tmp_path, prior_rows=[_prior_row(digest)])

    with pytest.raises(CredentialImportError, match="builder/codex metadata mismatch"):
        inherit_prior_credentials(
            receipt, prior, backend=LocalInstallBackend(require_root=False)
        )


def test_inherit_refuses_a_provider_the_prior_receipt_never_recorded(
    tmp_path: Path,
) -> None:
    digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    prior, receipt = _handoff(
        tmp_path,
        required=(("builder", "codex"), ("reviewer-planner", "copilot")),
        prior_rows=[_prior_row(digest)],
    )

    with pytest.raises(CredentialImportError) as exc:
        inherit_prior_credentials(
            receipt, prior, backend=LocalInstallBackend(require_root=False)
        )
    assert "reviewer-planner/copilot" in str(exc.value)
    assert "§4" in str(exc.value)
    assert receipt.to_dict()["credentials"] == []


def test_inherit_refuses_a_receipt_that_is_not_the_prior_successor(tmp_path: Path) -> None:
    digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    prior, receipt = _handoff(tmp_path, prior_rows=[_prior_row(digest)])
    del receipt._document["parent_receipt"]

    with pytest.raises(CredentialImportError, match="not the upgrade successor"):
        inherit_prior_credentials(
            receipt, prior, backend=LocalInstallBackend(require_root=False)
        )


@pytest.mark.parametrize("refreshed", [False, True])
def test_credentials_inherit_cli_records_rows_in_the_durable_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    refreshed: bool,
) -> None:
    monkeypatch.setattr(install_core, "_validate_receipt_parent", lambda _o, _p: None)
    monkeypatch.setattr(install_core, "_validate_receipt_file", lambda _o, _p: None)
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(install_cli, "_TRUST_ROOT_LOCK_ROOT", tmp_path / "host-locks")
    monkeypatch.setattr(
        install_cli, "_TRUST_ROOT_MAINTENANCE_ROOT", tmp_path / "maintenance-state"
    )
    monkeypatch.setattr(
        install_cli, "LocalInstallBackend", lambda: LocalInstallBackend(require_root=False)
    )
    digest = _write_credential(
        tmp_path / "cortex-builder", ".codex/auth.json", b'{"token":"prior"}'
    )
    prior_plan = _plan(required_credentials=[{"principal": "builder", "provider": "codex"}])
    prior_plan["accounts"] = _credential_accounts(tmp_path)
    prior_plan["candidate"] = {**prior_plan["candidate"], "wheel_sha256": "1" * 64}
    prior_path = (tmp_path / "receipts" / "prior.json").absolute()
    prior = new_install_receipt(prior_plan, path=prior_path)
    prior._document.update(state="applied", qualified=True, credentials=[_prior_row(digest)])
    prior._persist()
    plan = deepcopy(prior_plan)
    plan["candidate"]["wheel_sha256"] = "2" * 64
    next_path = (tmp_path / "receipts" / "next.json").absolute()
    successor = new_install_receipt(plan, path=next_path)
    successor._document["state"] = "applied"
    prior_document = prior.to_dict()
    successor._document["parent_receipt"] = {
        "path": str(prior_path),
        "receipt_id": prior_document["receipt_id"],
        "plan_sha256": prior_document["plan_sha256"],
    }
    successor._persist()
    if refreshed:
        digest = _rewrite_in_place(
            tmp_path / "cortex-builder/.codex/auth.json", b'{"token":"refreshed"}'
        )

    assert install_cli.main(
        [
            "credentials",
            "inherit",
            "--receipt",
            str(next_path),
            "--prior-receipt",
            str(prior_path),
        ]
    ) == 0

    emitted = json.loads(capsys.readouterr().out)
    assert emitted["inherited"] == [
        {
            "principal": "builder",
            "provider": "codex",
            "inherited_from": prior_document["receipt_id"],
        }
    ]
    assert InstallReceipt.load(next_path).to_dict()["credentials"] == [
        _inherited_row(digest, prior_document["receipt_id"])
    ]
    # Compatibility pin (#1275): the row keeps the exact key set every released
    # reader accepts, so an installed coordinator still loads this receipt.
    on_disk = json.loads(next_path.read_text(encoding="utf-8"))["credentials"]
    assert [set(row) for row in on_disk] == [
        {"principal", "provider", "mode", "sha256", "inherited_from"}
    ]
