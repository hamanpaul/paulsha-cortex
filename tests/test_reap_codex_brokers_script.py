from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "paulsha_cortex/scripts/reap-codex-brokers.sh"


def _cmdline(*argv: str) -> bytes:
    return ("\0".join(argv) + "\0").encode("utf-8")


def _stat_text(pid: int, *, ppid: int, starttime: int) -> str:
    fields = [str(pid), "(node)", "S", str(ppid), *(["0"] * 17), str(starttime), "0", "0", "0"]
    return " ".join(fields)


def _write_proc_pid(
    proc_root: Path,
    pid: int,
    *,
    ppid: int,
    starttime: int,
    cwd: Path | None,
    cmdline: bytes,
) -> None:
    pid_dir = proc_root / str(pid)
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "stat").write_text(_stat_text(pid, ppid=ppid, starttime=starttime), encoding="utf-8")
    (pid_dir / "cmdline").write_bytes(cmdline)
    if cwd is not None:
        cwd.mkdir(parents=True, exist_ok=True)
        os.symlink(cwd, pid_dir / "cwd", target_is_directory=True)


def _write_snapshot(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_killer(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env bash\n"
        "[[ -n \"${MUTATE_ON_KILL:-}\" ]] && \"$MUTATE_ON_KILL\"\n"
        "unset MUTATE_ON_KILL\n"
        "printf '%s\\n' \"$*\" >> \"$KILL_LOG\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_script(tmp_path: Path, *args: str, mutate_on_kill: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "REAP_PS_SNAPSHOT": str(tmp_path / "snapshot.txt"),
        "REAP_PROC_ROOT": str(tmp_path / "proc"),
        "REAP_KILL_CMD": str(tmp_path / "fake-kill.sh"),
        "KILL_LOG": str(tmp_path / "kill.log"),
    }
    if mutate_on_kill is not None:
        env["MUTATE_ON_KILL"] = str(mutate_on_kill)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def test_apply_scopes_to_cwd_root_and_rechecks_live_identity(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    project_root = tmp_path / "workspace" / "project"
    other_root = tmp_path / "workspace" / "project-other"
    _write_killer(tmp_path / "fake-kill.sh")
    mutate = tmp_path / "mutate-on-kill.sh"
    mutate.write_text(
        "#!/usr/bin/env bash\n"
        f"cat > {proc_root / '106' / 'stat'} <<'EOF'\n"
        f"{_stat_text(106, ppid=1, starttime=99999)}\n"
        "EOF\n",
        encoding="utf-8",
    )
    mutate.chmod(0o755)

    _write_proc_pid(
        proc_root,
        101,
        ppid=1,
        starttime=10101,
        cwd=project_root / "slice-a",
        cmdline=_cmdline("node", "app-server-broker.mjs", "serve", "--cwd", str(project_root / "slice-a")),
    )
    _write_proc_pid(
        proc_root,
        102,
        ppid=1,
        starttime=10202,
        cwd=other_root / "slice-b",
        cmdline=_cmdline("node", "app-server-broker.mjs", "serve", "--cwd", str(other_root / "slice-b")),
    )
    _write_proc_pid(
        proc_root,
        103,
        ppid=1,
        starttime=10303,
        cwd=None,
        cmdline=_cmdline("node", "app-server-broker.mjs", "serve", "--cwd", str(project_root / "gone")),
    )
    _write_proc_pid(
        proc_root,
        104,
        ppid=1,
        starttime=10404,
        cwd=project_root / "mutated-cmdline",
        cmdline=_cmdline("node", "something-else.mjs", "serve", "--cwd", str(project_root / "mutated-cmdline")),
    )
    _write_proc_pid(
        proc_root,
        105,
        ppid=999,
        starttime=10505,
        cwd=project_root / "mutated-parent",
        cmdline=_cmdline("node", "app-server-broker.mjs", "serve", "--cwd", str(project_root / "mutated-parent")),
    )
    _write_proc_pid(
        proc_root,
        106,
        ppid=1,
        starttime=10606,
        cwd=project_root / "mutated-start",
        cmdline=_cmdline("node", "app-server-broker.mjs", "serve", "--cwd", str(project_root / "mutated-start")),
    )

    _write_snapshot(
        tmp_path / "snapshot.txt",
        [
            "1 0 /sbin/init",
            f"101 1 node app-server-broker.mjs serve --cwd {project_root / 'slice-a'}",
            f"102 1 node app-server-broker.mjs serve --cwd {other_root / 'slice-b'}",
            f"103 1 node app-server-broker.mjs serve --cwd {project_root / 'gone'}",
            f"104 1 node app-server-broker.mjs serve --cwd {project_root / 'mutated-cmdline'}",
            f"105 1 node app-server-broker.mjs serve --cwd {project_root / 'mutated-parent'}",
            f"106 1 node app-server-broker.mjs serve --cwd {project_root / 'mutated-start'}",
        ],
    )

    result = _run_script(tmp_path, "--apply", "--cwd-root", str(project_root), mutate_on_kill=mutate)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "kill.log").read_text(encoding="utf-8").splitlines() == ["-TERM 101"]


def test_apply_skips_unknown_or_foreign_candidates_without_signaling(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    project_root = tmp_path / "workspace" / "project"
    other_root = tmp_path / "workspace" / "project-other"
    _write_killer(tmp_path / "fake-kill.sh")

    _write_proc_pid(
        proc_root,
        202,
        ppid=1,
        starttime=20202,
        cwd=other_root / "slice-b",
        cmdline=_cmdline("node", "app-server-broker.mjs", "serve", "--cwd", str(other_root / "slice-b")),
    )
    _write_proc_pid(
        proc_root,
        203,
        ppid=1,
        starttime=20303,
        cwd=None,
        cmdline=_cmdline("node", "app-server-broker.mjs", "serve", "--cwd", str(project_root / "gone")),
    )
    _write_proc_pid(
        proc_root,
        204,
        ppid=1,
        starttime=20404,
        cwd=project_root / "mutated-cmdline",
        cmdline=_cmdline("node", "other-broker.mjs", "serve", "--cwd", str(project_root / "mutated-cmdline")),
    )

    _write_snapshot(
        tmp_path / "snapshot.txt",
        [
            "1 0 /sbin/init",
            f"202 1 node app-server-broker.mjs serve --cwd {other_root / 'slice-b'}",
            f"203 1 node app-server-broker.mjs serve --cwd {project_root / 'gone'}",
            f"204 1 node app-server-broker.mjs serve --cwd {project_root / 'mutated-cmdline'}",
        ],
    )

    result = _run_script(tmp_path, "--apply", "--cwd-root", str(project_root))

    assert result.returncode == 0, result.stderr
    if (tmp_path / "kill.log").exists():
        assert (tmp_path / "kill.log").read_text(encoding="utf-8") == ""
