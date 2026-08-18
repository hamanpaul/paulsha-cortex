"""issue #686（#672 票 E）：`JobPlanningInvoker` 的行為契約。

本檔釘的是「planning 的每一次模型呼叫都變成一個 `cortex-reviewer-job@` 實例」這件事
的**可在單 UID 環境驗證的那一半**：角色、剖面、spec 形狀、instance 命名、逾時處置、
三分族的對應。

**不能在這裡驗的那一半**（OS 層語意）一律以具名 `@pytest.mark.skip` 標出並指向
runbook，不得靜默通過——#638／#657 的教訓逐字如此：單 UID 環境讓斷言真空，斷言恆真、
什麼都沒驗到，而它看起來是綠的。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import job_runner, planning_job, planning_runtime
from paulsha_cortex.coordinator.model_identities import (
    ENVIRONMENT_GRADE_PLANNING_FAMILIES,
    PLANNING_FAILURE_EXECUTOR,
    PLANNING_FAILURE_EXECUTOR_SILENT_EXIT,
    PLANNING_FAILURE_JOB_START,
    PLANNING_FAILURE_OUTPUT,
    ModelIdentity,
    classify_probe_failure,
)
from paulsha_cortex.trust_root import permgen, registry
from paulsha_cortex.trust_root.registry import Principal


IDENTITY_CODEX = ModelIdentity(
    executor="codex",
    model_id="gpt-5.3-codex-spark",
    independence_domain="openai",
    capabilities=("planning",),
)
IDENTITY_AGY = ModelIdentity(
    executor="agy",
    model_id="gemini-3.1-pro-high",
    independence_domain="google",
    capabilities=("planning",),
)
IDENTITY_UNREGISTERED = ModelIdentity(
    executor="cg",
    model_id="glm-5.2",
    independence_domain="zhipu",
    capabilities=("planning",),
)


class _FakeProcess:
    """`systemctl start --wait` client 的替身。

    `wait()` 的兩種行為對應真實世界的兩種：正常結束、以及**掛住**（由 `hang=True`
    模擬，`wait()` 直接拋 `TimeoutExpired`——那正是 `Popen.wait` 的真實語意）。
    """

    def __init__(self, *, returncode: int = 0, hang: bool = False) -> None:
        # 起動確認窗內 client **還活著**——那正是真實世界的形狀：`--wait` 的 client
        # 只有在 unit 跑完之後才返回，而 unit 啟動（systemd 排程 → 降權 → shim 讀
        # spec → exec 模型 CLI）不可能在預設 200ms 的窗內走完。讓替身在窗內就回一個
        # 非零狀態，等於把「executor 失敗」偽裝成「unit 起不來」，那會讓本檔的三分
        # 斷言全部驗錯對象。
        self.returncode = None
        self._hang = hang
        self._final = returncode
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self._hang:
            raise subprocess.TimeoutExpired(cmd="systemctl", timeout=timeout or 0)
        self.returncode = self._final
        return self.returncode

    def kill(self):  # pragma: no cover - 介面完整性
        self.killed = True


class _Harness:
    """把一次派工的每一個副作用都攔下來，並記錄下來供斷言。

    刻意**只**替換三個 seam：`prepare_systemd_template` 的 preflight（單 UID 環境沒有
    systemd／三個帳號）、`Popen`、以及 `systemctl` 的兩個查詢／控制動作。剖面決定、
    instance 推導、spec 組裝與寫入、env 白名單全部走**真的那一份**——那些正是本票要
    釘的東西，換掉就等於什麼都沒驗（#638 的形態）。
    """

    def __init__(self, tmp_path: Path, monkeypatch, *, log_payload: str = "{}", rc: int = 0,
                 hang: bool = False) -> None:
        self.tmp_path = tmp_path
        self.spool = tmp_path / "job-specs" / "reviewer"
        self.spool.mkdir(parents=True)
        self.log_payload = log_payload
        self.rc = rc
        self.hang = hang
        self.specs: list[dict] = []
        self.started: list[list[str]] = []
        self.stopped: list[str] = []
        self.active: set[str] = set()
        self.env = {
            job_runner.JOB_RUNNER_ENV: job_runner.RUNNER_SYSTEMD_TEMPLATE,
            "PSC_REVIEWER_PATH": "/opt/cortex/toolchain/bin:/usr/bin:/bin",
            "PSC_REVIEWER_HOME": "/var/lib/cortex-reviewer-planner",
        }
        real_prepare = job_runner.prepare_systemd_template

        def fake_prepare(env, *, job_id, executor, role=job_runner.JOB_ROLE_BUILDER,
                         unit_active=None):
            # 角色／剖面／instance 三條走真的推導；只跳過需要 systemd 與真實帳號的
            # preflight。剖面未登記時**照樣 fail-closed**（那正是 R4 要釘的）。
            config = job_runner.resolve_job_role(role)
            profile = job_runner.resolve_hardening_profile(executor)
            base = job_runner.resolve_template_unit(env, role=role)
            template = job_runner.template_unit_for_profile(base, profile)
            instance = job_runner.template_instance_id(job_id)
            unit = job_runner.template_unit_name(instance, template=template)
            if unit in self.active:
                raise job_runner._fail(
                    "job-runner-template-instance-busy",
                    f"模板實例 {unit} 已在執行中",
                    source="prepare_systemd_template",
                    unit=unit,
                )
            return job_runner.SystemdTemplatePlan(
                binary="/usr/bin/systemctl",
                template_unit=template,
                instance=instance,
                unit=unit,
                account=config.default_account,
                group=config.default_account,
                shim="/opt/cortex/bin/cortex-job-shim",
                spool_dir=str(self.spool),
                spec_path=job_runner.job_spec_path(str(self.spool), instance),
                hardening_profile=profile,
                executor=str(executor or ""),
                base_template_unit=base,
                role=config.role_id,
            )

        monkeypatch.setattr(job_runner, "prepare_systemd_template", fake_prepare)
        self._real_prepare = real_prepare

    def popen(self, argv, *, cwd=None, env=None, stdin=None, stdout=None, stderr=None):
        self.started.append(list(argv))
        # 真實世界裡這一段由 job（寫 log）與 Manager 側記帳 shell（寫 sentinel）分別
        # 完成；替身把兩者一起做掉，形狀與落點逐字相同。
        script = argv[-1]
        sentinel = script.split("> ")[-1].split(";")[0].strip().strip("'")
        if not self.hang:
            spec = self.specs[-1]
            Path(spec["log_path"]).write_text(self.log_payload, encoding="utf-8")
            Path(sentinel).write_text(str(self.rc), encoding="utf-8")
        return _FakeProcess(returncode=self.rc, hang=self.hang)

    def unit_active(self, systemctl: str, unit: str) -> bool:
        return unit in self.active

    def stop_unit(self, systemctl: str, unit: str) -> None:
        self.stopped.append(unit)
        self.active.discard(unit)

    def invoker(self, monkeypatch) -> planning_job.JobPlanningInvoker:
        real_write = job_runner.write_job_spec

        def recording_write(spec_path, spec, *, account=None):
            self.specs.append(dict(spec))
            # `account` 一律不傳給真的那一支：spec 落地後的「那個身分讀得到嗎」是
            # 跨 UID 語意，單 UID 環境驗不出來（見本檔最後的具名 skip）。
            return real_write(spec_path, spec)

        monkeypatch.setattr(planning_job.job_runner, "write_job_spec", recording_write)
        return planning_job.JobPlanningInvoker(
            env=self.env,
            scratch_root=self.tmp_path / "planning-scratch",
            log_spool_root=self.tmp_path / "planning-logs",
            popen=self.popen,
            unit_active=self.unit_active,
            stop_unit=self.stop_unit,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
        )


def _invocation(identity=IDENTITY_AGY, *, purpose="questioner", timeout=120, worktree=None):
    return planning_runtime.PlanningInvocation(
        identity=identity,
        prompt="Return only this JSON object: {}",
        purpose=purpose,
        timeout_seconds=timeout,
        worktree=Path(worktree or "/var/lib/cortex/repos/paulsha-cortex"),
        run_id="workflow-686abcdef012",
    )


# ---------------------------------------------------------------------------
# R1／R4：身分與剖面
# ---------------------------------------------------------------------------

def test_role_is_review_and_not_derivable_from_spec(tmp_path, monkeypatch) -> None:
    """角色恆為 `JOB_ROLE_REVIEW`，且 spec 不含任何身分／剖面欄位。

    「User 不在 spec 裡」是 B 案全部的價值：身分只有一個來源＝root-owned unit 檔的
    `User=`。`SPEC_FORBIDDEN_KEYS` 在寫端與讀端各擋一次，這裡釘的是**寫端真的沒放**。
    """

    harness = _Harness(tmp_path, monkeypatch, log_payload='{"ok":true}')
    invoker = harness.invoker(monkeypatch)
    invoker.run(_invocation())

    assert len(harness.specs) == 1
    spec = harness.specs[0]
    assert spec["unit"].startswith("cortex-reviewer-job")
    assert job_runner.forbidden_spec_keys(spec) == []
    for forbidden in job_runner.SPEC_FORBIDDEN_KEYS:
        assert forbidden not in spec
    # 角色由呼叫端在建構期固定，不從 prompt／spec／模型輸出導出。
    assert job_runner.JOB_ROLE_REVIEW == "review"
    assert spec["env"]["PSC_JOB_ID"] == spec["job_id"]


def test_profile_comes_only_from_executor(tmp_path, monkeypatch) -> None:
    """剖面唯一輸入是 `identity.executor`；未登記 executor fail-closed。

    `cg` 刻意不在 `EXECUTOR_HARDENING_PROFILE` 內——猜嚴格會讓它靜默起不來，猜寬鬆
    會讓 per-executor 剖面退化成「全域移除 MDWE」。正確行為是**拒絕**。
    """

    harness = _Harness(tmp_path, monkeypatch, log_payload='{"ok":true}')
    invoker = harness.invoker(monkeypatch)

    invoker.run(_invocation(IDENTITY_CODEX))
    assert harness.specs[-1]["unit"].startswith("cortex-reviewer-job-jit@")

    invoker.run(_invocation(IDENTITY_AGY))
    assert harness.specs[-1]["unit"].startswith("cortex-reviewer-job@")

    # `cg` 走不到 `_planning_argv`（那一層先拒），因此改用票 B 的 `ProcessRunner`
    # 接縫直接餵一條 `cg` argv——剖面決定就在那條路上。
    with pytest.raises(planning_job.PlanningJobError) as excinfo:
        invoker.capability_probe_runner()(["cg", "--version"], timeout=10)
    assert excinfo.value.family == PLANNING_FAILURE_JOB_START
    assert "hardening-profile-unknown" in str(excinfo.value)


# ---------------------------------------------------------------------------
# D3：wrapper 只有模型 argv 一段
# ---------------------------------------------------------------------------

def test_planning_wrapper_has_no_gate_bundle_verdict_sentinel(tmp_path, monkeypatch) -> None:
    """spec 的 `command` **就是**模型 argv，不是任何一種 wrapper script。

    不靠「既有旗標碰巧為 None」：這裡釘的是 command[0] 是 executor 本身、整條 argv 裡
    沒有 shell、沒有 gate、沒有 bundle、沒有 verdict、沒有 sentinel。理由不只是潔癖
    ——wrapper 自產的任何一個位元組都會進同一份 log，而那份 log 就是 `_extract_json`
    的輸入。
    """

    harness = _Harness(tmp_path, monkeypatch, log_payload='{"ok":true}')
    invoker = harness.invoker(monkeypatch)
    invoker.run(_invocation(IDENTITY_CODEX))

    command = harness.specs[-1]["command"]
    assert command[0] == "codex"
    joined = " ".join(command)
    for banned in ("bash", "sh -c", "git bundle", "verdict", ".exit", "gate"):
        assert banned not in joined
    # working_directory 是 scratch，不是 operator 樹。
    assert harness.specs[-1]["working_directory"].startswith(
        str(tmp_path / "planning-scratch")
    )
    assert "/repos/" not in harness.specs[-1]["working_directory"]


def test_instance_name_unique_per_invocation(tmp_path, monkeypatch) -> None:
    """四種 purpose ＋ 重試的 instance 名互不相同，且全部過 `JOB_SEGMENT_RE`。

    一輪 brainstorm 會連續起 questioner → secondary → integrator，加上 probe；
    `systemctl start` 對已 active 的 unit 會**靜默回 0**，因此撞名的後果是「以為起了
    一個 job，實際掛在別人的 unit 上等」。
    """

    harness = _Harness(tmp_path, monkeypatch, log_payload='{"ok":true}')
    invoker = harness.invoker(monkeypatch)
    for purpose in (
        planning_runtime.PLANNING_PURPOSE_PROBE,
        planning_runtime.PLANNING_PURPOSE_QUESTIONER,
        planning_runtime.PLANNING_PURPOSE_SECONDARY,
        planning_runtime.PLANNING_PURPOSE_INTEGRATOR,
        planning_runtime.PLANNING_PURPOSE_QUESTIONER,  # 重試
    ):
        invoker.run(_invocation(purpose=purpose))

    instances = [spec["instance"] for spec in harness.specs]
    assert len(set(instances)) == len(instances) == 5
    for instance in instances:
        assert job_runner.instance_name_valid(instance)
    # purpose 進 instance 名（D9）：`systemctl list-units` 因此說得出這批 job 在做什麼。
    assert any("questioner" in instance for instance in instances)
    assert any("integrator" in instance for instance in instances)


# ---------------------------------------------------------------------------
# R7／D4：逾時
# ---------------------------------------------------------------------------

def test_timeout_stops_unit_and_classifies_as_timeout(tmp_path, monkeypatch) -> None:
    """逾時 ⇒ 發 `systemctl stop` ⇒ 確認離開 active ⇒ 落 environment 級失敗。

    **不能只是放棄等待**：那會讓下一次同名 instance 撞
    `job-runner-template-instance-busy`，而那個症狀與逾時完全無關，會把排查帶偏。
    """

    harness = _Harness(tmp_path, monkeypatch, hang=True)
    invoker = harness.invoker(monkeypatch)
    unit_holder: list[str] = []

    def stop_unit(systemctl: str, unit: str) -> None:
        unit_holder.append(unit)
        harness.active.discard(unit)

    invoker._stop_unit = stop_unit
    # unit 起來了（active），模型掛住。
    original_popen = harness.popen

    def popen(argv, **kwargs):
        process = original_popen(argv, **kwargs)
        harness.active.add(harness.specs[-1]["unit"])
        Path(
            harness.specs[-1]["log_path"]
        ).write_text("", encoding="utf-8")
        sentinel = argv[-1].split("> ")[-1].split(";")[0].strip().strip("'")
        Path(sentinel).write_text("0", encoding="utf-8")
        return process

    invoker._popen = popen

    with pytest.raises(planning_job.PlanningJobError) as excinfo:
        invoker.run(_invocation(timeout=5))

    failure = excinfo.value
    assert failure.family == PLANNING_FAILURE_EXECUTOR
    assert planning_job.PLANNING_JOB_TIMEOUT in failure.detail
    assert unit_holder and unit_holder[0].startswith("cortex-reviewer-job@")
    assert failure.diagnostics["unit_left_active"] == "no"
    # environment 級 ⇒ `_resume_decision` 浮得出 `recover-planning`（D8 的改判）。
    assert failure.family in ENVIRONMENT_GRADE_PLANNING_FAMILIES


# ---------------------------------------------------------------------------
# R6：三分族
# ---------------------------------------------------------------------------

def test_job_start_failure_maps_to_job_start_failed(tmp_path, monkeypatch) -> None:
    """`JobRunnerError` 各族 ⇒ `planning-job-start-failed`（environment）。"""

    harness = _Harness(tmp_path, monkeypatch, log_payload='{"ok":true}')
    invoker = harness.invoker(monkeypatch)

    def refuse(*_args, **_kwargs):
        raise job_runner._fail(
            "job-runner-job-template-missing",
            "模板 unit 未安裝",
            source="preflight_systemd_template",
        )

    monkeypatch.setattr(job_runner, "prepare_systemd_template", refuse)
    with pytest.raises(planning_job.PlanningJobError) as excinfo:
        invoker.run(_invocation())
    assert excinfo.value.family == PLANNING_FAILURE_JOB_START
    assert PLANNING_FAILURE_JOB_START in ENVIRONMENT_GRADE_PLANNING_FAMILIES
    # 族名活著抵達拒因表：`classify_probe_failure` 原樣採用（票 A 已預留這條路）。
    assert classify_probe_failure(
        excinfo.value.family, excinfo.value.detail
    ) == PLANNING_FAILURE_JOB_START


def test_silent_rc1_maps_to_executor_silent_exit(tmp_path, monkeypatch) -> None:
    """rc≠0 且無任何輸出 ⇒ 具名 `executor-silent-exit`，並指名四個線索來源。

    這一類是整個家族裡最難查的一種（**連錯誤訊息都沒有**），因此診斷 MUST 指名
    unit、加固剖面、實際解析到的可執行檔，並帶 `seccomp_filter_is_fatal()` 的機械
    答案——#673 整張票走偏，正是因為當時沒有任何地方回答得了最後那一個。
    """

    harness = _Harness(tmp_path, monkeypatch, log_payload="", rc=1)
    invoker = harness.invoker(monkeypatch)
    with pytest.raises(planning_job.PlanningJobError) as excinfo:
        invoker.run(_invocation(IDENTITY_CODEX))

    failure = excinfo.value
    assert failure.family == PLANNING_FAILURE_EXECUTOR
    assert PLANNING_FAILURE_EXECUTOR_SILENT_EXIT in failure.detail
    assert "cortex-reviewer-job-jit@" in failure.detail
    assert "profile=jit" in failure.detail
    assert "binary=" in failure.detail
    assert "version=" in failure.detail
    # `SystemCallErrorNumber=EPERM` 在時，答案是「不該懷疑 seccomp」。
    assert failure.diagnostics["seccomp_filter_fatal"] == "no"
    assert "seccomp_filter_fatal=no" in failure.detail


def test_malformed_output_maps_to_output_malformed(tmp_path, monkeypatch) -> None:
    """rc=0 但輸出非 JSON ⇒ `planning-output-malformed`（content），detail 帶 stdout 前綴。

    分級的方向性在這裡也一併釘住：格式問題**不得**被改判成環境問題（反向誤報同樣
    不可接受）。
    """

    harness = _Harness(tmp_path, monkeypatch, log_payload="I am not JSON at all.")
    invoker = harness.invoker(monkeypatch)
    outcome = invoker.run(_invocation())
    assert outcome.returncode == 0

    with pytest.raises(ValueError) as excinfo:
        planning_runtime._extract_json_candidates(outcome.stdout, outcome.output_text)
    assert "I am not JSON" in str(excinfo.value)
    assert classify_probe_failure("safe-probe-failed", "ValueError") == PLANNING_FAILURE_OUTPUT
    assert PLANNING_FAILURE_OUTPUT not in ENVIRONMENT_GRADE_PLANNING_FAMILIES


def test_second_output_candidate_is_none_in_job_mode(tmp_path, monkeypatch) -> None:
    """D-j／R-2：codex 的 `-o last.json` 落在 job 的 `PrivateTmp`，Manager 讀不到。

    退步是**已知且已標註**的，不是被吞掉的：`output_text` 恆為 `None`，
    `_extract_json` 因此從雙候選退成單候選。argv 仍帶 `-o`——那一份對 job 自己有效
    （codex 少了它會改變行為），只是第二候選這條路對 Manager 消失了。
    """

    harness = _Harness(tmp_path, monkeypatch, log_payload='{"ok":true}')
    invoker = harness.invoker(monkeypatch)
    outcome = invoker.run(_invocation(IDENTITY_CODEX))
    assert outcome.output_text is None
    command = harness.specs[-1]["command"]
    assert "-o" in command
    assert command[command.index("-o") + 1].startswith("/tmp/")


# ---------------------------------------------------------------------------
# R2／U-2：可寫面由登記表機械導出
# ---------------------------------------------------------------------------

def test_operator_tree_not_in_job_rwp() -> None:
    """D-e：`repo-source-tree` 不在 reviewer 模板 unit 的 RWP 產出中。

    「planner 經絕對路徑寫 operator 樹」這條路在降權模式下由 `ProtectSystem=strict`
    直接關掉——本測試讓那條保證**有測試釘住**，而不是只靠 unit 檔的註解。
    """

    unit = permgen.build_job_unit(permgen.THREE_WAY_SCHEME, principal=Principal.REVIEWER)
    layout = permgen.PathLayout()
    assert layout.repo_source_root not in unit.read_write_paths
    for path in unit.read_write_paths:
        assert "/repos/" not in path


def test_scratch_pool_is_read_only_for_every_job_unit() -> None:
    """U-2 的機械形式：scratch pool 的 writer 面只有 Manager ⇒ 不進任何 job unit 的 RWP。

    這一條就是「模型弄髒自己的拋棄式 sandbox」從**偵測**變成**結構上不可能**的地方。
    要打破它必須改登記表 writer 面，而那會在產生器輸出與 unit 檔上留下痕跡。
    """

    asset = next(
        item for item in registry.ASSET_REGISTRY if item.asset_id == "planning-scratch-pool"
    )
    assert asset.writers == (Principal.MANAGER,)
    assert Principal.PLANNER in asset.readers

    layout = permgen.PathLayout()
    for principal in registry.DOWNGRADED_JOB_PRINCIPALS:
        unit = permgen.build_job_unit(permgen.FOUR_WAY_SCHEME, principal=principal)
        assert all(
            not path.startswith(layout.planning_scratch_root)
            for path in unit.read_write_paths
        ), f"{principal} 的 unit 不得對 planning scratch 可寫"


def test_planning_log_spool_needs_no_new_write_surface() -> None:
    """log 通道掛在既有 verdict spool 底下 ⇒ 模板 unit 的 RWP **逐字不變**。

    design D3 第一句是「不新開通道」，U-3 更把「新開一條 job→Manager 的寫入面」列為
    未決。這條斷言讓「本票沒有偷偷開一條」變成可檢查的事實。
    """

    layout = permgen.PathLayout()
    assert layout.planning_job_log_spool_root.startswith(
        layout.review_verdict_spool_root + "/"
    )
    unit = permgen.build_job_unit(permgen.THREE_WAY_SCHEME, principal=Principal.REVIEWER)
    assert sorted(unit.read_write_paths) == [
        # #698：codex 的狀態樹從 `cache/codex` 搬成 HOME 底下的 root-owned sticky 真
        # 目錄，因此多這一條。**這不是本票（票 D／planning log）開的通道**——同一棵樹
        # 換位置，換到的是「目錄由 root 擁有」（`hooks.json` 守得住的前提，#698）。
        permgen.asset_paths(layout)["reviewer-planner-codex-state"],
        layout.cache_of(layout.reviewer_planner_account),
        layout.review_verdict_spool_root,
    ]


def test_probe_cache_asset_still_absent_from_job_rwp() -> None:
    """票 C 的不變式在本票之後仍成立：probe 快取不得被 job 寫。

    快取一旦可由 job 寫，「這個 provider 是 ready 的」就變成模型可以自證的東西。
    """

    layout = permgen.PathLayout()
    cache_path = permgen.asset_paths(layout)["planning-probe-cache"]
    for principal in registry.DOWNGRADED_JOB_PRINCIPALS:
        unit = permgen.build_job_unit(permgen.FOUR_WAY_SCHEME, principal=principal)
        assert all(not cache_path.startswith(path + "/") for path in unit.read_write_paths)
        assert cache_path not in unit.read_write_paths


# ---------------------------------------------------------------------------
# capability probe 接縫
# ---------------------------------------------------------------------------

def test_capability_probe_runner_makes_each_cli_call_a_job(tmp_path, monkeypatch) -> None:
    """`agy models` 與 smoke **各算一次 invocation**（各自一個 unit 實例）。

    agy 的能力探測是一段兩步 CLI 協定，真相在 `model_identities.probe_agy_capability`；
    複製一份到 invoker 就是第二份真相，因此這裡走的是票 B 立的 `ProcessRunner` 接縫。
    """

    harness = _Harness(tmp_path, monkeypatch, log_payload="gemini-3.1-pro-high\tGemini")
    invoker = harness.invoker(monkeypatch)
    runner = invoker.capability_probe_runner()

    first = runner(["agy", "models"], shell=False, capture_output=True, text=True, timeout=45)
    second = runner(["agy", "--print", "x"], shell=False, capture_output=True, text=True, timeout=45)

    assert first.returncode == 0 and isinstance(first.stdout, str)
    assert isinstance(first.stderr, str)  # `_process_fields` 要求三個欄位型別正確
    assert second.returncode == 0
    assert len(harness.specs) == 2
    assert harness.specs[0]["instance"] != harness.specs[1]["instance"]
    assert all(spec["unit"].startswith("cortex-reviewer-job@") for spec in harness.specs)


def test_scratch_and_log_slots_are_removed_after_the_call(tmp_path, monkeypatch) -> None:
    """D-a：一次性——呼叫結束即銷毀（成功與失敗兩條路都要）。"""

    harness = _Harness(tmp_path, monkeypatch, log_payload='{"ok":true}')
    invoker = harness.invoker(monkeypatch)
    invoker.run(_invocation())
    scratch_root = tmp_path / "planning-scratch"
    log_root = tmp_path / "planning-logs"
    assert list(scratch_root.iterdir()) == []
    assert not log_root.exists() or list(log_root.iterdir()) == []
    # spec 也不留：留著只會讓下一個人以為有一個未回收的派工。
    assert list(harness.spool.iterdir()) == []


def test_direct_mode_is_unchanged(tmp_path) -> None:
    """direct 模式逐字不變——本票只新增第二個實作，不動第一個。"""

    invoker = planning_runtime._select_planning_invoker(
        {job_runner.JOB_RUNNER_ENV: job_runner.RUNNER_DIRECT}
    )
    assert isinstance(invoker, planning_runtime.InProcessPlanningInvoker)


# ---------------------------------------------------------------------------
# OS 層語意：單 UID／無 systemd 的 CI 重現不了，具名 skip（不得靜默通過）
# ---------------------------------------------------------------------------

@pytest.mark.skip(
    reason=(
        "跨 UID 的唯讀 scratch 語意在單 UID／無 systemd 的 CI 重現不了：這裡要驗的是"
        "「job 帳號對 scratch 的寫入回 EROFS」，而它的成立條件是 `ProtectSystem=strict`"
        "＋該路徑不在模板 unit 的 `ReadWritePaths=` 內——兩者在 CI 都不存在，斷言會"
        "**恆真**（#638／#657 的形態：斷言真空、什麼都沒驗到、看起來是綠的）。"
        "實機驗證在 runbook 第 4e-3 步（`psc_run_under cortex-reviewer-job /bin/sh -c "
        "'cd <scratch> && touch w'` 期望 `Read-only file system`）；#686 的 PR body "
        "有 0818 的實測輸出。"
    )
)
def test_real_readonly_scratch_rejects_job_writes() -> None:  # pragma: no cover
    raise AssertionError("此測項只在具備三分帳號與已落檔 unit 的實機上執行")


@pytest.mark.skip(
    reason=(
        "「Manager 讀得到 job 寫的 log」是跨 UID ＋ POSIX ACL mask 的語意：Manager 預建"
        "檔案的 mode 0620 讓繼承來的 `user:<planner>:wx` 保住 effective `-w-`，用 0600 "
        "會被壓成 `#effective:---`（#638 缺陷 1 的同一個機制）。單 UID 環境下 producer "
        "與 consumer 是同一個 uid，ACL 沒生效也會綠。實機驗證在 runbook 第 4e-4 步；"
        "#686 的 PR body 有 0818 的實測輸出。"
    )
)
def test_real_cross_uid_log_channel_roundtrip() -> None:  # pragma: no cover
    raise AssertionError("此測項只在具備三分帳號與已落檔 unit 的實機上執行")


@pytest.mark.skip(
    reason=(
        "「一輪完整 planning 期間 cortex-manager 的行程樹不出現任何 executor 可執行檔」"
        "需要真的 systemd、真的三個帳號與真的模型呼叫；CI 沒有任何一項，而在單 UID "
        "環境下 `systemd-cgls`／`ps --ppid` 的輸出對這個宣稱完全不承載語意。"
        "實機驗證在 runbook 第 4e-5 步；#686 的 PR body 有 0818 的實測輸出（cgroup "
        "逐行 ＋ `ps --ppid` 逐行）。"
    )
)
def test_real_manager_process_tree_has_no_executor() -> None:  # pragma: no cover
    raise AssertionError("此測項只在具備三分帳號與已落檔 unit 的實機上執行")
