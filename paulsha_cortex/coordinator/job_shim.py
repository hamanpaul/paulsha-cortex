"""降權 job shim——root-owned 模板 unit（`cortex-job@.service`）的固定 `ExecStart=`。

operator 0816 第三輪裁決 **A+B** 的 B 半：builder job 不再由 Manager 用
`systemd-run` 起 transient unit（polkit 對 transient unit 看不到 `User=`，#603
實測），改成 `systemctl start cortex-job@<instance>.service`——unit 檔 root 擁有、
`User=` 與 `ExecStart=` 都硬寫死，Manager 帳號**選不了 UID、也給不了命令列**。

命令列既然給不了，per-job 的參數就得走一條**帶外通道**：Manager 把一份 spec
原子寫進 Manager-owned spool（登記表資產 `job-spec-spool-<principal>`，該 job 帳號
唯讀），systemd 起 unit、unit exec 本模組、本模組讀 spec 後 exec 真正的 job。

**#657：spool 是 per-principal 的，而本模組因此一行都沒改。** spool 根的唯一來源
是模板 unit 的 `Environment=PSC_JOB_SPEC_SPOOL=`（root-owned，本模組對未設此變數
fail-closed、不猜、不落回預設）——「哪個身分讀哪個 spool」是那份 unit 檔上可逐字
稽核的一行，不是本模組的推導。

## 這支程式在信任鏈上的位置

執行本模組時**降權已經完成**（systemd 依 unit 的 `User=` 切好身分）。因此它
**不是**特權邊界，它是「已經是 builder 的行程，去把自己變成那個 job」的一段
接線。它能做的事 builder 本來就能做；它的價值在於**收斂**：

- spec 檔只從固定的 spool 根、以 `<instance>.json` 推導，**不接受任何路徑參數**；
- 開 spec 與開 log 一律 `O_NOFOLLOW`——即使有人在 spool／log 目錄埋 symlink，
  也只會失敗，不會沿著連結走到別處；
- schema 驗證是**白名單**：spec 裡出現 `user`／`uid`／`group`／`gid` 這類身分欄位
  一律拒絕。身分只有一個來源＝root-owned 的 unit 檔，spec 連提都不准提；
- env 走與 `job_runner` **同一條** `CREDENTIAL_ENV_RE`／`DENIED_ENV_NAMES` 守衛，
  spec 被塞進 token 或 `LD_PRELOAD` 時 fail-closed，而不是照單 exec；
- **`PATH` 兩層都缺時 fail-closed**（#679，見 :func:`resolve_job_env`）：spec 的 env
  沒有 `PATH` 時退回模板 unit 的 `Environment=PATH=`（root-owned，比 spec 更可信），
  兩層都沒有才拒絕 exec——絕不讓 `execvpe` 退回 `os.defpath` 去靜默解系統層那份。

## 為什麼 log 由這裡接管，而不是 unit 的 `StandardOutput=append:`

`file:`／`append:` 的目標檔由 **PID 1（root）在降權之前**開啟。log 路徑必然含有
Manager 帳號可寫的段落，把它交給 root 開啟等於允許 Manager 用一個 symlink 讓 root
對任意檔案 append——那會把「cortex 任何元件永不具 root」這條裁決賣掉。改由本模組在
**已降權之後**用 `O_NOFOLLOW` 開同一個檔，最壞情況只是一個 job 權限的寫入。

## spec 的 `log_path` 指向哪裡（#708）

本模組**只認 spec 給的那一條**，不推導、不猜。而它是不是寫得進去，取決於派工端把它
指到哪裡：在 #708 之前 builder 那條指的是 Manager 的 dispatch log 目錄
（`0700 cortex-manager`、零具名 ACL），於是**每一個** builder job 都死在上面那個
`os.open()`——連一行 log 都寫不出來，**失敗發生在它能記錄失敗之前**。修法在派工端：
三個降權 principal 各有一格由登記表導出的 log spool（`registry.JOB_LOG_SPOOLS`），
Manager 端直接讀這個由 Manager 預建的 canonical job-log surface；exit sentinel／gate
ledger 則留在獨立的 Manager-only control anchor（見
`coordinator/job_workspace.py:prepare_job_log_spool`）。

錯誤處理分兩段（這是刻意的可觀測性設計）：

1. **接管 log 之前**的失敗（instance 名非法、spec 缺席／是 symlink／schema 不合、
   log 開不起來）寫 stderr → 進 journal。此時 Manager 那邊的 log 檔會是空的，但
   `systemctl start --wait` 會以非零收場，Manager 的
   `confirm_template_instance_started()` 因此 fail-closed。
   **#708 起這一族另外留一筆機器可讀紀錄**（:func:`write_shim_error` →
   `<log 那一格>/shim-error.json`，由 `job_runner.read_shim_error()` 撿回錯誤訊息）
   ——journal 只有 operator 登入才讀得到，而 Manager 端在此之前只看得到
   `systemctl exit=1`。唯一仍然只有 journal 的是「連 spec 都讀不到」那一族：那時
   job 對「自己該往哪裡寫」沒有任何可信來源。
2. **接管之後**的失敗與 job 自身的輸出全部進那份 JSONL log，與 direct／
   systemd-run 模式逐字同路徑。
"""
from __future__ import annotations

import json
import os
import stat
import sys
from typing import Mapping, Sequence

from .job_runner import (
    JOB_SPEC_SPOOL_ENV,
    JOB_SPEC_VERSION,
    SPEC_FORBIDDEN_KEYS,
    SPEC_REQUIRED_KEYS,
    forbidden_spec_keys,
    instance_name_valid,
    malformed_job_command,
    reject_unsafe_env,
)

__all__ = [
    "EXIT_SPEC_ERROR",
    "SHIM_ERROR_FILENAME",
    "SHIM_ERROR_SCHEMA",
    "ShimError",
    "forbidden_spec_keys",
    "load_spec",
    "malformed_job_command",
    "main",
    "resolve_job_env",
    "resolve_spec_path",
    "write_shim_error",
]

#: `EX_CONFIG`（sysexits.h）。刻意不用 1：1 是「job 自己失敗」的正常出口，
#: 78 讓 operator 一眼分辨「job 跑了但失敗」與「job 根本沒起來」。
EXIT_SPEC_ERROR = 78

#: shim 在**接管 log 之前**失敗時，留給 Manager 的那一筆機器可讀紀錄的檔名（#708）。
#:
#: 它落在 **job 自己那一格 log spool 目錄裡**（＝`dirname(log_path)`）。這個位置不是
#: 隨手挑的：#708 之後那一格是這個 job 帳號**唯一**由登記表保證寫得進去的落點
#: （`registry.JOB_LOG_SPOOLS`），而「寫不進去」正是本紀錄要描述的那一族失敗之一。
SHIM_ERROR_FILENAME = "shim-error.json"

#: 上述紀錄的 schema 標記。採信端（`job_runner.read_shim_error`）以它辨識版本；
#: 內容只有診斷用途，**不進任何採信路徑**——它由 job 帳號寫，因此可被偽造，
#: 只用來把 operator 的排查從 journal 拉回 Manager 讀得到的地方。
SHIM_ERROR_SCHEMA = "cortex-job-shim-error/1"

#: spec 檔大小上限。spec 帶的是 wrapper script（含 prompt），可以不小，但也不該
#: 無界——讀進一個被灌爆的檔只會讓 job 帳號自己 OOM。
SPEC_MAX_BYTES = 4 * 1024 * 1024


class ShimError(RuntimeError):
    """shim 在 exec 之前判定本次啟動不該發生。一律 fail-closed，絕不猜。"""


def resolve_spec_path(instance: str, spool_root: str) -> str:
    """`<spool_root>/<instance>.json`。**instance 是唯一輸入，且先被驗過形狀。**

    不接受呼叫端給整條路徑：模板 unit 只傳 `%i`，路徑必須由固定的 spool 根推導，
    否則「spec 一定落在 Manager-owned 樹裡」這條性質就不成立了。
    """

    if not instance_name_valid(instance):
        raise ShimError(f"instance 名不合法（只允許 [A-Za-z0-9_.-]）: {instance!r}")
    if not spool_root.startswith("/"):
        raise ShimError(f"spec spool 根必須是絕對路徑: {spool_root!r}")
    return f"{spool_root.rstrip('/')}/{instance}.json"


def _read_regular_file(path: str) -> bytes:
    """以 `O_NOFOLLOW` 讀檔，並要求它是**普通檔**。

    `O_NOFOLLOW` 只擋最後一段是 symlink 的情況；中間目錄仍可能被替換，但那些
    目錄全在 Manager-owned 樹裡（permgen 產出 owner-only ＋ 唯讀 ACL），能改的
    身分本來就能改 spec 內容，沒有額外的提權面。
    """

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise ShimError(f"讀不到 job spec {path}: {exc}") from exc
    try:
        stat_result = os.fstat(fd)
        if not os_path_isreg(stat_result.st_mode):
            raise ShimError(f"job spec 不是普通檔（symlink／fifo／目錄？）: {path}")
        if stat_result.st_size > SPEC_MAX_BYTES:
            raise ShimError(
                f"job spec 超過上限 {SPEC_MAX_BYTES} bytes: {path}（{stat_result.st_size}）"
            )
        return os.read(fd, SPEC_MAX_BYTES)
    finally:
        os.close(fd)


def os_path_isreg(mode: int) -> bool:
    """`stat.S_ISREG` 的等價判定（避免為了一個位元再拉一個 import）。"""

    return (mode & 0o170000) == 0o100000


def load_spec(instance: str, spool_root: str) -> dict[str, object]:
    """讀出並**完整驗證**該 instance 的 spec。任何一項不合即 `ShimError`。"""

    path = resolve_spec_path(instance, spool_root)
    raw = _read_regular_file(path)
    try:
        spec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ShimError(f"job spec 不是合法 UTF-8 JSON: {path}: {exc}") from exc
    if not isinstance(spec, dict):
        raise ShimError(f"job spec 必須是 JSON object: {path}")

    version = spec.get("spec_version")
    if version != JOB_SPEC_VERSION:
        raise ShimError(
            f"job spec 版本不符（期望 {JOB_SPEC_VERSION}，收到 {version!r}）: {path}"
        )
    missing = [key for key in SPEC_REQUIRED_KEYS if key not in spec]
    if missing:
        raise ShimError(f"job spec 缺少必要欄位 {missing}: {path}")
    # 身分與加固剖面欄位是**結構性禁止**，不是「忽略」：spec 一旦被容忍帶 user/uid
    # 或 hardening_profile，日後有人「順手支援一下」就會把 B 案／#643 的整個保證
    # 還回去。掃的是與寫端**同一支** `forbidden_spec_keys()`。
    present_forbidden = forbidden_spec_keys(spec)
    if present_forbidden:
        raise ShimError(
            f"job spec 不得攜帶身分／加固剖面欄位 {present_forbidden}（身分只由 "
            f"root-owned unit 的 User= 決定，剖面只由 executor 決定）: {path}"
        )
    if spec.get("instance") != instance:
        raise ShimError(
            f"job spec 的 instance 與 unit 實例名不符（{spec.get('instance')!r} != "
            f"{instance!r}）: {path}"
        )

    # #687：判準與寫端 `job_runner.build_job_spec()` 走**同一支**
    # `malformed_job_command()`——`argv` 非空、`argv[0]` 非空、每個元素都是 str，
    # 但**其餘元素允許空字串**（`claude --tools ""` 是成文 API，見該函式 docstring）。
    # 這裡不再自寫一份判準：#679 已經買過「同一件事兩份實作會漂移」的單。
    problem = malformed_job_command(spec.get("command"))
    if problem is not None:
        raise ShimError(f"job spec 的 command 不合法（{problem}）: {path}")
    for key in ("working_directory", "log_path"):
        value = spec.get(key)
        if not isinstance(value, str) or not value.startswith("/"):
            raise ShimError(f"job spec 的 {key} 必須是絕對路徑: {path}（{value!r}）")
    env = spec.get("env")
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise ShimError(f"job spec 的 env 必須是 str→str 的 object: {path}")
    # 與 Manager 端寫入時**同一條**守衛：兩邊共用 job_runner 的 pattern／名單，
    # 因此「白名單被改壞」在讀端也一樣會炸，不會只靠寫端自律。
    try:
        reject_unsafe_env(env, source="job_shim.load_spec")
    except ValueError as exc:
        raise ShimError(f"job spec 的 env 命中憑證／注入形狀守衛: {path}: {exc}") from exc
    return spec


#: 出口 proxy 的環境變數名（#716）。**與 `permgen.EGRESS_PROXY_ENV_NAMES` 是成對契約**
#: ——permgen 與 coordinator 刻意不互相 import（既有慣例，見 `DEFAULT_TEMPLATE_UNIT`／
#: `JOB_PATH_ENV_BY_PRINCIPAL`），兩邊由契約測試釘住逐字相等。
EGRESS_PROXY_ENV_NAMES: tuple[str, ...] = (
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
)


def _apply_egress_proxy_env(
    env: "dict[str, str]", environ: Mapping[str, str]
) -> None:
    """把模板 unit 上的 proxy 宣告帶進 job 的環境（#716）。

    ## 為什麼非有這一段不可

    shim 以 `os.execvpe(file, args, env)` **整份換掉**環境——unit 的 `Environment=`
    只到得了 shim，到不了模型。這與 `HOME` 那一條是同一個機制（模板 unit 的註解逐字
    記著「`Environment=HOME=` 到不了模型」），也與 `PATH` 的第二層同一個理由。

    少了這一段，`#716` 的落地會變成一個**看起來很嚴、實際全開**的形態：unit 上有
    `IPAddressDeny=any`、有 `Environment=HTTPS_PROXY=`，而 job 拿到的環境裡一個 proxy
    變數都沒有 ⇒ executor 直連 ⇒ 被核心層擋掉 ⇒ 症狀是「模型 API 逾時」，離原因很遠。
    這正是本 repo 記過八次以上的那一族。

    ## 判準

    來源是**模板 unit**（root-owned，job 帳號改不了）。spec 已經宣告的不覆蓋——
    spec 由 Manager 產生，同樣在授權線內側，而且那是唯一能表達 per-job 例外的地方。

    空字串是**有意義的值**（`NO_PROXY=` 明示清空），因此判斷用 `in`，不是 truthiness。
    """
    for name in EGRESS_PROXY_ENV_NAMES:
        if name in env:
            continue
        if name in environ:
            env[name] = environ[name]


def resolve_job_env(spec: Mapping[str, object], environ: Mapping[str, str]) -> dict[str, str]:
    """spec 的 `env` → job 的**完整**環境，並補上 `PATH` 這一條的第二層（#679）。

    ## 為什麼這裡要有第二層

    `os.execvpe(file, args, env)` 解析 `file` 用的是 **`env` 這個參數裡的 `PATH`**，
    不是本行程的 `os.environ`；`env` 沒有 `PATH` 時退回 `os.defpath`＝`:/bin:/usr/bin`。
    也就是說「spec 的 env 少了 PATH」不會報錯，只會讓 job 解到系統層那份同名 CLI
    ——實機上 `codex` 因此跑的是 0.42.0，而 toolchain（登記表登記的那份）是 0.147.0。

    第二層的來源是**模板 unit 的 `Environment=PATH=`**（#679 同時補上的那一行）：
    root-owned、可逐字稽核、job 帳號改不了。因此這**不是** fail-open——它退回的是比
    spec 更可信的來源，而不是猜一個預設值。它涵蓋兩種現實情況：

    - Manager 端的產生器被繞過（手工組 spec；#645 逐字記錄過的同型前例）；
    - spool 裡還躺著升級前寫的舊 spec。

    兩層**都**沒有時 fail-closed。這一條刻意不落回 `os.defpath`：那正是本票的原症狀，
    而它的失敗模式是「不報錯、只是版本不對」——最難查的那一種。
    """

    env = {str(k): str(v) for k, v in dict(spec["env"]).items()}  # type: ignore[index]
    home = (env.get("HOME") or "").strip()
    if not home:
        raise ShimError(
            "job spec 的 env 沒有 HOME——模板 unit 的 `Environment=HOME=` 到不了模型，"
            "也不得回退到 unit/daemon 的 HOME。請確認 Manager 端已宣告 "
            "PSC_BUILDER_HOME／PSC_REVIEWER_HOME／PSC_GATE_HOME。"
        )
    if not home.startswith("/"):
        raise ShimError("job spec 的 env 裡的 HOME 必須是絕對路徑")
    try:
        stat_result = os.lstat(home)
    except FileNotFoundError:
        stat_result = None
    except OSError as exc:
        raise ShimError("job spec 的 env 裡的 HOME 目前無法判定型態或存取性") from exc
    if stat_result is not None:
        if stat.S_ISLNK(stat_result.st_mode):
            raise ShimError("job spec 的 env 裡的 HOME 不得是 symlink")
        if not stat.S_ISDIR(stat_result.st_mode):
            raise ShimError("job spec 的 env 裡的 HOME 必須指向目錄")
    env["HOME"] = home
    _apply_egress_proxy_env(env, environ)
    if (env.get("PATH") or "").strip():
        return env
    inherited = (environ.get("PATH") or "").strip()
    if not inherited:
        raise ShimError(
            "job spec 的 env 沒有 PATH，模板 unit 也沒有 Environment=PATH=——"
            "execvpe 會退回 os.defpath（:/bin:/usr/bin）並**靜默**解到系統層那份同名 "
            "CLI（實機：codex 0.42.0，而 toolchain 是 0.147.0）。兩層都缺時一律拒絕 "
            "exec：請確認 Manager 端已宣告 PSC_BUILDER_PATH／PSC_REVIEWER_PATH／"
            "PSC_GATE_PATH，且模板 unit 是由現行 permgen 產生的那一份"
            "（`python3 -m paulsha_cortex.trust_root unit four-way --job` 等）。"
        )
    env["PATH"] = inherited
    return env


def write_shim_error(log_path: str, instance: str, message: str) -> bool:
    """把「shim 在接管 log 之前就失敗了」寫成一筆機器可讀紀錄（**best-effort**）。

    ## 為什麼需要這一筆

    `_take_over_stdio()` 之前的失敗只寫 stderr ⇒ 只進 **unit journal**，而 Manager
    帳號讀不到那份 journal。Manager 端看得到的因此只有 `systemctl exit=1`／
    `78/CONFIG`，逐字原因要人登入去翻——#708 的整個排查就是這樣開始的
    （「**失敗發生在它能記錄失敗之前**」）。

    ## 為什麼寫在 `dirname(log_path)`

    #708 之後那是這個 job 帳號由登記表保證寫得進去的一格（`JOB_LOG_SPOOLS`），也是
    Manager 一定讀得到、且**已經知道路徑**的一格（它就是自己建的那一格）。不新開任
    何通道，不需要 job 知道任何額外的路徑。

    ## 幾個刻意的選擇

    - **`O_EXCL`**：只寫第一筆。同一格重跑會由 Manager 側整格重建，因此「已存在」
      只可能是同一輪的第二次失敗，第一筆才是根因。
    - **`O_NOFOLLOW` ＋ 明確 `0644` ＋ `fchmod`**：降權 unit 帶 `UMask=0077`，不補
      `fchmod` 的話 Manager 是目錄 owner 也讀不到檔案內容（#638 缺陷 2）。
    - **全部例外吞掉並回 `False`**：診斷面失敗絕不能變成新的失敗來源，也不能改變
      shim 的退出碼——它的 exit code 是 Manager 的 fail-closed 判準。
    """

    try:
        directory = os.path.dirname(log_path) or "."
        target = os.path.join(directory, SHIM_ERROR_FILENAME)
        payload = json.dumps(
            {
                "schema": SHIM_ERROR_SCHEMA,
                "instance": instance,
                "log_path": log_path,
                "error": message,
            },
            ensure_ascii=False,
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(target, flags, 0o644)
        try:
            os.fchmod(fd, 0o644)
            os.write(fd, (payload + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:  # noqa: BLE001 - 診斷面失敗不得掩蓋上游的真實失敗
        return False


def _take_over_stdio(log_path: str) -> None:
    """把 stdout／stderr 接到 spec 指定的 JSONL log（append、`O_NOFOLLOW`）。

    append 而非 truncate：Manager 在 spawn 前已經把該檔清空並可能寫入
    `systemctl` client 自己的輸出，這裡再截一次會把那段診斷抹掉。
    """

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
    try:
        fd = os.open(log_path, flags, 0o600)
    except OSError as exc:
        raise ShimError(f"開不了 job log {log_path}: {exc}") from exc
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        if fd > 2:
            os.close(fd)


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """模板 unit 的進入點：讀 spec → 接管 log → chdir → exec。**成功時不返回。**"""

    args = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if environ is None else environ
    instance = args[0] if len(args) == 1 else ""
    # spec 一旦讀進來，`log_path` 就是已知的——它同時是「job 那一格 log spool 在哪
    # 裡」。#708 之後那一格是本帳號唯一由登記表保證寫得進去的落點，因此接下來每一種
    # 失敗都留得下一筆 Manager 讀得到的紀錄（見 :func:`write_shim_error`）。
    known_log_path = ""
    try:
        if len(args) != 1:
            raise ShimError(
                f"用法：cortex-job-shim <instance>（unit 應傳 %i）；收到 {args!r}"
            )
        spool_root = (env.get(JOB_SPEC_SPOOL_ENV) or "").strip()
        if not spool_root:
            raise ShimError(
                f"{JOB_SPEC_SPOOL_ENV} 未設定——模板 unit 應以 Environment= 宣告 spec "
                f"spool 根（root-owned unit 檔是這個值唯一的合法來源）"
            )
        spec = load_spec(args[0], spool_root)
        known_log_path = str(spec["log_path"])
        # PATH 的解析刻意在**接管 log 之前**（與 spool 根 fail-closed 同一段）：
        # 這一族失敗代表 job 根本不該起跑，理由要進 journal，而不是進一份 job
        # 自己的 log——那份 log 在 Manager 眼裡與「job 跑了但沒輸出」長得一樣。
        job_env = resolve_job_env(spec, env)
        _take_over_stdio(known_log_path)
        os.chdir(str(spec["working_directory"]))
    except (ShimError, OSError) as exc:
        print(f"cortex-job-shim: {exc}", file=sys.stderr, flush=True)
        # **journal 之外再留一筆**（#708 第 3 項）。stderr 那一行維持不動——它是
        # operator 在機器上的既有入口，本紀錄補的是 **Manager 端**看得到的那一半。
        # 讀不到 spec ⇒ 連 log 落點都不知道 ⇒ 這一族仍然只有 journal（結構如此：
        # 那時 job 對「自己該往哪裡寫」沒有任何可信來源）。
        if known_log_path:
            write_shim_error(known_log_path, instance, str(exc))
        return EXIT_SPEC_ERROR

    command = [str(item) for item in spec["command"]]  # type: ignore[index]
    try:
        # execvpe：同一個 pid 直接變成 job，unit 的 MAINPID 因此就是 job 本身
        # （`systemctl start --wait` 的等待語意與 exit sentinel 都建立在這點上）。
        os.execvpe(command[0], command, job_env)
    except OSError as exc:  # pragma: no cover - exec 成功時不返回
        print(f"cortex-job-shim: exec {command[0]} 失敗: {exc}", file=sys.stderr, flush=True)
        return EXIT_SPEC_ERROR
    return EXIT_SPEC_ERROR  # pragma: no cover - 不可達


if __name__ == "__main__":  # pragma: no cover - 由模板 unit 執行
    raise SystemExit(main())
