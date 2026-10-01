"""#716：closeout 拿 Manager 真實派工產出的探針 job spec 做端到端 conformance。

## 為什麼要有這一檔

deployment canary 的 closeout（`qualification/driver.py::_bound_codex_builder_spec`）逐項
比對 Manager 寫在 `<coordinator>/job-specs/builder/<slot>.json` 的探針卡 spec。過去每一輪
canary（約 1.5 小時）只能暴露一個不一致：唯讀模板 unit（PR #1245）、PATH 少
`/usr/local/bin`（PR #1246）。既有的 hardening 測試都用手寫的 spec／prompt，與 driver
同源，抓不到「driver 的預期與 Manager 實際產出不同」這一類錯。

這一檔讓 Manager 走 production 派工路徑真的產出探針卡的 spec 與 job 記錄，再整份交給
driver。第一次跑就一次列出剩下的四處不一致，全在探針卡 prompt 的契約上（driver 抄錯／過時，
已改為從 production 同一個來源導出）：

1. `action`：driver 抄的是 Manager 的 legacy fallback（英文），Manager 用的是 deck 卡片的
   action。
2. `inputs`：feature-oneshot 的探針卡宣告 accepted plan 為 input，driver 寫死 `[]`。
3. `source_material`：Manager 把 pin 下來的 plan 內容放進 prompt，driver 寫死 `[]`。
4. `terminal_schema.status_policy`：driver 抄的是「Manager 沒有宣告 gate」那一版，installer
   一定宣告 `PSC_GATE_CMD_PYTEST`，Manager 發的是揭露 gate 命令那一版。

## 哪些是真的、哪些是 mock

真的：installer `plan` 產出的 Manager EnvironmentFile 與 unit 的 `Environment=`（狀態路徑
整體搬到 tmp 底下）；`feature-oneshot` 的 deck 編譯；`JobRegistry`；canary overlay 經
`load_model_identities()` 載入；`manager.dispatch_workflow_card()` 配 production 的
launcher factory（`manager_daemon._resolve_launcher_compat`）與 `ScriptWorktreeCreator`
（真的 per-job git clone）；input snapshot 與內容 evidence；`SubprocessLauncher` 的模板
plan、spool slot、CODEX_HOME、job env、spec 寫出；`attach_launch_handle` 寫的 job 記錄。

mock：systemd／帳號存在性／檔案 ACL（`_template_seams()`、`_apply_slot_acl`、
`grant_workspace_acl`）；launcher 的 `subprocess.Popen`（`systemctl start` 那一層，回假
process）；driver 的 `_manager_uid`（本機沒有 `cortex-manager` 帳號）。job 本身不跑，
因此 harvest 之後才有的產物（evidence、gate ledger、bundle、completion、codex log 內容）
不在本檔範圍。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paulsha_cortex.coordinator import job_runner, manager, manager_daemon, seams  # noqa: E402
from paulsha_cortex.coordinator import launcher as launcher_module  # noqa: E402
from paulsha_cortex.coordinator.dispatcher import Dispatcher  # noqa: E402
from paulsha_cortex.coordinator.model_identities import load_model_identities  # noqa: E402
from paulsha_cortex.coordinator.registry import JobRegistry  # noqa: E402
from paulsha_cortex.coordinator.work_bridge import default_workflow_manifest  # noqa: E402
from paulsha_cortex.coordinator.workflow import (  # noqa: E402
    PlanningArtifactAuthority,
    WorkflowStep,
)
from paulsha_cortex.trust_root.install import cli as install_cli  # noqa: E402
from qualification.contract import (  # noqa: E402
    CANARY_BUILDER,
    canary_identity,
    render_model_identity_overlay,
)
from test_inner_sandbox_714 import _nested, _RecordingPopen, _template_seams  # noqa: E402
from test_qualification_driver_hardening import _load_driver  # noqa: E402
from test_trust_root_install_legacy_inventory import (  # noqa: E402
    _release_config,
    _write_bundle,
)

WORK_ID = "deployment-canary-probe"
REPOSITORY = "owner/probe"
ISSUE = 42
#: installer 的狀態樹前綴；測試把它整體搬到 tmp 底下，其餘值（PATH、/opt/cortex）原樣保留。
STATE_PREFIX = "/var/lib/"


def _installed_plan(root: Path) -> dict:
    root.mkdir(parents=True)
    bundle = _write_bundle(root)
    config = _release_config(root, bundle)
    output = root / "plan.json"
    assert install_cli.main(
        ["plan", "--config", str(config), "--bundle", str(bundle), "--output", str(output)]
    ) == 0
    return json.loads(output.read_text(encoding="utf-8"))


def _plan_content(plan: dict, suffix: str) -> str:
    contents = [
        step["content"]
        for step in plan["apply_order"]
        if isinstance(step, dict)
        and str(step.get("path", "")).endswith(suffix)
        and isinstance(step.get("content"), str)
    ]
    assert len(contents) == 1, suffix
    return contents[0]


def _installed_manager_environment(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """回傳 (Manager 行程的 environ, driver `_installed_runtime_env` 會讀到的 PSC_* 投影)。

    兩者都取 installer 實際產出的內容：EnvironmentFile 的每一行，加上 Manager unit 的
    `Environment=`（HOME／XDG_CACHE_HOME 只寫在 unit 上）。
    """

    plan = _installed_plan(root)
    environ: dict[str, str] = {}
    for line in _plan_content(plan, "/cortex-manager.env").splitlines():
        key, _, raw = line.partition("=")
        environ[key] = json.loads(raw)
    installed = {key: value for key, value in environ.items() if key.startswith("PSC_")}
    for line in _plan_content(plan, "/cortex-manager.service").splitlines():
        if line.startswith("Environment="):
            key, _, value = line[len("Environment="):].partition("=")
            environ[key] = value
    return environ, installed


def _relocate(values: dict[str, str], host: Path) -> dict[str, str]:
    return {
        key: (str(host) + value if value.startswith(STATE_PREFIX) else value)
        for key, value in values.items()
    }


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


class _LauncherSubprocess:
    """只替換 launcher 模組看到的 `Popen`（`systemctl start` 那一層）；git 等其他子行程照常。"""

    def __init__(self, popen) -> None:
        self.Popen = popen

    def __getattr__(self, name: str):
        return getattr(subprocess, name)


@pytest.fixture(scope="module")
def dispatched(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """Manager 以 production 路徑為 canary 探針卡派出一個模板 job。"""

    tmp_path = tmp_path_factory.mktemp("closeout-conformance")
    host = tmp_path / "host"
    environ, installed = _installed_manager_environment(tmp_path / "install")
    environ = _relocate(environ, host)
    installed = _relocate(installed, host)
    # builder spec spool 沒有寫進 EnvironmentFile，Manager 用的是 job_runner 的部署預設值；
    # 搬到 tmp 底下時同樣由那個常數導出。
    environ[job_runner.JOB_SPEC_SPOOL_ENV] = str(host) + job_runner.DEFAULT_JOB_SPEC_SPOOL
    agents_root = Path(environ["PSC_AGENTS_ROOT"])
    coordinator_root = agents_root / "coordinator"

    # installer 預建的部署面：帳號 HOME、spec spool、Codex 控制面與憑證、model overlay。
    for name in ("PSC_BUILDER_HOME", "PSC_REVIEWER_HOME", "PSC_GATE_HOME", "HOME", "XDG_CACHE_HOME"):
        Path(environ[name]).mkdir(parents=True, exist_ok=True)
    Path(environ[job_runner.JOB_SPEC_SPOOL_ENV]).mkdir(parents=True)
    controls = agents_root / "config" / "codex-controls" / "builder"
    (controls / "plugins").mkdir(parents=True)
    (controls / "skills").mkdir()
    (controls / "config.toml").write_text("# deployment policy\n", encoding="utf-8")
    (controls / "hooks.json").write_text("{}\n", encoding="utf-8")
    credential = agents_root / "config" / "codex-credentials" / "builder" / "auth.json"
    credential.parent.mkdir(parents=True)
    credential.write_text("{}\n", encoding="utf-8")
    config_root = Path(environ["PSC_PROJECT_CONFIG_ROOT"])
    config_root.mkdir(parents=True)
    (config_root / "model-identities.yaml").write_text(
        render_model_identity_overlay(), encoding="utf-8"
    )

    # 來源 repo（PSC_REPO_ROOT）與 plan phase 留下的 accepted plan（以 planning authority 綁定）。
    source = Path(environ["PSC_REPO_ROOT"])
    source.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=source)
    _git("config", "user.email", "probe@example.invalid", cwd=source)
    _git("config", "user.name", "Probe", cwd=source)
    (source / "README.md").write_text("probe\n", encoding="utf-8")
    _git("add", "README.md", cwd=source)
    _git("commit", "-qm", "init", cwd=source)
    plan_ref = f"docs/superpowers/plans/2026-10-01-{WORK_ID}.md"
    plan_doc = source / plan_ref
    plan_doc.parent.mkdir(parents=True)
    plan_doc.write_text("# plan\n\n- [ ] confirm the worktree\n", encoding="utf-8")

    # canary intake：`--combo feature-oneshot --builder-executor codex --builder-model ...`；
    # run 停在 build phase、探針卡是第一張待派的卡。
    driver = _load_driver()
    manifest = default_workflow_manifest(
        WORK_ID, change=WORK_ID, combo_name=driver.DEPLOYMENT_CANARY_COMBO
    )
    executor, model_id = canary_identity(CANARY_BUILDER)
    steps = tuple(
        WorkflowStep(
            phase=step.phase, persona=step.persona, card=step.card,
            executor=step.executor, model=step.model, domain=step.domain,
            inputs=step.inputs, outputs=step.outputs,
            gate_result="passed" if step.phase in {"claim", "define", "plan"} else "pending",
            skill_ref=step.skill_ref, action=step.action,
            commit_policy=step.commit_policy, test_policy=step.test_policy,
        )
        for step in manifest.steps
    )
    registry = JobRegistry(state_path=coordinator_root / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id=WORK_ID,
        repo=REPOSITORY,
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(source),
        combo=manifest.combo,
        current_phase="build",
        steps=steps,
        issue_refs=(f"{REPOSITORY}#{ISSUE}",),
        openspec_refs=(WORK_ID,),
        attempts={"build": 1},
        gate_status="running",
        planning_authority=(
            PlanningArtifactAuthority(
                ref=plan_ref, kind="plan", work_id=WORK_ID,
                baseline_sha256=hashlib.sha256(plan_doc.read_bytes()).hexdigest(),
            ),
        ),
        model_chain_override={"builder": {"executor": executor, "model_id": model_id}},
    )

    popen = _RecordingPopen()
    with mock.patch.dict(os.environ, environ, clear=True), _nested(
        [
            *_template_seams(),
            mock.patch.object(launcher_module.spool_slot, "_apply_slot_acl"),
            mock.patch.object(job_runner.job_workspace, "grant_workspace_acl"),
            mock.patch.object(launcher_module, "subprocess", _LauncherSubprocess(popen)),
        ]
    ):
        job = manager.dispatch_workflow_card(
            Dispatcher(registry, None, seams.ScriptWorktreeCreator()),
            run=run,
            identities=load_model_identities(),
            launcher_factory=lambda identity: manager_daemon._resolve_launcher_compat(
                identity.executor,
                None,
                allow_unsafe=False,
                model=identity.model_id,
                identity=identity,
            ),
            coordinator_root=coordinator_root,
        )
    assert job is not None and len(popen.calls) == 1

    # closeout 讀的是 registry 檔本身，不是 JobRegistry 的物件。
    state = json.loads((coordinator_root / "jobs.json").read_text(encoding="utf-8"))
    return SimpleNamespace(
        coordinator_root=coordinator_root.resolve(),
        job=next(row for row in state["jobs"] if row["job_id"] == job["job_id"]),
        workflow=next(row for row in state["workflows"] if row["run_id"] == run.run_id),
        installed=installed,
    )


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch):
    module = _load_driver()
    # 本機沒有 `cortex-manager` 帳號：Manager 產物由測試行程寫出，owner 就是它。
    monkeypatch.setattr(module, "_manager_uid", lambda: os.getuid())
    return module


def test_closeout_accepts_the_manager_authored_probe_job_spec(dispatched, driver) -> None:
    """spec 的每一項（keys、unit、worktree、env、CODEX_HOME、PATH、safe.directory、prompt、
    整條 `bash -c` script）都與 Manager 真實產出逐字相符。"""

    spec_path, codex_home, _digest = driver._bound_codex_builder_spec(
        dispatched.coordinator_root,
        dispatched.job,
        dispatched.workflow,
        manager_env=dispatched.installed,
    )

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    assert spec["unit"].startswith("cortex-job-ro")
    assert spec["env"]["CODEX_HOME"] == str(codex_home)
    assert spec["env"]["PATH"] == dispatched.installed["PSC_BUILDER_PATH"]


def test_closeout_accepts_the_manager_authored_probe_job_log(dispatched, driver) -> None:
    path, _content = driver._bound_codex_builder_log(
        dispatched.coordinator_root, dispatched.job
    )

    assert str(path) == dispatched.job["log_path"]


def test_closeout_accepts_the_manager_authored_builder_binding(dispatched, driver) -> None:
    """workflow 的 builder 覆寫與 job 的身分／runtime 欄位都是 Manager 派工時寫下的。"""

    driver._validate_codex_builder_binding(dispatched.workflow, [dispatched.job])


def test_probe_prompt_gate_text_follows_the_installed_manager_environment(
    dispatched, driver
) -> None:
    """負向對照：拿一份沒有宣告 gate 的 env 重建，就是先前寫死的那一版——必須對不上。"""

    with pytest.raises(driver.QualificationFailure, match="job prompt is invalid"):
        driver._bound_codex_builder_spec(
            dispatched.coordinator_root,
            dispatched.job,
            dispatched.workflow,
            manager_env={},
        )
