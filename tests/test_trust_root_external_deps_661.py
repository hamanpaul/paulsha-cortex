"""#661：job／服務需要的**所有**外部程式進登記表，＋ preflight 的 typed-argv 落定。

兩個實機 doctor FAIL 的回歸測試：

- `review-sandbox`：`srt` 從未進過 toolchain 名冊，且它是 npm 套件樹、單檔複製會壞；
- `preflight`：`PSC_PREFLIGHT_CMD` 沒有一個落在保護面內、又符合 typed-argv 的值。

本檔刻意同時釘住「**兩張名冊**」這個設計：`EXECUTOR_TOOLS` 之所以不能直接擴充，是
因為它同時是 dispatch 的 executor 名字判準（`executor_hardening_profile()` fail-closed，
spec §R8）。把 `srt` 併進去會讓 `executor: srt` 這種派工變成合法。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex import doctor, preflight_ci
from paulsha_cortex.coordinator.preflight import load_preflight_command
from paulsha_cortex.trust_root.permgen import (
    DEFAULT_LAYOUT,
    EXECUTOR_TOOLS,
    FOUR_WAY_SCHEME,
    JOB_PATH_SYSTEM_TAIL,
    MANAGER_SURFACE,
    PREFLIGHT_ADAPTER_MODULE,
    PREFLIGHT_BACKEND_DISTRIBUTION,
    PREFLIGHT_BACKEND_MODULE,
    SERVICE_TOOLS,
    SYSTEM_PROGRAMS,
    TOOLCHAIN_PROGRAMS,
    TOOLCHAIN_SYSTEM_RUNTIMES,
    THREE_WAY_SCHEME,
    ExecutorShape,
    UnknownExecutorProfileError,
    build_toolchain_plan,
    executor_hardening_profile,
    node_execution_surfaces,
    unresolved_node_execution_surfaces,
)
from paulsha_cortex.trust_root.registry import check_registry_equation

ALL_SCHEMES = (THREE_WAY_SCHEME, FOUR_WAY_SCHEME)


# ---------------------------------------------------------------------------
# 1. 盤點完整性：登記表要涵蓋「job／服務需要的所有外部程式」
# ---------------------------------------------------------------------------

def test_service_tools_cover_the_non_executor_programs_the_code_actually_execs() -> None:
    names = [tool.name for tool in SERVICE_TOOLS]
    assert names == ["srt", "openspec"]
    assert len(set(names)) == len(names)
    for tool in SERVICE_TOOLS:
        assert isinstance(tool.shape, ExecutorShape)
        assert tool.note.strip(), tool.name
        # 非 executor 沒有自己的 unit：不填消費者就等於「它跑在哪個加固面下」沒人知道。
        assert tool.consumed_by, tool.name


def test_toolchain_roster_covers_every_review_sandbox_executable() -> None:
    """#661 的核心不變式：doctor 要求的程式必須每一支都在某張名冊上。

    實機症狀就是這條不成立——四分部署把四個 executor 都搬進部署樹之後 doctor 仍紅在
    `review-sandbox`，因為它另外要求 `srt`／`bwrap`／`socat`，而登記表只認得 executor。
    `python3` 是部署 venv 自己的 interpreter（不是外部相依），因此不在名冊上。
    """
    covered = {tool.name for tool in TOOLCHAIN_PROGRAMS} | {p.name for p in SYSTEM_PROGRAMS}
    required = set(doctor.REVIEW_SANDBOX_EXECUTABLES) - {"python3"}
    assert required <= covered, sorted(required - covered)


def test_toolchain_programs_is_the_union_of_both_rosters() -> None:
    assert TOOLCHAIN_PROGRAMS == EXECUTOR_TOOLS + SERVICE_TOOLS
    names = [tool.name for tool in TOOLCHAIN_PROGRAMS]
    assert len(set(names)) == len(names), "兩張名冊不得有同名（同一棵 bin/ 會互相覆蓋）"


def test_system_program_roster_is_the_single_source_of_the_runtime_names() -> None:
    """`TOOLCHAIN_SYSTEM_RUNTIMES` 改為導出值，不再是寫死的 `("node",)`。"""
    assert TOOLCHAIN_SYSTEM_RUNTIMES == tuple(p.name for p in SYSTEM_PROGRAMS)
    names = set(TOOLCHAIN_SYSTEM_RUNTIMES)
    # `srt` 的兩支相依（doctor 會逐一實跑 `--version`）必須在系統層名冊上。
    assert {"bwrap", "socat"} <= names
    for program in SYSTEM_PROGRAMS:
        assert program.required_by, program.name
        assert program.source.strip() and program.note.strip(), program.name


def test_node_dependency_is_recorded_for_every_node_script_program() -> None:
    """node script ⇒ 必須 `needs_node` 且 `copy_tree`（單搬進入點會缺 node_modules）。"""
    for tool in TOOLCHAIN_PROGRAMS:
        if tool.shape is ExecutorShape.NODE_SCRIPT:
            assert tool.needs_node, tool.name
            assert tool.copy_tree, tool.name
    assert "node" in TOOLCHAIN_SYSTEM_RUNTIMES


# ---------------------------------------------------------------------------
# 2. 為什麼是兩張表：dispatch 的 fail-closed 不得被盤點完整性稀釋
# ---------------------------------------------------------------------------

def test_non_executor_programs_are_still_rejected_as_dispatch_executors() -> None:
    """把 `srt`／`openspec` 併進 `EXECUTOR_TOOLS` 就會讓這條測試變紅。

    `executor_hardening_profile()` 是 dispatch 的名字判準（spec §R8）：表上有的名字
    就是一個合法的 executor。盤點完整性不可以用「往那張表塞東西」來換。
    """
    for tool in SERVICE_TOOLS:
        with pytest.raises(UnknownExecutorProfileError):
            executor_hardening_profile(tool.name)
    # 四個真正的 executor 仍然解得到剖面（沒有被這次改動波及）。
    assert {t.name for t in EXECUTOR_TOOLS} == {"codex", "claude", "copilot", "agy"}
    for tool in EXECUTOR_TOOLS:
        assert executor_hardening_profile(tool.name).profile_id in {"strict", "jit"}


# ---------------------------------------------------------------------------
# 3. #643 的剖面推導看不到的那一格（只列舉，不宣稱已驗證）
# ---------------------------------------------------------------------------

def test_node_execution_surfaces_enumerate_every_needs_node_service_tool() -> None:
    surfaces = node_execution_surfaces()
    expected = {
        (tool.name, consumer)
        for tool in SERVICE_TOOLS
        if tool.needs_node
        for consumer in tool.consumed_by
    }
    assert {(s.program, s.surface) for s in surfaces} == expected
    assert expected, "至少要有一列，否則這個機制形同不存在"


def test_known_wx_conflicts_cannot_disappear_silently() -> None:
    """`srt`／`openspec` 目前都跑在禁 W+X 的面上——這是**未決點**，不是已修好。

    釘住它的理由：這兩格如果哪天靜默變空（例如有人把 `consumed_by` 清掉、或把
    `MemoryDenyWriteExecute` 全域拿掉），那不是「問題解決了」而是「問題不見了」。
    真正解決的樣子是 operator 裁決後**剖面改變**，那時本測試會紅，改的人必須連同
    理由一起更新它。
    """
    unresolved = {s.program: s for s in unresolved_node_execution_surfaces()}
    assert set(unresolved) == {"srt", "openspec"}
    assert unresolved["srt"].surface == "claude"
    assert unresolved["openspec"].surface == MANAGER_SURFACE
    for surface in unresolved.values():
        assert "MemoryDenyWriteExecute=yes" in surface.detail
        assert surface.allows_wx is False


@pytest.mark.skip(
    reason=(
        "#638／#657 的教訓：`MemoryDenyWriteExecute=yes` 對 V8 的效果是 **OS／systemd "
        "層語意**，這個測試環境沒有那個加固面（也沒有第二個 UID 與 systemd-run 授權），"
        "重現不了。在這裡跑 `srt --version` 只會證明「沒有加固面時它跑得起來」，"
        "那與待驗的命題無關，卻會讓人以為已經驗過。實機量測步驟在 runbook 第 4e 步"
        "（systemd-run 帶該 unit 的關鍵 property），結果回報 #661 的 follow-up 由 "
        "operator 裁決。"
    )
)
def test_srt_runs_under_the_reviewer_hardening_surface() -> None:  # pragma: no cover
    raise AssertionError("見 skip 理由：需要真實的 systemd 加固面")


# ---------------------------------------------------------------------------
# 4. 落位計畫：整包搬 ＋ symlink（#661 實機量到的兩個後果）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_toolchain_plan_covers_both_rosters_and_the_system_layer(scheme) -> None:
    text = "\n".join(build_toolchain_plan(scheme, DEFAULT_LAYOUT))
    for tool in TOOLCHAIN_PROGRAMS:
        assert f"--- {tool.name}（" in text, tool.name
    for program in SYSTEM_PROGRAMS:
        assert program.name in text, program.name


def test_toolchain_plan_demands_a_symlink_for_every_copy_tree_program() -> None:
    """單檔複製會壞兩件事，其中一件是**無聲**的——計畫必須把這句話帶在身上。"""
    lines = build_toolchain_plan(FOUR_WAY_SCHEME, DEFAULT_LAYOUT)
    text = "\n".join(lines)
    for tool in TOOLCHAIN_PROGRAMS:
        if not tool.copy_tree:
            continue
        if tool.name == "copilot":
            assert f'#     cp -a "$PKG" "$tmp/{tool.name}"' in text
            assert f'#     cp -a "$PKG" {DEFAULT_LAYOUT.toolchain_lib}/{tool.name}' not in text
            assert f'#     test -f {DEFAULT_LAYOUT.toolchain_lib}/{tool.name}/"$ENTRY_REL"' in text
            assert f'#     mv -T "$tmp/{tool.name}" {DEFAULT_LAYOUT.toolchain_lib}/{tool.name}' in text
            assert f"#     cat > {DEFAULT_LAYOUT.toolchain_bin}/{tool.name} <<EOF" in text
            assert '#     NODE_ABS="$(readlink -f "$(command -v node)")"' in text
            assert (
                f'#     case "$NODE_ABS" in {DEFAULT_LAYOUT.toolchain_root}/*) '
                'echo "node must stay system-level" >&2; exit 1 ;; esac'
            ) in text
            assert (
                f'#     exec $NODE_ABS "{DEFAULT_LAYOUT.toolchain_lib}/{tool.name}/$ENTRY_REL" '
                '"\\$@"'
            ) in text
            assert f"#     chmod 0755 {DEFAULT_LAYOUT.toolchain_bin}/{tool.name}" in text
            assert f"ln -sfn {DEFAULT_LAYOUT.toolchain_lib}/{tool.name}/" not in text
            continue
        assert f"ln -sfn {DEFAULT_LAYOUT.toolchain_lib}/{tool.name}/" in text, tool.name
        assert f"`{DEFAULT_LAYOUT.toolchain_bin}/{tool.name}` **必須是指進 lib/ 的 " in text


def test_toolchain_plan_lines_are_comments_or_the_three_allowed_commands() -> None:
    """多行 note（#661 起有實測輸出）不得產生「既不是註解也不是命令」的行。"""
    allowed = ("install -d ", "chown ", "chmod ")
    for scheme in ALL_SCHEMES:
        for line in build_toolchain_plan(scheme, DEFAULT_LAYOUT):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assert stripped.startswith(allowed), (scheme.scheme_id, stripped)


def test_toolchain_plan_surfaces_the_unresolved_wx_conflicts() -> None:
    text = "\n".join(build_toolchain_plan(FOUR_WAY_SCHEME, DEFAULT_LAYOUT))
    for surface in unresolved_node_execution_surfaces():
        assert f"{surface.program} ← {surface.detail}" in text
    assert "不得就地放寬" in text


def test_toolchain_plan_is_deterministic() -> None:
    assert build_toolchain_plan(FOUR_WAY_SCHEME, DEFAULT_LAYOUT) == build_toolchain_plan(
        FOUR_WAY_SCHEME, DEFAULT_LAYOUT
    )


# ---------------------------------------------------------------------------
# 5. `PSC_PREFLIGHT_CMD`：形態、落點、與 runtime validator 的相容性
# ---------------------------------------------------------------------------

def test_preflight_command_value_is_typed_argv_inside_the_deploy_tree() -> None:
    value = DEFAULT_LAYOUT.preflight_command_value()
    parts = value.split()
    assert parts == [
        f"{DEFAULT_LAYOUT.venv_root}/bin/python3",
        "-m",
        PREFLIGHT_ADAPTER_MODULE,
    ]
    assert parts[0].startswith(DEFAULT_LAYOUT.deploy_root), "必須落在 root-owned 部署樹內"
    assert not value.startswith("~") and "/home/" not in value, (
        "ProtectHome=yes 之後 operator HOME 底下的任何東西都不可達——這正是 #661 的原症狀"
    )


def test_generated_preflight_command_passes_the_runtime_validator(tmp_path: Path) -> None:
    """用真實的檔案系統驗 `load_preflight_command()`（它會 stat ＋ 檢查 X_OK）。"""
    interpreter = tmp_path / "venv" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o755)
    value = f"{interpreter} -m {PREFLIGHT_ADAPTER_MODULE}"
    assert load_preflight_command(env={"PSC_PREFLIGHT_CMD": value}) == (
        str(interpreter),
        "-m",
        PREFLIGHT_ADAPTER_MODULE,
    )
    probe = doctor._preflight_probe({"PSC_PREFLIGHT_CMD": value})
    assert probe.status == "pass", probe.detail


def test_unreachable_home_wrapper_is_exactly_what_doctor_rejects(tmp_path: Path) -> None:
    """#661 的原症狀：舊值不是被 `shell-wrapper-not-allowed` 擋下，而是**不可達**。

    這一條同時修正票上的一個前提：`doctor.py` 的 shell-wrapper 類別只在 argv 第一段
    真的是 `bash`／`sh` 且帶 `-c` 時才成立，一個 `#!/usr/bin/env bash` 的腳本檔並不
    落在那個類別裡。舊值真正的失敗原因是 `ProtectHome=yes` 之後那條路徑 stat 不到。
    """
    missing = tmp_path / "home" / "bin" / "cortex-preflight-ci"
    probe = doctor._preflight_probe({"PSC_PREFLIGHT_CMD": str(missing)})
    assert probe.status == "fail"
    assert "executable-unavailable" in probe.detail
    assert "shell-wrapper" not in probe.detail


def test_preflight_backend_is_named_but_never_imported() -> None:
    """cortex 不 import 治理引擎——只以 typed argv spawn，未安裝時 fail-closed。"""
    assert preflight_ci.BACKEND_MODULE == PREFLIGHT_BACKEND_MODULE
    assert PREFLIGHT_BACKEND_DISTRIBUTION == "policy-check"
    source = Path(preflight_ci.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        assert not stripped.startswith(("import policy_check", "from policy_check")), stripped


def test_toolchain_plan_tells_the_operator_where_the_backend_goes() -> None:
    text = "\n".join(build_toolchain_plan(FOUR_WAY_SCHEME, DEFAULT_LAYOUT))
    assert f"PSC_PREFLIGHT_CMD=\"{DEFAULT_LAYOUT.preflight_command_value()}\"" in text
    assert f"{DEFAULT_LAYOUT.venv_root}/bin/pip install" in text
    assert PREFLIGHT_BACKEND_DISTRIBUTION in text


# ---------------------------------------------------------------------------
# 6. adapter：契約翻譯（cortex 的 typed argv → 引擎的 typed argv）
# ---------------------------------------------------------------------------

def _metadata(tmp_path: Path, payload: object) -> str:
    target = tmp_path / "pr-metadata.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(target)


def test_adapter_accepts_the_exact_contract_cortex_emits(tmp_path: Path) -> None:
    """`build_preflight_argv()` 只會產生這三種旗標；adapter 必須剛好收得下。"""
    from paulsha_cortex.coordinator.preflight import PreflightRequest, build_preflight_argv

    command = ("python3", "-m", PREFLIGHT_ADAPTER_MODULE)
    metadata = _metadata(tmp_path, {"title": "t", "body": "b", "labels": ["x"]})
    for request in (
        PreflightRequest(pr_number=7),
        PreflightRequest(metadata_path=metadata),
    ):
        argv = build_preflight_argv(command=command, request=request)
        parsed = preflight_ci.build_parser().parse_args(argv[len(command):])
        assert (parsed.pr is None) != (parsed.metadata is None)
        assert parsed.skip_tests is False


def test_adapter_reads_cortex_written_metadata(tmp_path: Path) -> None:
    path = _metadata(
        tmp_path,
        {"title": "feat(x): 標題", "body": "## 摘要\n\nCloses #661\n", "labels": ["enhancement"]},
    )
    title, body, labels = preflight_ci.load_metadata(path)
    assert title == "feat(x): 標題"
    assert "Closes #661" in body
    assert labels == ("enhancement",)


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "", "body": "b", "labels": []},
        {"title": "t", "body": 1, "labels": []},
        {"title": "t", "body": "b", "labels": ["ok", 2]},
        ["not", "an", "object"],
    ],
)
def test_adapter_metadata_is_fail_closed(tmp_path: Path, payload: object) -> None:
    with pytest.raises(preflight_ci.PreflightAdapterError):
        preflight_ci.load_metadata(_metadata(tmp_path, payload))


def test_adapter_metadata_must_be_an_absolute_regular_file(tmp_path: Path) -> None:
    with pytest.raises(preflight_ci.PreflightAdapterError):
        preflight_ci.load_metadata("relative/path.json")
    link = tmp_path / "link.json"
    link.symlink_to(_metadata(tmp_path, {"title": "t", "body": "b", "labels": []}))
    with pytest.raises(preflight_ci.PreflightAdapterError):
        preflight_ci.load_metadata(str(link))


def test_adapter_backend_argv_pins_the_offline_resolver(tmp_path: Path) -> None:
    """`--offline` 不是可選的最佳化——它換掉「執行期 clone 引擎再執行它」那條路。"""
    argv = preflight_ci.build_backend_argv(
        repo_root=tmp_path,
        body_file=tmp_path / "body.md",
        title="t",
        labels=("a", "b"),
        base="main",
        skip_tests=True,
        python="/opt/cortex/venv/bin/python3",
    )
    assert argv[:3] == ["/opt/cortex/venv/bin/python3", "-m", PREFLIGHT_BACKEND_MODULE]
    assert "--offline" in argv
    assert argv[argv.index("--pr-labels") + 1] == "a,b"
    assert argv[argv.index("--base") + 1] == "main"
    assert "--skip-tests" in argv
    # `--head` 由引擎自己從 checkout 導出：傳一份等於製造第二個真相（引擎會拒絕不符）。
    assert "--head" not in argv
    # `--pr` 與 `--offline` 互斥；adapter 一律走手動上下文。
    assert "--pr" not in argv


def test_adapter_backend_argv_omits_base_when_there_is_no_pr_yet(tmp_path: Path) -> None:
    argv = preflight_ci.build_backend_argv(
        repo_root=tmp_path,
        body_file=tmp_path / "body.md",
        title="t",
        labels=(),
        base=None,
        skip_tests=False,
    )
    assert "--base" not in argv
    assert argv[argv.index("--pr-labels") + 1] == ""
    assert "--skip-tests" not in argv
    assert argv[0] == sys.executable, "預設用當前 interpreter＝部署 venv 的那一支"


def test_adapter_parses_gh_pull_request_payload(tmp_path: Path) -> None:
    payload = {
        "title": "fix: x",
        "body": "b",
        "labels": [{"name": "bug"}, {"name": "wip"}],
        "baseRefName": "main",
    }

    def runner(argv, **kwargs):
        assert argv[:3] == ["gh", "pr", "view"]
        assert "--json" in argv
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    title, body, labels, base = preflight_ci.load_pull_request(
        "12", cwd=tmp_path, runner=runner
    )
    assert (title, body, labels, base) == ("fix: x", "b", ("bug", "wip"), "main")


def test_adapter_rejects_bad_pr_numbers_and_gh_failures(tmp_path: Path) -> None:
    def never(argv, **kwargs):  # pragma: no cover - 不該被呼叫
        raise AssertionError("非法 PR 編號不得叫到 gh")

    for bad in ("0", "-1", "abc", "1x"):
        with pytest.raises(preflight_ci.PreflightAdapterError):
            preflight_ci.load_pull_request(bad, cwd=tmp_path, runner=never)

    def failing(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="Bad credentials ghp_secret")

    with pytest.raises(preflight_ci.PreflightAdapterError) as excinfo:
        preflight_ci.load_pull_request("3", cwd=tmp_path, runner=failing)
    assert "ghp_secret" not in str(excinfo.value), "命令輸出不得進診斷訊息"


def test_adapter_reports_a_missing_external_program_by_name(tmp_path: Path) -> None:
    """`gh` 不在 PATH 上時要指回登記表，而不是丟一個裸 traceback。"""

    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    with pytest.raises(preflight_ci.PreflightAdapterError) as excinfo:
        preflight_ci.load_pull_request("5", cwd=tmp_path, runner=missing)
    assert "gh" in str(excinfo.value)
    assert "SYSTEM_PROGRAMS" in str(excinfo.value)

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 120)

    with pytest.raises(preflight_ci.PreflightAdapterError):
        preflight_ci.load_pull_request("5", cwd=tmp_path, runner=slow)


def test_adapter_propagates_the_backend_exit_code(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = _metadata(tmp_path, {"title": "t", "body": "b", "labels": []})
    seen: list[list[str]] = []

    def runner(argv, **kwargs):
        seen.append(list(argv))
        return SimpleNamespace(returncode=3, stdout="", stderr="")

    assert preflight_ci.main(["--metadata", path], runner=runner) == 3
    assert seen and seen[0][1:3] == ["-m", PREFLIGHT_BACKEND_MODULE]
    body_file = Path(seen[0][seen[0].index("--pr-body-file") + 1])
    assert not body_file.exists(), "body 暫存檔必須在退出前清掉"


def test_adapter_usage_errors_exit_two(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    def never(argv, **kwargs):  # pragma: no cover - 不該被呼叫
        raise AssertionError("輸入不合契約時不得叫 backend")

    assert preflight_ci.main(["--metadata", "relative.json"], runner=never) == 2
    with pytest.raises(SystemExit):  # 兩個都給／都不給：argparse 自己擋
        preflight_ci.main(["--pr", "1", "--metadata", str(tmp_path / "x.json")], runner=never)
    with pytest.raises(SystemExit):
        preflight_ci.main([], runner=never)


# ---------------------------------------------------------------------------
# 7. 既有不變式沒有被本票破壞
# ---------------------------------------------------------------------------

def test_registry_equation_stays_green() -> None:
    result = check_registry_equation()
    assert result.ok, result


def test_job_path_still_puts_the_toolchain_first() -> None:
    """`srt`／`openspec` 進的是同一棵 bin/，因此 PATH 契約一字未動。"""
    segments = DEFAULT_LAYOUT.job_path_value().split(":")
    assert segments[0] == DEFAULT_LAYOUT.toolchain_bin
    assert tuple(segments[1:]) == JOB_PATH_SYSTEM_TAIL


def test_generators_still_touch_no_filesystem() -> None:
    """新增的名冊與 `node_execution_surfaces()` 都必須是純函式。"""
    from paulsha_cortex.trust_root import permgen

    source = Path(permgen.__file__).read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "os.chown", "os.chmod", "shutil."):
        assert forbidden not in source, forbidden
    before = sorted(os.listdir("."))
    build_toolchain_plan(FOUR_WAY_SCHEME, DEFAULT_LAYOUT)
    node_execution_surfaces()
    assert sorted(os.listdir(".")) == before


def test_toolchain_cli_verb_still_prints_the_plan(capsys) -> None:
    from paulsha_cortex.trust_root.__main__ import main

    assert main(["toolchain", "four-way"]) == 0
    out = capsys.readouterr().out
    for tool in TOOLCHAIN_PROGRAMS:
        assert tool.name in out, tool.name


def test_review_sandbox_probe_passes_once_srt_resolves_as_a_package_tree(
    tmp_path: Path, monkeypatch
) -> None:
    """整包搬 vs 單檔複製，用假的 toolchain 樹把兩種形態的差別釘死。

    刻意不呼叫真的 `srt`／`claude`（CI 沒有它們）：這裡驗的是 probe 的判準——
    相依的 `--version` 一旦非零，`review-sandbox` 就報
    `Claude sandbox dependency execution failed`，而單檔形態下 `srt` 正是這樣失敗的。
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in doctor.REVIEW_SANDBOX_EXECUTABLES:
        stub = bindir / name
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)

    broken = {"srt"}

    def runner(argv, **kwargs):
        name = Path(argv[0]).name
        if name == "claude" and "--version" in argv:
            return SimpleNamespace(returncode=0, stdout="2.1.233 (Claude Code)", stderr="")
        if name == "claude":
            flags = " ".join(
                (
                    "--disable-slash-commands --json-schema --permission-mode "
                    "--safe-mode --setting-sources --settings --tools"
                ).split()
            )
            return SimpleNamespace(returncode=0, stdout=flags, stderr="")
        if name in broken:
            return SimpleNamespace(returncode=1, stdout="", stderr="ERR_MODULE_NOT_FOUND")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    env = {"PATH": str(bindir)}
    failed = doctor._review_sandbox_checks(env, runner=runner, live=False)
    assert failed.status == "fail"
    assert failed.detail == "Claude sandbox dependency execution failed"

    broken.clear()
    healthy = doctor._review_sandbox_checks(env, runner=runner, live=False)
    assert healthy.status == "pass", healthy.detail
