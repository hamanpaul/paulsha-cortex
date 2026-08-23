"""Issue #681: RED regressions for trust-root Copilot toolchain pinning."""

from __future__ import annotations

from pathlib import PurePosixPath

from paulsha_cortex.coordinator.launcher import build_copilot_argv
from paulsha_cortex.trust_root.permgen import (
    DEFAULT_LAYOUT,
    THREE_WAY_SCHEME,
    build_path_resolution_probe,
    build_toolchain_plan,
    path_resolution_cases,
)
from paulsha_cortex.trust_root.registry import Principal


def _command_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _builder_copilot_case():
    for case in path_resolution_cases(THREE_WAY_SCHEME, DEFAULT_LAYOUT):
        if case.executor == "copilot" and case.principal is Principal.BUILDER:
            return case
    raise AssertionError("missing builder copilot path-resolution case")


def test_production_copilot_argv_uses_the_pinned_wrapper_path() -> None:
    argv = build_copilot_argv(prompt="P", slice_id="slice-681", log_dir="/lg")
    assert argv[0] == f"{DEFAULT_LAYOUT.toolchain_bin}/copilot"


def test_copilot_path_probe_resolves_via_path_before_sanitized_invariants() -> None:
    copilot_commands = [
        line
        for line in _command_lines(build_path_resolution_probe(THREE_WAY_SCHEME, DEFAULT_LAYOUT))
        if "copilot" in line
    ]
    resolution_commands = [line for line in copilot_commands if "/bin/sh -c 'command -v " in line]
    assert resolution_commands
    assert all("command -v copilot" in line for line in resolution_commands)
    assert all('command -v "$1"' not in line for line in resolution_commands)
    assert all("env -u HOME" not in line and "env -u PATH" not in line for line in resolution_commands)
    followup_commands = [line for line in copilot_commands if line not in resolution_commands]
    assert any("-u HOME" in line for line in followup_commands)
    assert any("-u PATH" in line for line in followup_commands)
    for index, line in enumerate(copilot_commands):
        if line not in resolution_commands:
            continue
        assert index + 1 < len(copilot_commands)
        next_line = copilot_commands[index + 1]
        assert "env -u PATH -u HOME" in next_line
        assert 'PATH_VERSION="$("$1" --version)"' in next_line


def test_copilot_version_checks_use_dedicated_metadata_not_the_wrapper_path() -> None:
    case = _builder_copilot_case()
    assert case.version_reference != case.expected_binary
    assert PurePosixPath(case.version_reference).is_absolute()


def test_copilot_toolchain_plan_rejects_symlinked_sources_before_copying() -> None:
    plan_text = "\n".join(build_toolchain_plan(THREE_WAY_SCHEME, DEFAULT_LAYOUT))
    assert any(
        guard in plan_text
        for guard in (
            'test ! -L "$SRC"',
            'test ! -L "$PKG"',
            'find "$PKG" -type l',
        )
    )


def test_copilot_toolchain_plan_rejects_entry_traversal_before_publishing_wrapper() -> None:
    plan_text = "\n".join(build_toolchain_plan(THREE_WAY_SCHEME, DEFAULT_LAYOUT))
    assert any(
        guard in plan_text
        for guard in (
            'case "$ENTRY_REL" in ../*|*/../*|..)',
            'test "${ENTRY_REL#../}" = "$ENTRY_REL"',
            "realpath --relative-to",
        )
    )


def test_copilot_toolchain_plan_reinstalls_through_tempdir_and_rename() -> None:
    plan_lines = build_toolchain_plan(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    plan_text = "\n".join(plan_lines)
    assert "mktemp -d" in plan_text
    assert 'trap \'rm -rf -- "$tmp"\'' in plan_text
    tmp_copy = '#     cp -a "$PKG" "$tmp/copilot"'
    final_copy = f'#     cp -a "$PKG" {DEFAULT_LAYOUT.toolchain_lib}/copilot'
    final_entry = (
        f'#     test -f {DEFAULT_LAYOUT.toolchain_lib}/copilot/"$ENTRY_REL"   # mv 後再驗最終落位'
    )
    mv_line = f'#     mv -T "$tmp/copilot" {DEFAULT_LAYOUT.toolchain_lib}/copilot'
    assert final_copy not in plan_text
    assert plan_lines.index(tmp_copy) < plan_lines.index(mv_line) < plan_lines.index(final_entry)
    assert "只先複製到 `$tmp`；最終" in plan_text


def test_copilot_toolchain_wrapper_version_guards_fail_closed_before_exec() -> None:
    plan_text = "\n".join(build_toolchain_plan(THREE_WAY_SCHEME, DEFAULT_LAYOUT))
    for guard in (
        '#     test -f "$VERSION_FILE" || exit 1',
        '#     test -f "$ENTRY_ABS" || exit 1',
        '#     EXPECTED_VERSION="$(cat "$VERSION_FILE")" || exit 1',
        '#     test -n "$EXPECTED_VERSION" || exit 1',
        '#     ACTUAL_VERSION="$($NODE_ABS "$ENTRY_ABS" --version)" || exit 1',
        '#     test -n "$ACTUAL_VERSION" || exit 1',
        '#     [ "$ACTUAL_VERSION" = "$EXPECTED_VERSION" ] || exit 1',
    ):
        assert guard in plan_text
