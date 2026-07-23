from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


INIT_SAMPLE_SCHEMA = "cortex-porcelain/init-sample/v1"


def _load_cli():
    for module_name in (
        "paulsha_cortex.cli",
        "paulsha_cortex.porcelain",
        "paulsha_cortex.porcelain.init_sample",
    ):
        sys.modules.pop(module_name, None)
    return importlib.import_module("paulsha_cortex.cli")


def _run_cli(argv: list[str]) -> int:
    cli = _load_cli()
    try:
        return cli.main(argv)
    except SystemExit as error:
        code = error.code
        return code if isinstance(code, int) else 1


@pytest.fixture
def specs_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "specs"
    monkeypatch.setenv("PSC_MANAGER_SPECS_DIR", str(root))
    return root


def _seed_emitted_spec(specs_root: Path, *, change: str) -> Path:
    path = specs_root / f"{change}-build.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "dispatch: hold\n"
        "plan: docs/superpowers/plans/*demo*.md\n"
        "target_branch: null\n"
        "verification: null\n"
        "---\n",
        encoding="utf-8",
    )
    return path


def test_init_sample_routes_before_coordinator_and_prints_hold_checklist(
    monkeypatch: pytest.MonkeyPatch,
    specs_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    emitted = _seed_emitted_spec(specs_root, change="demo-change")
    deck_calls: list[list[str]] = []

    def fake_deck_main(argv=None):
        deck_calls.append(list(argv or []))
        return 0

    def fake_coordinator_main(argv=None):
        raise AssertionError(f"coordinator should not handle init-sample argv: {argv}")

    monkeypatch.setattr("paulsha_cortex.deck.cli.main", fake_deck_main)
    monkeypatch.setattr("paulsha_cortex.coordinator.cli.main", fake_coordinator_main)

    assert _run_cli(["init-sample", "--task", "Demo feature", "--change", "demo-change"]) == 0

    captured = capsys.readouterr()
    assert deck_calls == [
        [
            "compile",
            "feature-oneshot",
            "--task",
            "Demo feature",
            "--change",
            "demo-change",
            "--allow-external",
            "--emit",
        ]
    ]
    assert "dispatch: hold" in emitted.read_text(encoding="utf-8")
    assert emitted.name in captured.out
    assert "plan" in captured.out
    assert "target_branch" in captured.out
    assert "main" in captured.out
    assert "verification" in captured.out
    assert "persona-scope" in captured.out
    assert "policy" in captured.out
    assert "full_suite" in captured.out
    assert "deck verify" in captured.out
    assert "auto" in captured.out
    assert captured.err == ""


def test_init_sample_unknown_combo_exits_two_without_calling_deck(
    monkeypatch: pytest.MonkeyPatch,
    specs_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def fake_deck_main(argv=None):
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr("paulsha_cortex.deck.cli.main", fake_deck_main)

    assert _run_cli(["init-sample", "--task", "Demo feature", "--combo", "not-a-real-combo"]) == 2

    captured = capsys.readouterr()
    assert called is False
    assert list(specs_root.rglob("*.md")) == []
    assert "not-a-real-combo" in (captured.out + captured.err)


def test_init_sample_json_emits_versioned_schema(
    monkeypatch: pytest.MonkeyPatch,
    specs_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_emitted_spec(specs_root, change="json-change")

    def fake_deck_main(argv=None):
        return 0

    def fake_coordinator_main(argv=None):
        raise AssertionError(f"coordinator should not handle init-sample argv: {argv}")

    monkeypatch.setattr("paulsha_cortex.deck.cli.main", fake_deck_main)
    monkeypatch.setattr("paulsha_cortex.coordinator.cli.main", fake_coordinator_main)

    assert _run_cli(
        ["init-sample", "--task", "Demo feature", "--change", "json-change", "--json"]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == INIT_SAMPLE_SCHEMA
