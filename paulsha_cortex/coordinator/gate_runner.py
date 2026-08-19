"""#629：以**第四個帳號**執行 operator 宣告的 gate，並由 Manager 落地權威 ledger。

`#604`／PR `#628` 把 gate ledger 與 exit sentinel 的**作者**收斂到 Manager，但刻意
沒做執行面。理由寫在 `launcher._should_run_gates` 的 docstring 裡：operator 宣告的
gate 命令（`PSC_GATE_CMD_*`）是在 **builder 完全掌控內容的工作樹**上執行的，`pytest`
會載入該樹的 `conftest.py` 與 plugin ⇒ **執行者取得任意程式碼執行**。

後果是 `PSC_JOB_RUNNER=systemd-template` 下 build 卡恆因 `require_ledger` 而
`gate-ledger-missing` fail closed——安全，但不能用。本模組補上那一半。

## 執行身分：為什麼是第四個帳號

既有三個帳號逐一檢查，沒有一個能承接（完整論證見
`trust_root.permgen.FOUR_WAY_SCHEME`）：

- `cortex-builder`：模型自證自己的驗收證據，違反 #540 的 acceptance chain；
- `cortex-manager`：持 spawn 授權、擁有全部 Manager-owned durable state，是授權線的
  **內側**；
- `cortex-reviewer-planner`：它是寫 verdict 的那一個——把 gate 掛上去，被攻陷的
  builder 就能經由 gate 執行影響 reviewer 的帳號，等於把 #638／#639 剛關掉的通道
  重新打開。

「不開帳號」的唯一認真候選是 `DynamicUser=yes`，它**結構性地**不成立：本方案兩端
都靠具名 POSIX ACL 授權（`gate-ledger-spool` 給 gate `wx` 無 `r`、`repo-worktree`
給 gate `rX`），而 `setfacl -m u:<名>:…` 在產生權限的當下就要解析得到那個名字；
DynamicUser 的 UID 在 unit 起動前不存在、每次還不同。

## 工作樹：拋棄式副本，不是唯讀

唯讀在可行性上不成立（`pytest` 要寫 `.pytest_cache`／`__pycache__`，`npm test`／
`make` 更是必寫），而副本另外買到「gate 的寫入不污染 builder 交付的樹」與「快照在
單一時點取得，builder 的背景行程改不了跑到一半的樹」。複製由 **gate 自己**執行
（`gate_ledger.snapshot_worktree`）——它是唯一同時讀得到來源（`rX` ACL）、寫得進
目的地（自己的 pool）的身分；Manager 在 #641 之後讀不到 builder 的樹，那條刻意不
回頭放寬。

## 產出落點：spool，不是直接寫 `gate-ledger`

#628 的採信端以 `terminal_contract.foreign_evidence_author()` 檢查檔案擁有者，**非
Manager 產生的 ledger 一律不採信**；而 `gate-ledger` 這個資產同時是 exit sentinel
的落點，開放寫入面等於讓一個跑 untrusted code 的帳號能偽造任何 job 的完成狀態。

因此 gate 寫的是 `gate-ledger-spool` 的自己那一格（`wx` 無 `r`，per-job，pre-seed
守衛與封口共用 `spool_slot`，不另寫一份），Manager 讀它、**以不受信任輸入驗證**
（schema 嚴格、gate 名稱必須落在 operator 的宣告集合內），然後**自己重寫一份**權威
ledger 到 `gate-ledger`。作者歸屬與 #628 逐字不變。

## 誠實邊界

本模組買到的是**圍堵**，不是「ledger 不可偽造」。取得任意程式碼執行的 builder 依然
能影響**自己那一份** ledger 的內容——那是「執行對方交出來的測試碼」這件事本身的
性質，換誰執行都一樣，唯一的替代是不跑 gate。它買到的是：那個執行碰不到 Manager 的
durable state、碰不到 verdict 通道、碰不到別的 job 的工作樹、也沒有 spawn 授權。
spec §R3 有對應的風險段落。

## `direct` 模式零回歸

`PSC_JOB_RUNNER=direct`（現行預設）下 builder 與 Manager 同 UID，「第四個身分」沒有
任何邊界可言。本模組因此在 direct 模式下**逐字沿用既有行為**：就地呼叫
`gate_ledger.write_gate_ledger()`，與 `work_actions._regenerate_gates_action` 改動前
做的事完全相同。兩種模式共用**同一個進入點**（:func:`run_declared_gates`），差別
只在那一個分支——這正是 #629 要求「`_regenerate_gates_action` 一併收斂」的形狀。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

from ..config import paths
from . import gate_ledger, job_runner, spool_slot, terminal_contract


#: gate 執行身分用來跑 `gate_ledger` 的直譯器。降權模式下 Manager 的 repo root 在
#: `/home` 底下，而 job unit 帶 `ProtectHome=yes`——`PYTHONPATH=<repo>` 那條路在那裡
#: 不成立（#623 缺口 1 的同一件事）。可行的是**部署樹裡的 venv**：`/opt/cortex`
#: root-owned `0755`，對每個 job 帳號唯讀＋可執行，`ProtectSystem=strict` 讓它唯讀
#: 但讀得到。
GATE_PYTHON_ENV = "PSC_GATE_PYTHON"
DEFAULT_GATE_PYTHON = "/opt/cortex/venv/bin/python3"

#: gate 那一格裡的 ledger 檔名（目錄本身以 spool key 定址）。
GATE_LEDGER_FILENAME = "ledger.json"

#: gate unit 的 stdout／stderr 落點的檔名。**診斷用，不是證據**：權威結論在 ledger
#: 裡，每個 gate 的輸出尾段由 `gate_ledger.run_gates()` 放進 `detail`。
#:
#: **#708 起它落在自己的一格**（登記表資產 `gate-job-log-spool`，
#: `<gate-ledger-spool>/gate-logs/<key>/`），不再與 ledger 共用那一格。理由不是潔癖，
#: 是**它原本 Manager 讀不到**：舊路徑下這個檔由 gate job 自己建（shim 的
#: `O_CREAT`），帶降權 unit 的 `UMask=0077` ⇒ `0600 cortex-gate`，而 Manager 只是
#: **目錄**的 owner，那不給檔案內容的讀取權（#638 缺陷 2 的同一個機制）。結果是
#: gate 失敗時逐字原因只存在於一個 Manager 讀不到的檔裡。改由 Manager 預先建立
#: （`spool_slot.prepare_job_log()`，mode `0620`）之後，`_run_as_gate_identity()`
#: 的錯誤訊息才帶得出 job 端的 tail。
GATE_LOG_FILENAME = "gate.log"

#: ledger 每一列 `detail` 的上限（與 `gate_ledger.run_gates` 的 `[-2000:]` 同數量級）。
#: spool 內容是**不受信任輸入**：一個被攻陷的 gate 可以塞進數百 MB 的 detail，而那
#: 份 payload 會進 Manager 的 durable evidence 樹。
MAX_DETAIL_CHARS = 2000

#: 一份 ledger 最多幾列。同上：宣告了 N 個 gate 就只該有 N 列（外加宣告失敗那一
#: 列），多出來的一律是 spool 被塞了東西。
MAX_LEDGER_ROWS = 64


class GateRunnerError(RuntimeError):
    """gate 執行面失敗。`reason` 是機器可讀的診斷碼，與採信端的錯誤碼同一套風格。"""

    def __init__(self, reason: str, message: str, **context: object) -> None:
        super().__init__(message)
        self.reason = reason
        self.context = dict(context)


# ---------------------------------------------------------------------------
# 路徑（與 R1 登記表的 path 契約成對）
# ---------------------------------------------------------------------------

def gate_ledger_spool_root(coordinator_root: str | Path | None = None) -> Path:
    """`<coordinator_root>/gate-ledger-spool/`（登記表資產 `gate-ledger-spool`）。

    接受顯式 root 的理由與 `job_workspace.commit_spool_root()` 逐字相同：起 gate 與
    消費 spool 兩端都可能拿到呼叫端傳下來的 root，一律回頭讀 env 會讓同一個 job 的
    兩端指到不同的樹。
    """

    if coordinator_root is None:
        return paths.gate_ledger_spool_root()
    return Path(coordinator_root) / paths.GATE_LEDGER_SPOOL_DIRNAME


def gate_ledger_spool_dir(
    *, spool_key: str, coordinator_root: str | Path | None = None
) -> Path:
    """單一 job 的 gate spool 目錄（唯一定址點）。"""

    _validate_spool_key(spool_key)
    return gate_ledger_spool_root(coordinator_root).resolve() / spool_key


def gate_spool_ledger_path(
    *, spool_key: str, coordinator_root: str | Path | None = None
) -> Path:
    """gate 寫、Manager 讀的那一個檔。"""

    return (
        gate_ledger_spool_dir(spool_key=spool_key, coordinator_root=coordinator_root)
        / GATE_LEDGER_FILENAME
    )


def gate_job_log_spool_root(coordinator_root: str | Path | None = None) -> Path:
    """`<gate-ledger-spool>/gate-logs/`（登記表資產 `gate-job-log-spool`，#708）。

    掛在 gate **既有**的輸出通道底下，因此 `permgen.read_write_paths()` 的
    `_minimize()` 把它吃掉——`cortex-gate-job@.service` 的 `ReadWritePaths=` 逐字
    不變、default ACL 自動繼承、零部署動作。
    """

    if coordinator_root is None:
        return paths.job_log_spool_root("gate")
    return (
        Path(coordinator_root)
        / paths.GATE_LEDGER_SPOOL_DIRNAME
        / paths.GATE_JOB_LOG_SPOOL_DIRNAME
    )


def gate_job_log_path(
    *, spool_key: str, coordinator_root: str | Path | None = None
) -> Path:
    """gate job 的 log 檔（Manager 預建、gate 以 `O_APPEND` 接管的那一個）。"""

    _validate_spool_key(spool_key)
    root = gate_job_log_spool_root(coordinator_root).resolve()
    return root / spool_key / GATE_LOG_FILENAME


def prepare_gate_job_log(
    *, spool_key: str, coordinator_root: str | Path | None = None
) -> Path:
    """建出 gate 的 log 一格並由 Manager 預先建檔；回傳 log 路徑（#708）。

    與另外兩個 principal 共用 `spool_slot.prepare_job_log()`——三格的差別只在
    「掛在哪一條既有通道底下」，那件事由 `registry.JOB_LOG_SPOOLS` 一張表決定。
    """

    log_path = gate_job_log_path(
        spool_key=spool_key, coordinator_root=coordinator_root
    )
    try:
        return spool_slot.prepare_job_log(log_path.parent, log_path)
    except (spool_slot.SpoolSlotError, OSError) as exc:
        raise GateRunnerError(
            "gate-job-log-unavailable",
            f"gate job log slot unavailable: {log_path.parent}: {exc}",
            spool_dir=str(log_path.parent),
        ) from exc


def gate_worktree_dir(
    *, spool_key: str, gate_worktree_root: str | Path | None = None
) -> Path:
    """gate 的拋棄式副本落點（登記表資產 `gate-worktree-pool` 底下一格）。"""

    _validate_spool_key(spool_key)
    root = (
        paths.gate_worktree_root()
        if gate_worktree_root is None
        else Path(gate_worktree_root)
    )
    return root / spool_key


def _validate_spool_key(spool_key: str) -> None:
    """key 會被接成絕對路徑並嵌進 spec，形狀在**組路徑之前**就驗。

    判準刻意複用 `job_workspace.job_segment_valid()`：gate 的 spool key 與 builder
    的 spool key／worktree 目錄名是**同一個字串**（`Path(log_path).stem`），兩邊用
    不同的判準就會出現「builder 那格建得起來、gate 這格建不起來」的錯位。
    """

    from . import job_workspace

    if not isinstance(spool_key, str) or not job_workspace.job_segment_valid(spool_key):
        raise GateRunnerError(
            "gate-spool-key-invalid",
            f"unsafe gate spool key: {spool_key!r}",
            spool_key=str(spool_key),
        )


def spool_key_for_job(job: Mapping[str, object]) -> str | None:
    """該 job 的 gate spool key。**推導規則只有一條**，且與 commit spool 共用。

    `job_workspace.spool_key_for_job()` 是那一條規則的唯一實作（`Path(log_path).stem`
    ＝ `launcher.launch()` 收到的 `slice_id`，也就是 exit sentinel 與 gate ledger 的
    定址基準）。這裡直接委派而不是重寫，理由與該函式 docstring 的「必須是單一規則」
    逐字相同——各自猜 key 的失敗形態是「找不到 → 靜默不回收」。
    """

    from . import job_workspace

    return job_workspace.spool_key_for_job(job)


# ---------------------------------------------------------------------------
# gate 執行
# ---------------------------------------------------------------------------

def resolve_gate_python(env: Mapping[str, str]) -> str:
    """gate unit 用來跑 `gate_ledger` 的直譯器絕對路徑。"""

    raw = str(env.get(GATE_PYTHON_ENV, "") or "").strip() or DEFAULT_GATE_PYTHON
    if not raw.startswith("/"):
        raise GateRunnerError(
            "gate-python-not-absolute",
            f"{GATE_PYTHON_ENV} 必須是絕對路徑（gate unit 沒有 Manager 的 PATH），收到 {raw!r}",
            requested=raw,
        )
    return raw


def build_gate_argv(
    *,
    python: str,
    ledger_out: str | Path,
    snapshot: str | Path,
    source_worktree: str | Path,
) -> list[str]:
    """gate unit 實際執行的 argv。**封閉：沒有任何來自 job 的可變輸入。**

    要跑哪些命令**不在這條 argv 上**——它們由 operator 的 `PSC_GATE_CMD_*` 宣告，
    經 `job_runner.gate_declaration_env()` 進到 unit 的環境，再由
    `gate_ledger.load_gate_specs()` 解析。這是刻意的：讓 Manager 把命令展開到 argv
    上會產生第二份真實來源，而 `gate_evidence_name_hint()`／`gate_scope_honesty_hint()`
    給 builder 看的 prompt 用的是第一份。

    `--publish`：那一格是 `wx` 無 `r`，檔由 gate 的 uid 建立、unit 帶 `UMask=0077`，
    Manager 是**目錄**的 owner 但那不給檔案內容的讀取權（#638 缺陷 2）。
    """

    return [
        str(python),
        "-m",
        "paulsha_cortex.coordinator.gate_ledger",
        "--out",
        str(ledger_out),
        "--worktree",
        str(snapshot),
        "--snapshot-from",
        str(source_worktree),
        "--publish",
    ]


def prepare_gate_spool(
    *, spool_key: str, coordinator_root: str | Path | None = None
) -> Path:
    """起 gate 之前建立那一格，回傳 ledger 應該落地的路徑。

    生命週期整條走 :mod:`spool_slot`（#639 已有這套語意，本模組不另寫一份）：

    - **`reset=True`**：同一個 key 會被重跑（retry 用同一個 slice_id、
      `regenerate-gates` 更是明著重跑），上一輪封存過的那一格必須重新開封，而
      `spool_slot.create_slot()` 的解封做法是**整格重建**而不是 `chmod` 回去。
    - **pre-seed 守衛**：重建同時把任何預埋的 `ledger.json` 刪掉。**這比「已存在
      即拒絕」更強**——預埋的人得到的不是拒絕，而是自己的檔案被刪掉；而 Manager 是
      這一格的 owner，刪得掉 gate（或任何人）寫的檔。builder 更是連容器都
      traverse 不進來（`0700 cortex-manager` ＋ 只給 gate 一條 `wx` 具名條目）。
    - **不傳明確 mode**：在帶 default ACL 的樹上 `mkdir(mode=…)` 會把 mask 一起
      重設，把 gate 繼承來的具名條目壓成 `#effective:---`（#638 缺陷 1）。
    """

    spool_dir = gate_ledger_spool_dir(
        spool_key=spool_key, coordinator_root=coordinator_root
    )
    try:
        spool_slot.create_slot(spool_dir, reset=True)
    except spool_slot.SpoolSlotError as exc:
        raise GateRunnerError(
            "gate-spool-unavailable",
            f"gate ledger spool slot unavailable: {spool_dir}: {exc}",
            spool_dir=str(spool_dir),
        ) from exc
    return spool_dir / GATE_LEDGER_FILENAME


def seal_gate_spool(ledger_file: str | Path) -> None:
    """消費完成後把那一格封口（best-effort，與兩個既有 spool 同一支實作）。

    封的是**目錄**：ledger 由 gate 的 uid 建立，Manager `chmod` 不了它（#638 缺陷
    3）；但 Manager 是目錄的 owner，收掉目錄的 `w` 之後那一格再也建不了、改不了名、
    刪不掉任何檔，`chmod` 同時把 ACL mask 收成 `---`，gate 具名條目的授權一併失效。

    失敗不得讓一次**已經成功**的消費反而失敗：權威副本此時已經落在 `gate-ledger`。
    """

    spool_slot.seal_slot(Path(ledger_file).parent)


def read_gate_spool(ledger_file: str | Path, *, env: Mapping[str, str]) -> dict[str, Any]:
    """讀回 gate 交付的 ledger 並**以不受信任輸入驗證**，回傳正規化後的 payload。

    寫這份檔的身分正在執行 builder 交出來的程式碼；它可以被攻陷。因此這裡逐項驗
    形狀，並把 gate 名稱收斂到 operator 的宣告集合內——一個被攻陷的 gate 可以把
    `status` 全填 `passed`（那是圍堵買不到的東西，見模組 docstring 的誠實邊界），
    但**不能**發明一個 operator 沒宣告過的 gate 名混進 ledger、不能塞進無界的
    payload、也不能讓 ledger 的 `slice_id` 指到別的 job。
    """

    path = Path(ledger_file)
    if path.is_symlink():
        raise GateRunnerError(
            "gate-spool-unsafe", f"gate spool ledger is a symlink: {path}", path=str(path)
        )
    if not path.is_file():
        raise GateRunnerError(
            "gate-spool-empty",
            f"gate 執行結束但沒有交付 ledger：{path}"
            "（快照失敗／直譯器不存在／unit 起不來時皆為此形狀，"
            "診斷見 `journalctl -u <gate unit>` 與同一格的 gate.log）",
            path=str(path),
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GateRunnerError(
            "gate-spool-unreadable", f"gate spool ledger unreadable: {path}: {exc}",
            path=str(path),
        ) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GateRunnerError(
            "gate-spool-invalid", f"gate spool ledger 不是合法 JSON: {exc}", path=str(path)
        ) from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("kind") != terminal_contract.GATE_LEDGER_KIND
        or type(payload.get("schema_version")) is not int
        or not isinstance(payload.get("gates"), list)
    ):
        raise GateRunnerError(
            "gate-spool-invalid", f"gate spool ledger 形狀非法: {path}", path=str(path)
        )
    rows = payload["gates"]
    if len(rows) > MAX_LEDGER_ROWS:
        raise GateRunnerError(
            "gate-spool-invalid",
            f"gate spool ledger 有 {len(rows)} 列，超過上限 {MAX_LEDGER_ROWS}",
            path=str(path),
        )
    allowed = set(gate_ledger.ledger_gate_names(env)) | {gate_ledger.GATE_SPEC_FAILURE_NAME}
    normalized: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise GateRunnerError(
                "gate-spool-invalid", "gate spool ledger gates 項目必須為物件", path=str(path)
            )
        name = row.get("name")
        if not isinstance(name, str) or name not in allowed:
            raise GateRunnerError(
                "gate-spool-unknown-gate",
                f"gate spool ledger 出現未宣告的 gate 名稱 {name!r}"
                f"（operator 宣告的是 {sorted(allowed)}）",
                path=str(path),
                name=str(name),
            )
        exit_code = row.get("exit_code")
        if type(exit_code) is not int:
            raise GateRunnerError(
                "gate-spool-invalid",
                f"gate {name!r} 的 exit_code 必須是整數", path=str(path),
            )
        detail = row.get("detail")
        normalized.append(
            {
                "name": name,
                "command": str(row.get("command", ""))[:MAX_DETAIL_CHARS],
                "exit_code": exit_code,
                # **status 由 exit_code 重算，不採信 spool 自報的那一欄。** 這與
                # `terminal_contract._ledger_outcomes()` 是同一條紀律（ledger 自身
                # 矛盾＝記了非 0 卻標 passed，一律不採信），只是往上游多做一次：
                # 這樣 Manager 落地的權威 ledger 本身就沒有那種矛盾可言。
                "status": "passed" if exit_code == 0 else "failed",
                "detail": (detail if isinstance(detail, str) else "")[-MAX_DETAIL_CHARS:],
            }
        )
    return gate_ledger.build_ledger(normalized, slice_id=str(env.get("PSC_SLICE_ID", "")))


def run_declared_gates(
    *,
    job_id: str,
    spool_key: str,
    ledger_path: str | Path,
    worktree: str | Path,
    env: Mapping[str, str] | None = None,
    coordinator_root: str | Path | None = None,
    runner: Any | None = None,
) -> dict[str, Any]:
    """執行 operator 宣告的 gate 並讓 **Manager** 落地權威 ledger，回傳 payload。

    這是 #629 的**單一進入點**：自動路徑（`manager.terminalize_workflow_job`）與
    operator 明著要求的 `regenerate-gates` 走的是同一支，因此不會出現「自動的那條
    降權了、手動的那條還在 Manager 進程裡跑」這種半套狀態——那正是 #629 本文點名
    `work_actions._regenerate_gates_action` 要一併收斂的原因。

    模式分支只有一個，而且在最上面：

    - `direct`（現行預設）：builder 與 Manager 同 UID，第四個身分沒有任何邊界可言。
      逐字沿用既有行為（就地 `write_gate_ledger`），**零回歸**。
    - `systemd-run`／`systemd-template`：以 `cortex-gate` 起一份 root-owned 模板
      unit 執行，產出經 spool 回到 Manager 手上。
    """

    source = dict(os.environ if env is None else env)
    mode = job_runner.resolve_runner_mode(source)
    if mode == job_runner.RUNNER_DIRECT:
        return gate_ledger.write_gate_ledger(
            ledger_path=ledger_path, worktree=worktree, env=source
        )
    return _run_as_gate_identity(
        job_id=job_id,
        spool_key=spool_key,
        ledger_path=ledger_path,
        worktree=worktree,
        env=source,
        coordinator_root=coordinator_root,
        runner=runner,
    )


def _gate_log_tail(log_path: Path, *, limit: int = 2000) -> str:
    """gate job 端 log 的尾段（診斷用）。**#708 之後 Manager 才讀得到它。**

    讀不到就回空字串——診斷用的補充資訊不該反過來變成新的失敗來源（與
    `job_runner._log_tail()` 逐字同一條原則）。
    """

    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    return f"gate job log 尾段: {text[-limit:]}\n"


def _run_as_gate_identity(
    *,
    job_id: str,
    spool_key: str,
    ledger_path: str | Path,
    worktree: str | Path,
    env: Mapping[str, str],
    coordinator_root: str | Path | None,
    runner: Any | None,
) -> dict[str, Any]:
    """降權模式的實作：起 `cortex-gate-job@<instance>.service`，消費 spool，落地。"""

    resolved_worktree = Path(worktree)
    if not resolved_worktree.is_dir():
        raise GateRunnerError(
            "gate-worktree-missing",
            f"被驗的工作樹不存在：{resolved_worktree}",
            worktree=str(resolved_worktree),
        )
    # 前置物（模板 unit／shim／spec spool／帳號）任一缺席都在**任何副作用之前**
    # fail-closed；`executor=None` 是 gate 角色的契約（剖面由 operator 平面決定）。
    plan = job_runner.prepare_systemd_template(
        env, job_id=job_id, executor=None, role=job_runner.JOB_ROLE_GATE
    )
    spool_ledger = prepare_gate_spool(
        spool_key=spool_key, coordinator_root=coordinator_root
    )
    # #708：log 走自己那一格，且**由 Manager 預先建檔**——舊路徑（與 ledger 同格、
    # 由 job 自己建）下這個檔是 `0600 cortex-gate`，Manager 讀不到，於是 gate 失敗時
    # 逐字原因只存在於一個看不見的檔裡。
    gate_log = prepare_gate_job_log(
        spool_key=spool_key, coordinator_root=coordinator_root
    )
    snapshot = gate_worktree_dir(spool_key=spool_key)
    gate_env = job_runner.build_job_env(
        manager_env=env,
        job_id=job_id,
        slice_id=str(env.get("PSC_SLICE_ID") or spool_key),
        repo_root=str(env.get("PSC_REPO_ROOT") or "/opt/cortex"),
        role=job_runner.JOB_ROLE_GATE,
    )
    argv = build_gate_argv(
        python=resolve_gate_python(env),
        ledger_out=spool_ledger,
        snapshot=snapshot,
        source_worktree=resolved_worktree,
    )
    spec = job_runner.build_job_spec(
        job_id=job_id,
        instance=plan.instance,
        unit=plan.unit,
        command=argv,
        # cwd 是 gate 自己的 pool 根，不是被驗的樹：那棵樹對 gate 只有 `rX`，而
        # 副本在 unit 起動的當下還不存在（是 gate 的第一個動作才建的）。
        working_directory=str(paths.gate_worktree_root()),
        log_path=str(gate_log),
        env=gate_env,
    )
    job_runner.write_job_spec(plan.spec_path, spec, account=plan.account)
    start = job_runner.build_systemctl_start_argv(
        systemctl=plan.binary, unit=plan.unit
    )
    execute = subprocess.run if runner is None else runner
    completed = execute(start, capture_output=True, text=True, check=False)
    returncode = int(getattr(completed, "returncode", 1))
    try:
        payload = read_gate_spool(spool_ledger, env=env)
    except GateRunnerError as exc:
        # unit 起不來（polkit 拒絕、模板未安裝、shim 讀不到 spec）與「跑了但沒交付」
        # 在 spool 端是同一個形狀，因此把 client 的 exit code 與 stderr 帶進錯誤裡
        # ——少了它，operator 看到的只有「沒有 ledger」，指不出真正的原因（#643 的
        # 教訓：症狀是空輸出的失敗最難查）。
        raise GateRunnerError(
            exc.reason,
            f"{exc}\n"
            f"gate unit={plan.unit} account={plan.account} "
            f"profile={plan.hardening_profile} systemctl_exit={returncode}"
            # #708：shim 在接管 log 之前的失敗只進 journal，Manager 讀不到；那一族
            # 現在另外留一筆機器可讀紀錄在 job 自己那一格 log spool 裡。
            f"{job_runner.read_shim_error(str(gate_log))}\n"
            f"{_gate_log_tail(gate_log)}"
            f"{(getattr(completed, 'stderr', '') or '').strip()[-2000:]}",
            **{**exc.context, "unit": plan.unit, "systemctl_exit": returncode},
        ) from exc
    # **權威 ledger 由 Manager 自己寫**——#628 的 `foreign_evidence_author()` 檢查的
    # 就是這個檔的擁有者。gate 交付的那一份留在 spool 裡封起來，不進採信路徑。
    gate_ledger.write_ledger_payload(ledger_path, payload)
    seal_gate_spool(spool_ledger)
    # log 那一格同樣封口（best-effort，語意與 ledger 那一格逐字相同）：已經被判讀過
    # 的診斷不該再被追寫。封的是**目錄**——Manager 是它的 owner，收掉 `w` 之後 gate
    # 具名條目的 `wx` 隨 ACL mask 一併失效。
    spool_slot.seal_slot(gate_log.parent)
    return payload


# ---------------------------------------------------------------------------
# 自動路徑：terminalize 之前補上缺席的 ledger
# ---------------------------------------------------------------------------

def ensure_gate_ledger(
    job: Mapping[str, object],
    *,
    phases: frozenset[str],
    env: Mapping[str, str] | None = None,
    coordinator_root: str | Path | None = None,
    runner: Any | None = None,
) -> dict[str, Any] | None:
    """降權模式下，在採信之前把缺席的 gate ledger 補上；回傳 payload 或 `None`。

    `None` 代表「這一輪不該由本模組動手」，四種情況：

    1. **`direct` 模式**——ledger 由 job wrapper 在模型結束後寫，本模組不介入。
       這條是「零回歸」的來源：direct 下的行為與 #629 之前逐字相同。
    2. **phase 不在 `phases` 內**——`plan`／`review` 不跑 gate（planner 與 reviewer
       都不改 candidate），判準由呼叫端給，本模組不自己發明一份。
    3. **ledger 已經存在**——重跑會覆蓋一份已經被採信路徑讀過的證據。要重跑是
       operator 的明示決定，走 `regenerate-gates`。
    4. **推導不出 spool key／工作樹已不在**——沒有可驗的東西，維持 `require_ledger`
       的既有 fail-closed（缺 ledger ⇒ 拒），而不是產生一份空的假裝驗過。

    **失敗一律往上拋**（`GateRunnerError`／`JobRunnerError`）：gate 跑不起來時最壞的
    行為是靜默略過——那會讓 `require_ledger` 看到「沒有 ledger」而以一個指不出原因
    的錯誤收場。呼叫端負責把它翻成可操作的診斷。
    """

    source = dict(os.environ if env is None else env)
    if job_runner.resolve_runner_mode(source) == job_runner.RUNNER_DIRECT:
        return None
    if job.get("workflow_phase") not in phases:
        return None
    log_path = job.get("log_path")
    if not isinstance(log_path, str) or not log_path:
        return None
    ledger_path = terminal_contract.gate_ledger_path(log_path)
    if Path(ledger_path).exists():
        return None
    spool_key = spool_key_for_job(job)
    if spool_key is None:
        return None
    worktree = job.get("worktree")
    if not isinstance(worktree, str) or not Path(worktree).is_dir():
        return None
    job_id = str(job.get("job_id") or spool_key)
    return run_declared_gates(
        job_id=job_id,
        spool_key=spool_key,
        ledger_path=ledger_path,
        worktree=worktree,
        env=source,
        coordinator_root=coordinator_root,
        runner=runner,
    )


__all__ = [
    "DEFAULT_GATE_PYTHON",
    "GATE_LEDGER_FILENAME",
    "GATE_LOG_FILENAME",
    "gate_job_log_path",
    "gate_job_log_spool_root",
    "prepare_gate_job_log",
    "GATE_PYTHON_ENV",
    "GateRunnerError",
    "MAX_DETAIL_CHARS",
    "MAX_LEDGER_ROWS",
    "build_gate_argv",
    "ensure_gate_ledger",
    "gate_ledger_spool_dir",
    "gate_ledger_spool_root",
    "gate_spool_ledger_path",
    "gate_worktree_dir",
    "prepare_gate_spool",
    "read_gate_spool",
    "resolve_gate_python",
    "run_declared_gates",
    "seal_gate_spool",
    "spool_key_for_job",
]
