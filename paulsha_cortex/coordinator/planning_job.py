"""issue #686（#672 票 E）：`JobPlanningInvoker`——planning 改走模板 unit。

## 這個檔換掉的是什麼

票 B（#683）把「怎麼跑一個 executor」抽成 `PlanningInvoker`，並把現行行為原封搬進
`InProcessPlanningInvoker`。本檔是那個介面的**第二個實作**：拿到一個 identity ＋
prompt，起一個 `cortex-reviewer-job@<instance>.service` 實例，以
`cortex-reviewer-planner` 的身分執行模型 CLI，再把輸出取回來。

`cortex-manager` 的 passwd 註記逐字寫著 `no model code`。#615（M2）讓 reviewer 兌現了
那句話，planner 沒有——在 #687 切換之前它仍在 daemon 行程內 `subprocess.run` 模型
CLI（#672）。本檔是那半條的補齊。

**#687（票 F）之後的狀態**：四分部署的 `PSC_JOB_RUNNER=systemd-template` 使本類成為
planning 的實際執行後端，`direct` 在該部署上不是可達組態。實機一輪 define 的每一次
模型呼叫（probe／questioner／secondary／integrator）都落成一個
`cortex-reviewer-job@`／`-jit@` 實例，`User=cortex-reviewer-planner`；同一段期間
`systemd-cgls -u cortex-manager.service` 只有 daemon ＋ `systemctl start --wait`
兩層，零 executor（runbook 第 5-6c 步）。

## 十條防線（design D2）在本實作裡分別由誰保證

===  ===========================  =============================================
 #    現行（in-process）           job 側對應
===  ===========================  =============================================
D-a  `TemporaryDirectory`         Manager 在 `planning-scratch-pool` 下建
                                  per-invocation 一格，呼叫結束 `rmtree`；
                                  unit 的 `CollectMode=inactive-or-failed`
                                  讓 systemd 自己也不留實例
D-b  整棵 repo 複本                **空 scratch**（見下方「U-1」段）
D-c  `cwd=sandbox`                spec 的 `working_directory`＝那一格；
                                  shim 在降權**之後** `chdir`
D-d  sandbox 弄髒即 fail-closed    **結構上不可能**：scratch 的 writer 面只有
                                  Manager ⇒ 不進任何 job 模板 unit 的
                                  `ReadWritePaths=` ⇒ `ProtectSystem=strict`
                                  下 job 連寫都寫不進去（U-2 裁決＝唯讀）
D-e  operator 樹雙向快照            `repo-source-tree` 不在 reviewer 模板 unit 的
                                  RWP 內 ⇒ kernel 直接擋（**升級**）
D-f  `_contain_operator_drift`     job 模式下 operator 樹結構性不可能被 job 改；
                                  程式保留給 direct 模式（**等價、範圍縮小**）
D-g  hermetic `CLAUDE_CONFIG_DIR`  job 的 HOME 是 `cortex-reviewer-planner`，
                                  `ProtectHome=yes` 讓 `/home` 整個不可見，
                                  `build_job_env()` 的白名單不含
                                  `CLAUDE_CONFIG_DIR`（**升級**）
D-h  `subprocess.run(timeout=)`    Manager 側 `Popen.wait(timeout)` →
                                  `systemctl stop` → 確認離開 active（D4）
D-i  `capture_output=True`         Manager-owned log ＋ shim 降權後 O_APPEND
                                  接管（`planning-job-log-spool`）
D-j  codex `-o last.json`          **#727 起**落點由 `planning_last_message_path()`
                                  從該 job 那份 log 導出（`<planning-logs>/
                                  <instance>/planning.last.json`），Manager 預建、
                                  job 寫得進去、Manager 讀得回來 ⇒ 第二候選回來了
                                  （退步 R-2 **解除**）
===  ===========================  =============================================

⚠️ **R-2 的原始描述（「落點在 job 的 `PrivateTmp` ⇒ Manager 讀不到」）是對現象的
正確描述，但把它當成結構限制是錯的**：`PrivateTmp` 是 `_planning_argv` 自己組
`Path(temp_dir)/"last.json"` 造成的，而 `temp_dir` 在 job 模式下被硬填 `"/tmp"`。
#714 缺陷 2 早就替 builder lane 回答過同一個問題（落點＝該 job 那份 log 的兄弟檔），
只是 planning 這條路沒被涵蓋——那是**第二份落點決定**，不是不同的約束。代價逐字記在
#727：codex 是唯一有憑證、剖面也對的 planner 候選，它連續四輪派工只留下 `ValueError`
五個字，define 至今從未真正收斂過。

## U-1（scratch 要不要放 repo 複本）：job 模式實作 (b)＝空 scratch

design 傾向 (b) 而未定案。本實作在 **job 模式**走 (b)，direct 模式逐字不變（仍是整棵
複製）。理由不是偏好，是 (a) 在 job 模式下**還不存在可行的機制**：複本由 Manager 建
立、Manager 的 unit 帶 `UMask=0077`，複製出來的每個檔都是 `0600 cortex-manager`，job
一個位元組都讀不到。要讓 (a) 成立得先有一套「把 Manager 建的整棵樹遞迴授權給 job 帳號」
的機制——那是**新的一整個授權面**，不在任何一張票的範圍內。

實測補充（本票在實機量的，寫進 PR body）：**(b) 在 job 模式下並不比 (a) 更緊**。
`ProtectSystem=strict` 只擋寫，不擋讀；`/var/lib/cortex/repos/<slug>` 對
`cortex-reviewer-planner` 是可讀的。因此「模型讀不到 repo」在 job 模式下**不成立**，
差別只在它不是 cwd。這一點 design 的 U-1 取捨段（「(b) 是收緊」）在 job 模式下要更正。

## U-2（scratch 對 job 可寫還是唯讀）：**唯讀**

design 自己指出：選唯讀可以把安全退步 R-1（「模型弄髒自己的拋棄式 sandbox」的偵測，
Manager 在 job 側做不到）從「失去行為訊號」變成「結構上不可能」。本票採唯讀。

實作上它**不是**一個 `if`，而是登記表 writer 面的機械後果：`planning-scratch-pool`
的 writers 只有 `Principal.MANAGER` ⇒ `permgen.required_write_targets()` 不收它 ⇒
它不出現在任何 job 模板 unit 的 `ReadWritePaths=` ⇒ `ProtectSystem=strict` 讓寫入回
EROFS。要打破這條性質必須改登記表，而那會在產生器輸出與 unit 檔上留下痕跡。

executor 需要的可寫落點改指向 unit 的 `PrivateTmp=yes` 私有 `/tmp`（agy 的 log／
state）：per-invocation、job-owned、unit 結束即消失，且 Manager 看不到。**codex 的
`-o` #727 起不在此列**——見上面 D-j 那一列與 `job_workspace.job_last_message_path()`。

## 只支援模板模式（B 案），`systemd-run` 模式 fail-closed

`PSC_JOB_RUNNER=systemd-run`（A 案）下 Manager 自己組 `--property=`／`--setenv=`，
「身分與加固面只由 root-owned unit 決定」這條性質不成立——而那正是 planning 這個
**吃 untrusted issue 內容**的角色最需要的性質。本檔因此只實作 B 案；A 案下
:func:`planning_runtime._select_planning_invoker` 明確拒絕，**不**退回 in-process
（退回去看起來會是成功的，那正是本票要消除的失效模式）。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from uuid import uuid4

from ..config import paths
from . import job_runner, job_workspace, spool_slot
from .model_identities import (
    PLANNING_FAILURE_EXECUTOR,
    PLANNING_FAILURE_EXECUTOR_SILENT_EXIT,
    PLANNING_FAILURE_JOB_START,
)


logger = logging.getLogger(__name__)


#: 逾時的具名子類。design D4 把它寫成一個獨立的失敗族（`planning-job-timeout`），
#: 但票 A 已經把三分族的集合釘成不變式
#: （`tests/test_planning_failure_taxonomy_672.py` 對
#: `ENVIRONMENT_GRADE_PLANNING_FAMILIES` 做的是**逐字相等**斷言），新增第四個
#: environment 級族名會動到那條契約。
#:
#: 因此逾時在本實作裡是 `planning-executor-failed` 的**子類**——與
#: `executor-silent-exit` 同一種收法：族名（＝分級輸入）不變，子類名進 diagnostic
#: 並在拒因表上逐字現形。分級結果與 design 要求的一致（environment ⇒
#: `_resume_decision` 浮得出 `recover-planning`），差別只在它掛在哪一個族底下。
PLANNING_JOB_TIMEOUT = "planning-job-timeout"

#: `systemctl stop` 之後確認 unit 真的離開 active 的輪詢上限（秒）。
#:
#: design D4：停止之後 MUST 確認 unit 已離開 active，否則下一次同 `job_id` 的呼叫會撞
#: `job-runner-template-instance-busy`——而那個錯誤訊息與「逾時」毫無關係，會把下一輪
#: 的排查完全帶偏。
STOP_CONFIRM_TIMEOUT_SECONDS = 30.0
STOP_CONFIRM_POLL_SECONDS = 0.2

#: Manager 預建 log 檔的 mode。**#708 起真相在 `spool_slot.JOB_LOG_FILE_MODE`**
#: （三個降權 principal 共用同一個值與同一份論證）；本名稱保留為別名，避免既有
#: 呼叫端／測試多一次無意義的改名。
LOG_FILE_MODE = spool_slot.JOB_LOG_FILE_MODE

#: 失敗診斷帶回的 log 尾段上限。診斷會一路進 log／evidence／`blocking_reason`。
LOG_EXCERPT_LIMIT = 400

#: executor 版本字串的探測逾時（秒）。它自己也是一個降權 job。
VERSION_PROBE_TIMEOUT_SECONDS = 60


class PlanningJobError(ValueError):
    """降權 planning job 的 fail-closed，**帶三分族**（design D8／spec R6）。

    繼承 `ValueError` 與 `job_runner.JobRunnerError` 同一個理由：本 repo 全部的
    fail-closed 驗證都是 `ValueError`，daemon 的 tick isolation（#246）攔的就是它，
    因此一次 planning 失敗不會打掛 daemon。

    `family` 是**分級的唯一輸入**。它刻意不從 `detail` 的字串 substring-search
    ——`detail` 帶的是模型輸出與 CLI 訊息，那是 job 影響得到的東西（票 A 的
    `grade=` 錨在開頭就是為了同一件事）。
    """

    def __init__(
        self,
        family: str,
        detail: str,
        *,
        diagnostics: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(f"{family}: {detail}")
        self.family = family
        self.detail = detail
        self.diagnostics: dict[str, str] = dict(diagnostics or {})


@dataclass(frozen=True)
class _CompletedJob:
    """`subprocess.CompletedProcess` 的最小同形物（`ProcessRunner` 契約）。

    `stderr` 恆為空字串而不是 `None`：`model_identities._process_fields` 要求三個欄位
    型別正確，回 `None` 會讓每一次 agy probe 落在 `malformed process result`——一個與
    現場完全無關的診斷。

    **stdout 為什麼同時含 stderr**：shim 在降權之後把 fd 1 與 fd 2 **同時** dup 到
    spec 的 `log_path`（`job_shim._take_over_stdio`），Manager 這一側拿到的是一份合併
    輸出。這是 design 未預期的第五條退步（R-5），已逐字記在票 E 的 PR body。

    `output_text`／`last_message` 是 #727 加的兩格：codex 的 `-o` 落點改掛到該 job
    自己那份 log 的兄弟檔之後，Manager **讀得回來**（D-j／R-2 在 planning 這條路上
    解除）；`last_message` 則是那一格的唯讀狀態標記，讓「寫不進去」與「模型沒輸出」
    在 evidence 上分得開。`ProcessRunner` 那條路（agy 的兩次裸 CLI）不用這兩格，
    維持預設值。
    """

    returncode: int
    stdout: str
    stderr: str = ""
    output_text: str | None = None
    last_message: str | None = None


def _job_repo_root() -> str:
    """job spec 的 `PSC_REPO_ROOT`（與 `launcher.launch()` 逐字同一個推導）。"""

    return str(Path(__file__).resolve().parents[2])


def _excerpt(text: str, *, limit: int = LOG_EXCERPT_LIMIT) -> str:
    collapsed = " ".join(text.split())
    if not collapsed:
        return "<empty>"
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


class JobPlanningInvoker:
    """在 `cortex-reviewer-job@.service` 模板實例裡執行 planning 的模型呼叫。

    **本類別不複製任何一份 `job_runner` 的邏輯**（plan 票 E 的硬性要求）：身分、
    模板、加固剖面、env 白名單、preflight、spec 形狀、起動確認全部經
    `job_runner` 的既有函式，本類別只負責「把一次 planning 呼叫翻成一次派工」。
    """

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        scratch_root: str | Path | None = None,
        log_spool_root: str | Path | None = None,
        popen: Callable[..., object] | None = None,
        unit_active: Callable[[str, str], bool] | None = None,
        stop_unit: Callable[[str, str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._env: Mapping[str, str] = env if env is not None else os.environ
        self._scratch_root = (
            Path(scratch_root) if scratch_root is not None else paths.planning_scratch_root()
        )
        self._log_spool_root = (
            Path(log_spool_root)
            if log_spool_root is not None
            else paths.job_log_spool_root("reviewer")
        )
        self._popen = popen if popen is not None else subprocess.Popen
        self._unit_active = unit_active if unit_active is not None else job_runner._unit_is_active
        self._stop_unit = stop_unit if stop_unit is not None else self._systemctl_stop
        self._monotonic = monotonic
        self._sleep = sleep
        self._sequence = 0
        self._version_cache: dict[str, str] = {}
        self._resolving_version = False

    # -- PlanningInvoker 介面 ------------------------------------------------

    def run(self, invocation) -> object:
        """跑一次 identity＋prompt 呼叫（`PlanningInvoker.run` 的 job 實作）。"""

        from .planning_runtime import _planning_argv, planning_last_message_path
        from .planning_runtime import PlanningOutcome

        identity = invocation.identity
        scratch_hint = self._reserve(invocation.run_id, invocation.purpose)
        # `_planning_argv` 的 `temp_dir` 是 executor 的**其餘**可寫落點（agy 的
        # log_dir）。U-2 裁決下 scratch 唯讀，因此它指向 unit 的 `PrivateTmp=yes`
        # 私有 /tmp——per-invocation、job-owned、unit 結束即消失。
        #
        # **#727：codex 的 `-o` 不再是它們之一。** 修法前 `-o` 也落在私有 /tmp，
        # 於是 job 寫得進去、Manager 讀不回來 ⇒ `_extract_json` 退成單候選 ⇒ 那時
        # 串流備援解不了 codex 的 JSONL ⇒ `ValueError: planning launcher returned
        # no JSON object`。落點改由 `planning_last_message_path()` 從**該 job 自己
        # 那份 log** 導出（#714 缺陷 2 的同一條規則）：同一格 log spool、job 已有
        # `wx`、Manager 是 owner ⇒ 模板 unit 的 `ReadWritePaths=` 逐字不變，而第二
        # 候選回得來。
        log_path = self._job_log_path(scratch_hint.instance)
        last_message_path = planning_last_message_path(log_path)
        argv = _planning_argv(
            identity,
            invocation.prompt,
            "/tmp",
            scratch_hint.cwd,
            last_message_path=last_message_path,
        )
        completed = self._dispatch(
            argv=argv,
            executor=identity.executor,
            timeout_seconds=invocation.timeout_seconds,
            reservation=scratch_hint,
            last_message_path=last_message_path,
        )
        return PlanningOutcome(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            output_text=completed.output_text,
            diagnostics=dict(self._diagnostics_cache),
            last_message=completed.last_message,
        )

    def capability_probe_runner(self) -> Callable[..., object]:
        """`probe_agy_capability` 兩次裸 CLI 呼叫用的執行接縫（票 B 的 `ProcessRunner`）。

        agy 的能力探測是一段**兩步 CLI 協定**（`agy models` → smoke），它的真相在
        `model_identities.probe_agy_capability`；複製一份到這裡就是第二份真相。因此
        這裡回傳的是「一個 argv → 一個降權 job」的閉包，**兩次 CLI 呼叫各算一次
        invocation**（各自一個 unit 實例、各自一格 scratch 與 log）。
        """

        def runner(argv: Sequence[str], **kwargs: object) -> _CompletedJob:
            timeout = kwargs.get("timeout")
            timeout_seconds = int(timeout) if isinstance(timeout, (int, float)) else 45
            command = [str(item) for item in argv]
            if not command:
                raise PlanningJobError(
                    PLANNING_FAILURE_JOB_START, "capability probe argv is empty"
                )
            reservation = self._reserve("ephemeral", "probe")
            return self._dispatch(
                argv=command,
                executor=command[0],
                timeout_seconds=timeout_seconds,
                reservation=reservation,
            )

        return runner

    # -- 內部 ---------------------------------------------------------------

    @property
    def _diagnostics_cache(self) -> Mapping[str, str]:
        return getattr(self, "_last_diagnostics", {})

    def _reserve(self, run_id: str, purpose: str) -> "_Reservation":
        """算出本次呼叫的 job_id 與 scratch cwd（D9 的 instance 命名）。

        格式 `plan-<run_id 前 12 字>-<purpose>-<序號>-<隨機 8 碼>`：

        - `purpose` 進名字是刻意的——`systemctl list-units 'cortex-reviewer-job@*'`
          因此直接說得出「這一批 job 在做什麼」，不必回頭查 spec。它**不**進 spec 的
          任何決策欄位。
        - 隨機後綴：probe 的 `run_id` 在非 daemon 呼叫端是 `"ephemeral"`（D9 已點名），
          而 daemon 與 CLI 的 `apply_work_action` 可能同時跑——序號只在行程內唯一，
          跨行程要靠隨機碼，否則兩個並行的 probe 會互撞 `instance-busy`。

        instance 名的**推導**不在這裡：`job_runner.template_instance_id()` →
        `job_workspace.job_segment()` 是全 repo 唯一的推導點（#645），呼叫端一律只傳
        job_id、不得自己拼名字。
        """

        self._sequence += 1
        job_id = (
            f"plan-{str(run_id or 'ephemeral')[:12]}-{purpose}-"
            f"{self._sequence}-{uuid4().hex[:8]}"
        )
        instance = job_runner.template_instance_id(job_id)
        slot = self._scratch_root / instance
        return _Reservation(job_id=job_id, instance=instance, slot=slot, cwd=slot / "cwd")

    def _job_log_path(self, instance: str) -> Path:
        """這一個 instance 的 job log 落點——**本類別唯一的推導點**（#727）。

        `-o` 的落點是它的兄弟檔（`planning_last_message_path()`），而 argv 在
        `run()` 就要組好、log 一格卻在 `_dispatch()` 才建；兩處各推導一次正是 #714
        缺陷 2 的形狀。收斂成一支方法之後，兩邊拿到的是同一條路徑，而 `_dispatch()`
        另外對 `run()` 傳下來的那一份做逐字比對（漂移即 fail-closed）。
        """

        return self._log_spool_root / instance / job_workspace.PLANNING_JOB_LOG_FILENAME

    def _dispatch(
        self,
        *,
        argv: Sequence[str],
        executor: str,
        timeout_seconds: int,
        reservation: "_Reservation",
        last_message_path: Path | None = None,
    ) -> _CompletedJob:
        """一次呼叫 = 一個模板 unit 實例。**在任何副作用之前先 fail-closed。**

        `last_message_path` 由 `run()` 傳下來（`ProcessRunner` 那條路沒有 `-o`，
        維持 ``None``）。本方法**不重新推導**它，只驗證它與這一個 plan 的 log 兄弟檔
        逐字相同——比照 `launcher.launch()` 對 `#714` 那一格做的事。
        """

        self._last_diagnostics: dict[str, str] = {}
        try:
            plan = job_runner.prepare_systemd_template(
                self._env,
                job_id=reservation.job_id,
                executor=executor,
                role=job_runner.JOB_ROLE_REVIEW,
                unit_active=self._unit_active,
            )
        except job_runner.JobRunnerError as exc:
            raise PlanningJobError(
                PLANNING_FAILURE_JOB_START, str(exc)
            ) from exc

        diagnostics = self._base_diagnostics(plan, executor)
        self._last_diagnostics = dict(diagnostics)
        log_path = self._job_log_path(plan.instance)
        log_slot = log_path.parent
        if last_message_path is not None:
            from .planning_runtime import planning_last_message_path

            derived = planning_last_message_path(log_path)
            if Path(last_message_path) != derived:
                # #714 的教訓逐字：「argv 指著 A、shim 開的是 B」這種錯位只在實機上
                # 看得見。兩處推導漂移時當場停，不要派出去。
                raise PlanningJobError(
                    PLANNING_FAILURE_JOB_START,
                    (
                        "planning last-message 落點漂移："
                        f"argv={last_message_path} 導出={derived}"
                    ),
                    diagnostics=diagnostics,
                )
        try:
            self._prepare_scratch(reservation)
            self._prepare_log(log_slot, log_path)
            if last_message_path is not None:
                # `-o` 那一格**由 Manager 預建**，理由與 log 檔逐字相同（#638 缺陷 2）：
                # job 自己建的檔帶降權 unit 的 `UMask=0077` ⇒ `0600 <job 帳號>`，
                # Manager 是目錄的 owner 但那不給檔案內容的讀取權。差別在於這一格
                # Manager **真的要讀它**，因此讀不回來不是診斷面的損失、是功能面的。
                spool_slot.preseed_job_writable_file(last_message_path)
        except (OSError, spool_slot.SpoolSlotError) as exc:
            self._cleanup(reservation, log_slot)
            raise PlanningJobError(
                PLANNING_FAILURE_JOB_START,
                f"planning job scratch/log 準備失敗: {type(exc).__name__}: {exc}",
                diagnostics=diagnostics,
            ) from exc

        try:
            return self._start_and_wait(
                argv=argv,
                plan=plan,
                reservation=reservation,
                log_path=log_path,
                timeout_seconds=timeout_seconds,
                diagnostics=diagnostics,
                last_message_path=last_message_path,
            )
        finally:
            self._cleanup(reservation, log_slot)
            # spec 是 job 的命令列，收割之後就沒有消費者；留著只會讓下一個人以為
            # 有一個未回收的派工（`instance-busy` 的常見誤判來源）。
            try:
                os.unlink(plan.spec_path)
            except OSError:
                pass

    def _start_and_wait(
        self,
        *,
        argv: Sequence[str],
        plan: job_runner.SystemdTemplatePlan,
        reservation: "_Reservation",
        log_path: Path,
        timeout_seconds: int,
        diagnostics: dict[str, str],
        last_message_path: Path | None = None,
    ) -> _CompletedJob:
        sentinel = reservation.slot / "client.exit"
        client_log = reservation.slot / "client.log"
        # #712：spec 的 `working_directory` 與 env 的 git 放行必須是**同一個已解析
        # 的字串**——git 比對 `safe.directory` 逐字相等，且比的是 `getcwd()` 之後的
        # physical path。因此在這裡解析一次，兩邊共用。
        #
        # planning 的 scratch 裡**沒有 repo**（`_prepare_scratch` 建的是一格空目錄），
        # 所以這條放行在 define／brainstorm 上是無害的 no-op；形態是 **per-principal**
        # 的，而 reviewer 帳號的**另一種**工作區（foreign review 的 linked worktree）
        # 確實跨 owner——見 `registry.JOB_GIT_WORKSPACE_TRUST` 的 reviewer 那一列。
        workspace = str(Path(reservation.cwd).resolve())
        spool_slot.provision_runtime_surfaces(
            principal="reviewer", job_id=reservation.job_id
        )
        env = job_runner.build_job_env(
            manager_env=self._env,
            job_id=reservation.job_id,
            slice_id=reservation.job_id,
            repo_root=_job_repo_root(),
            workspace=workspace,
            role=job_runner.JOB_ROLE_REVIEW,
        )
        # design D3 的第 1 條：planning 的 job **顯式**只有模型 argv 一段。
        # 不跑 gate、不產 bundle、不寫 verdict、不寫 sentinel，也不包 `bash -c`
        # ——不是「既有旗標碰巧為 None」，是這裡根本沒有 wrapper 可言。理由不只是
        # 潔癖：wrapper 自產的任何一個位元組都會進同一份 log，而那份 log 就是
        # `_extract_json` 的輸入（D3 第 2 條）。
        spec = job_runner.build_job_spec(
            job_id=reservation.job_id,
            instance=plan.instance,
            unit=plan.unit,
            command=list(argv),
            working_directory=workspace,
            log_path=str(log_path),
            env=env,
        )
        try:
            job_runner.write_job_spec(plan.spec_path, spec, account=plan.account)
        except job_runner.JobRunnerError as exc:
            raise PlanningJobError(
                PLANNING_FAILURE_JOB_START, str(exc), diagnostics=diagnostics
            ) from exc

        client_argv = job_runner.build_manager_exit_recorder_argv(
            client_argv=job_runner.build_systemctl_start_argv(
                systemctl=plan.binary, unit=plan.unit
            ),
            sentinel=str(sentinel),
        )
        # client 的 stdout／stderr **刻意不與模型 log 共用一個檔**：那份 log 是
        # `_extract_json` 的輸入，systemctl 自己的訊息（polkit 拒絕等）混進去就是
        # design D3 第 2 條要擋的污染。它們落在 job 讀得到、寫不了的 scratch 那一格。
        with open(client_log, "wb") as handle:
            process = self._popen(
                client_argv,
                cwd=None,
                env=self._client_env(),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
        try:
            job_runner.confirm_template_instance_started(
                process=process,
                sentinel=str(sentinel),
                unit=plan.unit,
                account=plan.account,
                log_path=str(client_log),
                # #708 第 3 項：shim 在接管 log 之前的失敗只進 unit journal，Manager
                # 讀不到；那一族現在另外留一筆機器可讀紀錄在 job 自己那一格 log
                # spool 裡（`job_shim.write_shim_error`）。
                job_log_path=str(log_path),
                timeout_ms=job_runner.resolve_start_timeout_ms(self._env),
                manager_authored_sentinel=True,
            )
        except job_runner.JobRunnerError as exc:
            self._stop_and_confirm(plan)
            raise PlanningJobError(
                PLANNING_FAILURE_JOB_START, str(exc), diagnostics=diagnostics
            ) from exc

        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            # D4：逾時由 Manager 側強制終止。**不能只是放棄等待**——那會讓下一次
            # 同名 instance 撞 `job-runner-template-instance-busy`，而那個症狀與
            # 逾時完全無關。
            stopped = self._stop_and_confirm(plan)
            diagnostics = {
                **diagnostics,
                "timeout_seconds": str(timeout_seconds),
                "unit_left_active": "no" if stopped else "yes",
            }
            self._last_diagnostics = dict(diagnostics)
            raise PlanningJobError(
                PLANNING_FAILURE_EXECUTOR,
                (
                    f"{PLANNING_JOB_TIMEOUT} unit={plan.unit} "
                    f"timeout={timeout_seconds}s "
                    f"stopped={'yes' if stopped else 'no'}"
                ),
                diagnostics=diagnostics,
            ) from exc

        returncode = self._read_sentinel(sentinel, process)
        stdout = self._read_log(log_path)
        # #727：`-o` 那一格必須在 `_cleanup()` 把 log 一格 rmtree 之前讀出來，而且
        # **成功與失敗兩條路都要**——失敗路徑上「寫進去了沒」正是四輪派工都問不出來
        # 的那件事。讀取／標記兩支都在 `planning_runtime`，這裡不另寫一份。
        output_text: str | None = None
        last_message: str | None = None
        if last_message_path is not None:
            from .planning_runtime import _last_message_marker, _read_last_message

            last_message = _last_message_marker(last_message_path)
            output_text = _read_last_message(last_message_path)
        if returncode != 0:
            client_tail = self._read_log(client_log)
            silent = not stdout.strip()
            detail = (
                f"rc={returncode} unit={plan.unit} "
                f"profile={plan.hardening_profile} "
                f"binary={diagnostics.get('resolved_binary', '<unresolved>')} "
                f"version={self._binary_version(executor=plan.executor, plan=plan)} "
                f"seccomp_filter_fatal={diagnostics.get('seccomp_filter_fatal', '<unknown>')}"
            )
            if last_message is not None:
                detail = f"{detail} last_message={last_message}"
            if silent:
                # spec R6：rc≠0 且完全無輸出是整個家族裡最難查的一種——**連錯誤訊息
                # 都沒有**，歸因於是會落到模型、prompt、逾時或憑證，而不會落到執行
                # 環境。它必須被顯式命名，而且診斷要指名 unit／剖面／resolved_binary
                # ／`seccomp_filter_is_fatal()`——那四個就是唯一的線索來源，而最後
                # 一個正是 #673 整張票走偏的原因（當時沒有任何地方回答得了它）。
                detail = f"{PLANNING_FAILURE_EXECUTOR_SILENT_EXIT} {detail}"
                detail = f"{detail} client={_excerpt(client_tail)}"
            else:
                detail = f"{detail} log={_excerpt(stdout)}"
            diagnostics = {**diagnostics, "returncode": str(returncode)}
            self._last_diagnostics = dict(diagnostics)
            raise PlanningJobError(
                PLANNING_FAILURE_EXECUTOR, detail, diagnostics=diagnostics
            )
        return _CompletedJob(
            returncode=returncode,
            stdout=stdout,
            output_text=output_text,
            last_message=last_message,
        )

    # -- 前置物 -------------------------------------------------------------

    def _prepare_scratch(self, reservation: "_Reservation") -> None:
        """建出 per-invocation 的 scratch 一格（**Manager-owned、job 唯讀**）。

        兩個 mode 是刻意的，而且方向相反：

        - 那一格與 cwd 給 `0755`：job 必須 chdir 進去、讀得到裡面（D-c）。「讀得到」
          不等於「寫得進」——寫入面由 `ProtectSystem=strict` ＋ 本 pool 不在任何 job
          模板 unit 的 RWP 內擋掉（U-2），與 mode 無關。
        - pool 根若不存在才建，且**只在自己建的時候**設 mode：operator 依產生器套過
          `chmod 0700` ＋ `setfacl rX` 之後，那是更緊的形態，這裡不得把它蓋回去。
        """

        root = self._scratch_root
        if not root.exists():
            root.mkdir(parents=True, exist_ok=True)
            os.chmod(root, 0o711)
        if reservation.slot.exists():
            # instance 名帶 job_id 的 sha256 前 8 碼，撞名等於同一個 job_id 被重派，
            # 而那是 `prepare_systemd_template` 的 `instance-busy` 該擋的事。走到這裡
            # 代表上一輪留了殘骸，清掉再建（留著會讓模型讀到上一輪的東西）。
            shutil.rmtree(reservation.slot, ignore_errors=True)
        reservation.slot.mkdir(parents=True)
        os.chmod(reservation.slot, 0o755)
        reservation.cwd.mkdir()
        os.chmod(reservation.cwd, 0o755)

    def _prepare_log(self, slot: Path, log_path: Path) -> None:
        """建出 per-invocation 的 log 一格，並**由 Manager 預先建立** log 檔。

        **#708 起實作在 `spool_slot.prepare_job_log()`**，三個降權 principal 共用同一
        份（builder 與 gate 各自的 log spool 走的是逐字相同的一段）。本方法保留只是
        為了讓 planning 這邊的呼叫點與錯誤翻譯不變——邏輯一個位元組都沒有第二份。
        """

        spool_slot.prepare_job_log(slot, log_path)

    def _cleanup(self, reservation: "_Reservation", log_slot: Path) -> None:
        """D-a：呼叫結束即銷毀。診斷面失敗不得反過來變成新的失敗來源。"""

        for target in (reservation.slot, log_slot):
            try:
                shutil.rmtree(target, ignore_errors=True)
            except Exception:  # noqa: BLE001 - 清理失敗不得掩蓋上游的真實失敗
                logger.warning("planning-job-cleanup-failed path=%s", target)

    # -- 診斷 ---------------------------------------------------------------

    def _base_diagnostics(
        self, plan: job_runner.SystemdTemplatePlan, executor: str
    ) -> dict[str, str]:
        """`unit`／`hardening_profile`／`resolved_binary` ＋ seccomp 那一維（D8）。

        `seccomp_filter_is_fatal()` 是 PR #677 提供的**機械答案**：
        `SystemCallErrorNumber=EPERM` 在時答案是「不該懷疑 seccomp」，不在時才是
        「該懷疑」。#673 整張票走偏，就是因為當時沒有任何地方回答得了這個問題。
        """

        diagnostics: dict[str, str] = {
            "family": "",
            "unit": plan.unit,
            "instance": plan.instance,
            "hardening_profile": plan.hardening_profile,
            "account": plan.account,
        }
        try:
            search_path = job_runner.resolve_job_path(
                self._env, role=job_runner.JOB_ROLE_REVIEW
            )
            resolved = shutil.which(executor, path=search_path)
        except Exception as exc:  # noqa: BLE001 - 診斷不得拖垮派工
            resolved = None
            diagnostics["job_path"] = f"<unresolved:{type(exc).__name__}>"
        else:
            diagnostics["job_path"] = search_path
        diagnostics["resolved_binary"] = resolved or "<absent>"
        try:
            from ..trust_root import permgen

            table = permgen.executor_hardening_profile(executor).effective()
            diagnostics["seccomp_filter_fatal"] = (
                "yes" if permgen.seccomp_filter_is_fatal(table) else "no"
            )
            surfaces = [
                f"{surface.program}@{surface.surface}:"
                f"{'|'.join(surface.syscalls)}:fatal={surface.fatal}"
                for surface in permgen.filtered_syscall_surfaces()
                if surface.program == executor
            ]
            diagnostics["filtered_syscalls"] = ",".join(surfaces) or "<none>"
        except Exception as exc:  # noqa: BLE001 - 同上
            diagnostics["seccomp_filter_fatal"] = f"<unresolved:{type(exc).__name__}>"
        return diagnostics

    def _binary_version(
        self, *, executor: str, plan: job_runner.SystemdTemplatePlan
    ) -> str:
        """`<executor> --version` 的字串——**在同一個降權 job 面下量**（D10）。

        為什麼非量不可：#681 就是「只比路徑會漏掉」的那一類缺陷（toolchain 的
        `copilot` 是一支依 PATH／HOME 搜尋的 shim，job 實際跑系統層 0.0.330、operator
        是 1.0.79，**路徑完全相同**）。而「解析到非預期版本」**不會表現為失敗**——它
        會安靜地產出一份用別的 CLI 跑出來的結果。

        只在失敗路徑上量（一次失敗多一個短命 job），並以「這一支檔案的 stat」為快取
        鍵。遞迴由 `_resolving_version` 擋住：版本探測本身失敗時不得再去探它的版本。
        """

        if self._resolving_version:
            return "<version-probe-skipped>"
        key = f"{executor}"
        cached = self._version_cache.get(key)
        if cached is not None:
            return cached
        self._resolving_version = True
        try:
            reservation = self._reserve("version", "probe")
            completed = self._dispatch(
                argv=[executor, "--version"],
                executor=plan.executor or executor,
                timeout_seconds=VERSION_PROBE_TIMEOUT_SECONDS,
                reservation=reservation,
            )
            value = _excerpt(completed.stdout, limit=120)
        except Exception as exc:  # noqa: BLE001 - 診斷不得拖垮失敗回報
            value = f"<unresolved:{type(exc).__name__}>"
        finally:
            self._resolving_version = False
        self._version_cache[key] = value
        return value

    # -- systemd 互動 -------------------------------------------------------

    def _client_env(self) -> dict[str, str]:
        """`systemctl` **client 自己**的環境（與 `launcher.launch()` 同一份理由）。

        它不會流進 unit——unit 的環境來自 root-owned 模板檔，job 的環境來自 spec，
        由 shim 在 exec 時整份指定。client 端保留完整 env 是刻意的：polkit 授權可能
        要查呼叫端的 session（`XDG_SESSION_ID` 等），砍掉會讓授權在某些部署下無故失敗。
        """

        from .launcher import _git_scope_env

        return _git_scope_env()

    def _systemctl_stop(self, systemctl: str, unit: str) -> None:
        subprocess.run(
            [systemctl, "stop", "--no-ask-password", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )

    def _stop_and_confirm(self, plan: job_runner.SystemdTemplatePlan) -> bool:
        """發 `systemctl stop` 並確認 unit 真的離開 active（D4）。回傳是否確認離開。"""

        try:
            self._stop_unit(plan.binary, plan.unit)
        except Exception as exc:  # noqa: BLE001 - stop 失敗仍要回報逾時本身
            logger.error(
                "planning-job-stop-failed unit=%s error=%s", plan.unit, type(exc).__name__
            )
        deadline = self._monotonic() + STOP_CONFIRM_TIMEOUT_SECONDS
        while True:
            try:
                if not self._unit_active(plan.binary, plan.unit):
                    return True
            except Exception:  # noqa: BLE001 - 查不動狀態不代表沒停
                return False
            if self._monotonic() >= deadline:
                logger.error(
                    "planning-job-still-active-after-stop unit=%s", plan.unit
                )
                return False
            self._sleep(STOP_CONFIRM_POLL_SECONDS)

    # -- 收割 ---------------------------------------------------------------

    def _read_sentinel(self, sentinel: Path, process) -> int:
        """exit code 的權威是 **Manager 側**記帳 shell 寫下的那一個（#604）。

        job 一個位元組都沒參與：sentinel 落在 job 唯讀的 scratch 那一格，寫者是
        `systemctl` client 外面那層 `bash -c`，跑在 Manager 的 uid 上。
        """

        try:
            raw = sentinel.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        if raw.lstrip("-").isdigit():
            return int(raw)
        code = getattr(process, "returncode", None)
        return int(code) if isinstance(code, int) else 1

    def _read_log(self, log_path: Path) -> str:
        try:
            return log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


@dataclass(frozen=True)
class _Reservation:
    job_id: str
    instance: str
    slot: Path
    cwd: Path
