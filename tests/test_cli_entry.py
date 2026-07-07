import pytest

from paulsha_cortex.cli import main


def test_delegates_to_coordinator_cli(monkeypatch):
    seen = {}

    def fake_main(argv=None):
        seen["argv"] = list(argv or [])
        return 0

    monkeypatch.setattr("paulsha_cortex.coordinator.cli.main", fake_main)
    assert main(["status"]) == 0
    assert seen["argv"] == ["status"]


def test_unknown_empty_shows_usage(capsys):
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_relay_hook_execs_packaged_script(monkeypatch, tmp_path):
    script = tmp_path / "psc-relay-hook.sh"
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    seen = {}

    def fake_execv(executable, argv):
        seen["executable"] = executable
        seen["argv"] = list(argv)
        raise SystemExit(0)

    monkeypatch.setattr("paulsha_cortex.cli._relay_hook_script_path", lambda: script)
    monkeypatch.setattr("paulsha_cortex.cli.os.execv", fake_execv)

    with pytest.raises(SystemExit) as exc:
        main(["relay-hook", "--flag", "value"])

    assert exc.value.code == 0
    assert seen["executable"] == str(script)
    assert seen["argv"] == [str(script), "--flag", "value"]


def test_relay_hook_falls_back_to_bash_when_not_executable(monkeypatch, tmp_path):
    script = tmp_path / "psc-relay-hook.sh"
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    script.chmod(0o644)

    calls = []

    def fake_execv(executable, argv):
        calls.append((executable, list(argv)))
        if executable == str(script):
            raise OSError(13, "Permission denied")
        raise SystemExit(0)

    monkeypatch.setattr("paulsha_cortex.cli._relay_hook_script_path", lambda: script)
    monkeypatch.setattr("paulsha_cortex.cli.os.execv", fake_execv)

    with pytest.raises(SystemExit):
        main(["relay-hook", "--flag"])

    # 先試直接 exec（失敗），再 fallback 到 env bash 讀取執行
    assert calls[0][0] == str(script)
    assert calls[1] == ("/usr/bin/env", ["env", "bash", str(script), "--flag"])
