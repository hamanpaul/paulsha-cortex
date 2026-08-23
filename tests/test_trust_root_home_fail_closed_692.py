"""#692：降權 job 的 `HOME` 必須像 `PATH` 一樣在起跑前 fail-closed。

`#679` 已把 `PATH` 收成「缺席即拒絕起跑」，但 `HOME` 仍停留在舊形態：Manager
未宣告 `PSC_*_HOME` 時，job spec 會直接少掉 `HOME`，shim 也不會在 exec 前拒絕。
結果不是一筆 pre-launch 診斷，而是模型程式碼在更深處以 `$HOME is not defined`
之類的症狀收場。

本檔把 accepted plan 要求的缺口逐條釘成 RED：

1. 缺／空 `PSC_*_HOME` 是 launch configuration error。
2. `PATH`＋`HOME` 同時缺時，診斷要把兩條契約都講出來。
3. `HOME` 必須是核准過的 per-principal 絕對路徑，不能是相對路徑、symlink、
   或 owner 不符。
4. shim 不得把 unit/daemon 的 `HOME` 當成 child env 的 fallback。
5. 缺 `HOME` 必須在接管 log／exec 前就攔下，並留下 redacted 的 pre-launch 診斷。
"""

from __future__ import annotations

import json
import os
import pwd
from pathlib import Path
from unittest import mock

import pytest

from paulsha_cortex.coordinator import job_runner, job_shim
from paulsha_cortex.coordinator.job_runner import JobRunnerError
from paulsha_cortex.trust_root import permgen
from paulsha_cortex.trust_root.registry import Principal

_ROLE_HOME_ENV = {
    role: job_runner.resolve_job_role(role).home_env
    for role in sorted(job_runner.JOB_ROLE_CONFIG)
}
_ROLE_PATH_ENV = {
    role: job_runner.resolve_job_role(role).path_env
    for role in sorted(job_runner.JOB_ROLE_CONFIG)
}


def _manager_env(**overrides: str) -> dict[str, str]:
    env = {
        "PATH": "/opt/cortex/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/var/lib/cortex-manager",
        "LANG": "en_US.UTF-8",
        job_runner.BUILDER_PATH_ENV: "/opt/cortex/toolchain/bin:/usr/bin:/bin",
        job_runner.REVIEWER_PATH_ENV: "/opt/cortex/toolchain/bin:/usr/bin:/bin",
        job_runner.GATE_PATH_ENV: "/opt/cortex/toolchain/bin:/usr/bin:/bin",
    }
    env.update(overrides)
    return env


def _build_env(*, role: str, manager_env: dict[str, str]) -> dict[str, str]:
    return job_runner.build_job_env(
        manager_env=manager_env,
        job_id="job-692",
        slice_id="slice-692",
        repo_root="/srv/cortex/repo",
        workspace=None,
        role=role,
    )


def _shim_spec(*, root: Path, env: dict[str, str]) -> dict[str, object]:
    return {
        "spec_version": job_runner.JOB_SPEC_VERSION,
        "instance": "demo",
        "job_id": "job-692",
        "unit": "cortex-builder-job@demo.service",
        "command": ["bash", "-c", "true"],
        "working_directory": str(root),
        "log_path": str(root / "demo.log"),
        "env": env,
    }


@pytest.mark.parametrize("role", sorted(_ROLE_HOME_ENV))
@pytest.mark.parametrize("declared", [None, "", "   "], ids=["missing", "empty", "blank"])
def test_build_job_env_rejects_missing_or_blank_role_home(
    role: str, declared: str | None,
) -> None:
    env = _manager_env()
    home_env = _ROLE_HOME_ENV[role]
    if declared is not None:
        env[home_env] = declared

    with pytest.raises(JobRunnerError) as excinfo:
        _build_env(role=role, manager_env=env)

    diagnostic = excinfo.value.diagnostic
    assert diagnostic.reason == "job-runner-home-undeclared"
    message = str(excinfo.value)
    assert home_env in message
    assert "Environment=HOME=" in message


@pytest.mark.parametrize("role", sorted(_ROLE_HOME_ENV))
def test_prelaunch_diagnostic_names_path_and_home_when_both_are_missing(role: str) -> None:
    missing = {_ROLE_PATH_ENV[role], _ROLE_HOME_ENV[role]}
    env = {key: value for key, value in _manager_env().items() if key not in missing}

    with pytest.raises(JobRunnerError) as excinfo:
        _build_env(role=role, manager_env=env)

    message = str(excinfo.value)
    assert _ROLE_PATH_ENV[role] in message
    assert _ROLE_HOME_ENV[role] in message


def test_build_job_env_rejects_relative_home_paths() -> None:
    env = _manager_env()
    env[job_runner.BUILDER_HOME_ENV] = "relative/builder-home"

    with pytest.raises(JobRunnerError) as excinfo:
        _build_env(role=job_runner.JOB_ROLE_BUILDER, manager_env=env)

    assert excinfo.value.diagnostic.reason == "job-runner-home-not-absolute"
    assert job_runner.BUILDER_HOME_ENV in str(excinfo.value)
    assert "absolute" in str(excinfo.value)


def test_build_job_env_rejects_symlink_home_without_echoing_operator_secrets(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real-home"
    target.mkdir()
    link = tmp_path / "operator-secret-home"
    link.symlink_to(target, target_is_directory=True)
    env = _manager_env()
    env.update(
        {
            "GH_TOKEN": "gh-secret",
            "GITHUB_TOKEN": "github-secret",
            job_runner.BUILDER_HOME_ENV: str(link),
        }
    )

    with pytest.raises(JobRunnerError) as excinfo:
        _build_env(role=job_runner.JOB_ROLE_BUILDER, manager_env=env)

    message = str(excinfo.value)
    assert excinfo.value.diagnostic.reason == "job-runner-home-symlink"
    assert job_runner.BUILDER_HOME_ENV in message
    assert str(link) not in message
    assert str(target) not in message
    assert "gh-secret" not in message
    assert "github-secret" not in message


def test_build_job_env_rejects_home_owned_by_someone_else(tmp_path: Path) -> None:
    home = tmp_path / "builder-home"
    home.mkdir()
    env = _manager_env()
    env[job_runner.BUILDER_HOME_ENV] = str(home)
    fake_builder = pwd.struct_passwd(
        (
            "cortex-builder",
            "x",
            home.stat().st_uid + 1,
            os.getegid(),
            "",
            str(home),
            "/usr/sbin/nologin",
        )
    )

    with mock.patch.object(job_runner.pwd, "getpwnam", return_value=fake_builder):
        with pytest.raises(JobRunnerError) as excinfo:
            _build_env(role=job_runner.JOB_ROLE_BUILDER, manager_env=env)

    assert excinfo.value.diagnostic.reason == "job-runner-home-owner-mismatch"
    assert "owner" in str(excinfo.value)


@pytest.mark.parametrize(
    ("role", "principal"),
    (
        (job_runner.JOB_ROLE_BUILDER, Principal.BUILDER),
        (job_runner.JOB_ROLE_REVIEW, Principal.REVIEWER),
        (job_runner.JOB_ROLE_GATE, Principal.GATE),
    ),
)
def test_generated_unit_home_contract_matches_runtime_env(role: str, principal: Principal) -> None:
    account = job_runner.resolve_job_account({}, role=role)
    declared_home = permgen.DEFAULT_LAYOUT.home_of(account)
    with mock.patch.object(job_runner, "_account_ids", return_value=None):
        env = _build_env(
            role=role, manager_env=_manager_env(**{_ROLE_HOME_ENV[role]: declared_home})
        )
    unit = permgen.build_job_unit(permgen.FOUR_WAY_SCHEME, principal=principal)

    assert env["HOME"] == declared_home
    assert f"#      {_ROLE_HOME_ENV[role]}={declared_home}" in unit.content
    assert f"Environment=HOME={declared_home}" in unit.content


def test_shim_refuses_to_inherit_home_from_the_unit_layer() -> None:
    with pytest.raises(job_shim.ShimError) as excinfo:
        job_shim.resolve_job_env(
            {"env": {"PATH": "/opt/cortex/toolchain/bin:/usr/bin:/bin"}},
            {
                "PATH": "/opt/cortex/toolchain/bin:/usr/bin:/bin",
                "HOME": "/var/lib/cortex-builder",
            },
        )

    assert "HOME" in str(excinfo.value)


def test_shim_main_reports_missing_home_before_taking_over_the_log(tmp_path: Path) -> None:
    spool = tmp_path / "job-specs"
    spool.mkdir()
    spec = _shim_spec(
        root=tmp_path, env={"PATH": "/opt/cortex/toolchain/bin:/usr/bin:/bin"},
    )
    (spool / "demo.json").write_text(json.dumps(spec), encoding="utf-8")

    with mock.patch.object(
        job_shim,
        "_take_over_stdio",
        side_effect=AssertionError("missing HOME must fail before log takeover"),
    ):
        rc = job_shim.main(
            ["demo"],
            {
                job_runner.JOB_SPEC_SPOOL_ENV: str(spool),
                "PATH": "/opt/cortex/toolchain/bin:/usr/bin:/bin",
                "HOME": "/var/lib/cortex-builder",
            },
        )

    assert rc == job_shim.EXIT_SPEC_ERROR
    assert not Path(spec["log_path"]).exists()
    shim_error = tmp_path / job_shim.SHIM_ERROR_FILENAME
    record = json.loads(shim_error.read_text(encoding="utf-8"))
    assert record["instance"] == "demo"
    assert "HOME" in record["error"]
