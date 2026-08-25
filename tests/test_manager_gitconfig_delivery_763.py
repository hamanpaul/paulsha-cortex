"""#763：Manager 的 GitHub HTTPS helper 必須由 trust-root 產生。

accepted plan 要求兩件事先被鎖成可重現的缺口：

1. Manager 的 root-owned `.gitconfig` 必須宣告
   `credential.https://github.com.helper`；
2. Git 自己對 `https://github.com` 的 credential lookup 必須能從那份
   generated config 解析出 helper，且不能擴寬成其他 host 的預設 helper。

這兩條先紅，後續 GREEN 才有明確目標。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from paulsha_cortex.trust_root import permgen
from paulsha_cortex.trust_root.permgen import DEFAULT_LAYOUT, build_account_gitconfig
from paulsha_cortex.trust_root.registry import Principal

SOURCE_SLUG = "paulsha-cortex"
LAYOUT = DEFAULT_LAYOUT.with_source_repo_slugs((SOURCE_SLUG,))


def _write_manager_gitconfig(path: Path) -> None:
    blob = build_account_gitconfig(permgen.THREE_WAY_SCHEME, LAYOUT, Principal.MANAGER)
    path.write_text(blob.content, encoding="utf-8")


def _git_config(config_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "config", "--file", str(config_path), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_manager_gitconfig_declares_github_https_credential_helper(tmp_path: Path) -> None:
    config_path = tmp_path / "manager.gitconfig"
    _write_manager_gitconfig(config_path)

    result = _git_config(config_path, "--get", "credential.https://github.com.helper")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def test_manager_gitconfig_scopes_credential_helper_to_github_https_only(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "manager.gitconfig"
    _write_manager_gitconfig(config_path)

    github = _git_config(
        config_path,
        "--get-urlmatch",
        "credential.helper",
        "https://github.com",
    )
    other_host = _git_config(
        config_path,
        "--get-urlmatch",
        "credential.helper",
        "https://example.com",
    )

    assert github.returncode == 0, github.stderr
    assert github.stdout.strip()
    assert other_host.returncode == 1
    assert other_host.stdout == ""


def test_manager_gitconfig_dry_runs_https_credential_lookup_via_generated_helper(
    tmp_path: Path,
    monkeypatch,
) -> None:
    helper_dir = tmp_path / "helper space"
    helper_dir.mkdir()
    fake_gh = helper_dir / "fake gh"
    helper_log = helper_dir / "helper.log"
    fake_gh.write_text(
        "\n".join(
            (
                "#!/bin/sh",
                f'printf %s "$*" > "{helper_log}"',
                "printf 'username=x-access-token\\npassword=******\\n'",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    monkeypatch.setattr(permgen, "SYSTEM_GH_EXECUTABLE", str(fake_gh))

    ambient_helper = tmp_path / "ambient-helper"
    ambient_log = tmp_path / "ambient.log"
    ambient_helper.write_text(
        "\n".join(
            (
                "#!/bin/sh",
                f'printf %s "$*" > "{ambient_log}"',
                "printf 'username=ambient\\npassword=ambient\\n'",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    ambient_helper.chmod(0o755)
    ambient_config = tmp_path / "ambient.gitconfig"
    ambient_config.write_text(
        "\n".join(
            (
                f'[credential "{permgen.GITHUB_HTTPS_CREDENTIAL_URL}"]',
                "\thelper =",
                f"\thelper = {permgen.durable_owner_git_credential_helper(str(ambient_helper))}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(ambient_config))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ambient-xdg"))

    home = tmp_path / "home with spaces"
    home.mkdir()
    config_path = home / ".gitconfig"
    _write_manager_gitconfig(config_path)

    result = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        check=False,
        capture_output=True,
        text=True,
        env={
            "GIT_CONFIG_GLOBAL": str(config_path),
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(home),
            "PATH": os.defpath,
        },
    )

    assert result.returncode == 0, result.stderr
    assert "username=x-access-token" in result.stdout
    assert "******" in result.stdout
    assert not ambient_log.exists()
    assert helper_log.read_text(encoding="utf-8") == "auth git-credential get"
