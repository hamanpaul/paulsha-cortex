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


def test_copilot_path_probe_strips_operator_home_and_path_before_resolving() -> None:
    copilot_commands = [
        line
        for line in _command_lines(build_path_resolution_probe(THREE_WAY_SCHEME, DEFAULT_LAYOUT))
        if "copilot" in line
    ]
    assert any(
        any(fragment in line for fragment in ("env -i", "env -u HOME", "HOME=", "unset HOME"))
        for line in copilot_commands
    )
    assert any(
        any(fragment in line for fragment in ("env -i", "env -u PATH", "PATH=", "unset PATH"))
        for line in copilot_commands
    )


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
    plan_text = "\n".join(build_toolchain_plan(THREE_WAY_SCHEME, DEFAULT_LAYOUT))
    assert "mktemp -d" in plan_text
    assert 'trap \'rm -rf -- "$tmp"\'' in plan_text
    assert any(token in plan_text for token in ('mv "$tmp"', 'mv -T', 'mv -- "$tmp"'))
