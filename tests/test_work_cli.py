from __future__ import annotations

import json

import pytest

from paulsha_cortex import cli


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, payload):
        self.requests.append(payload)
        return self.responses.pop(0)


def _envelope(items):
    return {
        "schema": "cortex-work/v1",
        "generated_at": "2026-07-17T10:00:00Z",
        "sequence": 1,
        "degraded": False,
        "providers": [],
        "items": items,
    }


def _item(work_id="work", state="todo"):
    return {
        "work_id": work_id,
        "repo": "example/acme",
        "title": "Work",
        "state": state,
        "phase": None,
        "facets": [],
        "sources": [],
        "next_actions": ["start"],
        "workflow_run_id": None,
        "updated_at": "2026-07-17T10:00:00Z",
    }


def test_cortex_list_json_sends_filters_and_prints_single_object(capsys):
    client = FakeClient([{"ok": True, "data": _envelope([_item()])}])
    rc = cli.main(
        ["list", "--repo", "example/acme", "--state", "on-going", "--json"],
        work_client=client,
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["work_id"] == "work"
    assert client.requests == [
        {
            "kind": "list_work_items",
            "repo": "example/acme",
            "states": ["on-going"],
            "include_done": False,
            "explain": False,
        }
    ]


def test_cortex_list_human_defaults_hide_done(capsys):
    client = FakeClient([{"ok": True, "data": _envelope([_item()])}])
    assert cli.main(["list"], work_client=client) == 0
    output = capsys.readouterr().out
    assert "example/acme" in output
    assert "todo" in output
    assert client.requests[0]["include_done"] is False


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["list", "--help"], "--state"),
        (["work", "show", "--help"], "--repo"),
    ],
)
def test_work_read_commands_expose_standard_help(argv, expected, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(argv)

    assert error.value.code == 0
    assert expected in capsys.readouterr().out


def test_cortex_list_human_explain_displays_explanation(capsys):
    payload = _envelope([_item()])
    payload["explanations"] = {
        "work": {"work_id": "work", "reducer_trace": [{"rule": "active_todo"}]}
    }
    client = FakeClient([{"ok": True, "data": payload}])

    assert cli.main(["list", "--explain"], work_client=client) == 0

    output = capsys.readouterr().out
    assert "active_todo" in output
    assert client.requests[0]["explain"] is True


def test_cortex_work_show_json_and_explain(capsys):
    payload = _envelope([])
    payload.pop("items")
    payload["item"] = _item("active", "on-going")
    payload["explanation"] = {"work_id": "active", "reducer_trace": []}
    client = FakeClient([{"ok": True, "data": payload}])

    rc = cli.main(["work", "show", "active", "--json", "--explain"], work_client=client)

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["item"]["work_id"] == "active"
    assert client.requests == [
        {"kind": "explain_work_item", "work_id": "active"}
    ]


def test_cortex_work_show_passes_repo_for_composite_identity(capsys):
    payload = _envelope([])
    payload.pop("items")
    payload["item"] = _item("shared", "todo")
    client = FakeClient([{"ok": True, "data": payload}])

    assert cli.main(
        ["work", "show", "shared", "--repo", "example/acme", "--json"],
        work_client=client,
    ) == 0

    assert json.loads(capsys.readouterr().out)["item"]["work_id"] == "shared"
    assert client.requests == [
        {
            "kind": "get_work_item",
            "work_id": "shared",
            "repo": "example/acme",
        }
    ]


def test_cortex_work_show_reports_socket_error_to_stderr(capsys):
    client = FakeClient([{"ok": False, "error": "unknown work item"}])
    assert cli.main(["work", "show", "missing"], work_client=client) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown work item" in captured.err


def test_cortex_work_help_lists_read_and_manager_actions(capsys):
    assert cli.main(["work", "--help"]) == 0
    output = capsys.readouterr().out
    for command in (
        "show", "link", "unlink", "start", "resume", "retry-build", "auto",
        "abandon", "review-attest", "ship",
    ):
        assert command in output


def test_cortex_work_mutation_routes_to_coordinator(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "paulsha_cortex.coordinator.cli.main",
        lambda argv: calls.append(argv) or 0,
    )

    assert cli.main(["work", "start", "work", "--repo", "example/acme"]) == 0
    assert calls == [["work", "start", "work", "--repo", "example/acme"]]
