"""Phase 2a 降權啟動器（#584 未決 1 裁決＝systemd-run transient unit）的測試。

覆蓋面：

1. **direct 零回歸**——預設模式下 argv／env／shell flag 與改動前逐字相同。
2. **systemd-run argv 組裝**——unit 名（可追蹤＋唯一）／uid／gid／白名單 `--setenv`／
   `bash -c`（非 `-lc`，#588 第 2 點）。
3. **env 白名單不含 token 類**——GH_TOKEN／GITHUB_TOKEN／ANTHROPIC_API_KEY／
   CLAUDE_CONFIG_DIR 等在任何情況下都不得出現（#588 第 1 點）。
4. **fail-fast**——systemd 不可用／帳號不存在／模式值非法／transient unit 起不來時
   fail-closed 並帶 DiagnosticReason，**絕不**退回 direct。

`systemd-run` 本體全程 mock，測試不真跑 systemd、不建帳號、不碰 polkit。
"""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import paulsha_cortex.coordinator.job_runner as job_runner
import paulsha_cortex.coordinator.launcher as launcher_module
from paulsha_cortex.coordinator.diagnostics import DiagnosticReason
from paulsha_cortex.coordinator.job_runner import JobRunnerError
from paulsha_cortex.coordinator.launcher import SubprocessLauncher


# 每一輪 launch 測試都在乾淨的 env 上疊加，避免 operator 自己的 shell 汙染判定。
_ISOLATED_AGENTS_ROOT = tempfile.mkdtemp(prefix="psc-agents-root-")

_BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/var/lib/cortex-svc",
    # conftest 的 `_clear_runtime_env` 把 PSC_AGENTS_ROOT 指向 per-test 暫存目錄，
    # 但本檔的 launch 測試以 `clear=True` 重建整份 environ（要驗的就是白名單本身），
    # 那層保護因此被清掉。顯式帶上一個 per-process 暫存根：`launcher.launch()` 會在
    # coordinator 樹底下建這個 job 的成果 bundle spool（#623），少了它會落到 operator
    # 的真實 `$HOME`——正是 conftest #303 註記要防的事。
    "PSC_AGENTS_ROOT": _ISOLATED_AGENTS_ROOT,
    "LANG": "en_US.UTF-8",
}

_SECRET_ENV = {
    "GH_TOKEN": "gh-secret",
    "GITHUB_TOKEN": "github-secret",
    "COPILOT_GITHUB_TOKEN": "copilot-secret",
    "ANTHROPIC_API_KEY": "anthropic-secret",
    "OPENAI_API_KEY": "openai-secret",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "CLAUDE_CONFIG_DIR": "/var/lib/cortex-svc/.claude",
    "GH_CONFIG_DIR": "/var/lib/cortex-svc/.config/gh",
    "BASH_ENV": "/tmp/credential-exporter",
    "NODE_OPTIONS": "--require /tmp/inject.js",
    "PGPASSWORD": "postgres-secret",
}


class _FakeProc:
    """systemd-run client 的替身。`poll()` 語意即真實 Popen 的語意。"""

    def __init__(self, *, pid: int = 4242, exit_status: int | None = None) -> None:
        self.pid = pid
        self._exit_status = exit_status

    def poll(self) -> int | None:
        return self._exit_status


class _RecordingPopen:
    def __init__(self, *, proc: _FakeProc | None = None) -> None:
        self.calls: list[dict] = []
        self._proc = proc or _FakeProc()

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        return self._proc

    @property
    def call(self) -> dict:
        return self.calls[0]


def _degraded_env(**overrides: str) -> dict[str, str]:
    env = {
        **_BASE_ENV,
        **_SECRET_ENV,
        job_runner.JOB_RUNNER_ENV: job_runner.RUNNER_SYSTEMD_RUN,
    }
    env.update(overrides)
    return env


def _launch(
    launcher: SubprocessLauncher,
    *,
    env: dict[str, str],
    popen: _RecordingPopen,
    slice_id: str = "psc-0001-demo",
    preflight_ok: bool = True,
) -> None:
    """在完全受控的 env／systemd 探測 seam 下跑一次 launch。"""

    original = launcher_module.subprocess.Popen
    launcher_module.subprocess.Popen = popen
    patches = []
    if preflight_ok:
        patches = [
            mock.patch.object(job_runner.shutil, "which", return_value="/usr/bin/systemd-run"),
            mock.patch.object(job_runner, "_systemd_booted", return_value=True),
            mock.patch.object(job_runner, "_account_exists", return_value=True),
            mock.patch.object(job_runner, "_group_exists", return_value=True),
        ]
    try:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
            os.environ, env, clear=True
        ):
            with _nested(patches):
                launcher.launch(
                    slice_id=slice_id,
                    prompt="PROMPT",
                    worktree=d,
                    log_dir=str(Path(d) / "logs"),
                )
    finally:
        launcher_module.subprocess.Popen = original


class _nested:
    """把可變數量的 context manager 疊起來（測試 helper，不進生產路徑）。"""

    def __init__(self, managers) -> None:
        self._managers = list(managers)

    def __enter__(self):
        self._entered = [m.__enter__() for m in self._managers]
        return self._entered

    def __exit__(self, *exc_info):
        for manager in reversed(self._managers):
            manager.__exit__(*exc_info)
        return False


def _unwrap_exit_recorder(argv: list[str]) -> list[str]:
    """剝掉 #604 的 Manager 側 exit 記帳 shell，取回真正的 systemd client argv。

    降權啟動的最外層現在恆為 `bash -c '<client…>; rc=$?; printf … > <sentinel>;
    exit "$rc"'`（寫者＝Manager 的 uid，不是 job）。本 helper 同時**釘住那層的形狀**：
    形狀變了就整批降權測試立刻紅，而不是靜默退回檢查外層 argv 的空集合。
    """

    assert argv[:2] == ["bash", "-c"], argv
    head, sep, tail = argv[2].partition("; rc=$?; ")
    assert sep, argv[2]
    assert tail.startswith('printf %s "$rc" > '), tail
    assert tail.endswith('; exit "$rc"'), tail
    return shlex.split(head)


def _recorded_sentinel(argv: list[str]) -> str:
    """從 exit 記帳 shell 取出它會寫的 sentinel 路徑。"""

    _, _, tail = argv[2].partition("; rc=$?; ")
    written = tail[len('printf %s "$rc" > ') : -len('; exit "$rc"')]
    return shlex.split(written)[0]


def _setenv_map(argv: list[str]) -> dict[str, str]:
    """把 argv 上的 `--setenv=K=V` 還原成 dict——transient unit 實際看到的環境。"""

    out: dict[str, str] = {}
    for item in argv:
        if item.startswith("--setenv="):
            key, _, value = item[len("--setenv=") :].partition("=")
            out[key] = value
    return out


class RunnerModeTests(unittest.TestCase):
    def test_unset_is_direct(self) -> None:
        self.assertEqual(job_runner.resolve_runner_mode({}), job_runner.RUNNER_DIRECT)

    def test_blank_is_direct(self) -> None:
        self.assertEqual(
            job_runner.resolve_runner_mode({job_runner.JOB_RUNNER_ENV: "  "}),
            job_runner.RUNNER_DIRECT,
        )

    def test_explicit_modes(self) -> None:
        for value, expected in (
            ("direct", job_runner.RUNNER_DIRECT),
            ("systemd-run", job_runner.RUNNER_SYSTEMD_RUN),
            ("SYSTEMD-RUN", job_runner.RUNNER_SYSTEMD_RUN),
        ):
            self.assertEqual(
                job_runner.resolve_runner_mode({job_runner.JOB_RUNNER_ENV: value}),
                expected,
            )

    def test_unknown_mode_is_fail_closed_not_direct(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            job_runner.resolve_runner_mode({job_runner.JOB_RUNNER_ENV: "systemdrun"})
        self.assertEqual(ctx.exception.diagnostic.reason, "job-runner-mode-invalid")

    def test_builder_account_defaults_to_permgen_two_way_scheme(self) -> None:
        from paulsha_cortex.trust_root import permgen
        from paulsha_cortex.trust_root.registry import Principal

        self.assertEqual(
            job_runner.resolve_builder_account({}),
            permgen.TWO_WAY_SCHEME.resolve(Principal.BUILDER),
        )

    def test_builder_account_is_configurable(self) -> None:
        env = {job_runner.BUILDER_ACCOUNT_ENV: "cortex-worker"}
        self.assertEqual(job_runner.resolve_builder_account(env), "cortex-worker")
        # group 預設沿用 UidScheme.group_of（每帳號一個同名 group）
        self.assertEqual(job_runner.resolve_builder_group(env), "cortex-worker")

    def test_builder_group_is_separately_configurable(self) -> None:
        env = {
            job_runner.BUILDER_ACCOUNT_ENV: "cortex-worker",
            job_runner.BUILDER_GROUP_ENV: "cortex-jobs",
        }
        self.assertEqual(job_runner.resolve_builder_group(env), "cortex-jobs")

    def test_account_name_injection_is_rejected(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            job_runner.resolve_builder_account(
                {job_runner.BUILDER_ACCOUNT_ENV: "root --property=User=root"}
            )
        self.assertEqual(ctx.exception.diagnostic.reason, "job-runner-account-name-invalid")

    def test_start_timeout_invalid_is_fail_closed(self) -> None:
        for value in ("abc", "-1"):
            with self.assertRaises(JobRunnerError):
                job_runner.resolve_start_timeout_ms({job_runner.START_TIMEOUT_ENV: value})

    def test_start_timeout_default_and_override(self) -> None:
        self.assertEqual(
            job_runner.resolve_start_timeout_ms({}), job_runner.DEFAULT_START_TIMEOUT_MS
        )
        self.assertEqual(
            job_runner.resolve_start_timeout_ms({job_runner.START_TIMEOUT_ENV: "0"}), 0
        )


class BuilderEnvAllowlistTests(unittest.TestCase):
    def _build(self, **overrides) -> dict[str, str]:
        manager_env = {**_BASE_ENV, **_SECRET_ENV}
        manager_env.update(overrides)
        return job_runner.build_builder_env(
            manager_env=manager_env,
            job_id="job-1",
            slice_id="slice-1",
            repo_root="/opt/cortex/src",
        )

    def test_never_carries_token_shaped_variables(self) -> None:
        env = self._build()
        for name in _SECRET_ENV:
            self.assertNotIn(name, env, f"{name} 不得出現在 builder env")

    def test_no_key_matches_credential_pattern(self) -> None:
        env = self._build()
        for key in env:
            self.assertIsNone(
                job_runner.CREDENTIAL_ENV_RE.search(key),
                f"builder env 出現憑證形狀變數: {key}",
            )
            self.assertNotIn(key, job_runner.DENIED_ENV_NAMES)

    def test_exact_key_set_is_the_allowlist(self) -> None:
        env = self._build()
        allowed = {item.name for item in job_runner.BUILDER_FORWARDED_ENV}
        allowed |= set(job_runner.BUILDER_SYNTHESIZED_ENV)
        self.assertLessEqual(set(env), allowed)

    def test_synthesized_markers_are_present(self) -> None:
        env = self._build()
        self.assertEqual(env["PSC_JOB_ID"], "job-1")
        self.assertEqual(env["PSC_SLICE_ID"], "slice-1")
        self.assertEqual(env["PSC_REPO_ROOT"], "/opt/cortex/src")

    def test_daemon_home_is_never_forwarded(self) -> None:
        # #588 第 1 點：daemon 的 HOME 是 cortex-svc 的樹；未設 PSC_BUILDER_HOME 時
        # 交給 systemd 依 passwd 填 builder 自己的 HOME，launcher 一律不轉發。
        env = self._build()
        self.assertNotIn("HOME", env)

    def test_builder_home_override_is_used(self) -> None:
        env = self._build(**{job_runner.BUILDER_HOME_ENV: "/var/lib/cortex-builder"})
        self.assertEqual(env["HOME"], "/var/lib/cortex-builder")
        self.assertNotEqual(env["HOME"], _BASE_ENV["HOME"])

    def test_path_is_forwarded_and_overridable(self) -> None:
        self.assertEqual(self._build()["PATH"], _BASE_ENV["PATH"])
        env = self._build(**{job_runner.BUILDER_PATH_ENV: "/opt/cortex/bin:/usr/bin"})
        self.assertEqual(env["PATH"], "/opt/cortex/bin:/usr/bin")

    def test_relay_target_only_when_configured(self) -> None:
        manager_env = {**_BASE_ENV}
        without = job_runner.build_builder_env(
            manager_env=manager_env, job_id="j", slice_id="s", repo_root="/r"
        )
        self.assertNotIn("PSC_RELAY_TARGET", without)
        with_relay = job_runner.build_builder_env(
            manager_env=manager_env,
            job_id="j",
            slice_id="s",
            repo_root="/r",
            relay_target="psc",
        )
        self.assertEqual(with_relay["PSC_RELAY_TARGET"], "psc")

    def test_git_repository_selectors_are_not_forwarded(self) -> None:
        env = self._build(GIT_DIR="/evil/.git", GIT_WORK_TREE="/evil", GIT_CONFIG_COUNT="1")
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_COUNT"):
            self.assertNotIn(key, env)

    def test_every_forwarded_entry_carries_a_rationale(self) -> None:
        for item in job_runner.BUILDER_FORWARDED_ENV:
            self.assertTrue(item.rationale.strip(), f"{item.name} 缺少白名單理由")

    def test_credential_pattern_is_shared_with_launcher(self) -> None:
        # 兩處漂移就會出現「reviewer sandbox 認為是 secret、builder 白名單不認為」。
        self.assertIs(launcher_module._CREDENTIAL_ENV_RE, job_runner.CREDENTIAL_ENV_RE)

    def test_guard_rejects_credential_shaped_allowlist_entry(self) -> None:
        # defense-in-depth：白名單被改壞時當場紅，而不是等 dogfooding 才發現漏 token。
        poisoned = job_runner.BUILDER_FORWARDED_ENV + (
            job_runner.ForwardedEnvVar("SOME_API_KEY", "刻意注入的壞白名單"),
        )
        with mock.patch.object(job_runner, "BUILDER_FORWARDED_ENV", poisoned):
            with self.assertRaises(JobRunnerError) as ctx:
                job_runner.build_builder_env(
                    manager_env={"SOME_API_KEY": "leaked"},
                    job_id="j",
                    slice_id="s",
                    repo_root="/r",
                )
        self.assertEqual(ctx.exception.diagnostic.reason, "job-runner-credential-env-leak")

    def test_newline_in_value_is_rejected(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            job_runner.build_builder_env(
                manager_env={"PATH": "/usr/bin\n--property=User=root"},
                job_id="j",
                slice_id="s",
                repo_root="/r",
            )
        self.assertEqual(ctx.exception.diagnostic.reason, "job-runner-env-value-invalid")


class TransientUnitNameTests(unittest.TestCase):
    def test_name_is_traceable_and_scoped_to_prefix(self) -> None:
        unit = job_runner.transient_unit_name("psc-0042-add-thing")
        self.assertTrue(unit.startswith(job_runner.UNIT_NAME_PREFIX))
        self.assertTrue(unit.endswith(".service"))
        self.assertIn("psc-0042-add-thing", unit)

    def test_illegal_characters_are_sanitized(self) -> None:
        unit = job_runner.transient_unit_name("job/../etc passwd")
        self.assertNotIn("/", unit[: -len(".service")])
        self.assertNotIn(" ", unit)

    def test_distinct_job_ids_never_collide_after_sanitization(self) -> None:
        first = job_runner.transient_unit_name("job/a")
        second = job_runner.transient_unit_name("job:a")
        self.assertNotEqual(first, second)

    def test_deterministic(self) -> None:
        self.assertEqual(
            job_runner.transient_unit_name("job-1"), job_runner.transient_unit_name("job-1")
        )

    def test_empty_job_id_is_fail_closed(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            job_runner.transient_unit_name("  ")
        self.assertEqual(ctx.exception.diagnostic.reason, "job-runner-unit-name-invalid")

    def test_length_is_bounded(self) -> None:
        unit = job_runner.transient_unit_name("x" * 400)
        self.assertLess(len(unit), 128)


class SystemdRunArgvTests(unittest.TestCase):
    def _argv(self, **overrides) -> list[str]:
        kwargs = {
            "systemd_run": "/usr/bin/systemd-run",
            "unit": "cortex-job-demo-deadbeef.service",
            "account": "cortex-builder",
            "group": "cortex-builder",
            "working_directory": "/var/lib/cortex/worktree/demo",
            "env": {"PATH": "/usr/bin", "PSC_JOB_ID": "demo"},
            "command": ["bash", "-c", "echo hi"],
        }
        kwargs.update(overrides)
        return job_runner.build_systemd_run_argv(**kwargs)

    def test_core_flags(self) -> None:
        argv = self._argv()
        self.assertEqual(argv[0], "/usr/bin/systemd-run")
        for flag in (
            "--quiet",
            "--collect",
            "--pipe",
            "--wait",
            "--unit=cortex-job-demo-deadbeef.service",
            "--uid=cortex-builder",
            "--gid=cortex-builder",
            "--service-type=exec",
            "--working-directory=/var/lib/cortex/worktree/demo",
            "--property=NoNewPrivileges=yes",
        ):
            self.assertIn(flag, argv)

    def test_command_follows_double_dash(self) -> None:
        argv = self._argv()
        self.assertIn("--", argv)
        self.assertEqual(argv[argv.index("--") + 1 :], ["bash", "-c", "echo hi"])

    def test_setenv_is_sorted_and_complete(self) -> None:
        argv = self._argv(env={"B": "2", "A": "1"})
        self.assertEqual(
            [item for item in argv if item.startswith("--setenv=")],
            ["--setenv=A=1", "--setenv=B=2"],
        )

    def test_no_same_dir_flag(self) -> None:
        # Manager 的 cwd 不是 worktree，`--same-dir` 會把 unit 的 cwd 指錯。
        argv = self._argv()
        self.assertNotIn("--same-dir", argv)
        self.assertNotIn("-d", argv)


class PreflightTests(unittest.TestCase):
    def _preflight(self, **overrides):
        kwargs = {
            "account": "cortex-builder",
            "group": "cortex-builder",
            "which": lambda name: "/usr/bin/systemd-run",
            "account_exists": lambda name: True,
            "group_exists": lambda name: True,
            "systemd_booted": lambda: True,
        }
        kwargs.update(overrides)
        return job_runner.preflight_systemd_run(**kwargs)

    def test_happy_path_returns_absolute_binary(self) -> None:
        self.assertEqual(self._preflight(), "/usr/bin/systemd-run")

    def test_missing_systemd_run_binary(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            self._preflight(which=lambda name: None)
        self.assertEqual(ctx.exception.diagnostic.reason, "job-runner-systemd-run-missing")

    def test_not_booted_with_systemd(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            self._preflight(systemd_booted=lambda: False)
        self.assertEqual(ctx.exception.diagnostic.reason, "job-runner-systemd-unavailable")

    def test_missing_builder_account(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            self._preflight(account_exists=lambda name: False)
        self.assertEqual(ctx.exception.diagnostic.reason, "job-runner-builder-account-missing")
        self.assertEqual(ctx.exception.diagnostic.context["account"], "cortex-builder")

    def test_missing_builder_group(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            self._preflight(group_exists=lambda name: False)
        self.assertEqual(ctx.exception.diagnostic.reason, "job-runner-builder-group-missing")

    def test_every_failure_carries_a_diagnostic_reason(self) -> None:
        # #570／#527 契約：fail-closed 一定帶結構化理由，不是裸字串。
        for override in (
            {"which": lambda name: None},
            {"systemd_booted": lambda: False},
            {"account_exists": lambda name: False},
            {"group_exists": lambda name: False},
        ):
            with self.assertRaises(JobRunnerError) as ctx:
                self._preflight(**override)
            self.assertIsInstance(ctx.exception.diagnostic, DiagnosticReason)
            self.assertTrue(ctx.exception.diagnostic.source.startswith("job_runner."))
            self.assertIn(ctx.exception.diagnostic.reason, str(ctx.exception))


class PreparePlanTests(unittest.TestCase):
    def _prepare(self, env: dict[str, str]):
        with mock.patch.object(
            job_runner.shutil, "which", return_value="/usr/bin/systemd-run"
        ), mock.patch.object(
            job_runner, "_systemd_booted", return_value=True
        ), mock.patch.object(
            job_runner, "_account_exists", return_value=True
        ), mock.patch.object(
            job_runner, "_group_exists", return_value=True
        ):
            return job_runner.prepare_systemd_run(env, job_id="psc-0001-demo")

    def test_plan_carries_verified_identity(self) -> None:
        plan = self._prepare({})
        self.assertEqual(plan.binary, "/usr/bin/systemd-run")
        self.assertEqual(plan.account, job_runner.DEFAULT_BUILDER_ACCOUNT)
        self.assertEqual(plan.group, job_runner.DEFAULT_BUILDER_ACCOUNT)
        self.assertEqual(plan.unit, job_runner.transient_unit_name("psc-0001-demo"))

    def test_preflight_failure_propagates(self) -> None:
        with mock.patch.object(job_runner.shutil, "which", return_value=None):
            with self.assertRaises(JobRunnerError):
                job_runner.prepare_systemd_run({}, job_id="j")


class StartConfirmationTests(unittest.TestCase):
    def test_running_unit_passes_after_window(self) -> None:
        slept: list[float] = []
        clock = iter([0.0, 0.05, 0.3])
        job_runner.confirm_transient_unit_started(
            process=_FakeProc(exit_status=None),
            sentinel="/nonexistent/sentinel",
            unit="cortex-job-x.service",
            account="cortex-builder",
            timeout_ms=200,
            monotonic=lambda: next(clock),
            sleep=slept.append,
        )
        self.assertTrue(slept)

    def test_immediate_exit_without_sentinel_is_fail_closed(self) -> None:
        with self.assertRaises(JobRunnerError) as ctx:
            job_runner.confirm_transient_unit_started(
                process=_FakeProc(exit_status=1),
                sentinel="/nonexistent/sentinel",
                unit="cortex-job-x.service",
                account="cortex-builder",
                timeout_ms=200,
                monotonic=lambda: 0.0,
                sleep=lambda _s: None,
            )
        diagnostic = ctx.exception.diagnostic
        self.assertEqual(diagnostic.reason, "job-runner-transient-unit-start-failed")
        self.assertEqual(diagnostic.context["unit"], "cortex-job-x.service")
        self.assertEqual(diagnostic.context["account"], "cortex-builder")

    def test_polkit_denial_text_reaches_the_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "job.jsonl"
            log_path.write_text(
                "Failed to start transient service unit: Access denied\n", encoding="utf-8"
            )
            with self.assertRaises(JobRunnerError) as ctx:
                job_runner.confirm_transient_unit_started(
                    process=_FakeProc(exit_status=1),
                    sentinel=str(Path(d) / "missing.exit"),
                    unit="cortex-job-x.service",
                    account="cortex-builder",
                    log_path=str(log_path),
                    timeout_ms=0,
                    monotonic=lambda: 0.0,
                    sleep=lambda _s: None,
                )
        self.assertIn("Access denied", ctx.exception.diagnostic.detail)

    def test_fast_job_with_sentinel_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sentinel = Path(d) / "job.exit"
            sentinel.write_text("0", encoding="utf-8")
            job_runner.confirm_transient_unit_started(
                process=_FakeProc(exit_status=0),
                sentinel=str(sentinel),
                unit="cortex-job-x.service",
                account="cortex-builder",
                timeout_ms=200,
                monotonic=lambda: 0.0,
                sleep=lambda _s: None,
            )


class DirectModeRegressionTests(unittest.TestCase):
    """預設（未設 PSC_JOB_RUNNER）與顯式 direct 一律走現行路徑。"""

    def test_default_builder_uses_login_shell_and_no_systemd_run(self) -> None:
        popen = _RecordingPopen()
        _launch(SubprocessLauncher("codex"), env=dict(_BASE_ENV), popen=popen, preflight_ok=False)
        argv = popen.call["argv"]
        self.assertEqual(argv[:2], ["bash", "-lc"])
        self.assertNotIn("stdin", popen.call)

    def test_explicit_direct_matches_default(self) -> None:
        default = _RecordingPopen()
        explicit = _RecordingPopen()
        _launch(SubprocessLauncher("codex"), env=dict(_BASE_ENV), popen=default, preflight_ok=False)
        _launch(
            SubprocessLauncher("codex"),
            env={**_BASE_ENV, job_runner.JOB_RUNNER_ENV: "direct"},
            popen=explicit,
            preflight_ok=False,
        )
        # argv 內嵌各自的 tempdir，因此比對「形狀」而非逐字：同樣的 shell flag、
        # 同樣不經 systemd-run、同樣的 Popen kwargs 形狀。
        self.assertEqual(default.call["argv"][:2], explicit.call["argv"][:2])
        self.assertEqual(len(default.call["argv"]), len(explicit.call["argv"]))
        self.assertEqual(set(default.call) - {"argv"}, set(explicit.call) - {"argv"})
        self.assertEqual(
            set(default.call["env"]) | {job_runner.JOB_RUNNER_ENV},
            set(explicit.call["env"]) | {job_runner.JOB_RUNNER_ENV},
        )

    def test_direct_mode_still_inherits_daemon_env(self) -> None:
        # 現行行為（#588 尚未修的那一半）：direct 模式下 builder 仍拿到 daemon environ。
        # 這條測試釘住「本 PR 不改 direct 行為」，也標出降權模式才是解法。
        popen = _RecordingPopen()
        _launch(
            SubprocessLauncher("codex"),
            env={**_BASE_ENV, "GH_TOKEN": "gh-secret"},
            popen=popen,
            preflight_ok=False,
        )
        self.assertEqual(popen.call["env"].get("GH_TOKEN"), "gh-secret")

    def test_invalid_mode_fails_closed_before_any_spawn(self) -> None:
        popen = _RecordingPopen()
        with self.assertRaises(JobRunnerError):
            _launch(
                SubprocessLauncher("codex"),
                env={**_BASE_ENV, job_runner.JOB_RUNNER_ENV: "systemd_run"},
                popen=popen,
                preflight_ok=False,
            )
        self.assertEqual(popen.calls, [])


class DegradedLaunchTests(unittest.TestCase):
    def _launch_builder(self, *, env=None, executor: str = "codex") -> _RecordingPopen:
        popen = _RecordingPopen()
        _launch(
            SubprocessLauncher(executor),
            env=env if env is not None else _degraded_env(),
            popen=popen,
        )
        return popen

    def _client_argv(self, *, env=None, executor: str = "codex") -> list[str]:
        """降權啟動的 systemd client argv（已剝掉 #604 的 exit 記帳外層）。"""

        return _unwrap_exit_recorder(
            self._launch_builder(env=env, executor=executor).call["argv"]
        )

    def test_exit_sentinel_is_written_by_the_manager_side_recorder(self) -> None:
        # #604：sentinel 的寫者必須在 Manager 這一側。外層 shell 由 Manager 的
        # environ／uid 執行（見 `test_client_env_is_not_the_unit_env`），job 側的
        # wrapper 內不得再出現任何 sentinel 重導向。
        call = self._launch_builder().call
        sentinel = _recorded_sentinel(call["argv"])
        self.assertTrue(sentinel.endswith(".exit"), sentinel)
        inner = _unwrap_exit_recorder(call["argv"])
        job_script = inner[inner.index("--") + 1 :][2]
        self.assertNotIn(sentinel, job_script)
        self.assertNotIn('printf %s "$?"', job_script)

    def test_builder_is_wrapped_in_systemd_run(self) -> None:
        argv = self._client_argv()
        self.assertEqual(argv[0], "/usr/bin/systemd-run")
        self.assertIn("--uid=cortex-builder", argv)
        self.assertIn("--gid=cortex-builder", argv)
        self.assertIn("--collect", argv)
        self.assertIn("--pipe", argv)

    def test_unit_name_carries_the_job_id(self) -> None:
        argv = self._client_argv()
        unit = next(item for item in argv if item.startswith("--unit="))
        self.assertTrue(unit.startswith(f"--unit={job_runner.UNIT_NAME_PREFIX}"))
        self.assertIn("psc-0001-demo", unit)

    def test_inner_shell_is_non_login(self) -> None:
        # #588 第 2 點：login shell 會讓 ~/.profile 在白名單 env 之後重新匯入。
        argv = self._client_argv()
        tail = argv[argv.index("--") + 1 :]
        self.assertEqual(tail[:2], ["bash", "-c"])
        self.assertNotIn("-lc", argv)

    def test_unit_env_never_contains_tokens(self) -> None:
        argv = self._client_argv()
        unit_env = _setenv_map(argv)
        for name in _SECRET_ENV:
            self.assertNotIn(name, unit_env)
        for key in unit_env:
            self.assertIsNone(job_runner.CREDENTIAL_ENV_RE.search(key))
        # 值層面的兜底：任何一個 secret 值都不得出現在整條 argv 上。
        joined = "\x00".join(argv)
        for value in _SECRET_ENV.values():
            self.assertNotIn(value, joined)

    def test_copilot_token_normalization_is_inert_under_degraded_runner(self) -> None:
        # direct 模式會把 GH_TOKEN 正規化成 COPILOT_GITHUB_TOKEN 送進 job；
        # 降權模式下 env 白名單裡沒有任何 token 候選，因此那條路徑自然變成 no-op。
        argv = self._client_argv(executor="copilot")
        self.assertNotIn("COPILOT_GITHUB_TOKEN", _setenv_map(argv))

    def test_unit_env_carries_job_markers(self) -> None:
        unit_env = _setenv_map(self._client_argv())
        self.assertEqual(unit_env["PSC_JOB_ID"], "psc-0001-demo")
        self.assertEqual(unit_env["PSC_SLICE_ID"], "psc-0001-demo")
        self.assertIn("PSC_REPO_ROOT", unit_env)
        self.assertEqual(unit_env["PATH"], _BASE_ENV["PATH"])

    def test_stdin_is_devnull_and_working_directory_is_the_worktree(self) -> None:
        call = self._launch_builder().call
        self.assertEqual(call["stdin"], subprocess.DEVNULL)
        worktree = call["cwd"]
        self.assertIn(
            f"--working-directory={worktree}", _unwrap_exit_recorder(call["argv"])
        )

    def test_client_env_is_not_the_unit_env(self) -> None:
        # systemd-run client 保留完整 env（polkit 可能要查 session），但那份 env
        # 不會進到 unit——unit 只看得到 `--setenv` 白名單。
        call = self._launch_builder().call
        self.assertIn("GH_TOKEN", call["env"])
        self.assertNotIn("GH_TOKEN", _setenv_map(_unwrap_exit_recorder(call["argv"])))

    def test_builder_account_override_flows_into_argv(self) -> None:
        env = _degraded_env(**{job_runner.BUILDER_ACCOUNT_ENV: "cortex-worker"})
        argv = self._client_argv(env=env)
        self.assertIn("--uid=cortex-worker", argv)
        self.assertNotIn("--uid=cortex-builder", argv)

    def test_reviewer_is_never_degraded(self) -> None:
        # 二分方案：reviewer 與 Manager 同帳號（cortex-svc），不經降權。
        popen = _RecordingPopen()
        _launch(
            SubprocessLauncher("claude").as_review_only(
                terminal_kind="workflow-verification-result"
            ),
            env=_degraded_env(),
            popen=popen,
        )
        self.assertEqual(popen.call["argv"][:2], ["bash", "-c"])
        self.assertNotIn("stdin", popen.call)

    def test_planner_is_never_degraded(self) -> None:
        popen = _RecordingPopen()
        _launch(SubprocessLauncher("codex").as_read_only(), env=_degraded_env(), popen=popen)
        self.assertEqual(popen.call["argv"][:2], ["bash", "-lc"])

    def test_missing_account_fails_closed_without_spawning(self) -> None:
        popen = _RecordingPopen()
        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = popen
        try:
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ, _degraded_env(), clear=True
            ), mock.patch.object(
                job_runner.shutil, "which", return_value="/usr/bin/systemd-run"
            ), mock.patch.object(
                job_runner, "_systemd_booted", return_value=True
            ), mock.patch.object(
                job_runner, "_account_exists", return_value=False
            ):
                with self.assertRaises(JobRunnerError) as ctx:
                    SubprocessLauncher("codex").launch(
                        slice_id="s", prompt="P", worktree=d, log_dir=str(Path(d) / "logs")
                    )
        finally:
            launcher_module.subprocess.Popen = original
        self.assertEqual(
            ctx.exception.diagnostic.reason, "job-runner-builder-account-missing"
        )
        # 不得靜默退回 direct：一個 job 都不能被 spawn。
        self.assertEqual(popen.calls, [])

    def test_transient_unit_start_failure_fails_closed(self) -> None:
        popen = _RecordingPopen(proc=_FakeProc(exit_status=1))
        with self.assertRaises(JobRunnerError) as ctx:
            _launch(
                SubprocessLauncher("codex"),
                env=_degraded_env(**{job_runner.START_TIMEOUT_ENV: "0"}),
                popen=popen,
            )
        self.assertEqual(
            ctx.exception.diagnostic.reason, "job-runner-transient-unit-start-failed"
        )

    def test_executor_environment_reports_the_builder_env(self) -> None:
        # #262 D2：preflight 必須回報 job 真的會看到的環境，否則只是安慰劑。
        with mock.patch.dict(
            os.environ,
            _degraded_env(**{job_runner.BUILDER_HOME_ENV: "/var/lib/cortex-builder"}),
            clear=True,
        ):
            reported = SubprocessLauncher("codex").executor_environment()
        self.assertEqual(reported.home, "/var/lib/cortex-builder")
        self.assertEqual(reported.path, _BASE_ENV["PATH"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
