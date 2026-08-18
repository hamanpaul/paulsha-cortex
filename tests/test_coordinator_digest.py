from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import digest as digest_module

FIXED_NOW = "2026-08-10T12:34:56.789012+00:00"


def _sample_status(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "updated_at": "2026-08-10T12:30:00+00:00",
        "degraded": False,
        "degraded_reason": None,
        "ready": ["slice-a"],
        "held": [{"slice_id": "slice-b", "reasons": ["dispatch-hold"]}],
        "attention": [{"slice_id": "slice-c", "slice_state": "needs_human", "reason": "verify-failed"}],
        "recent_done": [{"slice_id": "slice-z", "gate_status": "passed", "at": "2026-08-10T12:00:00+00:00"}],
        "in_flight": [],
    }
    base.update(overrides)
    return base


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_assemble_digest_builds_structured_summary_from_status_snapshot() -> None:
    status = _sample_status()

    result = digest_module.assemble_digest(status, now=FIXED_NOW)

    assert result["schema"] == digest_module.DIGEST_SCHEMA
    assert result["generated_at"] == FIXED_NOW
    assert result["status_updated_at"] == "2026-08-10T12:30:00+00:00"
    assert result["degraded"] is False
    assert result["degraded_reason"] is None
    assert result["counts"] == {
        "attention": 1,
        # #669：claim 判定不可 claim 而刻意不建 run 的 work item 計數。
        "not_claimable": 0,
        "ready": 1,
        "held": 1,
        "recent_done": 1,
    }
    assert result["attention"] == status["attention"]
    assert result["ready"] == status["ready"]
    assert result["held"] == status["held"]
    assert result["recent_done"] == status["recent_done"]
    assert "slice-c" in result["summary_text"]
    assert "verify-failed" in result["summary_text"]


def test_assemble_digest_tolerates_sparse_or_degraded_status() -> None:
    status = {"degraded": True, "degraded_reason": "stalled"}

    result = digest_module.assemble_digest(status, now=FIXED_NOW)

    assert result["degraded"] is True
    assert result["degraded_reason"] == "stalled"
    assert result["counts"] == {
        "attention": 0,
        "not_claimable": 0,
        "ready": 0,
        "held": 0,
        "recent_done": 0,
    }
    assert result["attention"] == []
    assert result["ready"] == []
    assert result["held"] == []
    assert result["recent_done"] == []


def test_render_digest_text_lists_attention_ready_held_recent_done() -> None:
    digest = digest_module.assemble_digest(_sample_status(), now=FIXED_NOW)

    text = digest_module.render_digest_text(digest)

    assert f"digest @ {FIXED_NOW}" in text
    assert "attention=1 not_claimable=0 ready=1 held=1 recent_done=1" in text
    assert "- slice-c: verify-failed" in text
    assert "- slice-a" in text
    assert "- slice-b: dispatch-hold" in text
    assert "- slice-z: passed @ 2026-08-10T12:00:00+00:00" in text


def test_render_digest_text_omits_empty_sections() -> None:
    digest = digest_module.assemble_digest({}, now=FIXED_NOW)

    text = digest_module.render_digest_text(digest)

    assert "attention:" not in text
    assert "ready:" not in text
    assert "held:" not in text
    assert "recent_done:" not in text


def test_load_delivery_command_returns_none_when_unset() -> None:
    assert digest_module.load_delivery_command(env={}) is None


def test_load_delivery_command_returns_none_when_blank() -> None:
    assert digest_module.load_delivery_command(env={"PSC_DIGEST_DELIVERY_CMD": "   "}) is None


def test_load_delivery_command_parses_typed_argv() -> None:
    command = digest_module.load_delivery_command(
        env={"PSC_DIGEST_DELIVERY_CMD": "/usr/bin/env relay --channel ops"}
    )

    assert command == ("/usr/bin/env", "relay", "--channel", "ops")


def test_load_delivery_command_rejects_malformed_quoting() -> None:
    with pytest.raises(ValueError):
        digest_module.load_delivery_command(env={"PSC_DIGEST_DELIVERY_CMD": "relay \"unterminated"})


def test_deliver_via_command_pipes_json_digest_via_fake_runner() -> None:
    digest = digest_module.assemble_digest(_sample_status(), now=FIXED_NOW)
    captured: dict[str, object] = {}

    def fake_runner(argv, *, input, shell, capture_output, timeout):  # noqa: A002 - match subprocess.run kw
        captured["argv"] = list(argv)
        captured["input"] = input
        captured["shell"] = shell
        captured["capture_output"] = capture_output
        captured["timeout"] = timeout
        return _FakeCompleted(returncode=0, stdout=b"ok\n", stderr=b"")

    result = digest_module.deliver_via_command(
        digest, ("relay", "--channel", "ops"), runner=fake_runner, timeout=5.0
    )

    assert result == {"command": ["relay", "--channel", "ops"], "returncode": 0, "stderr": ""}
    assert captured["argv"] == ["relay", "--channel", "ops"]
    assert captured["shell"] is False
    assert captured["capture_output"] is True
    assert captured["timeout"] == 5.0
    piped = json.loads(captured["input"])
    assert piped["schema"] == digest_module.DIGEST_SCHEMA
    assert piped["attention"][0]["slice_id"] == "slice-c"


def test_deliver_via_command_raises_on_nonzero_exit_without_writing_anything() -> None:
    digest = digest_module.assemble_digest(_sample_status(), now=FIXED_NOW)

    def fake_runner(argv, **kwargs):
        return _FakeCompleted(returncode=3, stdout=b"", stderr=b"boom")

    with pytest.raises(digest_module.DigestDeliveryError) as excinfo:
        digest_module.deliver_via_command(digest, ("relay",), runner=fake_runner)

    assert excinfo.value.returncode == 3
    assert excinfo.value.stderr == "boom"
    assert excinfo.value.command == ("relay",)


def test_deliver_via_command_raises_on_timeout() -> None:
    digest = digest_module.assemble_digest(_sample_status(), now=FIXED_NOW)

    def fake_runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

    with pytest.raises(digest_module.DigestDeliveryError):
        digest_module.deliver_via_command(digest, ("relay",), runner=fake_runner, timeout=1.0)


def test_write_outbox_digest_creates_json_file_under_outbox_root(tmp_path: Path) -> None:
    digest = digest_module.assemble_digest(_sample_status(), now=FIXED_NOW)
    outbox_root = tmp_path / "digest" / "outbox"

    path = digest_module.write_outbox_digest(digest, outbox_root=outbox_root)

    assert path.parent == outbox_root
    assert path.suffix == ".json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["schema"] == digest_module.DIGEST_SCHEMA
    assert on_disk["attention"][0]["slice_id"] == "slice-c"


def test_write_outbox_digest_does_not_collide_on_repeated_same_timestamp(tmp_path: Path) -> None:
    digest = digest_module.assemble_digest(_sample_status(), now=FIXED_NOW)
    outbox_root = tmp_path / "outbox"

    first = digest_module.write_outbox_digest(digest, outbox_root=outbox_root)
    second = digest_module.write_outbox_digest(digest, outbox_root=outbox_root)

    assert first != second
    assert first.exists()
    assert second.exists()


def test_emit_digest_defaults_to_file_outbox_when_no_delivery_cmd(tmp_path: Path) -> None:
    outbox_root = tmp_path / "outbox"

    envelope = digest_module.emit_digest(
        status_provider=lambda: _sample_status(),
        now_fn=lambda: FIXED_NOW,
        env={},
        outbox_root=outbox_root,
    )

    assert envelope["schema"] == digest_module.DIGEST_SCHEMA
    assert envelope["delivery"]["method"] == "file"
    written_path = Path(envelope["delivery"]["path"])
    assert written_path.parent == outbox_root
    on_disk = json.loads(written_path.read_text(encoding="utf-8"))
    assert on_disk["attention"][0]["slice_id"] == "slice-c"


def test_emit_digest_uses_delivery_command_when_env_set_and_skips_file_outbox(tmp_path: Path) -> None:
    outbox_root = tmp_path / "outbox"
    captured: dict[str, object] = {}

    def fake_runner(argv, *, input, shell, capture_output, timeout):  # noqa: A002
        captured["argv"] = list(argv)
        captured["input"] = input
        return _FakeCompleted(returncode=0, stdout=b"", stderr=b"")

    envelope = digest_module.emit_digest(
        status_provider=lambda: _sample_status(),
        now_fn=lambda: FIXED_NOW,
        env={"PSC_DIGEST_DELIVERY_CMD": "relay --channel ops"},
        runner=fake_runner,
        outbox_root=outbox_root,
    )

    assert envelope["delivery"]["method"] == "command"
    assert envelope["delivery"]["returncode"] == 0
    assert captured["argv"] == ["relay", "--channel", "ops"]
    assert not outbox_root.exists()


def test_emit_digest_propagates_command_failure_without_writing_file(tmp_path: Path) -> None:
    outbox_root = tmp_path / "outbox"

    def fake_runner(argv, **kwargs):
        return _FakeCompleted(returncode=1, stdout=b"", stderr=b"nope")

    with pytest.raises(digest_module.DigestDeliveryError):
        digest_module.emit_digest(
            status_provider=lambda: _sample_status(),
            now_fn=lambda: FIXED_NOW,
            env={"PSC_DIGEST_DELIVERY_CMD": "relay"},
            runner=fake_runner,
            outbox_root=outbox_root,
        )

    assert not outbox_root.exists()
