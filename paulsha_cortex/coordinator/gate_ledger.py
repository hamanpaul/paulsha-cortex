"""#261：gate ledger writer——由 manager 掌控的 wrapper 執行，不由模型自述。

R2 的重驗只有在「被驗的東西不是模型講的話」時才有意義。本模組因此刻意**不**接受
任何來自模型輸出的資料：gate 清單由 operator 以 ``PSC_GATE_CMD_<NAME>`` 環境變數
宣告（與既有 ``PSC_PREFLIGHT_CMD`` 同一套 typed-argv 規範），命令由
:mod:`paulsha_cortex.coordinator.launcher` 產生的 wrapper script 在模型行程結束後
執行，exit code 由 shell／``subprocess`` 產生。模型既不能選擇要跑哪些 gate，也不能
決定 exit code，更不能在自己結束之後改寫 ledger。

wrapper 的形狀（見 ``launcher.build_wrapper_script``）：

.. code-block:: text

    <model argv>; printf %s "$?" > <sentinel>; python3 -m ...gate_ledger --out <ledger> ...

因為 ledger 是在模型結束**之後**才產生的，terminal envelope 內的 ``gate_evidence``
只能是模型「自述跑了哪些 gate、結果如何」的宣告；manager 用本模組獨立產生的 ledger
去對照那份宣告，任何不一致都是 R2 定義的矛盾。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import spool_slot, terminal_contract


# operator 宣告 gate 的環境變數前綴；``PSC_GATE_CMD_PYTEST`` → gate ``pytest``。
GATE_ENV_PREFIX = "PSC_GATE_CMD_"

# operator 宣告本身不合法時，:func:`main` 仍會寫出一份只含這一項（failed）的
# ledger。它是「宣告壞掉」這個事實在 ledger 裡的 canonical 名稱，因此
# :func:`gate_evidence_name_hint` 也用同一個常數，不另外寫死字串。
GATE_SPEC_FAILURE_NAME = "gate-spec"

# 單一 gate 的預設逾時（秒）。逾時視同失敗，不得讓 job 無限期掛住。
DEFAULT_GATE_TIMEOUT_SECONDS = 1800
GATE_TIMEOUT_ENV = "PSC_GATE_TIMEOUT"

# 與 preflight._validate_typed_command 相同的紀律：不接受 shell wrapper，
# 避免 `bash -c "..."` 把任意字串重新變成可注入的 shell 片段。
_SHELL_EXECUTABLES = frozenset({"bash", "sh", "dash", "zsh", "ksh", "fish"})


class GateSpecError(ValueError):
    """gate 宣告本身不合法（operator 設定錯誤，不是模型的錯）。"""


@dataclass(frozen=True)
class GateSpec:
    """一個確定性 gate 的宣告。"""

    name: str
    argv: tuple[str, ...]

    @property
    def command(self) -> str:
        return shlex.join(self.argv)


def _validate_typed_command(name: str, command: Sequence[str]) -> None:
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise GateSpecError(f"gate {name!r} 命令必須是非空 typed argv")
    index = 0
    if Path(command[0]).name == "env":
        index = 1
        while index < len(command) and (
            command[index].startswith("-") or "=" in command[index]
        ):
            index += 1
    if index < len(command) and Path(command[index]).name in _SHELL_EXECUTABLES:
        if "-c" in command[index + 1 :]:
            raise GateSpecError(f"gate {name!r} 不允許 shell wrapper")


def load_gate_specs(env: Mapping[str, str] | None = None) -> tuple[GateSpec, ...]:
    """從環境變數讀出 operator 宣告的 gate 清單（依 gate 名排序，確保確定性）。"""

    source = os.environ if env is None else env
    specs: list[GateSpec] = []
    for key in sorted(source):
        if not key.startswith(GATE_ENV_PREFIX):
            continue
        name = key[len(GATE_ENV_PREFIX) :].strip().lower()
        if not name:
            raise GateSpecError(f"gate 環境變數缺少名稱：{key}")
        raw = str(source[key]).strip()
        if not raw:
            # 空值等同「未宣告」，讓 operator 可以用空字串暫時停用某個 gate。
            continue
        try:
            argv = tuple(shlex.split(raw))
        except ValueError as exc:
            raise GateSpecError(f"gate {name!r} 命令無法解析") from exc
        _validate_typed_command(name, argv)
        specs.append(GateSpec(name=name, argv=argv))
    return tuple(specs)


def declared_gate_names(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """operator 宣告出的 canonical gate 名稱集合（依名稱排序）。

    :func:`load_gate_specs` 是 ledger 內 ``gates[].name`` 的唯一產生處，因此這裡
    直接由它導出名稱，任何需要「ledger 會有哪些 gate 名」的呼叫端都不得自己重新
    解析 ``PSC_GATE_CMD_*``（#540：dispatch prompt 就是因為沒有這個導出，只能讓
    模型自由發明 gate 名稱）。宣告不合法時照樣往上拋 :class:`GateSpecError`——
    「宣告壞了」與「沒有宣告」是兩件事，呼叫端必須能分辨。
    """

    return tuple(spec.name for spec in load_gate_specs(env))


def ledger_gate_names(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """ledger 實際會出現的 gate 名稱；宣告不合法時為 ``(GATE_SPEC_FAILURE_NAME,)``。

    與 :func:`declared_gate_names` 的差別只在錯誤處理：需要「ledger 會長什麼樣」
    的呼叫端（dispatch prompt）不該因 operator 設定錯誤而炸掉派工——那張卡本來
    就會 fail closed，prompt 只要照實反映 ledger 會有的名稱即可。需要分辨
    「宣告壞掉」與「沒有宣告」的呼叫端（doctor）用 :func:`declared_gate_names`。
    """

    try:
        return declared_gate_names(env)
    except GateSpecError:
        return (GATE_SPEC_FAILURE_NAME,)


def card_requires_gate_evidence(test_policy: str | None) -> bool:
    """這張卡的契約要不要求**模型自己**交出確定性 gate 結果（#721）。

    判準轉呼叫 harvest 端唯一的權威導出
    :func:`terminal_contract.expected_gate_names_for_test_policy`——它回空集合就是
    「這張卡本就不要求測試」（``None``／``"none"``，例如 worktree-isolation 這種
    不動 candidate 程式碼的卡），回非空就是「這張卡的正確產出需要跑測試」。
    **dispatch 端不得在 prompt 上另寫第二份判準**：#540 已經把 gate *名稱*從手寫
    改成機械產生，#721 是同一個錯誤的另一半——名字機械化了，**適用範圍**沒有，
    於是 ``test_policy="none"`` 的卡仍被 prompt 告知「Manager 會重跑 pytest 並用
    它判你的 passed」，模型只能去跑 pytest，在 ``-s read-only`` 沙箱下必死。

    **這裡刻意只當布林用，不拿回傳的名稱集合去 ∩ ledger 的 gate 名稱。**
    ``expected_gate_names_for_test_policy`` 回的是「**測試**這一個訊號」
    （:data:`terminal_contract.RED_REQUIRED_TEST_GATE_NAME`），不是「這張卡會被
    判哪些 gate」；拿它截斷 ledger 名稱集合，會讓 prompt 講的範圍比 harvest 真正
    判的範圍**小**（`authorize_terminal` 對 ledger 裡**任何**非 passed 的 gate 都
    fail closed，不只應驗的那一個），那正是 #721 的鏡像缺陷。
    """

    return bool(terminal_contract.expected_gate_names_for_test_policy(test_policy))


def card_gate_names(
    env: Mapping[str, str] | None = None,
    *,
    test_policy: str | None,
) -> tuple[str, ...]:
    """這張卡在派工 prompt 上該出現的 gate 名稱集合（#721）。

    契約不要求模型交出 gate 結果時是空 tuple——prompt 因此沒有任何 gate 名稱可以
    宣告，``gate_evidence`` 只能是 ``[]``；其餘一律是 :func:`ledger_gate_names`，
    也就是 harvest 端真正會拿來對照的那一組，兩端不會各算一份。
    """

    if not card_requires_gate_evidence(test_policy):
        return ()
    return ledger_gate_names(env)


def gate_evidence_name_hint(
    env: Mapping[str, str] | None = None,
    *,
    test_policy: str | None,
) -> str:
    """回傳 dispatch prompt 用的 gate 名稱說明（由 gate 宣告機械產生）。

    issue #540（與 #486／#521 同族）：``terminal_contract.authorize_terminal``
    要求 envelope 自報的 ``gate_evidence[].name`` ⊆ ledger 實際跑過的 gate 名稱，
    但派工 prompt 從來沒有把那個集合告訴模型。實測 tdd-red 卡的 builder 自報
    ``'focused pytest RED expectation'``（自己造的描述性名稱），採信因
    ``gate-evidence-unknown-gate`` 必敗——模型被要求精確命中一個它看不到的集合。

    修法比照 #521：prompt 裡的可用值由判準常數機械產生，不手寫。這裡的判準常數
    就是 operator 的 ``PSC_GATE_CMD_*`` 宣告（:func:`declared_gate_names`），與
    :func:`write_gate_ledger` 產生 ledger 用的是同一條導出路徑，宣告改動會自動
    同步到 prompt。

    #721：``test_policy`` 決定**適用範圍**。契約不要求模型交出 gate 結果的卡
    （:func:`card_requires_gate_evidence` 為 False）拿到的是專屬文字：沒有任何可
    宣告的 gate 名稱，``gate_evidence`` 只能是 ``[]``。其餘 ``test_policy`` 的文字
    逐字不變。
    """

    if not card_requires_gate_evidence(test_policy):
        return (
            "This card's test_policy requires no test run from you, so there is no gate name "
            "for you to claim and gate_evidence must be exactly []. A name of your own "
            "invention fails the card closed; put whatever you observed into diagnostics "
            "instead."
        )
    # 宣告不合法時 ledger 只會有一項 `gate-spec`（見 `main`）；照實告知，不假裝
    # 有一組可用的 gate 名稱。這張卡本來就會 fail closed。
    names = ledger_gate_names(env)
    if not names:
        return (
            "The Manager declared no deterministic gates for this card, so the gate ledger it "
            "writes after your process exits will be empty; gate_evidence must be exactly []."
        )
    allowed = ", ".join(f'"{name}"' for name in names)
    return (
        "The Manager's gate ledger for this card can only contain these gate names: "
        f"{allowed}. Every gate_evidence[].name must be one of those exact strings, copied "
        "verbatim; the Manager rejects any name that is not in the ledger, so a descriptive "
        'label of your own (for example "focused pytest RED expectation") fails the card '
        "closed. If you did not run one of them, leave it out instead of renaming it."
    )


def _no_gate_evidence_scope_hint(env: Mapping[str, str] | None = None) -> str:
    """契約不要求模型交出 gate 結果時的範圍紀律文字（#721）。

    兩件事必須同時成立，缺一都是把範圍講錯：

    1. **不要求模型跑任何 gate。** 這是 ``test_policy=none`` 的卡與其他卡唯一的
       差別，也是 #721 的病灶所在。
    2. **仍據實揭露 Manager 自己那一次 gate 執行。** 它照樣會發生（見
       :func:`gate_scope_honesty_hint` 的說明與實機 ledger 證據），也照樣會把
       ``passed`` 打掉；隱瞞它會讓模型以為這張卡完全不受任何 gate 影響。
    """

    try:
        specs = load_gate_specs(env)
    except GateSpecError:
        specs = ()
    if not specs:
        return (
            "Scope discipline: this card's test_policy requires no test run from you, and the "
            "Manager has declared no gate commands either, so no deterministic gate result is "
            "expected from either side. Report the status you actually observed, keep the "
            "concrete detail in diagnostics, and leave gate_evidence exactly []."
        )
    rendered = "; ".join(f'"{spec.name}" = `{spec.command}`' for spec in specs)
    return (
        "Scope discipline: this card's test_policy requires no test run from you. Do not run a "
        "project test suite for it, and do not treat a command that cannot run inside this "
        "card's sandbox as this card's own failure: report the status you actually observed "
        "and keep the concrete detail in diagnostics. Separately from you, the Manager runs "
        f"its own declared gate commands ({rendered}) after your process exits and still fails "
        "this card closed on a non-passing result — that run is the Manager's own, you are not "
        "asked to reproduce it, and gate_evidence must stay exactly []."
    )


def gate_scope_honesty_hint(
    env: Mapping[str, str] | None = None,
    *,
    test_policy: str | None,
) -> str:
    """回傳 dispatch prompt 用的「gate 範圍紀律」說明（同樣由 gate 宣告機械產生）。

    issue #606 的附帶觀察：builder 在兩個 run（``workflow-084f...`` 的 job 488、
    ``workflow-7812...`` 的 job 492／493）重複出現同一個行為模式——只跑 focused
    測試看到綠，就自報 ``status=passed`` 並宣稱整組 gate 通過；manager 事後以
    宣告的 gate 命令獨立重跑，每次都抓到同一個失敗。prompt 的 ``status_policy``
    過去只說「gate 實際通過才回 passed」，沒有點名「focused 綠 ≠ 宣告的 gate
    綠」這個推定，模型因此把自己選的子集當成整組的證據。

    修法與 :func:`gate_evidence_name_hint`（#541／#540）同一條紀律：說明文字裡的
    具體值（gate 名稱與**它真正會被重跑的命令**）由 operator 的 ``PSC_GATE_CMD_*``
    宣告機械導出，與 :func:`write_gate_ledger` 同源，不手寫第二份真實來源。

    #721：``test_policy`` 決定**適用範圍**。契約不要求模型交出 gate 結果的卡拿到
    專屬文字，且那段文字**不得**出現「Manager 會重跑 X 並用它判你的 passed」——
    現場（job ``wf-6c37c77ca1-worktree-isolation-8``）就是這句話把模型推去跑
    ``python3 -m pytest -q``，在 ``-s read-only`` 沙箱下死於
    ``No usable temporary directory available``，terminal 回 ``failed``，Manager
    自動重派，形成確定性迴圈。

    但那段文字**仍必須據實揭露** Manager 自己那一次 gate 執行：
    :func:`gate_runner.ensure_gate_ledger` 只看 phase（``build``）不看
    ``test_policy``，所以 ``test_policy="none"`` 的 build 卡照樣會被 ``cortex-gate``
    重跑宣告的 gate 並寫進 ledger（實機證據：
    ``wf-6c37c77ca1-worktree-isolation-3.gates.json`` 內有一列 ``pytest``），而
    ``terminal_contract.authorize_terminal`` 的矛盾偵測對 ledger 裡**任何**非
    passed 的 gate 都 fail closed。只說「這張卡不宣告任何確定性 gate」會把這件事
    講小；正確的訊息是「**不要你跑**，但 Manager 仍會自己跑並據以判定」。
    """

    if not card_requires_gate_evidence(test_policy):
        return _no_gate_evidence_scope_hint(env)
    try:
        specs = load_gate_specs(env)
    except GateSpecError:
        specs = ()
    if not specs:
        return (
            "Scope discipline: a gate counts as passed only when the Manager's own declared "
            "gate command passes as the Manager runs it. Running a narrower subset of your "
            "own choosing never authorizes claiming the declared gate is green."
        )
    rendered = "; ".join(f'"{spec.name}" = `{spec.command}`' for spec in specs)
    return (
        "Scope discipline: after your process exits the Manager re-runs exactly these commands "
        f"({rendered}), and a passed status is judged against those real results. Running a "
        "focused subset first is fine, but then report only the scope you actually ran: a green "
        "focused subset is NOT evidence that the declared gate is green, and inferring the full "
        "gate from it fails the card closed."
    )


def _gate_timeout(env: Mapping[str, str]) -> int:
    raw = str(env.get(GATE_TIMEOUT_ENV, "")).strip()
    if not raw:
        return DEFAULT_GATE_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_GATE_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_GATE_TIMEOUT_SECONDS


Runner = Callable[..., subprocess.CompletedProcess]


def run_gates(
    specs: Sequence[GateSpec],
    *,
    worktree: str | Path,
    timeout: int = DEFAULT_GATE_TIMEOUT_SECONDS,
    runner: Runner | None = None,
) -> list[dict[str, object]]:
    """實際執行每個 gate，回傳 ledger 的 ``gates`` 欄位內容。

    任何無法執行、逾時或非 0 exit code 一律記為 ``failed``——「跑不起來」不得被
    當成「通過」，否則 operator 設定壞掉會靜默變成 fail-open。
    """

    execute = subprocess.run if runner is None else runner
    rows: list[dict[str, object]] = []
    for spec in specs:
        try:
            completed = execute(
                list(spec.argv),
                cwd=str(worktree),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            exit_code = int(completed.returncode)
            detail = ""
            if exit_code != 0:
                stderr = (completed.stderr or "").strip()
                stdout = (completed.stdout or "").strip()
                detail = (stderr or stdout or f"exit_code={exit_code}")[-2000:]
        except subprocess.TimeoutExpired:
            exit_code = 124
            detail = f"gate timed out after {timeout}s"
        except (OSError, ValueError) as exc:
            exit_code = 127
            detail = f"gate command could not be executed: {exc}"
        rows.append(
            {
                "name": spec.name,
                "command": spec.command,
                "exit_code": exit_code,
                "status": "passed" if exit_code == 0 else "failed",
                "detail": detail,
            }
        )
    return rows


class SnapshotError(OSError):
    """拋棄式副本無法建立（#629）。**不寫 ledger**，讓採信端照 `require_ledger` 拒。"""


# 可再生的直譯器／測試快取（#736）：不是候選樹的一部分（.gitignore 排除、gate 的
# pytest 會在副本裡重建自己的 cache），且 builder 在 `UMask=0077` 下產生它們時
# POSIX ACL mask 會被 create mode 的 group bits 歸零——default ACL 給 gate 的
# `r-x` 繼承了條目、繼承不到權限，gate 因此讀不到。依名跳過是語意判準（「這個
# 名字的目錄是快取」），不是「跳過讀不到的東西」——後者是禁手，見 docstring。
SNAPSHOT_REGENERABLE_CACHE_DIRS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)


def _snapshot_ignore_caches(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in SNAPSHOT_REGENERABLE_CACHE_DIRS}


def snapshot_worktree(source: str | Path, destination: str | Path) -> Path:
    """把被驗的工作樹複製成一份**拋棄式副本**，回傳副本路徑（#629）。

    ## 為什麼是副本而不是「工作樹對 gate 唯讀」

    - **唯讀不可行**：`pytest` 要寫 `.pytest_cache`／`__pycache__`，operator 宣告的
      `npm test`／`cargo test`／`make` 更是必寫。把工作樹掛成唯讀只會讓每個真實
      gate 以 EROFS 收場——那正是 #629 要修掉的「安全但不能用」。
    - **副本另外買到兩件事**：(a) gate 的寫入不會污染 builder 交付的那棵樹，harvest
      讀到的仍是 builder 自己的成果；(b) 快照在單一時點取得，builder 留下的背景
      行程改不了 gate 跑到一半的樹（TOCTOU）。

    ## 兩條刻意的取捨

    `symlinks=True`——symlink **原樣複製成 symlink，絕不跟隨**。跟隨的後果有兩個：
    指向樹外的絕對 symlink 會把外部內容**複製進**副本（gate 於是在自己的可寫區內
    得到一份 `/etc` 的複本），而指向上層目錄的 symlink 會讓 `copytree` 走進無界的
    遞迴。不跟隨之後，副本裡的 symlink 仍然是 symlink，解析它們是 gate 命令自己的
    事，而 gate 的 unit 已經把可寫面收斂到自己那兩個目錄。

    目的地**先整個移除再重建**：留下上一輪的殘留等於讓前一次 gate 的產物（甚至前一
    次被攻陷的 gate 留下的東西）參與這一次的判定。與 `spool_slot.create_slot(
    reset=True)` 同一條理由——重建比「就地清理」少一個要窮舉的清單。

    `SNAPSHOT_REGENERABLE_CACHE_DIRS` **依名跳過、任意深度**（#736）：builder 在
    `UMask=0077` 的 unit 下跑 pytest 產生的 `.pytest_cache/` mode 0700 ⇒ ACL mask
    `---` ⇒ gate 讀不到，而它根本不是候選樹的內容。**只跳這份具名清單**——「跳過
    任何讀不到的東西」是禁手：若候選內容本身讀不到（例如 RED 測試檔），靜默跳過
    會讓 gate 在殘缺的樹上判出假 verdict，正是「不把沒驗到偽裝成驗過沒過」要擋的
    ——清單外的不可讀項目照樣 `SnapshotError` fail-closed。
    """

    src = Path(source)
    dst = Path(destination)
    if src.is_symlink() or not src.is_dir():
        raise SnapshotError(f"gate snapshot source is not a directory: {src}")
    if dst == src or src in dst.parents:
        # 副本落在來源樹**裡面**會遞迴複製自己；落在同一個路徑則直接毀掉來源。
        raise SnapshotError(f"gate snapshot destination overlaps its source: {dst}")
    try:
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        elif dst.is_dir():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            src,
            dst,
            symlinks=True,
            ignore_dangling_symlinks=True,
            ignore=_snapshot_ignore_caches,
        )
    except OSError as exc:
        raise SnapshotError(f"gate snapshot failed: {src} -> {dst}: {exc}") from exc
    return dst


def build_ledger(
    gates: Sequence[Mapping[str, object]],
    *,
    slice_id: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": terminal_contract.GATE_LEDGER_SCHEMA_VERSION,
        "kind": terminal_contract.GATE_LEDGER_KIND,
        "slice_id": slice_id or "",
        "gates": [dict(row) for row in gates],
    }


def write_gate_ledger(
    *,
    ledger_path: str | Path,
    worktree: str | Path,
    env: Mapping[str, str] | None = None,
    runner: Runner | None = None,
) -> dict[str, object]:
    """執行宣告的 gate 並原子寫出 ledger；回傳寫出的 payload。

    即使沒有宣告任何 gate 也會寫出一份 ``gates: []`` 的 ledger——ledger 的**存在**
    本身就是「manager 掌控的 wrapper 確實跑完了」的證據，harvest 用它來區分
    「gate 全過」與「根本沒跑到」。
    """

    source = os.environ if env is None else env
    specs = load_gate_specs(source)
    gates = run_gates(
        specs,
        worktree=worktree,
        timeout=_gate_timeout(source),
        runner=runner,
    )
    payload = build_ledger(gates, slice_id=source.get("PSC_SLICE_ID"))
    write_ledger_payload(ledger_path, payload)
    return payload


def encode_ledger(payload: Mapping[str, object]) -> str:
    """ledger 的 canonical JSON 編碼（與 `terminal_contract.gate_ledger_digest` 同形）。"""

    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def write_ledger_payload(
    ledger_path: str | Path, payload: Mapping[str, object]
) -> Path:
    """把 ledger payload **原子**寫到指定路徑，回傳該路徑。

    抽出來是為了讓 **Manager 自己重寫權威 ledger** 這條路徑（#629 的
    `coordinator/gate_runner.py`）與本模組共用同一份編碼與同一套原子寫入——兩邊
    若各寫一次，`terminal_contract.gate_ledger_digest()` 算出來的 digest 就可能因為
    一個空白而不同，而那個 digest 是要進 evidence 的。
    """

    target = Path(ledger_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(encode_ledger(payload), encoding="utf-8")
    os.replace(tmp, target)
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paulsha-cortex-gate-ledger")
    parser.add_argument("--out", required=True, help="gate ledger 輸出路徑")
    parser.add_argument("--worktree", required=True, help="執行 gate 的工作目錄")
    parser.add_argument(
        "--snapshot-from",
        default=None,
        help=(
            "#629：先把這棵樹複製成 --worktree 的拋棄式副本，再在副本上跑 gate。"
            "gate 執行身分（cortex-gate）對來源只有唯讀 ACL，寫入一律落在副本。"
        ),
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help=(
            "寫完 ledger 後放寬到 0644，讓 spool 的 consumer（Manager）讀得到"
            "（#638 缺陷 2 的同一個修法；`wx` 無 `r` 的那一格上，檔由 producer 擁有）。"
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.snapshot_from is not None:
        try:
            snapshot_worktree(args.snapshot_from, args.worktree)
        except SnapshotError as exc:
            # **刻意不寫任何 ledger**：快照失敗代表我們根本沒有可判定的樹，寫一份
            # 「全部 failed」會把「沒驗到」偽裝成「驗過但沒過」。採信端看到 ledger
            # 不存在，照 `require_ledger` fail closed，理由在本行程的 stderr 上。
            print(str(exc), file=sys.stderr)
            return 74
    try:
        write_gate_ledger(ledger_path=args.out, worktree=args.worktree)
        if args.publish:
            spool_slot.publish_file(args.out)
    except GateSpecError:
        # operator 宣告錯誤：仍寫出一份「沒有任何 gate 通過」的 ledger，讓 harvest
        # fail closed 且訊息可追溯，而不是留下空目錄讓人以為 gate 沒被要求。
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(
                build_ledger(
                    [
                        {
                            "name": GATE_SPEC_FAILURE_NAME,
                            "command": "",
                            "exit_code": 78,
                            "status": "failed",
                            "detail": "PSC_GATE_CMD_* declaration is invalid",
                        }
                    ]
                ),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        if args.publish:
            # 宣告壞掉時**同樣要放寬**：那份「gate-spec failed」的 ledger 就是這一輪
            # 的結論，consumer 讀不到它等於把「設定錯誤」退化成「什麼都沒發生」。
            spool_slot.publish_file(args.out)
        return 78
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 進入點
    raise SystemExit(main())
