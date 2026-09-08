"""RED coverage for terminal JSONL record framing and Unicode fidelity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager


def _verification_payload(marker: str) -> dict[str, object]:
    """Return a terminal payload with string data in every relevant section."""

    return {
        "schema_version": 1,
        "kind": "workflow-verification-result",
        "status": "verified",
        "summary": "terminal framing regression",
        "details": {
            "job49_evidence": marker,
            "unicode": "一般文字與組合字 e\u0301",
        },
        "reports": [
            {"path": "reports/verify/terminal.md", "body": f"report {marker}"},
            {"path": "reports/verify/second.md", "body": f"another {marker}"},
        ],
    }


@pytest.mark.parametrize(
    ("separator", "name"),
    (
        pytest.param("\u0085", "NEL", id="u0085-nel"),
        pytest.param("\u2028", "LS", id="u2028-ls"),
        pytest.param("\u2029", "PS", id="u2029-ps"),
    ),
)
@pytest.mark.parametrize(
    "inner_ensure_ascii",
    (False, True),
    ids=("inner-raw", "inner-escaped"),
)
@pytest.mark.parametrize(
    "outer_ensure_ascii",
    (False, True),
    ids=("outer-raw", "outer-escaped"),
)
def test_extract_terminal_json_preserves_unicode_record_data(
    tmp_path: Path,
    separator: str,
    name: str,
    inner_ensure_ascii: bool,
    outer_ensure_ascii: bool,
) -> None:
    """A raw JSON string separator is payload data, not a JSONL boundary."""

    marker = f"before{separator}after"
    verification_payload = _verification_payload(marker)

    inner_result = json.dumps(
        verification_payload,
        ensure_ascii=inner_ensure_ascii,
        sort_keys=True,
    )
    outer_record = {
        "type": "result",
        "result": inner_result,
        "structured_output": verification_payload,
    }
    serialized_record = json.dumps(
        outer_record,
        ensure_ascii=outer_ensure_ascii,
        sort_keys=True,
    )
    if outer_ensure_ascii:
        assert f"\\u{ord(separator):04x}" in serialized_record
    else:
        assert separator in serialized_record

    log_path = tmp_path / f"terminal-{name}.jsonl"
    log_path.write_text(serialized_record + "\n", encoding="utf-8")

    extracted = manager._extract_terminal_json(str(log_path))

    assert extracted == verification_payload


def test_extract_terminal_json_uses_result_when_only_outer_copy_has_separator(
    tmp_path: Path,
) -> None:
    """The outer structured-output copy must not be allowed to split the record."""

    separator = "\u0085"
    result_payload = {
        "schema_version": 1,
        "kind": "workflow-verification-result",
        "status": "verified",
        "summary": "result carrier",
        "details": {"job49_evidence": "result without separator"},
        "reports": [{"path": "reports/verify/terminal.md", "body": "result"}],
    }
    outer_copy = {
        **result_payload,
        "details": {"job49_evidence": f"outer{separator}copy"},
        "reports": [
            {"path": "reports/verify/terminal.md", "body": f"outer{separator}report"}
        ],
    }
    serialized_record = json.dumps(
        {
            "type": "result",
            "result": json.dumps(result_payload, ensure_ascii=True, sort_keys=True),
            "structured_output": outer_copy,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    assert separator in serialized_record

    log_path = tmp_path / "outer-only-separator.jsonl"
    log_path.write_text(serialized_record + "\n", encoding="utf-8")

    assert manager._extract_terminal_json(str(log_path)) == result_payload
