"""Regression tests for quoted values in the zero-dependency YAML subset."""

from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex._yaml import YAMLError, safe_load
from paulsha_cortex.deck.compile import _format_inline_list, _format_scalar


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('["a, b", plain]', ["a, b", "plain"]),
        ('["a]b, c", plain]', ["a]b, c", "plain"]),
        ('["a]b"]', ["a]b"]),
        ("['a, b', plain]", ["a, b", "plain"]),
        (r'["say \"hi, there\"", done]', ['say "hi, there"', "done"]),
        (r"['it\'s, okay', plain]", ["it's, okay", "plain"]),
        ('["a",]', ["a"]),
        ('[""]', [""]),
        ("['', a]", ["", "a"]),
        ('["", a]', ["", "a"]),
        ("[]", []),
        ("[1, true, null, ~, x]", [1, True, None, None, "x"]),
    ],
)
def test_inline_flow_list_preserves_quoted_values(source: str, expected: list[object]) -> None:
    assert safe_load(f"value: {source}")['value'] == expected


@pytest.mark.parametrize(
    "source",
    [
        '[, a]',
        '["a",, "b"]',
        '["unterminated]',
    ],
)
def test_inline_flow_list_rejects_malformed_items(source: str) -> None:
    with pytest.raises(YAMLError, match="malformed inline list"):
        safe_load(f"value: {source}")


@pytest.mark.parametrize("formatter", [_format_inline_list, _format_scalar])
def test_deck_inline_list_formatters_round_trip(formatter) -> None:
    values = ["a, b", "x]y", "single'quote", 'double"quote']

    assert safe_load(f"value: {formatter(values)}")['value'] == values


def test_frontmatter_argv_with_quoted_commas_round_trips(monkeypatch, tmp_path: Path) -> None:
    from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter

    monkeypatch.setenv("PSC_REPO_ROOT", str(tmp_path))
    spec = tmp_path / "yaml-inline-list-quotes.md"
    spec.write_text(
        "---\n"
        "dispatch: auto\n"
        "slice_id: yaml-inline-list-quotes\n"
        "plan: docs/superpowers/plans/yaml-inline-list-quotes.md\n"
        "target_branch: feature/822-yaml-inline-list-quotes\n"
        "verification:\n"
        "  docs_class: code\n"
        "  required_artifacts: []\n"
        "  checks:\n"
        "    - kind: persona-scope\n"
        "    - kind: command\n"
        "      name: policy\n"
        "      argv: [python3, -m, policy_check, --repo, .]\n"
        "      cwd: .\n"
        "      timeout_seconds: 30\n"
        "  tests:\n"
        '    - argv: [python3, -m, pytest, "-k", "a or b, c"]\n'
        "      cwd: .\n"
        "      timeout_seconds: 60\n"
        "  full_suite:\n"
        '    argv: [python3, -m, pytest, "src,lib"]\n'
        "    cwd: .\n"
        "    timeout_seconds: 60\n"
        "    baseline: no-regression\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )

    meta = parse_spec_frontmatter(spec)

    assert meta["parse_error"] is None
    assert meta["dispatch"] == "auto"
    assert meta["verification"]["tests"][0]["argv"] == [
        "python3",
        "-m",
        "pytest",
        "-k",
        "a or b, c",
    ]
    assert meta["verification"]["full_suite"]["argv"] == [
        "python3",
        "-m",
        "pytest",
        "src,lib",
    ]
