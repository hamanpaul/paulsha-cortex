"""#506 / D5：headless-only hook 儀器化——claude 先。

本檔的第一責任不是「事件有沒有寫出來」，而是使用者的硬約束：**hook 絕不能影響
正常的互動式 agent 使用**。因此 A 節（自守）與 E 節（注入面）先於功能面存在——
它們釘死的是「沒有 `PSC_JOB_ID` 就什麼都不會發生」與「hook 只經 launcher 注入、
不碰使用者全域設定」這兩條結構性保證。
"""

from __future__ import annotations

import io
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import launcher as launcher_module
from paulsha_cortex.coordinator.launcher import (
    SubprocessLauncher,
    build_claude_argv,
    build_codex_argv,
    build_copilot_argv,
)
from paulsha_cortex.monitor.event_spool import EVENT_SCHEMA, EventSpool
from paulsha_cortex.porcelain import headless_hook


HOOK_TEMPLATES = Path(__file__).resolve().parents[1] / "paulsha_cortex" / "scripts" / "hooks"

JOB_ENV = {"PSC_JOB_ID": "job-42"}
MUTATION = "gh issue comment 123 --body hi"


def _payload(command: str = MUTATION, *, cwd: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": "s-1",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
    }
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def _events(root: Path) -> list[dict[str, object]]:
    if not root.exists():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.json"))
    ]


def _fake_git_remote(url: str):
    class _Completed:
        returncode = 0
        stdout = url

    def _runner(argv, **kwargs):
        assert argv[0] == "git"
        return _Completed()

    return _runner


def _builder_argv(**overrides) -> list[str]:
    kwargs: dict[str, object] = {
        "prompt": "P",
        "slice_id": "slice-a",
        "log_dir": "/lg",
        "worktree": None,
    }
    kwargs.update(overrides)
    return build_claude_argv(**kwargs)  # type: ignore[arg-type]


def _settings_from(argv: list[str]) -> dict[str, object] | None:
    if "--settings" not in argv:
        return None
    return json.loads(argv[argv.index("--settings") + 1])


def _hook_commands(settings: dict[str, object]) -> list[str]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    return [
        entry.get("command", "")
        for groups in hooks.values()
        for group in groups
        for entry in group.get("hooks", [])
    ]


# ---------------------------------------------------------------------------
# A. 自守：沒有 PSC_JOB_ID 就是完全的 no-op（互動 session 的唯一結局）
# ---------------------------------------------------------------------------


def test_without_the_job_marker_nothing_is_emitted(tmp_path: Path) -> None:
    """互動 session 的證明：連 spool 目錄都不會出現。"""

    root = tmp_path / "event-spool"
    emitted = headless_hook.emit_for_tool_use(
        _payload(), env={"HOME": str(tmp_path)}, spool=EventSpool(root)
    )

    assert emitted == ()
    assert not root.exists()
    assert _events(root) == []


@pytest.mark.parametrize("marker", ["", "   ", None])
def test_a_blank_or_missing_marker_is_not_a_headless_job(marker: object) -> None:
    env = {} if marker is None else {"PSC_JOB_ID": marker}
    assert headless_hook.headless_job_id(env) is None  # type: ignore[arg-type]


def test_the_marker_is_checked_before_anything_else_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """無標記時不解析命令、不解 repo、不建 spool——連一個 subprocess 都不起。

    這條比「沒有事件」更強：它釘住的是 hook 在互動 session 裡的**成本與副作用皆
    為零**，而不只是輸出為空。
    """

    def _explode(*args, **kwargs):  # pragma: no cover - 觸發即失敗
        raise AssertionError("no-op path must not run a subprocess")

    monkeypatch.setattr(headless_hook.subprocess, "run", _explode)
    monkeypatch.setattr(
        headless_hook,
        "parse_bash_command",
        lambda command: pytest.fail("no-op path must not parse the command"),
    )

    root = tmp_path / "event-spool"
    assert headless_hook.emit_for_tool_use(_payload(), env={}, spool=EventSpool(root)) == ()
    assert not root.exists()


def test_the_cli_is_a_no_op_without_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "event-spool"
    monkeypatch.delenv("PSC_JOB_ID", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_payload())))
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    code = headless_hook.main(["post-tool-use", "--spool-root", str(root)])

    assert code == 0
    assert captured.getvalue() == ""  # PostToolUse 的 stdout 會被當決策讀，保持空
    assert not root.exists()


def test_the_cli_stays_silent_and_successful_with_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "event-spool"
    monkeypatch.setenv("PSC_JOB_ID", "job-42")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_payload("gh issue edit 5 -R o/r"))))
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    code = headless_hook.main(["post-tool-use", "--spool-root", str(root)])

    assert code == 0
    assert captured.getvalue() == ""
    assert [event["payload"]["number"] for event in _events(root)] == [5]  # type: ignore[index]


# ---------------------------------------------------------------------------
# B. 有標記：寫出符合 D4 契約的事件
# ---------------------------------------------------------------------------


def test_a_headless_job_emits_a_d4_github_object_event(tmp_path: Path) -> None:
    root = tmp_path / "event-spool"

    emitted = headless_hook.emit_for_tool_use(
        _payload("gh issue comment 123 -R acme/demo --body hi"),
        env=JOB_ENV,
        spool=EventSpool(root),
    )

    assert emitted == ("acme/demo#123",)
    (event,) = _events(root)
    assert event["schema_version"] == EVENT_SCHEMA
    assert event["event_type"] == "github_object"
    assert event["source"] == headless_hook.EVENT_SOURCE == "agent-hook:claude"
    assert event["job_id"] == "job-42"
    assert event["payload"] == {
        "repo": "acme/demo",
        "kind": "github_issue",
        "number": 123,
        "action": "issue-comment",
    }


def test_events_name_an_object_and_never_carry_its_state(tmp_path: Path) -> None:
    """hint 不是 authority：信封裡沒有任何可以拿來直接改鏡像的欄位。"""

    root = tmp_path / "event-spool"
    headless_hook.emit_for_tool_use(
        _payload("gh issue edit 7 -R acme/demo --add-label bug --title new"),
        env=JOB_ENV,
        spool=EventSpool(root),
    )

    (event,) = _events(root)
    assert set(event["payload"]) & {"state", "labels", "title", "body", "updated_at"} == set()


def test_the_event_is_consumable_by_the_d4_scanner(tmp_path: Path) -> None:
    """端到端的契約面：D4 消費端掃得到、認得出、不隔離。"""

    root = tmp_path / "event-spool"
    spool = EventSpool(root)
    headless_hook.emit_for_tool_use(
        _payload("gh pr comment 45 -R acme/demo --body ok"), env=JOB_ENV, spool=spool
    )

    scan = spool.scan()

    assert scan.quarantined == ()
    assert [(hint.repo, hint.kind, hint.number) for hint in scan.hints] == [
        ("acme/demo", "github_pr", 45)
    ]


def test_one_command_can_name_several_objects(tmp_path: Path) -> None:
    root = tmp_path / "event-spool"

    emitted = headless_hook.emit_for_tool_use(
        _payload("gh issue comment 1 -R o/r --body a && gh pr edit 2 -R o/r --add-label x"),
        env=JOB_ENV,
        spool=EventSpool(root),
    )

    assert sorted(emitted) == ["o/r#1", "o/r#2"]


def test_the_same_object_twice_in_one_command_is_one_event(tmp_path: Path) -> None:
    root = tmp_path / "event-spool"

    emitted = headless_hook.emit_for_tool_use(
        _payload("gh issue comment 9 -R o/r --body a; gh issue comment 9 -R o/r --body b"),
        env=JOB_ENV,
        spool=EventSpool(root),
    )

    assert emitted == ("o/r#9",)
    assert len(_events(root)) == 1


def test_the_repo_falls_back_to_the_job_worktree_remote(tmp_path: Path) -> None:
    root = tmp_path / "event-spool"

    emitted = headless_hook.emit_for_tool_use(
        _payload(cwd=str(tmp_path)),
        env=JOB_ENV,
        spool=EventSpool(root),
        runner=_fake_git_remote("git@github.com:acme/demo.git\n"),
    )

    assert emitted == ("acme/demo#123",)


def test_an_unresolvable_repo_drops_the_hint_instead_of_guessing(tmp_path: Path) -> None:
    root = tmp_path / "event-spool"

    class _Failed:
        returncode = 128
        stdout = ""

    emitted = headless_hook.emit_for_tool_use(
        _payload(cwd=str(tmp_path)),
        env=JOB_ENV,
        spool=EventSpool(root),
        runner=lambda argv, **kwargs: _Failed(),
    )

    assert emitted == ()
    assert not root.exists()


def test_the_default_spool_follows_the_configured_monitor_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不指定 spool 時走 D4 的 `monitor_event_spool_root()`，不是自訂路徑。"""

    monkeypatch.setenv("PSC_MONITOR_STATE_ROOT", str(tmp_path / "monitor"))

    emitted = headless_hook.emit_for_tool_use(
        _payload("gh issue close 3 -R o/r"), env=JOB_ENV
    )

    assert emitted == ("o/r#3",)
    assert len(_events(tmp_path / "monitor" / "event-spool")) == 1


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:acme/demo.git\n", "acme/demo"),
        ("https://github.com/acme/demo.git", "acme/demo"),
        ("https://github.com/acme/demo", "acme/demo"),
        ("ssh://git@github.com/acme/demo.git", "acme/demo"),
        ("", None),
        ("not-a-remote", None),
    ],
)
def test_remote_urls_resolve_to_owner_and_name(url: str, expected: str | None) -> None:
    assert headless_hook.repo_from_remote_url(url) == expected


# ---------------------------------------------------------------------------
# C. 命令解析：只認會改動遠端物件的動作
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("gh issue comment 12 -R o/r", [("o/r", "github_issue", 12)]),
        ("gh issue close 12 --repo o/r", [("o/r", "github_issue", 12)]),
        ("gh issue edit 12 --repo=o/r --add-label bug", [("o/r", "github_issue", 12)]),
        ("gh pr merge 12 -R o/r --squash", [("o/r", "github_pr", 12)]),
        ("gh pr review 12 -R o/r --approve", [("o/r", "github_pr", 12)]),
        (
            "gh pr comment https://github.com/acme/demo/pull/77 --body y",
            [("acme/demo", "github_pr", 77)],
        ),
        ("gh api -X PATCH repos/o/r/issues/9", [("o/r", "github_issue", 9)]),
        ("gh api --method=DELETE repos/o/r/pulls/9/requested_reviewers", [("o/r", "github_pr", 9)]),
        ("gh api repos/o/r/issues/9/comments -f body=hi", [("o/r", "github_issue", 9)]),
        (
            "gh api https://api.github.com/repos/o/r/issues/9/labels -f labels[]=bug",
            [("o/r", "github_issue", 9)],
        ),
        ("gh issue edit 12 -R o/r --add-label 3", [("o/r", "github_issue", 12)]),
    ],
)
def test_mutating_commands_name_their_object(
    command: str, expected: list[tuple[str, str, int]]
) -> None:
    assert [
        (ref.repo, ref.kind, ref.number) for ref in headless_hook.parse_bash_command(command)
    ] == expected


@pytest.mark.parametrize(
    "command",
    [
        "gh issue view 12 -R o/r",
        "gh issue list -R o/r",
        "gh pr diff 12 -R o/r",
        "gh pr checks 12 -R o/r",
        "gh api repos/o/r/issues/9",
        "gh api -X GET repos/o/r/issues/9",
        "gh api --method HEAD repos/o/r/issues/9",
        "gh repo view o/r",
        "gh release create v1",
        "git commit -m 'gh issue comment 12'",
        "echo gh issue comment 12 -R o/r",
        "python3 -m pytest -q",
        "",
        "   ",
    ],
)
def test_non_mutating_commands_name_nothing(command: str) -> None:
    assert headless_hook.parse_bash_command(command) == ()


def test_a_comment_edit_is_not_mistaken_for_its_issue() -> None:
    """`issues/comments/{id}` 的數字是留言 id，不是 issue 編號。"""

    assert headless_hook.parse_bash_command(
        "gh api -X PATCH repos/o/r/issues/comments/98765 -f body=x"
    ) == ()


def test_gh_placeholders_leave_the_repo_for_the_worktree_to_resolve() -> None:
    (ref,) = headless_hook.parse_bash_command("gh api -X PATCH repos/{owner}/{repo}/pulls/12")
    assert (ref.repo, ref.kind, ref.number) == (None, "github_pr", 12)


@pytest.mark.parametrize(
    "command",
    ['gh issue comment 1 -R o/r --body "unbalanced', "gh issue comment 0 -R o/r"],
)
def test_unparseable_or_impossible_commands_are_dropped_not_raised(command: str) -> None:
    assert headless_hook.parse_bash_command(command) == ()


# ---------------------------------------------------------------------------
# D. fire-and-forget：hook 的失敗永不外溢到 job
# ---------------------------------------------------------------------------


def test_an_unusable_spool_is_swallowed(tmp_path: Path) -> None:
    blocked = tmp_path / "event-spool"
    blocked.write_text("not a directory", encoding="utf-8")

    assert (
        headless_hook.emit_for_tool_use(
            _payload("gh issue comment 1 -R o/r"), env=JOB_ENV, spool=EventSpool(blocked)
        )
        == ()
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not-a-mapping",
        {},
        {"tool_name": "Edit", "tool_input": {"command": MUTATION}},
        {"tool_name": "Bash"},
        {"tool_name": "Bash", "tool_input": "not-a-mapping"},
        {"tool_name": "Bash", "tool_input": {"command": None}},
    ],
)
def test_a_malformed_payload_never_raises(payload: object, tmp_path: Path) -> None:
    root = tmp_path / "event-spool"
    assert headless_hook.emit_for_tool_use(payload, env=JOB_ENV, spool=EventSpool(root)) == ()
    assert not root.exists()


def test_only_bash_tool_calls_are_inspected(tmp_path: Path) -> None:
    root = tmp_path / "event-spool"
    payload = _payload()
    payload["tool_name"] = "Task"

    assert headless_hook.emit_for_tool_use(payload, env=JOB_ENV, spool=EventSpool(root)) == ()


def test_a_broken_git_lookup_never_raises(tmp_path: Path) -> None:
    def _explode(argv, **kwargs):
        raise OSError("git is not installed")

    assert (
        headless_hook.emit_for_tool_use(
            _payload(cwd=str(tmp_path)),
            env=JOB_ENV,
            spool=EventSpool(tmp_path / "event-spool"),
            runner=_explode,
        )
        == ()
    )


def test_the_cli_survives_garbage_on_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "event-spool"
    monkeypatch.setenv("PSC_JOB_ID", "job-42")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))

    assert headless_hook.main(["post-tool-use", "--spool-root", str(root)]) == 0
    assert not root.exists()


# ---------------------------------------------------------------------------
# E. 注入面：hook 只經 launcher 進入 job，永不落地使用者全域設定
# ---------------------------------------------------------------------------


def test_a_headless_claude_builder_carries_the_hook_on_argv() -> None:
    settings = _settings_from(_builder_argv())

    assert settings is not None
    assert list(settings) == ["hooks"]  # 只加 hook，不動 permissions／sandbox
    assert [group["matcher"] for group in settings["hooks"]["PostToolUse"]] == ["Bash"]
    assert _hook_commands(settings) == ["cortex headless-hook post-tool-use || true"]
    assert settings["hooks"]["PostToolUse"][0]["hooks"][0]["timeout"] > 0


def test_the_hook_command_is_a_registered_porcelain_command() -> None:
    from paulsha_cortex import porcelain

    porcelain.load_commands()
    command = _hook_commands(_settings_from(_builder_argv()) or {})[0]
    argv = shlex.split(command.split("||")[0])

    assert argv[0] == "cortex"
    assert argv[1] in porcelain.COMMANDS
    assert argv[2] == "post-tool-use"


@pytest.mark.parametrize("mode", ["read_only", "review_only"])
def test_read_only_personas_get_no_hook(mode: str) -> None:
    """planner 沒有 Bash、reviewer 是 read-only 契約——兩者都不該掛 hook。"""

    if mode == "read_only":
        argv = _builder_argv(read_only=True)
    else:
        with tempfile.TemporaryDirectory() as worktree:
            argv = _builder_argv(
                review_only=True,
                worktree=worktree,
                review_terminal_kind="workflow-review-result",
            )
    settings = _settings_from(argv)

    assert settings is None or "hooks" not in settings


def test_other_executors_are_untouched() -> None:
    """本次只做 claude：codex（JSONL 已被 parse）與 copilot 不得出現 hook。"""

    for argv in (
        build_codex_argv(prompt="P", slice_id="s", log_dir="/lg"),
        build_copilot_argv(prompt="P", slice_id="s", log_dir="/lg"),
    ):
        assert not any("headless-hook" in token for token in argv)


def test_the_launcher_marks_the_job_environment() -> None:
    calls: list[dict[str, object]] = []

    class _FakeProc:
        pid = 4242

    def _fake_popen(argv, *, cwd, env, stdout, stderr):
        calls.append({"argv": argv, "env": env})
        return _FakeProc()

    original = launcher_module.subprocess.Popen
    launcher_module.subprocess.Popen = _fake_popen
    try:
        with tempfile.TemporaryDirectory() as d:
            SubprocessLauncher("claude").launch(
                slice_id="slice-a",
                prompt="P",
                worktree=d,
                log_dir=str(Path(d) / "logs"),
            )
    finally:
        launcher_module.subprocess.Popen = original

    env = calls[0]["env"]
    assert env["PSC_JOB_ID"] == "slice-a"  # type: ignore[index]
    # hook 與 marker 成對出現在同一個 job 的 argv/env 裡。
    assert "cortex headless-hook post-tool-use" in calls[0]["argv"][2]  # type: ignore[index]


def test_the_preflight_environment_matches_the_job_environment() -> None:
    """`executor_environment()` 與 `launch()` 若不同步，preflight 就是安慰劑。"""

    calls: list[dict[str, object]] = []

    class _FakeProc:
        pid = 1

    def _fake_popen(argv, *, cwd, env, stdout, stderr):
        calls.append({"env": env})
        return _FakeProc()

    original = launcher_module.subprocess.Popen
    launcher_module.subprocess.Popen = _fake_popen
    try:
        with tempfile.TemporaryDirectory() as d:
            launcher = SubprocessLauncher("claude")
            launcher.launch(
                slice_id="preflight", prompt="P", worktree=d, log_dir=str(Path(d) / "logs")
            )
            launcher.executor_environment()
    finally:
        launcher_module.subprocess.Popen = original

    assert calls[0]["env"]["PSC_JOB_ID"] == "preflight"  # type: ignore[index]


def test_launching_a_job_writes_nothing_into_the_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """紅線的直接證明：派工一個 claude job 之後，使用者的 `~/.claude` 毫髮無傷。"""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    class _FakeProc:
        pid = 7

    original = launcher_module.subprocess.Popen
    launcher_module.subprocess.Popen = lambda argv, **kwargs: _FakeProc()
    try:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        SubprocessLauncher("claude").launch(
            slice_id="slice-a",
            prompt="P",
            worktree=str(worktree),
            log_dir=str(tmp_path / "logs"),
        )
    finally:
        launcher_module.subprocess.Popen = original

    assert list(home.iterdir()) == []


def test_the_packaged_claude_template_never_ships_the_hook() -> None:
    """`scripts/hooks/claude.json` 是**使用者全域**安裝用的模板（paulshaclaw thin
    install 的切點）。事件 hook 一旦出現在這裡，就等於走上使用者全域設定那條被
    明確禁止的路；因此這條測試釘死它永遠不在模板裡。
    """

    template = (HOOK_TEMPLATES / "claude.json").read_text(encoding="utf-8")

    assert "headless-hook" not in template
    assert "PostToolUse" not in template


def test_no_executable_string_names_a_user_level_claude_settings_file() -> None:
    """全套件掃描：`~/.claude/settings.json` 只出現在文件（docstring／註解）裡。

    掃 AST 而非原始碼文字，因此 docstring 與註解裡的說明不算數——只有真的會被當成
    資料用的字面值才會被抓到。目前結果是空集合：本 repo 沒有任何一條路徑會去組出
    使用者全域的 claude 設定檔路徑，更不用說寫它。
    """

    import ast

    package = Path(__file__).resolve().parents[1] / "paulsha_cortex"
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        documentation = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in documentation:
                continue
            if ".claude/settings.json" in node.value or node.value == "settings.json":
                offenders.append(f"{path.relative_to(package)}:{node.lineno}")

    assert offenders == []


def test_the_hook_never_touches_the_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """寫入端的同一條紅線：hook 只寫 spool，`$HOME` 完全不動。"""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    emitted = headless_hook.emit_for_tool_use(
        _payload("gh issue comment 8 -R o/r --body x"),
        env=JOB_ENV,
        spool=EventSpool(tmp_path / "event-spool"),
    )

    assert emitted == ("o/r#8",)
    assert list(home.iterdir()) == []


# ---------------------------------------------------------------------------
# F. 端到端：launcher 注入 → job 內 gh mutation → 事件落 spool
# ---------------------------------------------------------------------------


def test_end_to_end_from_launcher_injection_to_a_consumable_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """走完整條路：launcher 組出 hook 宣告與 job env，hook 依那份 env 執行，
    事件落進 spool 並被 D4 消費端認得。中間沒有任何一步碰到使用者全域設定。
    """

    calls: list[dict[str, object]] = []

    class _FakeProc:
        pid = 99

    def _fake_popen(argv, *, cwd, env, stdout, stderr):
        calls.append({"argv": argv, "env": env})
        return _FakeProc()

    original = launcher_module.subprocess.Popen
    launcher_module.subprocess.Popen = _fake_popen
    try:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        SubprocessLauncher("claude").launch(
            slice_id="job-e2e",
            prompt="P",
            worktree=str(worktree),
            log_dir=str(tmp_path / "logs"),
        )
    finally:
        launcher_module.subprocess.Popen = original

    job_env = calls[0]["env"]
    script = calls[0]["argv"][2]  # type: ignore[index]
    settings = json.loads(
        [token for token in shlex.split(script) if token.startswith('{"hooks"')][0]
    )
    hook_command = _hook_commands(settings)[0]

    # job 內跑的 gh mutation：以 launcher 實際交給 job 的 env 執行 hook 命令。
    root = tmp_path / "spool"
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps(_payload("gh issue comment 314 -R acme/demo --body done"))),
    )
    monkeypatch.setattr(os, "environ", dict(job_env))  # type: ignore[arg-type]
    assert headless_hook.main(
        [*shlex.split(hook_command.split("||")[0])[2:], "--spool-root", str(root)]
    ) == 0

    owned = EventSpool(root, job_id="job-e2e")
    scan = EventSpool(root).scan()
    assert [(hint.repo, hint.kind, hint.number) for hint in scan.hints] == [
        ("acme/demo", "github_issue", 314)
    ]
    assert scan.hints[0].event.job_id == "job-e2e"
    assert scan.hints[0].path.parent == owned.root
    assert not tuple(root.glob("*.json"))
    assert scan.quarantined == ()
