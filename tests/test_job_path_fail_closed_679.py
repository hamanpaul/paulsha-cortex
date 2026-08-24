"""#679：job 的 `PATH` ——兩層都補，並禁止驗證指令自帶 `PATH`。

## 這一票在修什麼

實機上每一個經模板 unit 派出的 job，`PATH` 的來源是**沒有人做過決定的那一個**：

- 六份模板 unit 沒有一份有 `Environment=PATH=`；
- `job_runner.build_job_env()` 對 `PSC_*_PATH` **fail-open**（未宣告就不寫這個鍵）；
- 而 `PATH` 當時還在**轉發類**白名單上，因此未宣告時 job 靜默拿到 **Manager daemon
  的** `PATH`——那份值是否含 `<toolchain>/bin` 完全看該機器的 EnvironmentFile 被誰
  手動加過什麼；Manager 自己也沒有 `PATH` 時，spec 連 `PATH` 這個鍵都沒有，
  `os.execvpe` 於是退回 `os.defpath`（`:/bin:/usr/bin`）。

三條路的終點都一樣：`claude`／`agy` rc=127，而 `codex` **靜默**解到 `/usr/bin/codex`
（實機 0.42.0，toolchain 那份 0.147.0）。**不報錯，只是產出來自一支 operator 從未
判讀過的 CLI。**

## 為什麼它活過五輪驗證（本檔第 4 節釘住的就是這件事）

runbook 4e／5-2b、#661 與 #664 的量測、事故當天的每一次探針——**全部自帶
`--setenv=PATH=`**。驗證環境供應了 production 不供應的東西，於是缺陷在結構上不可能
被觀察到。runbook 4e 甚至逐字預言了症狀、連 0.42.0 這個版本號都寫對了，但那一條是
`sudo -u … env PATH=…` 跑的，所以它驗的是「toolchain 裡那份是對的版本」，
**不是「job 實際會解到哪一份」**。

這是「綠燈不承載語意」的第五個實例，且是新的一類：前四次（#638／#657／#673 兩次）
是「複本比 production 弱或強」，這次是「複本比 production **多**」。#677 立下
「加固面複本必須全量機械導出」，本票把它再推一格：**複本必須連「production 沒有設
什麼」也一起複製**。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from _home_paths import BUILDER_HOME, GATE_HOME, REVIEWER_HOME, fake_account_ids

from paulsha_cortex.coordinator import job_runner, job_shim
from paulsha_cortex.coordinator.job_runner import JobRunnerError
from paulsha_cortex.trust_root import permgen
from paulsha_cortex.trust_root.permgen import (
    DEFAULT_LAYOUT,
    EXECUTOR_TOOLS,
    FOUR_WAY_SCHEME,
    HARDENING_PROFILES,
    Principal,
    build_job_unit,
    downgraded_job_principals,
)

#: 三個角色 × 它們的 `PSC_*_PATH` 變數名（由產生器那張表導出，不是第二份手寫清單）。
_ROLE_PATH_ENV = {
    job_runner.JOB_ROLE_BUILDER: job_runner.BUILDER_PATH_ENV,
    job_runner.JOB_ROLE_REVIEW: job_runner.REVIEWER_PATH_ENV,
    job_runner.JOB_ROLE_GATE: job_runner.GATE_PATH_ENV,
}

_ROLE_HOME = {
    job_runner.JOB_ROLE_BUILDER: BUILDER_HOME,
    job_runner.JOB_ROLE_REVIEW: REVIEWER_HOME,
    job_runner.JOB_ROLE_GATE: GATE_HOME,
}


def _manager_env(**overrides: str) -> dict[str, str]:
    """一份「Manager daemon 有自己的 PATH」的環境——本票的關鍵前提。"""

    env = {
        "PATH": "/opt/cortex/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        job_runner.BUILDER_HOME_ENV: _ROLE_HOME[job_runner.JOB_ROLE_BUILDER],
        job_runner.REVIEWER_HOME_ENV: _ROLE_HOME[job_runner.JOB_ROLE_REVIEW],
        job_runner.GATE_HOME_ENV: _ROLE_HOME[job_runner.JOB_ROLE_GATE],
    }
    env.update(overrides)
    return env


def _build_job_env(*, role: str, manager_env: dict[str, str]) -> dict[str, str]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(job_runner, "_account_ids", fake_account_ids)
        return job_runner.build_job_env(
            manager_env=manager_env,
            job_id="j",
            slice_id="s",
            repo_root="/r",
            workspace=None,
            role=role,
        )


# ---------------------------------------------------------------------------
# 1. `build_job_env()` fail-closed（#679 修法 1，裁決 (a)）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", sorted(_ROLE_PATH_ENV))
def test_build_job_env_fails_closed_when_the_role_path_is_undeclared(role: str) -> None:
    """三個角色各自缺席時都必須 raise，**不得**靜默省略 PATH。"""

    with pytest.raises(JobRunnerError) as excinfo:
        _build_job_env(role=role, manager_env=_manager_env())
    diagnostic = excinfo.value.diagnostic
    assert diagnostic.reason == "job-runner-path-undeclared"
    # 訊息必須帶得出「哪個變數」與「去哪裡取正規值」——operator 讀到它就該能修。
    assert _ROLE_PATH_ENV[role] in str(excinfo.value)
    assert "trust_root unit" in str(excinfo.value)


@pytest.mark.parametrize("role", sorted(_ROLE_PATH_ENV))
def test_blank_and_whitespace_only_declarations_are_also_refused(role: str) -> None:
    """`PSC_*_PATH=` 空值＝沒宣告。空字串若被當成「宣告了」，job 的 PATH 就是空的。"""

    for blank in ("", "   ", "\t"):
        with pytest.raises(JobRunnerError) as excinfo:
            job_runner.resolve_job_path(
                _manager_env(**{_ROLE_PATH_ENV[role]: blank}), role=role
            )
        assert excinfo.value.diagnostic.reason == "job-runner-path-undeclared"


@pytest.mark.parametrize("role", sorted(_ROLE_PATH_ENV))
def test_the_job_path_never_falls_back_to_the_daemon_path(role: str) -> None:
    """**本票真正的 fail-open**：`PATH` 曾經在轉發類白名單上。

    未宣告 `PSC_*_PATH` 時 job 會靜默拿到 daemon 的 `PATH`——它帶著
    `<deploy_root>/venv/bin`（等於把 job 的 `python3` 綁回 Manager 的 venv），
    而且是否含 `<toolchain>/bin` 純看那台機器被誰手動加過什麼。
    """

    declared = "/opt/cortex/toolchain/bin:/usr/bin:/bin"
    env = _build_job_env(
        role=role,
        manager_env=_manager_env(**{_ROLE_PATH_ENV[role]: declared}),
    )
    assert env["PATH"] == declared
    assert env["PATH"] != _manager_env()["PATH"]


def test_path_is_synthesized_not_forwarded() -> None:
    """契約層的同一件事：`PATH` 不在轉發白名單上，且排除表講得出理由。"""

    forwarded = {item.name for item in job_runner.BUILDER_FORWARDED_ENV}
    assert "PATH" not in forwarded, "daemon 的 PATH 不得轉發"
    assert "PATH" in job_runner.BUILDER_SYNTHESIZED_ENV
    assert "PATH" in job_runner.EXCLUDED_ENV_RATIONALE
    assert job_runner.EXCLUDED_ENV_RATIONALE["PATH"].strip()


def test_roles_do_not_contaminate_each_other() -> None:
    """一個角色宣告了、另一個沒有 ⇒ 沒宣告的那個必須失敗，不得借用鄰居的值。"""

    env = _manager_env(**{job_runner.BUILDER_PATH_ENV: "/opt/cortex/toolchain/bin"})
    assert job_runner.resolve_job_path(env, role=job_runner.JOB_ROLE_BUILDER)
    for role in (job_runner.JOB_ROLE_REVIEW, job_runner.JOB_ROLE_GATE):
        with pytest.raises(JobRunnerError):
            job_runner.resolve_job_path(env, role=role)


def test_error_message_points_at_the_right_generator_flag() -> None:
    """錯誤訊息裡的 `trust_root unit <旗標>` 必須與 permgen 那張表一致。

    寫死 `--job` 會讓 operator 照抄之後拿到 builder 的 unit 覆蓋掉 reviewer／gate 的
    那一份——與 `permgen.JOB_UNIT_CLI_FLAG` 立下的理由逐字相同。
    """

    principal_of = {
        job_runner.JOB_ROLE_BUILDER: Principal.BUILDER,
        job_runner.JOB_ROLE_REVIEW: Principal.REVIEWER,
        job_runner.JOB_ROLE_GATE: Principal.GATE,
    }
    for role, principal in principal_of.items():
        with pytest.raises(JobRunnerError) as excinfo:
            job_runner.resolve_job_path(_manager_env(), role=role)
        assert permgen.JOB_UNIT_CLI_FLAG[principal] in str(excinfo.value), role


# ---------------------------------------------------------------------------
# 2. 模板 unit 的 `Environment=PATH=`（#679 修法 2：**兩層都補**）
# ---------------------------------------------------------------------------

_ALL_JOB_UNITS = [
    (principal, profile)
    for principal in downgraded_job_principals(FOUR_WAY_SCHEME)
    for profile in HARDENING_PROFILES
]


def test_the_matrix_is_the_six_units_the_issue_counted() -> None:
    """#679 的證據是「六份 unit 沒有一份有 PATH」——那個 6 必須是機械導出的。"""

    assert len(_ALL_JOB_UNITS) == 6


@pytest.mark.parametrize(
    ("principal", "profile"),
    _ALL_JOB_UNITS,
    ids=lambda item: getattr(item, "value", getattr(item, "profile_id", str(item))),
)
def test_every_job_unit_declares_path(principal, profile) -> None:
    unit = build_job_unit(FOUR_WAY_SCHEME, DEFAULT_LAYOUT, principal, profile=profile)
    expected = f"Environment=PATH={DEFAULT_LAYOUT.job_path_value()}"
    assert f"\n{expected}\n" in unit.content, unit.unit_name


@pytest.mark.parametrize(
    ("principal", "profile"),
    _ALL_JOB_UNITS,
    ids=lambda item: getattr(item, "value", getattr(item, "profile_id", str(item))),
)
def test_unit_path_value_is_exported_not_a_literal(principal, profile) -> None:
    """值必須由 `PathLayout` 導出：換一個部署根，unit 的 PATH 必須跟著換。"""

    from paulsha_cortex.trust_root.permgen import PathLayout

    layout = PathLayout(instance="acme", deploy_root="/srv/acme")
    unit = build_job_unit(FOUR_WAY_SCHEME, layout, principal, profile=profile)
    emitted = unit.content.split("\nEnvironment=PATH=", 1)[1].split("\n", 1)[0]
    assert emitted == layout.job_path_value()
    assert emitted.startswith("/srv/acme/toolchain/bin:")


def test_unit_path_and_manager_variable_are_the_same_value() -> None:
    """兩層必須同源。不同源就等於「兩份真相」，而漂移一定往少的那邊倒。"""

    for principal in downgraded_job_principals(FOUR_WAY_SCHEME):
        unit = build_job_unit(FOUR_WAY_SCHEME, DEFAULT_LAYOUT, principal)
        emitted = unit.content.split("\nEnvironment=PATH=", 1)[1].split("\n", 1)[0]
        variable = permgen.JOB_PATH_ENV_BY_PRINCIPAL[principal]
        assert f"{variable}={emitted}" in unit.content


def test_toolchain_comes_first_in_the_unit_path() -> None:
    """排在後面就會被系統層那份同名舊 CLI 蓋掉——症狀是「跑得起來但版本不對」。"""

    value = DEFAULT_LAYOUT.job_path_value()
    assert value.split(":")[0] == DEFAULT_LAYOUT.toolchain_bin


# ---------------------------------------------------------------------------
# 3. shim 端的第二層 ＋ 兩層都缺時 fail-closed
# ---------------------------------------------------------------------------

def _spec(env: dict[str, str]) -> dict[str, object]:
    return {
        "spec_version": job_runner.JOB_SPEC_VERSION,
        "instance": "demo",
        "command": ["bash", "-c", "true"],
        "working_directory": "/var/lib/cortex/worktree/demo",
        "log_path": "/var/lib/cortex/worktree/demo/demo.log",
        "env": {"HOME": _ROLE_HOME[job_runner.JOB_ROLE_BUILDER], **env},
    }


def test_spec_path_wins_when_present() -> None:
    resolved = job_shim.resolve_job_env(
        _spec({"PATH": "/opt/cortex/toolchain/bin:/usr/bin"}),
        {"PATH": "/unit/layer"},
    )
    assert resolved["PATH"] == "/opt/cortex/toolchain/bin:/usr/bin"


def test_shim_falls_back_to_the_unit_layer() -> None:
    """spec 漏了時退回模板 unit 的 `Environment=PATH=`。

    這**不是** fail-open：退回的是 root-owned、可逐字稽核的來源，不是猜一個預設值。
    它涵蓋「手工組 spec 繞過產生器」（#645 的同型前例）與「spool 裡還躺著升級前寫的
    舊 spec」兩種現實情況。
    """

    resolved = job_shim.resolve_job_env(
        _spec({"HOME": "/var/lib/cortex-builder"}),
        {"PATH": "/opt/cortex/toolchain/bin:/usr/bin:/bin"},
    )
    assert resolved["PATH"] == "/opt/cortex/toolchain/bin:/usr/bin:/bin"
    assert resolved["HOME"] == "/var/lib/cortex-builder"


@pytest.mark.parametrize("environ", [{}, {"PATH": ""}, {"PATH": "   "}])
def test_shim_refuses_to_exec_when_both_layers_are_missing(environ) -> None:
    """兩層都缺時**絕不** exec。

    退回 `os.defpath` 正是本票的原症狀，而它的失敗模式是「不報錯、只是版本不對」。
    """

    with pytest.raises(job_shim.ShimError) as excinfo:
        job_shim.resolve_job_env(_spec({"HOME": "/var/lib/cortex-builder"}), environ)
    assert "PATH" in str(excinfo.value)


def test_shim_main_reports_the_missing_path_before_taking_over_the_log() -> None:
    """理由要進 journal，而不是進一份 job 自己的 log。

    「job 跑了但沒輸出」與「job 從未起跑」在 Manager 眼裡長得一樣；接管 log 之前
    失敗才分辨得出來（與 spool 根 fail-closed 同一段，見 `job_shim` 模組 docstring）。
    """

    with tempfile.TemporaryDirectory() as root:
        spool = Path(root) / "job-specs"
        spool.mkdir()
        log_path = Path(root) / "demo.log"
        spec = _spec({"HOME": "/var/lib/cortex-builder"})
        spec["log_path"] = str(log_path)
        spec["working_directory"] = root
        (spool / "demo.json").write_text(json.dumps(spec), encoding="utf-8")
        rc = job_shim.main(
            ["demo"], {job_runner.JOB_SPEC_SPOOL_ENV: str(spool)}
        )
    assert rc == job_shim.EXIT_SPEC_ERROR
    # 接管之前就失敗 ⇒ 那份 log 根本沒被建立。
    assert not log_path.exists()


# ---------------------------------------------------------------------------
# 4. 驗證方法本身：複本不得注入 PATH（#679 修法 3）
# ---------------------------------------------------------------------------

def test_the_replica_carries_path_and_it_comes_from_the_unit() -> None:
    """複本的 PATH 必須**逐字**來自 unit——不是探針補的，也不是產生器另算的。"""

    unit = build_job_unit(FOUR_WAY_SCHEME, DEFAULT_LAYOUT)
    props = permgen.unit_replica_properties(unit.content, instance="probe")
    expected = f"--property=Environment=PATH={DEFAULT_LAYOUT.job_path_value()}"
    assert expected in props
    assert sum(1 for p in props if p.startswith("--property=Environment=PATH=")) == 1


def test_a_unit_without_path_produces_a_replica_without_path() -> None:
    """反向：unit 沒設，複本就必須沒有。

    這條是「複本連 production **沒有**設什麼也一起複製」的直接斷言——#679 的整個
    要害就在這裡。複本若自己補一個 PATH，「job 沒有 PATH」就永遠量不到。
    """

    unit = build_job_unit(FOUR_WAY_SCHEME, DEFAULT_LAYOUT)
    stripped = "\n".join(
        line for line in unit.content.splitlines()
        if not line.startswith("Environment=PATH=")
    )
    props = permgen.unit_replica_properties(stripped, instance="probe")
    assert not [p for p in props if "PATH=" in p]


@pytest.mark.parametrize("scheme_id", sorted(permgen.SCHEMES))
def test_the_probe_never_injects_environment(scheme_id: str) -> None:
    """探針產生器的**可執行行**不得出現 `--setenv=` 或任何 `PATH=`。

    註解行不在判準內：這一節的註解必須講得出「為什麼不能加 `--setenv=PATH=`」，
    而講這句話就得寫出那個字串。判準是「探針**做**了什麼」，不是「探針**提**到什麼」。
    """

    lines = permgen.build_path_resolution_probe(permgen.SCHEMES[scheme_id])
    assert permgen.path_probe_env_injections(lines) == ()


def test_the_probe_explains_why_it_must_not_inject() -> None:
    """理由必須留在產物本身——讀它的人正是會忍不住「補一個 PATH 讓它過」的人。"""

    text = "\n".join(permgen.build_path_resolution_probe(FOUR_WAY_SCHEME))
    assert "--setenv=" in text, "產物要講得出禁止的是什麼"
    assert "unit_replica_properties" in text, "加固面必須走共用的全量複本"


def test_the_probe_reuses_the_shared_helper_instead_of_redefining_it() -> None:
    """加固面的定義只有一份——連呼叫它的那幾行 shell 也不該有第二份複本。

    #638／#657／#673／#679 是同一族事故：**兩份複本一定會漂移，而方向不由人選**
    （偏寬得假綠、偏嚴得假紅，後者更貴）。因此本產生器**呼叫** runbook 第 4e 步的
    `psc_run_under`，並在它未定義時 fail-closed——而不是自己再定義一份長得一樣的。
    """

    lines = permgen.build_path_resolution_probe(FOUR_WAY_SCHEME)
    text = "\n".join(lines)
    assert f"{permgen.PATH_PROBE_HELPER} " in text
    assert f"{permgen.PATH_PROBE_HELPER}() {{" not in text, "不得自帶第二份定義"
    assert f"declare -F {permgen.PATH_PROBE_HELPER}" in text, "未定義時必須 fail-closed"
    # 也不得繞過共用探針自己叫 systemd-run。
    assert not [
        line for line in lines
        if line.strip().startswith("sudo systemd-run") or line.strip().startswith("systemd-run")
    ]


def test_the_injection_detector_actually_detects() -> None:
    """守衛自己的 negative control：偵測器對真的注入必須是紅的。"""

    poisoned = ["# 說明行提到 --setenv=PATH= 不算", "systemd-run --setenv=PATH=/usr/bin true"]
    offenders = permgen.path_probe_env_injections(poisoned)
    assert len(offenders) == 1
    assert offenders[0].startswith("systemd-run")


# ---------------------------------------------------------------------------
# 5. 反向不變式的矩陣（#679 修法 4）
# ---------------------------------------------------------------------------

def test_the_matrix_is_every_role_times_every_executor() -> None:
    cases = permgen.path_resolution_cases(FOUR_WAY_SCHEME, DEFAULT_LAYOUT)
    principals = downgraded_job_principals(FOUR_WAY_SCHEME)
    assert len(cases) == len(principals) * len(EXECUTOR_TOOLS)
    assert {c.principal for c in cases} == set(principals)
    assert {c.executor for c in cases} == {t.name for t in EXECUTOR_TOOLS}


def test_every_case_expects_the_toolchain_copy() -> None:
    """斷言的對象是「解到 `<toolchain>/bin/<cli>`」，而版本比對的對象是同一支檔案。

    版本不另立一份手抄清單：登記表把 toolchain 落點登記成 `<toolchain>/bin/<cli>`，
    因此「PATH 解出來的那支」與「絕對路徑那支」印出同一個版本字串，就是「job 跑的
    是登記表登記的那一份」的直接證據。第二份清單只會變成下一個會漂移的真相。
    """

    for case in permgen.path_resolution_cases(FOUR_WAY_SCHEME, DEFAULT_LAYOUT):
        assert case.expected_binary == f"{DEFAULT_LAYOUT.toolchain_bin}/{case.executor}"
        assert case.version_reference == case.expected_binary


def test_each_case_points_at_the_unit_that_executor_actually_starts() -> None:
    """剖面跟著 executor 走（#643）：`codex` 的 unit 與 `claude` 的**不是同一份**。

    只驗其中一份等於沒驗另一份的 PATH——這正是「角色 × executor」必須是兩層列舉的
    理由，與 `job_unit_stems()` 同一條論證。
    """

    by_executor = {
        case.executor: case
        for case in permgen.path_resolution_cases(FOUR_WAY_SCHEME, DEFAULT_LAYOUT)
        if case.principal is Principal.BUILDER
    }
    assert by_executor["codex"].hardening_profile == "jit"
    assert by_executor["claude"].hardening_profile == "strict"
    assert by_executor["codex"].unit_stem != by_executor["claude"].unit_stem
    for case in by_executor.values():
        expected_stem = permgen.job_unit_stem(
            DEFAULT_LAYOUT,
            case.principal,
            permgen.executor_hardening_profile(case.executor),
        )
        assert case.unit_stem == expected_stem


def test_schemes_without_a_gate_account_drop_those_rows() -> None:
    """本方案沒有的角色機械略去，不留一列指向不存在帳號的假斷言。"""

    cases = permgen.path_resolution_cases(permgen.THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    assert Principal.GATE not in {c.principal for c in cases}


def test_the_cli_emits_the_probe() -> None:
    from paulsha_cortex.trust_root.__main__ import main

    assert main(["path-probe", "four-way"]) == 0


# ---------------------------------------------------------------------------
# 6. OS 層語意：在這個環境重現不了，**明確 skip**
# ---------------------------------------------------------------------------

@pytest.mark.skip(
    reason=(
        "#638／#657／#673 立下的規矩：涉及 OS 層語意、這個環境重現不了的，明確 skip "
        "並說明理由，不得靜默通過。待驗的命題是**本票的核心**——「以零額外 env 起一個 "
        "降權 job，`codex` 解到的是 /opt/cortex/toolchain/bin/codex，而不是 "
        "/usr/bin/codex」。要驗它需要三樣這個環境都沒有的東西：(1) 第二個 UID"
        "（cortex-builder／cortex-reviewer-planner／cortex-gate）；(2) 真的 systemd ＋ "
        "polkit 授權，才起得了模板實例；(3) 一棵真的 /opt/cortex/toolchain 樹，裡面那份 "
        "codex 與系統層那份是**不同版本**——而「兩份同名不同版」正是待驗現象本身，"
        "CI 上只有一份（或一份都沒有）。\n"
        "在這裡跑 `command -v codex` 只會證明「這台 CI 的 PATH 上有沒有 codex」，"
        "與待驗命題無關，卻會讓人以為驗過了——而『以為驗過了』正是本票的病因：五輪"
        "驗證全部自帶 PATH，每一輪都是綠的。\n"
        "**這一半改由三個地方守**：(1) 本檔第 1～3 節（spec 層 fail-closed、unit 層"
        "有值且同源、shim 兩層都缺即拒絕 exec）；(2) 本檔第 4 節（複本不得注入 PATH——"
        "驗證方法本身的不變式）；(3) runbook 第 4e-2 步的實機矩陣，由 "
        "`trust_root path-probe` 產生，operator 逐列比對版本字串。"
    )
)
def test_a_downgraded_job_resolves_the_toolchain_copy_with_zero_extra_env() -> None:  # pragma: no cover
    raise AssertionError("需要第二個 UID ＋ 真實 systemd 加固面 ＋ 兩份同名不同版的 CLI；見 skip 理由")
