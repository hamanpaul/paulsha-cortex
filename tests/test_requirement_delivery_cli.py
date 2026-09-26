from __future__ import annotations

import json
from pathlib import Path

from paulsha_cortex.porcelain import delivery


def test_delivery_help_exposes_read_and_reconcile_commands() -> None:
    parser = delivery._build_parser()
    help_text = parser.format_help()
    assert "status" in help_text
    assert "gaps" in help_text
    assert "reconcile" in help_text


def test_reconcile_help_exposes_exact_index_cas(capsys) -> None:
    import pytest

    with pytest.raises(SystemExit) as exc:
        delivery.main(["reconcile", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--expected-index-revision" in output
    assert "--evidence-root" not in output
    assert "--coordinator-root" not in output
    assert "--work-snapshot" not in output
    assert "--index INDEX" not in output


def test_loaded_runtime_root_ignores_snapshot_hint_and_uses_service_contract(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(delivery, "selected_instance", lambda: "cortex")
    manager_root = tmp_path / "manager"
    monitor_root = tmp_path / "monitor"
    monkeypatch.setattr(delivery.paths, "coordinator_root", lambda: manager_root)
    monkeypatch.setattr(delivery.paths, "monitor_state_root", lambda: monitor_root)
    assert delivery._runtime_state_root("manager", "cortex", "../../attacker") == manager_root
    assert delivery._runtime_state_root("monitor", "cortex", "/attacker") == monitor_root


def test_loaded_runtime_reader_consumes_service_status_projection(monkeypatch) -> None:
    from paulsha_cortex.porcelain import service

    expected = {"status": "match", "loaded": {"pid": 321}, "trust_root": {"status": "verified"}}
    monkeypatch.setattr(delivery, "selected_instance", lambda: "cortex")
    monkeypatch.setattr(service, "_status_payload", lambda instance: {
        "loaded_runtime": {"manager": expected, "monitor": {"status": "unknown"}}
    })
    assert delivery._runtime_status_report("manager", "cortex") is expected


def test_status_is_machine_readable_and_does_not_write(tmp_path: Path, capsys) -> None:
    index_path = tmp_path / "missing" / "index.json"
    assert delivery.main(["status", "--index", str(index_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "cortex/requirement-delivery-status/v1"
    assert payload["index_generation"] == 0
    assert payload["gaps"] == []
    assert not index_path.parent.exists()


def test_status_preserves_forward_extension_and_gap_projection(tmp_path: Path, capsys) -> None:
    index_path = tmp_path / "index.json"
    original = {
        "schema": "cortex/requirement-delivery-index",
        "schema_version": 1,
        "generation": 3,
        "manifest_id": "refine",
        "manifest_sha256": "a" * 64,
        "snapshot_sha256": "b" * 64,
        "mappings": [],
        "gaps": [{"requirement_id": "R01", "stage": "live", "status": "missing"}],
        "reconcile_receipt": {"coverage": "not-ready"},
        "extensions": {"future_field": {"retained": True}},
    }
    index_path.write_text(json.dumps(original), encoding="utf-8")
    before = index_path.read_bytes()
    assert delivery.main(["status", "--index", str(index_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["gaps"] == original["gaps"]
    assert payload["extensions"] == original["extensions"]
    assert index_path.read_bytes() == before
