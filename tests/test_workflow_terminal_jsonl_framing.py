"""Regression coverage for terminal JSONL record framing and Unicode fidelity."""

from __future__ import annotations

import hashlib
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


@pytest.mark.parametrize(
    ("case", "prefix", "suffix"),
    (
        pytest.param("lf", b"", b"\n", id="lf"),
        pytest.param("crlf", b"", b"\r\n", id="crlf"),
        pytest.param("leading-blank", b"\n", b"\n", id="leading-blank"),
        pytest.param("trailing-blank", b"", b"\n\n", id="trailing-blank"),
        pytest.param("eof-without-newline", b"", b"", id="eof-without-newline"),
    ),
)
def test_extract_terminal_json_preserves_real_file_framing(
    tmp_path: Path,
    case: str,
    prefix: bytes,
    suffix: bytes,
) -> None:
    """Only LF frames records while ordinary Unicode remains byte-for-byte stable."""

    payload = _verification_payload("一般文字與組合字 e\u0301")
    record = json.dumps(
        {
            "type": "result",
            "result": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    raw = prefix + record + suffix
    assert b"\xe4\xb8\x80" in raw
    assert b"e\xcc\x81" in raw

    log_path = tmp_path / f"framing-{case}.jsonl"
    log_path.write_bytes(raw)
    before = log_path.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()

    assert manager._extract_terminal_json(str(log_path)) == payload
    after = log_path.read_bytes()
    assert after == before
    assert hashlib.sha256(after).hexdigest() == before_hash


def test_extract_terminal_json_rejects_bare_cr_record_boundary_without_mutating_file(
    tmp_path: Path,
) -> None:
    """A bare CR between JSON objects is not an alternate JSONL record delimiter."""

    payload = _verification_payload("terminal")
    raw = (
        json.dumps({"type": "progress", "message": "not terminal"})
        + "\r"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    ).encode("utf-8")
    log_path = tmp_path / "bare-cr-two-records.jsonl"
    log_path.write_bytes(raw)
    before = log_path.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()

    with pytest.raises(ValueError, match="no JSON evidence"):
        manager._extract_terminal_json(str(log_path))

    assert log_path.read_bytes() == before
    assert hashlib.sha256(log_path.read_bytes()).hexdigest() == before_hash


def test_extract_terminal_json_accepts_cr_as_whitespace_inside_one_record(
    tmp_path: Path,
) -> None:
    """CR remains valid JSON whitespace when there is only one record."""

    payload = _verification_payload("single-record")
    record = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    raw = b"\r" + record + b"\r"
    log_path = tmp_path / "single-record-cr-whitespace.jsonl"
    log_path.write_bytes(raw)
    before = log_path.read_bytes()

    assert manager._extract_terminal_json(str(log_path)) == payload
    assert log_path.read_bytes() == before


def test_extract_terminal_json_rejects_structured_output_without_recognized_carrier(
    tmp_path: Path,
) -> None:
    """An arbitrary structured_output field is not a new terminal carrier."""

    payload = _verification_payload("structured-output-only")
    raw = json.dumps(
        {"type": "result", "structured_output": payload},
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"
    log_path = tmp_path / "structured-output-only.jsonl"
    log_path.write_bytes(raw)

    with pytest.raises(ValueError, match="no JSON evidence"):
        manager._extract_terminal_json(str(log_path))


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    (
        pytest.param("missing-path", None, "log missing", id="missing-path"),
        pytest.param("empty-path", "", "log missing", id="empty-path"),
        pytest.param("missing-file", "missing-file.jsonl", "log unreadable", id="missing-file"),
    ),
)
def test_extract_terminal_json_rejects_missing_log_inputs(
    tmp_path: Path,
    name: str,
    raw: object,
    message: str,
) -> None:
    log_path = raw if raw in (None, "") else str(tmp_path / str(raw))
    with pytest.raises(ValueError, match=message):
        manager._extract_terminal_json(log_path)


@pytest.mark.parametrize(
    ("name", "content", "message"),
    (
        pytest.param("invalid-utf8", b"\xff\xfe", "log unreadable", id="invalid-utf8"),
        pytest.param("non-json", b"just progress prose\n", "no JSON evidence", id="non-json"),
        pytest.param(
            "invalid-terminal-shape",
            json.dumps({"schema_version": 1, "kind": "workflow-card", "status": "passed"}).encode()
            + b"\n",
            "no JSON evidence",
            id="invalid-terminal-shape",
        ),
    ),
)
def test_extract_terminal_json_rejects_unreadable_or_invalid_records(
    tmp_path: Path,
    name: str,
    content: bytes,
    message: str,
) -> None:
    log_path = tmp_path / f"{name}.jsonl"
    log_path.write_bytes(content)
    before = log_path.read_bytes()

    with pytest.raises(ValueError, match=message):
        manager._extract_terminal_json(str(log_path))

    assert log_path.read_bytes() == before


def test_extract_terminal_json_replays_public_incident_shape_with_byte_oracles(
    tmp_path: Path,
) -> None:
    """Synthetic replay: whole log has 3 NELs and its terminal record has 2."""

    payload = _verification_payload("before\u0085after")
    payload["reports"] = payload["reports"][:1]
    inner_result = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    terminal_record = json.dumps(
        {
            "type": "result",
            "result": inner_result,
            "structured_output": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    records = [
        json.dumps({"type": "progress", "message": "scan\u0085started"}, ensure_ascii=False),
        json.dumps({"type": "progress", "message": "terminal follows"}),
        terminal_record,
    ]
    raw = ("\n".join(records) + "\n").encode("utf-8")
    nel = "\u0085".encode("utf-8")
    assert raw.count(b"\n") == 3
    assert raw.count(nel) == 3
    assert raw.split(b"\n")[-2].count(nel) == 2
    assert b"\\u0085" in raw

    log_path = tmp_path / "synthetic-incident.jsonl"
    log_path.write_bytes(raw)
    before = log_path.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()

    assert manager._extract_terminal_json(str(log_path)) == payload

    after = log_path.read_bytes()
    assert after == before
    assert hashlib.sha256(after).hexdigest() == before_hash
