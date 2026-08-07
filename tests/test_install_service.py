import json
import subprocess
import sys

import pytest

from paulsha_cortex.deploy.installer import render_units


def _init_git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _init_git_repo_with_origin(path, url):
    _init_git_repo(path)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True)
    return path


def test_render_substitutes_instance_and_script(tmp_path):
    units = render_units(instance="alpha", interval=120)
    service = units["alpha-manager.service"]
    assert "__INSTANCE__" not in service and "__SERVICE_SCRIPT__" not in service
    assert "alpha persona manager service" in service
    assert "UMask=0022" in service
    timer = units["alpha-manager.timer"]
    assert "OnUnitActiveSec=120" in timer


def test_install_is_idempotent(tmp_path, monkeypatch):
    from paulsha_cortex.deploy import installer

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert installer.main(["service", "--instance", "beta"]) == 0
    first = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    assert installer.main(["service", "--instance", "beta"]) == 0
    second = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    assert first == second


def test_install_writes_current_python_to_env_file(tmp_path, monkeypatch):
    from paulsha_cortex.deploy import installer

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PSC_AGENTS_ROOT", raising=False)

    assert installer.main(["service", "--instance", "beta"]) == 0

    env_file = tmp_path / ".agents" / "core" / "runtime" / "beta-manager.env"
    env_lines = env_file.read_text(encoding="utf-8").splitlines()
    assert f"PY={sys.executable}" in env_lines
    assert "PSC_INSTANCE=beta" in env_lines
    assert f"PSC_RUN_ROOT={tmp_path / '.agents' / 'run' / 'beta'}" in env_lines
    assert f"PSC_MONITOR_STATE_ROOT={tmp_path / '.agents' / 'monitor'}" in env_lines
    assert f"PSC_PROJECT_CONFIG_ROOT={tmp_path / '.agents' / 'config' / 'paulsha'}" in env_lines


def test_install_writes_git_repo_root_to_env_file(tmp_path, monkeypatch):
    from paulsha_cortex.deploy import installer

    repo_root = _init_git_repo(tmp_path / "repo")
    work_dir = repo_root / "nested"
    work_dir.mkdir()

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PSC_AGENTS_ROOT", raising=False)
    monkeypatch.chdir(work_dir)

    assert installer.main(["service", "--instance", "beta"]) == 0

    env_file = tmp_path / "home" / ".agents" / "core" / "runtime" / "beta-manager.env"
    env_lines = env_file.read_text(encoding="utf-8").splitlines()
    assert f"PSC_REPO_ROOT={repo_root.resolve()}" in env_lines
    assert f"PSC_RUN_ROOT={tmp_path / 'home' / '.agents' / 'run' / 'beta'}" in env_lines


def test_install_preserves_existing_operator_env_lines(tmp_path, monkeypatch):
    from paulsha_cortex.deploy import installer

    repo_root = _init_git_repo(tmp_path / "repo")
    home = tmp_path / "home"
    runtime_dir = home / ".agents" / "core" / "runtime"
    runtime_dir.mkdir(parents=True)
    env_file = runtime_dir / "beta-manager.env"
    env_file.write_text(
        "# operator tuning\n"
        "PSC_WORKTREE_ROOT=/custom/worktrees\n"
        "PSC_RUN_ROOT=/custom/run\n"
        "PSC_MONITOR_STATE_ROOT=/custom/monitor\n"
        "PSC_PROJECT_CONFIG_ROOT=/custom/config\n"
        "PY=/stale/python\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo_root)

    assert installer.main(["service", "--instance", "beta"]) == 0

    env_lines = env_file.read_text(encoding="utf-8").splitlines()
    # operator 手動行與註解保留
    assert "# operator tuning" in env_lines
    assert "PSC_WORKTREE_ROOT=/custom/worktrees" in env_lines
    assert "PSC_RUN_ROOT=/custom/run" in env_lines
    assert "PSC_MONITOR_STATE_ROOT=/custom/monitor" in env_lines
    assert "PSC_PROJECT_CONFIG_ROOT=/custom/config" in env_lines
    # managed key 就地更新、不重複
    assert f"PY={sys.executable}" in env_lines
    assert sum(line.startswith("PY=") for line in env_lines) == 1
    assert f"PSC_REPO_ROOT={repo_root.resolve()}" in env_lines


def test_install_reconciles_legacy_codex_relay_hook(tmp_path, monkeypatch):
    """issue #155：install service 應 reconcile 既存的 legacy Codex hooks.json。"""
    from paulsha_cortex.deploy import installer

    repo_root = _init_git_repo(tmp_path / "repo")
    home = tmp_path / "home"
    codex_hooks = home / ".codex" / "hooks.json"
    codex_hooks.parent.mkdir(parents=True)
    codex_hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup|clear|compact",
                            "hooks": [
                                {
                                    "command": (
                                        "PSC_RELAY_EVENT=session_start "
                                        "$HOME/prj_pri/paulshaclaw/scripts/coordinator/"
                                        "psc-relay-hook.sh"
                                    ),
                                    "type": "command",
                                    "managedBy": "psc-coordinator-relay",
                                }
                            ],
                        }
                    ],
                    "Stop": [
                        {
                            "matcher": ".*",
                            "hooks": [
                                {
                                    "command": "PSC_RELAY_EVENT=stop /opt/psc-bro-return.sh",
                                    "type": "command",
                                    "managedBy": "psc-bro-return",
                                },
                                {
                                    "command": (
                                        "PSC_RELAY_EVENT=stop "
                                        "$HOME/prj_pri/paulshaclaw/scripts/coordinator/"
                                        "psc-relay-hook.sh"
                                    ),
                                    "type": "command",
                                    "managedBy": "psc-coordinator-relay",
                                },
                            ],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo_root)

    assert installer.main(["service", "--instance", "beta"]) == 0

    doc = json.loads(codex_hooks.read_text(encoding="utf-8"))
    stop_hooks = doc["hooks"]["Stop"][0]["hooks"]
    session_hooks = doc["hooks"]["SessionStart"][0]["hooks"]
    assert session_hooks[0]["command"] == "PSC_RELAY_EVENT=session_start cortex relay-hook"
    managed_stop = next(h for h in stop_hooks if h["managedBy"] == "psc-coordinator-relay")
    assert managed_stop["command"] == "PSC_RELAY_EVENT=stop cortex relay-hook"
    other_stop = next(h for h in stop_hooks if h["managedBy"] == "psc-bro-return")
    assert other_stop["command"] == "PSC_RELAY_EVENT=stop /opt/psc-bro-return.sh"
    assert list(codex_hooks.parent.glob("hooks.json.bak-*"))


def test_install_derives_runtime_defaults_from_agents_root_but_keeps_bootstrap_env_location(
    tmp_path, monkeypatch
):
    from paulsha_cortex.deploy import installer

    repo_root = _init_git_repo(tmp_path / "repo")
    home = tmp_path / "home"
    custom_agents = tmp_path / "custom-agents"
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(custom_agents))
    monkeypatch.chdir(repo_root)

    assert installer.main(["service", "--instance", "beta"]) == 0

    env_file = home / ".agents" / "core" / "runtime" / "beta-manager.env"
    env_lines = env_file.read_text(encoding="utf-8").splitlines()
    assert f"PSC_AGENTS_ROOT={custom_agents}" in env_lines
    assert f"PSC_RUN_ROOT={custom_agents / 'run' / 'beta'}" in env_lines
    assert f"PSC_MONITOR_STATE_ROOT={custom_agents / 'monitor'}" in env_lines
    assert f"PSC_PROJECT_CONFIG_ROOT={custom_agents / 'config' / 'paulsha'}" in env_lines


def test_install_existing_agents_root_drives_new_specific_defaults(tmp_path, monkeypatch):
    from paulsha_cortex.deploy import installer

    repo_root = _init_git_repo(tmp_path / "repo")
    home = tmp_path / "home"
    custom_agents = tmp_path / "operator-agents"
    env_file = home / ".agents" / "core" / "runtime" / "beta-manager.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(f"PSC_AGENTS_ROOT={custom_agents}\n", encoding="utf-8")
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PSC_AGENTS_ROOT", raising=False)
    monkeypatch.chdir(repo_root)

    assert installer.main(["service", "--instance", "beta"]) == 0

    env_lines = env_file.read_text(encoding="utf-8").splitlines()
    assert f"PSC_AGENTS_ROOT={custom_agents}" in env_lines
    assert f"PSC_RUN_ROOT={custom_agents / 'run' / 'beta'}" in env_lines
    assert f"PSC_MONITOR_STATE_ROOT={custom_agents / 'monitor'}" in env_lines


def test_install_rejects_symlinked_bootstrap_without_overwriting_target(tmp_path, monkeypatch):
    from paulsha_cortex.deploy import installer

    home = tmp_path / "home"
    env_file = home / ".agents" / "core" / "runtime" / "beta-manager.env"
    env_file.parent.mkdir(parents=True)
    outside = tmp_path / "outside.env"
    outside.write_text("DO_NOT_OVERWRITE=1\n", encoding="utf-8")
    env_file.symlink_to(outside)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ValueError, match="symlink"):
        installer.install_service("beta", 300, tmp_path / "repo")

    assert outside.read_text(encoding="utf-8") == "DO_NOT_OVERWRITE=1\n"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("NOT AN ENV LINE\n", "格式錯誤"),
        ('PSC_MANAGER_EXECUTOR="claude\n', "quote invalid"),
    ],
)
def test_install_reports_bootstrap_env_path_and_line_on_parse_failure(tmp_path, content, expected):
    from paulsha_cortex.deploy import installer

    env_file = tmp_path / "beta-manager.env"
    env_file.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=expected) as exc:
        installer._read_plain_env(env_file)

    message = str(exc.value)
    assert str(env_file) in message
    assert content.strip() in message


def test_install_rejects_non_git_repo_root(tmp_path, monkeypatch, capsys):
    from paulsha_cortex.deploy import installer

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    bad_root = tmp_path / "not-a-repo"
    bad_root.mkdir()

    with pytest.raises(SystemExit) as exc:
        installer.main(["service", "--repo-root", str(bad_root)])

    assert exc.value.code == 2
    assert f"{bad_root.resolve()} 不是 git repo" in capsys.readouterr().err


def test_install_service_installs_monitor_unit(tmp_path, monkeypatch):
    from paulsha_cortex.deploy import installer

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert installer.main(["service", "--repo-root", str(repo)]) == 0
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    assert (unit_dir / "cortex-manager.service").exists()
    assert (unit_dir / "cortex-monitor.service").exists()
    monitor_unit = (unit_dir / "cortex-monitor.service").read_text()
    assert "paulsha_cortex.monitor" in monitor_unit
    assert "__INSTANCE__.env".replace("__INSTANCE__", "cortex") in monitor_unit or "cortex.env" in monitor_unit
    assert "cortex-manager.env" in monitor_unit


def test_install_rejects_unsafe_instance_name(tmp_path, monkeypatch, capsys):
    from paulsha_cortex.deploy import installer

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    with pytest.raises(SystemExit) as exc:
        installer.main(["service", "--instance", "../bad"])

    assert exc.value.code == 2
    assert "instance 名稱不合法" in capsys.readouterr().err


def test_install_rejects_non_positive_interval(tmp_path, monkeypatch, capsys):
    from paulsha_cortex.deploy import installer

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    with pytest.raises(SystemExit) as exc:
        installer.main(["service", "--interval", "0"])

    assert exc.value.code == 2
    assert "interval 必須為正整數" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("failure_command",),
    [
        (["systemctl", "--user", "daemon-reload"],),
        (["systemctl", "--user", "enable", "beta-monitor.service"],),
        (["systemctl", "--user", "enable", "beta-manager.timer"],),
    ],
)
def test_install_service_and_install_reports_systemctl_step_error(
    tmp_path,
    monkeypatch,
    failure_command,
):
    from paulsha_cortex.deploy import installer

    calls: list[list[str]] = []
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    repo_root = tmp_path / "repo"
    # install_service_result 最先呼叫 _resolve_repo_identity()（#366），
    # 該函式也走 subprocess.run（git remote get-url origin），故排在
    # systemctl 步驟之前一併出現在 calls 記錄裡。
    identity_probe_command = ["git", "-C", str(repo_root), "remote", "get-url", "origin"]
    expected_commands = [
        identity_probe_command,
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "beta-monitor.service"],
        ["systemctl", "--user", "enable", "beta-manager.timer"],
    ]
    failure_index = expected_commands.index(failure_command)

    def fake_run(argv, *args, **kwargs):
        calls.append(list(argv))
        returncode = 7 if argv == failure_command else 0
        message = "Failed to enable unit" if argv == failure_command else ""
        return subprocess.CompletedProcess(argv, returncode, stdout="DO_NOT_SURFACE", stderr=message)

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(installer, "_systemctl_available", lambda: True)
    fake_result = installer.install_service_result("beta", 120, repo_root)

    assert fake_result.mode == "systemd"
    assert fake_result.exit_code == 7
    assert fake_result.message
    assert "Failed to enable unit" in fake_result.message
    assert "unit 已落檔於" in fake_result.message
    assert str(unit_dir) in fake_result.message
    assert " ".join(failure_command) in fake_result.message
    assert "DO_NOT_SURFACE" not in fake_result.message
    assert "CompletedProcess" not in fake_result.message
    assert "Traceback" not in fake_result.message
    assert calls == expected_commands[: failure_index + 1]


def test_systemctl_failure_still_reports_hook_migration_that_already_happened(
    tmp_path, monkeypatch
):
    """reconcile 跑在 systemctl for loop 之前；就算 systemctl 失敗，已發生的
    hook 遷移／備份副作用也必須出現在回報訊息中，不能悄悄消失。"""
    from paulsha_cortex.deploy import installer

    home = tmp_path / "home"
    codex_hooks = home / ".codex" / "hooks.json"
    codex_hooks.parent.mkdir(parents=True)
    codex_hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "matcher": ".*",
                            "hooks": [
                                {
                                    "command": (
                                        "PSC_RELAY_EVENT=stop "
                                        "$HOME/prj_pri/paulshaclaw/scripts/coordinator/"
                                        "psc-relay-hook.sh"
                                    ),
                                    "type": "command",
                                    "managedBy": "psc-coordinator-relay",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    def fake_run(argv, *args, **kwargs):
        if argv == ["systemctl", "--user", "daemon-reload"]:
            return subprocess.CompletedProcess(argv, 7, stdout="", stderr="boom")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer, "_systemctl_available", lambda: True)

    result = installer.install_service_result("beta", 120, tmp_path / "repo")

    assert result.mode == "systemd"
    assert result.exit_code == 7
    assert "codex hooks reconcile" in result.message
    assert "cortex relay-hook" in result.message

    # 副作用確實已發生：live 檔案被改寫、備份確實落檔。
    doc = json.loads(codex_hooks.read_text(encoding="utf-8"))
    managed = doc["hooks"]["Stop"][0]["hooks"][0]
    assert managed["command"] == "PSC_RELAY_EVENT=stop cortex relay-hook"
    assert list(codex_hooks.parent.glob("hooks.json.bak-*"))


# --- #366：install service 身分守衛（PSC_REPO_IDENTITY） ---------------------


def test_normalize_git_origin_ssh_and_https_are_equal():
    from paulsha_cortex.deploy import installer

    ssh = installer._normalize_git_origin("git@github.com:hamanpaul/paulsha-cortex.git")
    https = installer._normalize_git_origin("https://github.com/hamanpaul/paulsha-cortex")

    assert ssh == https == "github.com/hamanpaul/paulsha-cortex"


def test_install_service_first_install_backfills_identity_stamp(tmp_path, monkeypatch):
    """首次安裝（env 不存在）不得被身分守衛擋住，且要補寫 PSC_REPO_IDENTITY。"""
    from paulsha_cortex.deploy import installer

    repo_root = _init_git_repo(tmp_path / "repo")
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(repo_root)

    assert installer.main(["service", "--instance", "beta"]) == 0

    env_file = tmp_path / "home" / ".agents" / "core" / "runtime" / "beta-manager.env"
    assert "PSC_REPO_IDENTITY=" in env_file.read_text(encoding="utf-8")


def test_install_service_non_git_or_no_origin_repo_root_falls_back_to_path_identity(
    tmp_path, monkeypatch
):
    """非 git repo／無 origin 時，身分解析必須 fail-safe 退回路徑指紋，不得炸掉。"""
    from paulsha_cortex.deploy import installer

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    bad_root = tmp_path / "not-a-repo"
    bad_root.mkdir()

    result = installer.install_service_result("beta", 300, bad_root)

    assert result.exit_code == 0
    env_file = tmp_path / "home" / ".agents" / "core" / "runtime" / "beta-manager.env"
    content = env_file.read_text(encoding="utf-8")
    assert f"PSC_REPO_IDENTITY=path:{bad_root.resolve()}" in content


def test_install_service_blocks_repo_root_change_when_identity_stamp_mismatches(
    tmp_path, monkeypatch
):
    """既有 instance 已記錄 repo 身分後，換一個不同身分的 --repo-root 必須 fail-closed。"""
    from paulsha_cortex.deploy import installer

    repo_a = _init_git_repo_with_origin(
        tmp_path / "repo-a", "https://github.com/hamanpaul/paulsha-cortex"
    )
    repo_b = _init_git_repo_with_origin(
        tmp_path / "repo-b", "https://github.com/otherorg/other-repo"
    )
    home = tmp_path / "home"
    runtime_file = home / ".agents" / "core" / "runtime" / "beta-manager.env"
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_text(
        "PY=/tmp/venv-a/bin/python\n"
        f"PSC_REPO_ROOT={repo_a}\n"
        "PSC_REPO_IDENTITY=git:github.com/hamanpaul/paulsha-cortex\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(home))
    # 呼叫者 python 刻意與既有值相同：若守衛仍只比對「呼叫者」（舊 #198 邏輯），
    # 這裡會被誤判放行；必須靠身分真值比對才能擋下。
    monkeypatch.setattr(installer.sys, "executable", "/tmp/venv-a/bin/python")

    before = runtime_file.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        installer.install_service_result("beta", 300, repo_b)
    assert runtime_file.read_text(encoding="utf-8") == before


def test_install_service_identity_guard_survives_corrupted_existing_value(tmp_path, monkeypatch):
    """重現 F44：既有值已被腐化成錯的 repo，且呼叫者剛好與腐化值同源——
    比對基準是身分真值而非呼叫者，這次呼叫仍必須被擋下。"""
    from paulsha_cortex.deploy import installer

    repo_a = _init_git_repo_with_origin(
        tmp_path / "repo-a", "https://github.com/hamanpaul/paulsha-cortex"
    )
    repo_b_corrupted = _init_git_repo_with_origin(
        tmp_path / "repo-b", "https://github.com/otherorg/other-repo"
    )
    corrupted_python = "/tmp/venv-b/bin/python"
    home = tmp_path / "home"
    runtime_file = home / ".agents" / "core" / "runtime" / "beta-manager.env"
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_text(
        f"PY={corrupted_python}\n"
        f"PSC_REPO_ROOT={repo_b_corrupted}\n"
        "PSC_REPO_IDENTITY=git:github.com/otherorg/other-repo\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(home))
    # 呼叫者剛好跟腐化值同一個 python——舊守衛（比對呼叫者）在此情境下會誤判放行。
    monkeypatch.setattr(installer.sys, "executable", corrupted_python)

    before = runtime_file.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        installer.install_service_result("beta", 300, repo_a)
    assert runtime_file.read_text(encoding="utf-8") == before


def test_install_service_rebind_flag_allows_repo_identity_change(tmp_path, monkeypatch):
    """--rebind 是明確搬遷放行旗標：帶上後身分/PY/repo_root 三者一併改寫成新值。"""
    from paulsha_cortex.deploy import installer

    repo_a = _init_git_repo_with_origin(
        tmp_path / "repo-a", "https://github.com/hamanpaul/paulsha-cortex"
    )
    repo_b = _init_git_repo_with_origin(
        tmp_path / "repo-b", "https://github.com/otherorg/other-repo"
    )
    home = tmp_path / "home"
    runtime_file = home / ".agents" / "core" / "runtime" / "beta-manager.env"
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_text(
        "PY=/tmp/venv-b/bin/python\n"
        f"PSC_REPO_ROOT={repo_b}\n"
        "PSC_REPO_IDENTITY=git:github.com/otherorg/other-repo\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer.sys, "executable", "/tmp/venv-a/bin/python")

    result = installer.install_service_result("beta", 300, repo_a, rebind=True)

    assert result.exit_code == 0
    content = runtime_file.read_text(encoding="utf-8")
    assert "PY=/tmp/venv-a/bin/python" in content
    assert f"PSC_REPO_ROOT={repo_a}" in content
    assert "PSC_REPO_IDENTITY=git:github.com/hamanpaul/paulsha-cortex" in content


def test_install_service_same_origin_different_remote_style_or_path_does_not_block(
    tmp_path, monkeypatch
):
    """同 repo 換 remote 寫法（SSH↔HTTPS）或換 checkout 路徑不得被誤擋，且 PY 應
    自由跟著呼叫者更新（不再受舊的「PY 比對呼叫者」守衛牽制）。

    刻意讓既有 PY 與呼叫者 PY 不同：若身分守衛沒接手、舊 PY-vs-caller 邏輯還在，
    這裡會被誤擋。"""
    from paulsha_cortex.deploy import installer

    repo_old_path = _init_git_repo_with_origin(
        tmp_path / "repo-old-path", "git@github.com:hamanpaul/paulsha-cortex.git"
    )
    repo_new_path = _init_git_repo_with_origin(
        tmp_path / "repo-new-path", "https://github.com/hamanpaul/paulsha-cortex"
    )
    home = tmp_path / "home"
    runtime_file = home / ".agents" / "core" / "runtime" / "beta-manager.env"
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_text(
        "PY=/tmp/venv-old/bin/python\n"
        f"PSC_REPO_ROOT={repo_old_path}\n"
        "PSC_REPO_IDENTITY=git:github.com/hamanpaul/paulsha-cortex\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer.sys, "executable", "/tmp/venv-new/bin/python")

    result = installer.install_service_result("beta", 300, repo_new_path)

    assert result.exit_code == 0
    content = runtime_file.read_text(encoding="utf-8")
    assert "PY=/tmp/venv-new/bin/python" in content
    assert f"PSC_REPO_ROOT={repo_new_path}" in content
    assert "PSC_REPO_IDENTITY=git:github.com/hamanpaul/paulsha-cortex" in content


def test_install_service_identity_mismatch_error_includes_env_file_path(tmp_path, monkeypatch):
    """錯誤訊息必須帶上 env 檔實際路徑，讓 operator 不必自己推算。"""
    from paulsha_cortex.deploy import installer

    repo_a = _init_git_repo_with_origin(
        tmp_path / "repo-a", "https://github.com/hamanpaul/paulsha-cortex"
    )
    repo_b = _init_git_repo_with_origin(
        tmp_path / "repo-b", "https://github.com/otherorg/other-repo"
    )
    home = tmp_path / "home"
    runtime_file = home / ".agents" / "core" / "runtime" / "beta-manager.env"
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_text(
        "PY=/tmp/venv-a/bin/python\n"
        f"PSC_REPO_ROOT={repo_a}\n"
        "PSC_REPO_IDENTITY=git:github.com/hamanpaul/paulsha-cortex\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ValueError) as exc:
        installer.install_service_result("beta", 300, repo_b)

    assert str(runtime_file) in str(exc.value)
