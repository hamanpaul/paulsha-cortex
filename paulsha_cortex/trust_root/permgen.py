"""Phase 2a：權限產生器——用 R1 登記表機械產生目錄 owner／group／mode 清單。

spec §R10 Phase 2 第 2 條要求「目錄 owner／mode **由 R1 登記表產生**（權限產生器以
登記表為輸入，不手寫）」。本模組即該產生器：吃 `registry.ASSET_REGISTRY` ＋一個
**UID 方案 config**（`UidScheme`，persona→OS 帳號的映射），機械算出每個資產路徑的
目標權限，輸出 (a) 結構化清單（可轉 JSON）與 (b) 可供 Phase 2b runbook 引用的
`chown`／`chmod`／`setfacl` 命令字串。

**本模組純為產生器：只回傳資料與字串，絕不執行任何 root 操作、不 chown、不 chmod、
不建 UID。** 命令字串供 operator 在 runbook 中手動 `sudo` 執行。

## UID 方案的參數化（operator 0816 **第三輪**裁決：三分定案，二分保留為向後相容選項）

`UidScheme` 把每個 `Principal` 映射到具體 OS 帳號名，並指定：
- `durable_state_owner`：擁有 Manager-owned durable state 樹的帳號；
- `deploy_account`：enforcement plane（部署面）的擁有者（root／部署帳號）。

**三分**（**定案**，`THREE_WAY_SCHEME`＝:data:`DEFAULT_SCHEME`）：三個帳號——
`cortex-manager`（Manager＋monitor，durable state owner，**持 spawn 授權但不跑任何
模型程式碼**）／`cortex-reviewer-planner`（reviewer＋planner 的模型 job）／
`cortex-builder`（builder 的模型 job）。裁決的判準是 **「injection 可達的任何進程都
不得持有 spawn 授權」**：二分下 reviewer／planner 與 Manager 併帳，任一被 prompt
injection 攻陷即取得 polkit 的 start 授權；三分把模型執行面整個移出授權帳號。

**二分**（`TWO_WAY_SCHEME`，向後相容）：`cortex-builder`＋`cortex-svc`（Manager＋
reviewer＋planner＋monitor 共用）。保留是為了讓已按二分裝好的部署不必一次到位，
**不是**新部署的建議值。

兩個方案套用**同一套 policy 函式**，因此都能對登記表每一項產出一致（滿足同一組
不變式）的權限集合：Manager-owned／deployment 樹對任何 headless 帳號皆不可寫，
job-visible 樹由對應 job 帳號寫、跨 persona 互不可寫。全部既有不變式測試對兩案
逐一參數化跑（`tests/test_trust_root_permgen_p2a.py`／`_p2b.py` 的 `ALL_SCHEMES`）。

## 降權機制（0816 第三輪裁決 A+B）

- **A**＝上述三分。
- **B**＝root-owned 模板 unit：`build_job_unit()` 產出 `cortex-job@.service`，
  `User=` 硬寫死；`build_polkit_rule(plan=TEMPLATE)`（**預設**）只放行該模板實例的
  start／stop。per-job 參數走 Manager-owned spec spool（登記表資產 `job-spec-spool`），
  由 `build_job_shim()` 產出的 root-owned shim 讀取後 exec。
- C（Manager 端封閉 argv 產生器）自動保留為第三層，見 `coordinator/job_runner.py`。

## 部署決定型 principal 的對應（#626，fail-closed）

`registry.Principal` 有兩個成員是**抽象角色**、不是服務帳號：`OPERATOR`（人類操作者）
與 `EXTERNAL`（digest／engineering-outcome outbox 的下游 reader）。它們對應到哪個 OS
帳號是**部署決定**，產生器猜不到——單人機器上 operator 就是那個人的登入帳號，多人／CI
部署可能是專用帳號，而外部 reader 在很多部署裡**根本還不存在**。

因此這兩個 principal **不放進 `account_of`**（放進去等於在程式碼裡寫死一個部署決定），
而是 `UidScheme` 上兩個預設 `None` 的欄位（見 :data:`PRINCIPAL_ACCOUNT_OPTIONS`），由
CLI 參數或 env 於**產生當下**注入。三種狀態嚴格區分：

- `None`＝**未指定** → `plan_to_commands()` raise :class:`UnresolvedPrincipalError`，
  **一行命令都不輸出**；
- :data:`ABSENT_ACCOUNT`＝**明示本部署沒有這個角色的實體** → 該 principal 的授權整組
  略去（是一個被記錄下來的決定，不是遺漏）；
- 真實帳號名 → 照常產生 ACL。

為什麼必須 fail-closed 而不是「印出來讓 operator 自己看」：runbook 第 2b 步以
`sudo sh -e` 執行整份權限 script，而 `setfacl -m u:<不存在的帳號>:rX` 會回
`Invalid argument near character 3` 並讓 `sh -e` **中止整份 script**，留下一棵
**半套用**的權限樹——前段資產已 chown/chmod、後段完全沒動，而錯誤訊息完全看不出
是「帳號不存在」（#626）。少印一行安全，多印一行會炸掉部署。

輸出前另有一道自我檢查（:func:`assert_output_accounts_known`）：每一行命令裡出現的
`u:<name>:` 與 `chown <owner>:<group>` 都必須落在**本方案宣告的帳號集合**
（:meth:`UidScheme.declared_accounts`）內，否則 raise。這條擋的是「未來新增
principal 時再犯同一個錯」——新 principal 只要沒進對應表，字面角色名一漏出即 raise。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from . import registry
from .registry import (
    HEADLESS_PERSONAS,
    UNTRUSTED_EXECUTION_PRINCIPALS,
    AssetTier,
    IngressKind,
    Principal,
    TrustRootAsset,
    TrustTree,
)


# ---------------------------------------------------------------------------
# 部署決定型 principal 的對應（#626）
# ---------------------------------------------------------------------------

#: 明示「本部署沒有這個角色的實體」的 sentinel。與「未指定」（`None`）**嚴格區分**：
#: `None` 會讓產生器 fail-closed，本 sentinel 則是一個被記錄下來的決定——該 principal
#: 的授權整組略去，輸出裡一行都不會出現。
ABSENT_ACCOUNT: str = "<absent>"

#: OS 帳號名的合法形狀（POSIX portable：小寫開頭、可含數字／底線／減號）。
#: 這同時是**注入防線**：帳號名會被逐字嵌進 `setfacl`／`chown` 命令字串，帶空白或
#: shell metacharacter 的值必須在產生階段就被拒絕，而不是等到 operator `sudo` 執行時。
_ACCOUNT_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*\$?$")


@dataclass(frozen=True)
class PrincipalAccountOption:
    """一個「必須由部署提供對應」的 principal 及其注入管道。

    這張表是唯一真相：`UidScheme` 的欄位、CLI 旗標、env 變數名、fail-closed 的錯誤
    訊息全部由它導出。將來多一個部署決定型 principal，只需在
    :data:`PRINCIPAL_ACCOUNT_OPTIONS` 加一列 ＋ `UidScheme` 加一個欄位。
    """

    principal: Principal
    #: `UidScheme` 上對應的欄位名。
    field_name: str
    #: CLI 旗標（`python -m paulsha_cortex.trust_root permissions …`）。
    cli_flag: str
    #: env 變數名（CLI 未給時的來源；CLI 優先）。
    env_var: str
    #: 這個角色是什麼——進錯誤訊息，讓 operator 知道自己在決定什麼。
    description: str


PRINCIPAL_ACCOUNT_OPTIONS: tuple[PrincipalAccountOption, ...] = (
    PrincipalAccountOption(
        principal=Principal.OPERATOR,
        field_name="operator_account",
        cli_flag="--operator-account",
        env_var="PSC_OPERATOR_ACCOUNT",
        description=(
            "人類操作者的登入帳號——單人機器＝那個人的帳號，多人／CI 部署可能是專用帳號"
        ),
    ),
    PrincipalAccountOption(
        principal=Principal.EXTERNAL,
        field_name="external_reader_account",
        cli_flag="--external-reader-account",
        env_var="PSC_EXTERNAL_READER_ACCOUNT",
        description=(
            "外送管線／外部學習系統的唯讀 reader"
            "——digest outbox 與 engineering-outcome outbox 的下游"
        ),
    ),
)

#: principal → 注入管道，供錯誤訊息與 CLI 反查。
PRINCIPAL_ACCOUNT_OPTION_BY_PRINCIPAL: Mapping[Principal, PrincipalAccountOption] = {
    opt.principal: opt for opt in PRINCIPAL_ACCOUNT_OPTIONS
}


class UnresolvedPrincipalError(ValueError):
    """方案未把某個部署決定型 principal 對應到真實帳號（#626 的 fail-closed 出口）。

    raise 的時機是**輸出前**：一行命令都還沒印出去。這是刻意的——runbook 以
    `sudo sh -e` 跑整份 script，印出去一行指向不存在帳號的 `setfacl` 就足以中止
    整份 script 並留下半套用的權限樹。
    """

    def __init__(self, unresolved: tuple[Principal, ...], scheme_id: str) -> None:
        self.unresolved = tuple(unresolved)
        self.scheme_id = scheme_id
        super().__init__(unresolved_principal_message(self.unresolved, scheme_id))


class UnknownAccountInOutputError(ValueError):
    """輸出裡出現了不在本方案宣告帳號集合內的名字（自我檢查的最後一道）。"""

    def __init__(self, unknown: tuple[str, ...], scheme_id: str, declared: frozenset[str]) -> None:
        self.unknown = tuple(unknown)
        self.scheme_id = scheme_id
        self.declared = frozenset(declared)
        super().__init__(
            "permgen 自我檢查失敗：輸出含未宣告的帳號 "
            f"{list(self.unknown)}（scheme={scheme_id}）。\n"
            f"本方案宣告的帳號集合：{sorted(self.declared)}。\n"
            "這代表某個 principal 的對應仍是**字面角色名**而不是真實 OS 帳號——"
            "在 `sudo sh -e` 下會 `Invalid argument` 中止整份 script 並留下半套用的"
            "權限樹（#626）。請把該 principal 補進對應表，或明示它在本部署不存在。"
        )


def unresolved_principal_message(
    unresolved: tuple[Principal, ...],
    scheme_id: str,
) -> str:
    """fail-closed 的可操作訊息：指出是哪個 principal、以及怎麼指定。"""
    lines = [
        f"permgen 拒絕輸出命令：scheme={scheme_id} 有 {len(unresolved)} 個 principal "
        "未對應到真實 OS 帳號。",
        "",
    ]
    for principal in unresolved:
        opt = PRINCIPAL_ACCOUNT_OPTION_BY_PRINCIPAL.get(principal)
        if opt is None:
            lines.append(
                f"  - principal `{principal.value}`：`UidScheme.account_of` 缺這一項，"
                "請補上對應的 OS 帳號名。"
            )
            if principal is Principal.GATE:
                lines += [
                    "      這是 #629 新增的 **gate 執行身分**。它不能併到任何既有帳號：",
                    "      併到 builder＝模型自證（#540）、併到 manager＝把任意程式碼執行",
                    "      引到持 spawn 授權的帳號、併到 reviewer-planner＝把 builder 對",
                    "      verdict 通道的影響力重新打開（#638／#639）。",
                    "      改用四分方案：`--scheme four-way`（預設值），並先建立帳號：",
                    "      `sudo useradd --system --no-create-home --shell /usr/sbin/nologin cortex-gate`",
                ]
            continue
        lines += [
            f"  - principal `{principal.value}`（{opt.description}）未對應到真實帳號。",
            f"      指定：{opt.cli_flag} <帳號名>   或   env {opt.env_var}=<帳號名>",
            f"      本部署沒有這個角色時，明示：{opt.cli_flag} none"
            f"（等同 {opt.env_var}=none）——該 principal 的授權整組略去。",
        ]
    lines += [
        "",
        "為何不先印出來讓人自己看：runbook 第 2b 步以 `sudo sh -e` 執行整份權限 script，",
        "而 `setfacl -m u:<不存在的帳號>:rX` 會回 `Invalid argument near character 3`",
        "並**中止整份 script**，留下一棵半套用的權限樹（前段已 chown/chmod、後段完全沒動），",
        "錯誤訊息還完全看不出是「帳號不存在」（#626）。",
        "指定前請先確認帳號存在：`getent passwd <帳號名>`。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# UID 方案 config（參數化——二分為預設，同一資料結構可表達三分）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UidScheme:
    """persona→OS 帳號的映射方案。

    `account_of` 必須涵蓋登記表出現過的所有**服務／job** principal
    （`ANY_SAME_UID` 是「現況同 UID 任意行程」的標記，正是 Phase 2 要移除的對象，
    故**不**映射到任何目標帳號）。

    `INSTALLER`／`OPERATOR`／`EXTERNAL` 三者**不得**放進 `account_of`：前者永遠是
    `deploy_account`，後兩者是部署決定，走各自的欄位（見
    :data:`PRINCIPAL_ACCOUNT_OPTIONS`）。放進 `account_of` 會被靜默忽略，因此
    `__post_init__` 直接拒絕——「兩份真相」正是 #626 的成因。

    `account_of` 的值可以是 :data:`ABSENT_ACCOUNT`（#629），語意與部署決定型欄位
    的那個 sentinel **逐字相同**：「本方案明示沒有這個角色的實體」。它與「沒有這個
    鍵」嚴格區分——後者讓產生器 fail-closed（`unresolved_principals()` 會指名它），
    前者是一個**被記錄下來的決定**，該 principal 的授權整組略去、輸出裡一行都不會
    出現。二分／三分對 `GATE` 用的就是它：那兩個方案沒有第四個帳號，因此沒有 gate
    執行面，build 卡照 `require_ledger` fail closed（＝#629 之前的現況），但方案的
    其餘部分仍然完整可產出——不會為了一個它們本來就沒有的角色而整份拒絕輸出。
    """

    scheme_id: str
    account_of: Mapping[Principal, str]
    #: 擁有 Manager-owned durable state 樹的帳號（Manager 本人的服務帳號）。
    durable_state_owner: str
    #: enforcement plane（unit／venv／launcher／env／codex hooks）的擁有者。
    deploy_account: str = "root"
    #: 人類操作者的登入帳號。**部署決定**：`None`＝未指定（產生器 fail-closed），
    #: :data:`ABSENT_ACCOUNT`＝本部署沒有這個角色。
    operator_account: str | None = None
    #: 外部唯讀消費者（digest／engineering-outcome outbox 的下游）帳號。同上三態。
    external_reader_account: str | None = None

    def __post_init__(self) -> None:
        managed = {opt.principal for opt in PRINCIPAL_ACCOUNT_OPTIONS} | {
            Principal.INSTALLER, Principal.ANY_SAME_UID,
        }
        overlap = sorted(p.value for p in self.account_of if p in managed)
        if overlap:
            raise ValueError(
                f"UidScheme(scheme_id={self.scheme_id!r})：{overlap} 不得出現在 "
                "`account_of`——它們由專屬欄位決定（deploy_account／"
                + "／".join(opt.field_name for opt in PRINCIPAL_ACCOUNT_OPTIONS)
                + "），寫在 `account_of` 會被靜默忽略而形成第二份真相（#626）。"
            )
        for name in list(self.account_of.values()) + [
            self.durable_state_owner, self.deploy_account,
        ]:
            # `ABSENT_ACCOUNT`（#629）不是帳號名，是「本方案沒有這個角色」的 sentinel；
            # 它永遠不會被印進任何命令，因此不受 `_ACCOUNT_NAME_RE` 約束。
            if name == ABSENT_ACCOUNT:
                continue
            _validate_account_name(name)
        for opt in PRINCIPAL_ACCOUNT_OPTIONS:
            value = getattr(self, opt.field_name)
            if value is None or value == ABSENT_ACCOUNT:
                continue
            _validate_account_name(value, flag=opt.cli_flag)

    def resolve(self, principal: Principal) -> str | None:
        """回傳 principal 的目標帳號；`ANY_SAME_UID`、未指定與明示不存在者回傳 None。

        **回傳 None 不代表沒問題**：呼叫端要區分「本來就不該有帳號」
        （`ANY_SAME_UID`／`ABSENT_ACCOUNT`）與「對應表缺項」——後者由
        :meth:`unresolved_principals` 抓出來並讓產生器 fail-closed。
        """
        if principal is Principal.ANY_SAME_UID:
            return None
        if principal is Principal.INSTALLER:
            return self.deploy_account
        opt = PRINCIPAL_ACCOUNT_OPTION_BY_PRINCIPAL.get(principal)
        if opt is not None:
            value = getattr(self, opt.field_name)
            return None if value in (None, ABSENT_ACCOUNT) else value
        mapped = self.account_of.get(principal)
        # `ABSENT_ACCOUNT`（#629）與缺鍵在這裡故意收斂成同一個回傳值：兩者都代表
        # 「沒有帳號可用」。**分辨兩者是 `unresolved_principals()` 的職責**——它才是
        # 決定「這是決定還是遺漏」的地方。
        return None if mapped == ABSENT_ACCOUNT else mapped

    def group_of(self, account: str) -> str:
        """帳號的 primary group（慣例：每帳號一個同名 group）。"""
        return account

    def headless_accounts(self) -> frozenset[str]:
        """全部**執行不受信任程式碼**的身分解析到的帳號集合。

        Manager-owned／deployment 樹對這些帳號**必須**零寫入權——這是本產生器的
        核心不變式。

        #629 起來源是 :data:`registry.UNTRUSTED_EXECUTION_PRINCIPALS`（headless
        persona ＋ headless hook ＋ **gate**），而不是只有 `HEADLESS_PERSONAS`：
        gate 執行的是 operator 宣告的命令，但那些命令跑在 builder 完全掌控內容的
        工作樹上，載入的是該樹的 `conftest.py`／plugin——在「Manager-owned 樹對它
        必須零寫入權」這件事上，它與跑模型的三個 persona 完全同級。方法名維持
        `headless_accounts` 是刻意的：它的**語意**（不受信任的執行帳號集合）沒有
        改變，改名只會讓十幾個呼叫端跟著動而換不到任何東西。
        """
        accts: set[str] = set()
        for p in sorted(UNTRUSTED_EXECUTION_PRINCIPALS, key=lambda x: x.value):
            a = self.resolve(p)
            if a is not None:
                accts.add(a)
        return frozenset(accts)

    def model_job_accounts(self) -> frozenset[str]:
        """**跑模型 CLI** 的 job 帳號集合（`HEADLESS_PERSONAS` 解析結果）。

        與 :meth:`headless_accounts` 的差別只有一項——`cortex-gate` 不在其中——但那
        一項決定了「要不要幫這個帳號準備 root-owned 的 `~/.codex` 與 executor 憑證
        檔」。gate 不跑模型，因此那兩個前置物對它是純多餘：多兩個沒有消費者的
        root-owned 目錄，還會讓「無多餘」那組等式測試被迫放寬。
        """
        accts: set[str] = set()
        for p in sorted(HEADLESS_PERSONAS, key=lambda x: x.value):
            a = self.resolve(p)
            if a is not None:
                accts.add(a)
        return frozenset(accts)

    def declared_accounts(self) -> frozenset[str]:
        """本方案宣告過的**全部**帳號名——輸出自我檢查的比對基準。

        任何出現在產生命令裡的帳號名都必須落在這個集合內；落在集合外就代表某處把
        抽象角色名當帳號印出去了（#626）。
        """
        accts = {a for a in self.account_of.values() if a != ABSENT_ACCOUNT}
        accts.add(self.durable_state_owner)
        accts.add(self.deploy_account)
        for opt in PRINCIPAL_ACCOUNT_OPTIONS:
            value = getattr(self, opt.field_name)
            if value is not None and value != ABSENT_ACCOUNT:
                accts.add(value)
        return frozenset(accts)

    def unresolved_principals(
        self,
        assets: tuple[TrustRootAsset, ...] = registry.ASSET_REGISTRY,
    ) -> tuple[Principal, ...]:
        """登記表用到、但本方案**沒有**對應到帳號也沒明示不存在的 principal。

        `ANY_SAME_UID` 不算（它本來就不該有帳號）；明示 :data:`ABSENT_ACCOUNT` 的
        也不算（那是決定，不是遺漏）。回傳依 principal 值排序，確保訊息決定性。
        """
        missing: set[Principal] = set()
        for asset in assets:
            for principal in tuple(asset.writers) + tuple(asset.readers):
                if principal is Principal.ANY_SAME_UID:
                    continue
                opt = PRINCIPAL_ACCOUNT_OPTION_BY_PRINCIPAL.get(principal)
                if opt is not None:
                    if getattr(self, opt.field_name) is None:
                        missing.add(principal)
                    continue
                # #629：`ABSENT_ACCOUNT` 是決定、缺鍵才是遺漏。兩者 `resolve()` 都回
                # None，因此這裡要直接看原始對應表，不能只看解析結果。
                if self.account_of.get(principal) == ABSENT_ACCOUNT:
                    continue
                if self.resolve(principal) is None:
                    missing.add(principal)
        return tuple(sorted(missing, key=lambda p: p.value))

    def with_principal_accounts(
        self,
        accounts: Mapping[Principal, str],
    ) -> "UidScheme":
        """回傳帶上部署決定型 principal 對應的新方案（本體 frozen，不就地改）。

        CLI／env 注入的唯一入口。未列出的 principal 沿用原值。
        """
        overrides: dict[str, str] = {}
        for principal, account in accounts.items():
            opt = PRINCIPAL_ACCOUNT_OPTION_BY_PRINCIPAL.get(principal)
            if opt is None:
                raise ValueError(
                    f"principal `{principal.value}` 不是部署決定型的對應項；"
                    f"可指定的只有 {[o.principal.value for o in PRINCIPAL_ACCOUNT_OPTIONS]}。"
                )
            overrides[opt.field_name] = account
        return replace(self, **overrides)


def _validate_account_name(name: str, flag: str | None = None) -> None:
    """帳號名必須是合法 POSIX 帳號形狀——名字會被逐字嵌進命令字串。"""
    if not isinstance(name, str) or not _ACCOUNT_NAME_RE.match(name):
        where = f"（{flag}）" if flag else ""
        raise ValueError(
            f"不是合法的 OS 帳號名{where}：{name!r}。"
            "帳號名會被逐字嵌進 setfacl／chown 命令字串，只接受 "
            "`^[a-z_][a-z0-9_-]*\\$?$`。"
        )


#: repo slug 的合法形狀。slug 會被逐字嵌進 root 產生的 `.gitconfig`，且會被接成
#: `<repo_source_root>/<slug>`——不得含 `/`、空白、shell metacharacter，也不得以 `.`
#: 開頭（擋掉 `..` 這類往上跳的相對段）。
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_repo_slug(slug: str) -> None:
    if not isinstance(slug, str) or not _REPO_SLUG_RE.match(slug):
        raise ValueError(
            f"不是合法的 repo slug：{slug!r}。slug 會被接成 <repo_source_root>/<slug> "
            "並逐字寫進 root-owned 的 .gitconfig，只接受 `^[A-Za-z0-9][A-Za-z0-9._-]*$`。"
        )


#: executor 憑證在帳號 HOME 下的相對路徑形狀（#640）。**必須至少含一個 `/`**：
#: 裁決 (b) 的性質建立在「檔案 job-owned、放它的**目錄** root-owned」上，憑證直接
#: 落在 HOME 根本身就沒有那一層目錄可保護。段名限縮成無 shell metacharacter，且
#: 明確擋掉 `..`——這個值會被接成絕對路徑並嵌進 root 執行的 chown／chmod 命令。
_CREDENTIAL_RELPATH_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+$")


def _validate_credential_relpath(relpath: str) -> None:
    if (
        not isinstance(relpath, str)
        or not _CREDENTIAL_RELPATH_RE.match(relpath)
        or any(seg == ".." for seg in relpath.split("/"))
    ):
        raise ValueError(
            f"不是合法的憑證相對路徑：{relpath!r}。它會被接成 <帳號 HOME>/<relpath> "
            "並嵌進 root 執行的 chown／chmod 命令，只接受 "
            "`^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+$` 且不得含 `..`。"
            "**必須至少含一層目錄**——裁決 (b) 的「目錄 root-owned、檔案 job-owned」"
            "沒有那一層就不成立。"
        )


#: HOME 相對路徑的形狀（#685）。與 :data:`_CREDENTIAL_RELPATH_RE` 的差別**只有一條**：
#: 這裡允許單一 segment（`.codex`／`.gemini`／`.claude`）。理由是形狀不同——
#: `IN_PLACE_FILE` 需要「檔案上面還有一層 root-owned 目錄」，因此至少兩段；
#: `HOME_REDIRECT_TREE` 登記的是**那條 symlink 自己**，保護它的是 HOME 那一層
#: （`scaffold_directories()` 產出 root:root 0755），所以一段就夠。
#: 段名同樣限縮成無 shell metacharacter 且擋掉 `..`——值會被接成絕對路徑並嵌進 root
#: 執行的 `ln`／`chown` 命令。
_HOME_RELPATH_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


def _validate_home_relpath(relpath: str) -> None:
    if (
        not isinstance(relpath, str)
        or not _HOME_RELPATH_RE.match(relpath)
        or any(seg in ("..", ".") for seg in relpath.split("/"))
    ):
        raise ValueError(
            f"不是合法的 HOME 相對路徑：{relpath!r}。它會被接成 <帳號 HOME>/<relpath> "
            "並嵌進 root 執行的 ln／chown 命令，只接受 "
            "`^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$` 且不得含 `.`／`..` 段。"
        )


# ---------------------------------------------------------------------------
# 部署樹 toolchain 的實體形態（#640 實機盤點；#661 擴到非 executor）
#
# 形態不同 ⇒ 搬進部署樹的方式不能一概而論，因此固化成一張表：runbook 的安裝步驟與
# 測試都由它導出，新增／換掉一支程式只改這裡一列。
#
# **#661：這張表原本只涵蓋四個 executor，那是一個真實的缺口。** job／服務需要的外部
# 程式不只 executor——review sandbox 另有 `srt`、ship 另有 `openspec`，兩者同樣住在
# operator 的 HOME 底下，`ProtectHome=yes` 之後同樣不可達。因此本節現在有**兩張
# 名冊**（`EXECUTOR_TOOLS` 與 `SERVICE_TOOLS`），共用同一個形狀與同一棵樹，落位計畫
# 由兩者的聯集 `TOOLCHAIN_PROGRAMS` 導出。
#
# **為何不是把 `srt`／`openspec` 併進 `EXECUTOR_TOOLS`**：那張表不只是清單，它同時是
# **dispatch 的 executor 名字判準**——`executor_hardening_profile()` 對不在表上的名字
# fail-closed（spec §R8），把非 executor 併進去等於讓 `executor: srt` 這種派工變成
# 合法。兩張表是為了讓「盤點完整」與「dispatch 仍 fail-closed」同時成立。
# ---------------------------------------------------------------------------

#: `consumed_by` 的特別值：由 Manager／monitor 的 **system unit** 執行，而不是任何
#: job 模板 unit。它沒有 per-executor 剖面可分岔，走的就是共用的 `_HARDENING`。
MANAGER_SURFACE = "manager-unit"


class ExecutorShape(Enum):
    """toolchain 程式在檔案系統上的實體形態。"""

    NODE_SCRIPT = "node-script"    # `#!/usr/bin/env node` ＋ JS 本體（需 node runtime）
    NATIVE_ELF = "native-elf"      # 自帶原生執行檔（不依賴任何 runtime）
    SHELL_SCRIPT = "shell-script"  # shell script（可能再叫別的程式，安裝時要查一次）


# ---------------------------------------------------------------------------
# executor 自帶的**內層沙箱**（#714）——第三個與 `needs_node` 正交的加固維度
#
# ## 為什麼要有這一格
#
# job 已經被 systemd 關在四分降權 ＋ 完整加固面裡，而 executor 自己**還會**再開一層
# 沙箱來關住模型跑的 shell 命令。那一層與外層加固面是會打架的：#714 實機在 builder
# 的 job log 裡量到 **5 個 `command_execution` 全部 `status: failed`**，逐字都是
#
#     bwrap: Can't read /proc/sys/kernel/overflowuid: No such file or directory
#
# ——`ProcSubset=pid` 讓 `/proc/sys` 整個消失，codex 的 bubblewrap 起不來，於是它跑的
# 每一個命令都 `exit 1`，模型最後合理地回 `needs_human`。Manager 端看到的
# `card-terminal-schema-retry-exhausted` 是**症狀**，離病因四層遠。
#
# ## 為什麼落在**這張表**上，而不是加固剖面上
#
# 這與 #673 把 `filtered_syscalls` 放在同一張表上是同一個判斷：**事實是「這支程式跑
# 起來需要什麼」，屬於程式；處置才屬於加固面。** 而且與 `needs_node` 那條軸不同——
# `needs_node` 導向「換一份放寬的具名剖面」，本欄位導向「**全域**多放行一組**方向
# 相反**的 syscall」，因此不能共用那個欄位（#673 的教訓逐字：兩件事混在一個欄位上，
# 適用面與處置方向都會錯）。
#
# ## 0819 實機量到四道牆（逐條，其餘 property 固定，每次只加一條）
#
# 走 `psc_run_under` 全量導出（D13），跑 codex 自帶的 `codex-resources/bwrap`：
#
#     1  jit 剖面原樣            bwrap: Can't read /proc/sys/kernel/overflowuid
#     2  +ProcSubset=all         bwrap: No permissions to create a new namespace
#     3  +RestrictNamespaces=no  bwrap: loopback: Failed to create NETLINK_ROUTE socket
#     4  +AF_NETLINK             bwrap: Failed to make / slave: Operation not permitted
#     5  +SystemCallFilter 加 @mount   rc=0
#
# 也就是說「保留 bwrap」要付的是**四條**放寬，其中第 2、4 條放寬的正是 user namespace
# 與 mount——外層加固面存在的理由本身（`RestrictNamespaces` 那一列的註解逐字寫著
# 「user namespace 是 unprivileged 提權的常見起點」），而第 4 條的鍵是
# :data:`PROFILE_LOCKED_KEYS` 裡的 `SystemCallFilter`。0819 裁決因此由「A＝具名剖面
# 放寬 `ProcSubset`」更正為**票上的 C＝換一個不需要 bwrap 的執行形態**：
#
#     psc_run_under（全量導出）＋ SystemCallFilter=@system-service @sandbox
#       codex sandbox --enable use_legacy_landlock -- /bin/pwd   → rc=0
#       …-- sh -c 'echo hi > <builder HOME>/.codex/PWN'          → Permission denied
#       …-- sh -c 'getent hosts api.openai.com'                  → rc=2（網路被擋）
#
# 一條、全域、方向相反，其餘 26 項加固**逐字不動**（`ProcSubset=pid`／
# `RestrictNamespaces=yes`／`RestrictAddressFamilies` 全部留著）。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InnerSandboxSpec:
    """一個 executor 自帶的內層沙箱形態 × 它在外層加固面上的**執行條件**（#714）。"""

    #: 形態名（進產物註解與錯誤訊息）。
    kind: str
    #: 讓該 executor 走這個形態所需的 argv。空 tuple＝它預設就走這個形態。
    #:
    #: ⚠️ **這是對某一版工具的觀察，不是不變式**：旗標名、預設值、甚至旗標存不存在
    #: 都隨 executor 版本而異。因此它必須被一條**反向不變式探針**盯著
    #: （:func:`build_inner_sandbox_probe`），而不是只寫在註解裡。
    argv: tuple[str, ...]
    #: 這個形態在真實加固面下**實機量到**必須放行的 systemd syscall 群組。
    #: 由 `_validate_inner_sandbox_support()` 在 import 當下比對 `_HARDENING`。
    syscall_groups: tuple[str, ...]
    #: 相對於「executor 的預設沙箱形態」放棄了什麼。誠實標註：登記表、產物與 runbook
    #: 都引用它。
    accepted_loss: tuple[str, ...]
    note: str


#: codex 的內層沙箱形態（#714 實機，codex-cli 0.147.0）。
#:
#: 預設形態是 **bubblewrap**：user／mount／pid／net namespace ＋ 自己一套 mount 表。
#: 那個形態在本系統的加固面下要付四條放寬（見上方逐條量測），因此改走它的
#: **landlock ＋ seccomp** 路徑——不建立任何 namespace，只用兩個「把自己關得更緊」的
#: 核心介面。
CODEX_LEGACY_LANDLOCK = InnerSandboxSpec(
    kind="landlock-seccomp",
    argv=("--enable", "use_legacy_landlock"),
    syscall_groups=("@sandbox",),
    accepted_loss=(
        "**沒有 PID namespace**（bwrap 的形態有）。跨 UID 那一面由外層的 "
        "`ProtectProc=invisible` ＋ `ProcSubset=pid` 覆蓋——job 眼中的 `/proc` 只有"
        "自己這個 UID 的項；剩下的差異是「同一個 job 內部的行程彼此看得見」，那本來"
        "就不是隔離邊界（它們是同一個模型跑出來的同一串命令）。**這條是明載的取捨，"
        "不是隱性假設。**",
        "**沒有 mount namespace**，因此內層擋不住「把別的路徑 bind 進工作區」這類"
        "手法——但那需要 `mount(2)`，而 `SystemCallFilter=@system-service` 本來就沒有"
        "放行 `@mount`（#714 第 4 道牆量到的正是它）。外層擋住的東西不因內層換形態"
        "而鬆動。",
        "**依賴 codex 的 `use_legacy_landlock` 旗標，而上游已宣告它是過渡狀態**。"
        "0819 實機在真實派工的 `--json` 串流裡逐字收到："
        "`[features].use_legacy_landlock is deprecated and will be removed soon. "
        "(Remove this setting to stop opting into the legacy Linux sandbox behavior.)`"
        "——**這是倒數，不是穩態**：上游拿掉它的那天，codex 回 "
        "`Error: Unknown feature flag: …` 並以非零收場（同日實測），"
        "屆時只剩 bubblewrap，而 bubblewrap 要付的四條放寬正是本票否決的那四條 ⇒ "
        "A／B／C 會整個回到桌上。因此 `trust_root inner-sandbox-probe` 不只驗「還能不能"
        "用」，也把那句 deprecation 印出來當**早期警報**。"
        "※ 那句話以 `item.completed` ／ `item.type=error` 進 `--json` 串流，"
        "**不影響 terminal 契約**：`manager._extract_terminal_json()` 由**尾端往回**找 "
        "`agent_message`，開頭的 error 項會被跳過（0819 實機的 job log 逐字確認）。",
    ),
    note=(
        "0819 實機（codex-cli 0.147.0）：`codex sandbox --enable use_legacy_landlock "
        "-- /bin/pwd` 在 builder 的真實加固面下 rc=0；同一條命令改寫 "
        "`$CODEX_HOME/PWN` 得 `Permission denied`（landlock 生效）、`getent hosts` "
        "rc=2（seccomp 擋網路）。不加旗標則走 bubblewrap，逐字死在 "
        "`bwrap: Can't read /proc/sys/kernel/overflowuid`。"
        "※ `--disable use_linux_sandbox_bwrap` **不會**切走 bwrap（0819 實測仍是 "
        "bwrap 的錯誤），因此不能拿它當同義的開關。"
    ),
)


@dataclass(frozen=True)
class ToolchainProgram:
    """一支落進部署樹 toolchain 的外部程式的搬移契約。"""

    name: str
    shape: ExecutorShape
    #: 需要**系統層** runtime 才跑得起來（目前只有 node）。
    #:
    #: **這個欄位同時是加固剖面的分類來源**（#643）：node ⇒ V8 ⇒ JIT ⇒ 需要 W+X
    #: 記憶體 ⇒ `MemoryDenyWriteExecute=yes` 下起不來。剖面由
    #: :func:`executor_hardening_profile` 機械導出，**不另立第二張清單**。
    needs_node: bool
    #: 必須連同整包目錄複製（npm 套件樹），而不是單一檔案。
    copy_tree: bool
    note: str
    #: **誰在執行期 exec 它**（#661）。executor 是被 dispatch 直接執行的，因此留空；
    #: 非 executor 一定經由某個消費者被 exec，而**消費者所在的 unit 決定它實際跑在哪
    #: 一份加固面下**——這正是 #643 的剖面推導（只看 executor 名）看不到的那一格。
    #: 值為 executor 名（⇒ 該 executor 的 job 模板 unit）或 :data:`MANAGER_SURFACE`。
    consumed_by: tuple[str, ...] = ()
    #: 在 `SystemCallFilter=@system-service` 下，本程式**實機量到**會撞上的、被過濾
    #: 掉的 syscall（#673）。
    #:
    #: **這是與 `needs_node` 正交的第二個加固維度，刻意不共用那個欄位**：
    #: `needs_node` 導向「換一份放寬 `MemoryDenyWriteExecute` 的剖面」，本欄位導向
    #: 「被過濾時不得致命」——處置方向相反，適用面也不同（本欄位涵蓋跑在 Manager
    #: unit 上的非 executor，那一格根本沒有剖面）。詳見 `SECCOMP_FATALITY_KEY` 段。
    #:
    #: 只填**有 audit record 背書**的（`type=1326 … syscall=<n>`），不填形態推論。
    filtered_syscalls: tuple[str, ...] = ()
    #: 這支程式**自己會再開一層沙箱**來關住它跑的命令時，那一層的形態與執行條件
    #: （#714）。`None` ＝ 它不開內層沙箱（或還沒被量過——兩者在這裡是同一格，
    #: 因此新增 executor 時這一欄要與 `needs_node` 一樣被實機量一次）。
    #:
    #: **與 `needs_node`／`filtered_syscalls` 都正交**：`needs_node` 導向具名剖面，
    #: `filtered_syscalls` 導向「被過濾時不得致命」，本欄位導向「全域放行一組方向相反
    #: 的 syscall ＋ executor argv 上的一個形態選擇」。三個處置方向兩兩不同，因此是
    #: 三個欄位而不是一個（#673 立的規矩，本票是第二個實例）。
    inner_sandbox: "InnerSandboxSpec | None" = None


#: #640 的名字，保留為別名：那時表上只有 executor，型別名跟著語意走；#661 把非
#: executor 收進同一個形狀之後型別改叫 `ToolchainProgram`，舊名不動以免無謂的擾動。
ExecutorTool = ToolchainProgram


#: 0817 實機盤點的四個 executor。`needs_node` 為真者是 `codex` 與 `copilot`——
#: 這是「系統層 node 的版本風險涵蓋哪幾個 CLI」與「哪幾個 job 必須走放寬 W+X 的
#: 加固剖面」（#643）兩句話共同的機器可讀形式。
#:
#: **`copilot` 的 `needs_node` 於 #643 由 False 改為 True**：#640 落表時只知道它是
#: shell script、還沒查它內部 exec 什麼（表上的 note 當時就寫著「安裝時務必
#: `head -n 20` 查一次」）。#643 在真實加固面下逐項隔離量到 `copilot --version` 在
#: `MemoryDenyWriteExecute=yes` 下**空輸出**、拿掉即正常，與 `codex` 的症狀逐字相同
#: ——它內部 exec 的就是 node。把量到的事實回填到既有那張表，而不是為剖面另開一張。
EXECUTOR_TOOLS: tuple[ExecutorTool, ...] = (
    ExecutorTool(
        "codex", ExecutorShape.NODE_SCRIPT, needs_node=True, copy_tree=True,
        filtered_syscalls=("pkey_alloc",),
        inner_sandbox=CODEX_LEGACY_LANDLOCK,
        note=(
            "唯一硬需要 node 的：本體是 JS，進入點的 shebang 是 `#!/usr/bin/env node`。"
            "單搬那支 `.js` 會缺 `node_modules`，必須整包搬 npm 套件樹。"
            "**版本漂移的實例就在它身上**：同一台機器上系統層是 0.42.0、operator 實際"
            "在用的是 0.147.0，差 100 個以上小版本。"
        ),
    ),
    ExecutorTool(
        "claude", ExecutorShape.NATIVE_ELF, needs_node=False, copy_tree=False,
        note="自帶原生執行檔，**不因 node 版本而行為改變**——node 的版本風險不涵蓋它。",
    ),
    ExecutorTool(
        "copilot", ExecutorShape.SHELL_SCRIPT, needs_node=True, copy_tree=False,
        filtered_syscalls=("pkey_alloc",),
        note=(
            "shell script，但**內部再 exec node**（#643 實機量測確認：完整加固面下 "
            "`--version` 空輸出，單獨拿掉 `MemoryDenyWriteExecute` 即正常，與 codex "
            "的症狀逐字相同）。因此它同樣吃系統層 node 的版本風險，加固剖面也與 "
            "`codex` 同一份。安裝時仍應 `head -n 20` 查一次它實際 exec 什麼——它可能"
            "再叫別的程式，該相依同樣要在 job 的 PATH 上或一併搬進 toolchain。"
        ),
    ),
    ExecutorTool(
        "agy", ExecutorShape.NATIVE_ELF, needs_node=False, copy_tree=False,
        note="自帶原生執行檔，同 `claude`：不受系統層 node 版本影響。",
    ),
)


#: **非 executor** 的部署樹程式（#661）。與 :data:`EXECUTOR_TOOLS` 同一個形狀、同一棵
#: 樹、同一份權限（root-owned 0755，全部 job／服務帳號唯讀＋可執行），只是**不是**
#: dispatch 得到的模型 CLI，因此不參與 `executor_hardening_profile()` 的名字判準。
#:
#: 落點判準沿用 #640 裁決 (a)：**版本會不會影響治理產出**——會 ⇒ 部署樹（可稽核的
#: 部署決定）；純通用 runtime／傳輸層 ⇒ 系統層（見 :data:`SYSTEM_PROGRAMS`）。
#:
#: 每一列都必須填 `consumed_by`：非 executor 的程式沒有自己的 unit，它跑在**消費者
#: 的**加固面上，而那一格 #643 的剖面推導看不到（見 :func:`node_execution_surfaces`）。
SERVICE_TOOLS: tuple[ToolchainProgram, ...] = (
    ToolchainProgram(
        "srt", ExecutorShape.NODE_SCRIPT, needs_node=True, copy_tree=True,
        filtered_syscalls=("pkey_alloc",),
        note=(
            "`@anthropic-ai/sandbox-runtime` 的進入點（npm bin `srt` → `dist/cli.js`），"
            "**Claude review sandbox 的強制面**：doctor 的 `review-sandbox` probe 與 "
            "`coordinator/launcher.py` 都要它。\n"
            "**必須整包搬**——這是 #661 實機量到的失敗，不是預防性判斷：`dist/cli.js` 是 "
            "ESM，第一行就 `import { quote } from './utils/shell-quote.js'`，單搬那一支到 "
            "`<toolchain>/bin/srt` 之後相對 import 會解到 `<toolchain>/bin/utils/…`：\n"
            "    Error [ERR_MODULE_NOT_FOUND]: Cannot find module "
            "'<toolchain>/bin/utils/shell-quote.js'      rc=1\n"
            "而 `srt --version` rc≠0 正是 doctor 的 `Claude sandbox dependency execution "
            "failed`。套件另有自己的 `node_modules`（socks5-server／commander／node-forge／"
            "zod）與 `vendor/`（`apply-seccomp`），單檔複製一個都拿不到。\n"
            "**第二個、更安靜的後果**：`launcher._srt_runtime_root()` 是從 "
            "`which(\"srt\")` 往上找 `package.json` 且 `name == @anthropic-ai/"
            "sandbox-runtime` 來解出套件根，再把它加進 reviewer sandbox 政策的 "
            "`allowRead`。單檔形態下它解出 `None`——沙箱政策少一條放行且**不報錯**。"
            "因此 `bin/srt` 必須是指進 `lib/` 套件樹的 symlink（與 `codex` 同形），"
            "`resolve()` 之後才找得到那個 `package.json`。\n"
            "**版本是部署決定**：它決定 sandbox 政策**怎麼被套用**，與模型 CLI 同級。"
        ),
        consumed_by=("claude",),
    ),
    ToolchainProgram(
        "openspec", ExecutorShape.NODE_SCRIPT, needs_node=True, copy_tree=True,
        filtered_syscalls=("pkey_alloc",),
        note=(
            "`@fission-ai/openspec` 的進入點（`bin/openspec.js`）。ship 段的 "
            "`openspec archive -y` 與 preflight 的 `openspec validate` 都是**採信判準**"
            "——它的版本直接決定一筆交付能不能被接受，因此與模型 CLI 同級，進部署樹。\n"
            "**#661 實機盤點**：它和四個 executor 一樣住在 operator 的 nvm 樹底下"
            "（`~/.nvm/.../lib/node_modules/@fission-ai/openspec`），`ProtectHome=yes` "
            "之後同樣不可達——這是 #640 那一族**第三個**沒被盤到的成員。node script ＋ "
            "npm 套件樹，搬法與 `codex`／`srt` 逐字相同。"
        ),
        consumed_by=(MANAGER_SURFACE,),
    ),
)

#: 部署樹 toolchain 的**完整**盤點（executor ∪ 非 executor）。落位計畫由它導出；
#: `executor_hardening_profile()` 刻意只看 `EXECUTOR_TOOLS`。
TOOLCHAIN_PROGRAMS: tuple[ToolchainProgram, ...] = EXECUTOR_TOOLS + SERVICE_TOOLS


# ---------------------------------------------------------------------------
# per-(account, executor) 憑證表（#685／#672 U-5 裁決；U-4 追認雙 independence domain）
#
# ## 這張表取代了什麼
#
# #640 落地時，「executor 的登入態長什麼樣」是**單一部署決定**：
# `PathLayout.executor_credential_relpath`（預設 `.codex/auth.json`）。一個帳號因此只
# 表達得了一份憑證，而三分／四分部署下 `cortex-reviewer-planner` 同時需要 codex 與 agy
# 兩個 independence domain 的登入態（否則 `select_secondary_planner()` 結構性找不到異質
# planner，見 #668／#672）。U-5 的裁決就是把它擴成**兩軸**：
#
#   - **executor 軸**＝:data:`EXECUTOR_CREDENTIALS`：一個 executor 的登入態是什麼形狀、
#     落在 HOME 底下哪裡；
#   - **account 軸**＝:data:`CREDENTIALED_ACCOUNTS`：哪個帳號**被核可**持有哪幾個
#     executor 的登入態。
#
# 兩軸的交集就是登記表資產：`asset_paths()`／`scaffold_directories()`／
# :data:`IN_PLACE_CONTENT_WRITE_ASSETS`／unit 的 `ReadWritePaths` 全部由它機械導出，
# 加一格不必改產生器——這正是 #640 的 note 當年承諾、但因為只有一個純量而做不到的事。
#
# ## 為什麼形狀有三種（#686 實測，design 未預期；#698 補上第三種）
#
# design 假設所有 executor 的登入態都是**單檔**（codex 的 `auth.json`），因此 #640 裁決
# (b) 的整套性質（檔 job-owned 0600／父目錄 root-owned ⇒ 能改內容、不能增刪換）對它成立。
# 票 E（#686）在**完整 reviewer unit 沙箱**下逐 executor 實測，推翻了這個假設：
#
#   - `codex` 需要 **`$CODEX_HOME` 整個目錄可寫**（`state_5.sqlite`／`logs_2.sqlite`／
#     `sessions/`／`skills/`／`plugins/`／`thread-writer-locks/`…）。實機錯誤逐字：
#     `Error: failed to initialize in-process app-server client: Read-only file system`。
#     **只放行 `auth.json` 一個檔，codex 連起都起不來**；且症狀與 cwd 無關（cwd 換成可寫
#     的 `/tmp` 症狀相同，維持唯讀 cwd 只把 `CODEX_HOME` 指到可寫目錄則 rc=0）。
#   - `agy` 需要 `~/.gemini` 底下一整棵可寫狀態樹（conversations SQLite、crashes、
#     presence lock、builtin skills，並自解出一個 17 MB 的 `bin/webm_encoder`）。
#   - `claude` 在 job 沙箱下 CLI 走得完整條（rc=0），但回 `Not logged in`——它需要
#     `~/.claude` 底下的登入態，同樣是一棵它自己會寫的樹。
#
# 因此表上有多種形狀，見 :class:`CredentialShape`。
#
# ## #698：形狀不再由帳號挑，而是由**一條規則**導出
#
# #685 落地後表上是「builder×codex＝`IN_PLACE_FILE`、reviewer-planner×codex＝
# `HOME_REDIRECT_TREE`」——同一個 executor 在兩個帳號上兩種形狀。0818 的 R9 實測
# （#698）證明那個差異不是設計，是**一個尚未爆的洞**：`HOME_REDIRECT_TREE` 的樹由 job
# 帳號擁有，該帳號因此**成功植入了 `hooks.json`**（R9 T3.9 `!! SUCCEEDED`）。codex hooks
# **會執行命令** ⇒ 跨 job 持久化 ⇒ 四分隔離「每個 job 一次性」的前提在該帳號上不成立。
# builder 當天守得住只是因為它的 `.codex` 還沒遷成可寫樹；一旦要讓它在降權 unit 下真的
# 跑 codex，同一個洞原封不動出現。
#
# operator 裁決（#698，採方案 A）：**目錄 sticky bit ＋ `hooks.json` 由 root 擁有**。
# 落到本表上就是 :data:`EXECUTOR_ENFORCEMENT_LEAVES` 那一條規則：
#
#   **某個 executor 的狀態樹裡必須住著一個 root-owned 的 enforcement 檔 ⇒ 凡持有它
#   登入態的帳號，那一格一律是 `HOME_STICKY_TREE`。**
#
# 規則由 :func:`_assert_shape_follows_enforcement_rule` 在 **import 當下**強制（不是靠
# 註解也不是靠 review）：把某一格改回別的形狀、或新增第五個帳號時漏改，模組載入就炸。
# 「同一個 executor 在不同帳號上可以是不同形狀」這句話因此仍然為真（agy／claude 只在
# 一個帳號上），但**不再能對同一個 executor 成立**——那正是 #698 要根除的差異。
# ---------------------------------------------------------------------------

class CredentialShape(Enum):
    """executor 登入態在帳號 HOME 底下的**形狀**。兩種形狀的保護機制完全不同。"""

    #: **單檔、就地 `O_TRUNC` 覆寫**（#640 裁決 (b)）。檔案 job-owned 0600、父目錄維持
    #: root-owned ⇒ job 改得了內容、建不了新檔、刪不掉、也換不掉同目錄下的 root-owned
    #: 檔（例如 `codex-hooks`）。`ReadWritePaths` 因此掛在**檔案本身**
    #: （:data:`IN_PLACE_CONTENT_WRITE_ASSETS`），父目錄連 mount 層都不開放可寫。
    #: 代價（裁決刻意接受）：以「暫存檔 ＋ rename」做 refresh 的 CLI 走不通。
    IN_PLACE_FILE = "in-place-file"

    #: **HOME 底下的一整棵可寫狀態樹**，以一條 **root-owned symlink** 導進該帳號既有的
    #: `cache`（`<HOME>/<relpath> -> <HOME>/cache/<target>`）。
    #:
    #: 為什麼是這個形狀而不是「把 HOME 那一層目錄開成可寫」：
    #:
    #: 1. **不新增任何可寫面**——`cache` 早已在 job 模板 unit 的 `ReadWritePaths` 內
    #:    （`PathLayout.job_extra_write_paths`），因此登記這一格之後 unit 的
    #:    `ReadWritePaths=` **逐字不變**（`test_home_redirect_adds_no_writable_surface`
    #:    機械釘住）。executor 能做的事，該帳號今天就已經能做。
    #: 2. **symlink 本身放在 root-owned 的 HOME 裡**（`scaffold_directories()` 產出
    #:    `<HOME>` 為 root:root 0755），因此 job **換不掉它的指向**——換 symlink 需要對
    #:    父目錄的寫入權。
    #: 3. **不必動 job 的環境變數**。`CODEX_HOME`／`GEMINI_*` 這類覆寫要經
    #:    `job_runner.build_job_env()` 的白名單，那是另一條要維護的放行面；symlink 讓
    #:    executor 的**預設**路徑解析就落在對的地方。
    #:
    #: 代價（明講，見 CHANGELOG 的 R-6）：這棵樹由 job 帳號擁有，因此樹裡的憑證葉檔
    #: （`auth.json`／`.credentials.json`）**可被該 job 刪除或替換**——它換來的是
    #: 「codex／claude 能不能起得來」。影響面限於該帳號自己的登入態；同目錄下**不得**
    #: 再放任何 root-owned 的 enforcement 檔——這條在 #698 之前只是註解，現在由
    #: :func:`_assert_shape_follows_enforcement_rule` 在 import 當下強制。
    HOME_REDIRECT_TREE = "home-redirect-tree"

    #: **HOME 底下一棵 root-owned ＋ sticky bit 的真目錄**，job 帳號以一條 `rwx`
    #: **access** ACL 取得整棵的寫入權（#698 operator 裁決＝方案 A）。
    #:
    #: 它同時買到 `HOME_REDIRECT_TREE` 與 `IN_PLACE_FILE` 各買到一半的那兩件事，
    #: 而在 #685 的形狀下兩者互斥：
    #:
    #: 1. **executor 起得來**：整棵可寫。#686 實測 codex 需要 `$CODEX_HOME` 整棵可寫
    #:    （`state_5.sqlite`／`sessions/`／`skills/`／`plugins/`…，檔名帶版本序號，
    #:    逐項列舉會在下次 CLI 升版時無聲失效），因此放行的是整棵樹而不是清單。
    #: 2. **樹裡放得住一個 job 動不了的 root-owned 檔**：sticky bit（`chmod +t`）的
    #:    POSIX 語意是「目錄可寫，但只有**檔案 owner／目錄 owner／root** 刪得掉或
    #:    改得掉名字」。`hooks.json` 屬 root、目錄也屬 root ⇒ job 既 **unlink 不掉**、
    #:    也 **rename 不掉**；而它自己的 mode（root:root 0644）讓 job 落在 `other` 位
    #:    ⇒ 連**內容都改不了**。三個動詞同時關上，缺一條就等於沒關。
    #:
    #: **為什麼是具名 ACL 而不是 group 寫入位**：spec §R2 明定「group 寫入權 MUST
    #: 移除」，而本產生器的安全網（見 :func:`build_entry` 尾端）會機械地拿掉它。
    #: 具名 ACL 是這套權限模型既有的跨帳號授權手段（`AclEntry`），不必為本形狀鑿一個
    #: §R2 的洞。
    #:
    #: ⚠️ **只設 access ACL，絕不設 default ACL**。default ACL 決定的是「**之後**在這個
    #: 目錄裡新建的物件」的初值——包含 root 日後重放一次 `hooks.json` 的那一次。設了
    #: default ACL 等於把 `hooks.json` 自動交還給 job，整個形狀當場歸零。
    #: :meth:`PermissionEntry.commands` 因此對 sticky 目錄**不**補那條 default ACL。
    #:
    #: ⚠️ **enforcement 檔必須先存在**，否則 sticky 什麼也擋不住：sticky 管的是「刪／
    #: 改名別人的檔」，**不管「建一個還不存在的檔」**。因此本形狀的每一格都必須宣告
    #: `enforcement_leaf`，且部署／遷移步驟要先把它以 root 身分放進去（runbook 第
    #: 4e-2b 步）。少了那一步，R9 T3.9 會直接以「建得出新檔」翻紅。
    #:
    #: 代價（與 `HOME_REDIRECT_TREE` 相同、不多不少）：樹由 job 寫，因此樹裡的**憑證**
    #: 葉檔仍可被該 job 刪除或替換（R-6）。sticky 保護的是 root-owned 的那些檔，而
    #: token 葉檔刻意是 job-owned 的——它必須 refresh 得回來。
    HOME_STICKY_TREE = "home-sticky-tree"


@dataclass(frozen=True)
class ExecutorCredential:
    """per-(account, executor) 表的 **executor 軸**：一個 executor 的登入態長什麼樣。"""

    executor: str
    shape: CredentialShape
    #: HOME 相對落點。`HOME_REDIRECT_TREE` 時＝那條 symlink 自己。
    #: `None`＝由 :attr:`PathLayout.executor_credential_relpath` 這個既有部署決定欄位
    #: 供給（只有 :data:`PRIMARY_CREDENTIAL_EXECUTOR` 那一列會是 `None`，理由見該常數）。
    relpath: str | None
    #: 登記表資產 id 的後綴（`<帳號前綴>-<suffix>`）。
    asset_suffix: str
    #: `HOME_REDIRECT_TREE` 的 symlink 目標，**相對於該帳號的 `cache`**。刻意不是任意
    #: 絕對路徑：目標必須落在該帳號本來就可寫的那一層裡，否則「不新增可寫面」不成立。
    cache_target: str | None = None
    #: 這一格裡真正承載 token 的葉檔（相對於 `relpath`）。只進註解與 runbook，不另立資產
    #: ——它落在 job-owned 樹裡，權限由 unit 的 `UMask=0077` 決定，再登記一次是第二份真相。
    #: `relpath is None` 的那一列留空：它的樹與葉**都**由
    #: :attr:`PathLayout.executor_credential_relpath` 這個部署決定的 head／tail 導出。
    token_leaf: str = ""
    #: **這棵樹裡必須住著的 root-owned enforcement 檔**（相對於 `relpath`），例如 codex
    #: 的 `hooks.json`（#698）。有值 ⇒ 本格的形狀**必須**是 `HOME_STICKY_TREE`，且它會
    #: 機械地長出一個登記表資產（`<帳號前綴>-<enforcement_asset_suffix>`）。
    #: 空字串＝這個 executor 的狀態樹裡沒有 enforcement 檔（agy／claude）。
    enforcement_leaf: str = ""
    #: 上一欄那個資產 id 的後綴。宣告 `enforcement_leaf` 時必填。
    enforcement_asset_suffix: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.shape is CredentialShape.HOME_REDIRECT_TREE and not self.cache_target:
            raise ValueError(f"{self.executor}：HOME_REDIRECT_TREE 必須宣告 cache_target")
        if self.shape is CredentialShape.HOME_STICKY_TREE and self.cache_target:
            raise ValueError(
                f"{self.executor}：HOME_STICKY_TREE 是 HOME 底下的**真目錄**，沒有 "
                "cache_target。宣告一個目標會讓 `symlink_targets()` 把它當成 symlink，"
                "而 `ln -sfn` 對真目錄會把連結建到目錄**裡面**（#698 的部署陷阱）。"
            )
        # #698：兩個方向都擋。有 enforcement 檔卻不是 sticky ⇒ 那個檔擋不住替換
        # （正是 R9 T3.9 攻破 reviewer-planner 的形態）；是 sticky 卻沒有 enforcement
        # 檔 ⇒ 這棵樹沒有理由付出 root-owned 目錄的代價，形狀選錯了。
        if bool(self.enforcement_leaf) != (self.shape is CredentialShape.HOME_STICKY_TREE):
            raise ValueError(
                f"{self.executor}：`enforcement_leaf` 與 HOME_STICKY_TREE 必須同時成立"
                f"（現況 shape={self.shape.value}, enforcement_leaf={self.enforcement_leaf!r}）。"
                "sticky bit 的全部價值就是「樹裡放得住一個 job 動不了的 root-owned 檔」"
                "——沒有那個檔就不需要它，有那個檔就非它不可（#698）。"
            )
        if self.enforcement_leaf and not self.enforcement_asset_suffix:
            raise ValueError(
                f"{self.executor}：宣告 enforcement_leaf 就必須一併宣告 "
                "enforcement_asset_suffix——那個檔要進登記表才管得住。"
            )
        if self.relpath is not None:
            _validate_home_relpath(self.relpath)
        if self.cache_target is not None:
            _validate_home_relpath(self.cache_target)
        if self.enforcement_leaf:
            _validate_home_relpath(self.enforcement_leaf)


#: `relpath is None` 的那一列所對應的 executor。**它與
#: :attr:`PathLayout.executor_credential_relpath` 是同一個部署決定的兩半**：那個欄位
#: 存在的理由是「換 primary executor 時只改一個值」（#640），把它的值再抄一份進本表
#: 就是第二份真相。因此本表對它留 `None`，由 layout 供給。
PRIMARY_CREDENTIAL_EXECUTOR = "codex"

#: **executor → 它的狀態樹裡必須住著的 root-owned enforcement 檔**（#698）。
#:
#: 這一格就是 #698 的那條規則的**輸入**：有值的 executor，在**每一個**持有它登入態的
#: 帳號上都必須是 :attr:`CredentialShape.HOME_STICKY_TREE`
#: （由 :func:`_assert_shape_follows_enforcement_rule` 在 import 當下強制）。
#:
#: 只有 codex 有一格，而那不是偏好問題：codex hooks **會執行命令**，因此「誰能改
#: `hooks.json`」直接決定「上一個 job 能不能為下一個 job 埋伏」。agy／claude 的狀態樹
#: 裡沒有同性質的檔，因此它們留在 `HOME_REDIRECT_TREE`——**同一條規則**的另一邊。
#: 哪天量到 `claude` 也有一個會執行命令的 root-owned 設定檔，往這裡加一列即可，兩個
#: 帳號的形狀會一起跟著變。
EXECUTOR_ENFORCEMENT_LEAVES: Mapping[str, str] = MappingProxyType({
    "codex": "hooks.json",
})

#: **executor 軸**。順序＝資產在登記表裡的出現順序。
EXECUTOR_CREDENTIALS: tuple[ExecutorCredential, ...] = (
    ExecutorCredential(
        "codex", CredentialShape.HOME_STICKY_TREE,
        relpath=None, asset_suffix="codex-state",
        token_leaf="",
        enforcement_leaf=EXECUTOR_ENFORCEMENT_LEAVES["codex"],
        enforcement_asset_suffix="codex-hooks",
        note=(
            "codex 的 `$CODEX_HOME` 整棵（預設就是 `~/.codex`）。#686 在完整 reviewer "
            "unit 沙箱下實測：唯讀時 `failed to initialize in-process app-server client: "
            "Read-only file system`，且**與 cwd 無關**；只把 `CODEX_HOME` 指到可寫目錄"
            "即 rc=0、輸出正確。底下會出現 `state_5.sqlite`／`logs_2.sqlite`／"
            "`queue_1.sqlite`／`memories_1.sqlite`／`sessions/`／`skills/`／`plugins/`／"
            "`thread-writer-locks/`／`models_cache.json`／`installation_id`——**檔名帶版本"
            "序號**，逐項列舉會在下一次 CLI 升版時無聲失效，因此放行的是整棵樹而不是清單。\n"
            "**#698 起這一列是 `HOME_STICKY_TREE`，而且 builder 與 reviewer-planner 共用"
            "它**（表上因此只剩一列 codex）。#685 當時是兩列：builder 的 `IN_PLACE_FILE`"
            "（codex 在降權 unit 下**起不來**）＋ reviewer-planner 的 `HOME_REDIRECT_TREE`"
            "（codex 起得來，但 job **植得進 `hooks.json`** ⇒ 跨 job 持久化，R9 T3.9 實測"
            "攻破）。sticky 樹兩件事一起買到：整棵可寫 ⇒ codex 起得來；目錄 root-owned ＋"
            "sticky ⇒ root 的 `hooks.json` 刪不掉、改不掉名字、也改不了內容。\n"
            "`relpath=None`：樹與 token 葉檔**都**由 `PathLayout.executor_credential_relpath`"
            "（部署決定，預設 `.codex/auth.json`）的 head／tail 導出——換 primary executor "
            "時仍然只改那一個值，而且樹與葉不可能各改一半。"
        ),
    ),
    ExecutorCredential(
        "agy", CredentialShape.HOME_REDIRECT_TREE,
        relpath=".gemini", asset_suffix="agy-state",
        cache_target="gemini", token_leaf="antigravity-cli/antigravity-oauth-token",
        note=(
            "agy 的可寫狀態樹（U-7 裁決＝design 的 (a)：登記成 symlink 類資產）。agy 執行"
            "時往 `~/.gemini/antigravity-cli/` 寫 conversations SQLite、crashes、presence "
            "lock、builtin skills，並**自解出一個 17 MB 的可執行檔** `bin/webm_encoder`。"
            "0818 實測：以 `cortex-reviewer-planner` 身分、在逐條複製落檔 unit 全部 "
            "property 的沙箱下跑 `agy --print … --mode plan --sandbox`，rc=0、輸出逐位元"
            "等於 `probe_agy_capability()` 的 expected（#686 複驗同一結論）。"
        ),
    ),
    ExecutorCredential(
        "claude", CredentialShape.HOME_REDIRECT_TREE,
        relpath=".claude", asset_suffix="claude-state",
        cache_target="claude", token_leaf=".credentials.json",
        note=(
            "claude 的登入態與狀態樹。#686 實測：job 沙箱下 CLI **走得完整條**（rc=0），"
            "回的是 `{\"is_error\":true,…,\"result\":\"Not logged in · Please run /login\"}`"
            "——擋住的是憑證，不是加固面。job 模式下 `CLAUDE_CONFIG_DIR` 在 "
            "`job_runner.DENIED_ENV_NAMES` 內（design D-g 的帳號隔離取代了 in-process 的"
            "一次性 config dir），因此 claude 解到的就是 `$HOME/.claude`——本列登記的就是"
            "那一條。"
        ),
    ),
)

#: **account 軸**：登記表資產 id 的帳號前綴 → 該帳號**被核可持有登入態**的 (executor,
#: shape) 組合。前綴（而非 OS 帳號名）當 key 是刻意的——資產 id 必須與帳號改名無關。
#:
#: **`reviewer-planner` 有三格，這是 U-4 的追認範圍**：該帳號同時持有 openai（codex）、
#: google（agy）與 anthropic（claude）三個 provider 的登入態。design 的安全退步 **R-3**
#: （該帳號被攻陷時多邊 token 一起失，而 planner 正是吃 untrusted issue 內容的角色）是
#: **明文接受的有界殘餘風險**，不另拆帳號。它在後續任何「planner 攻擊面」討論中**不得**
#: 被當成未知——U-4 的裁決逐字如此。
#:
#: **`builder` 只有一格**：builder 不做 planning，不需要異質性；擴大它的 provider
#: 曝險面買不到任何東西。但那一格的**形狀**與 reviewer-planner 的 codex 逐字相同
#: （#698）——形狀不是 per-account 的偏好，是 :data:`EXECUTOR_ENFORCEMENT_LEAVES`
#: 那條規則的結果。
CREDENTIALED_ACCOUNTS: Mapping[str, tuple[tuple[str, CredentialShape], ...]] = (
    MappingProxyType({
        "builder": (("codex", CredentialShape.HOME_STICKY_TREE),),
        "reviewer-planner": (
            ("codex", CredentialShape.HOME_STICKY_TREE),
            ("agy", CredentialShape.HOME_REDIRECT_TREE),
            ("claude", CredentialShape.HOME_REDIRECT_TREE),
        ),
    })
)


class UnregisteredExecutorCredentialError(KeyError):
    """(account, executor) 這一格不在 :data:`CREDENTIALED_ACCOUNTS` 上。

    **fail-closed 而不是回一個猜出來的路徑**：猜錯的後果是「指紋盯著一個不存在的檔、
    unit 放行一條錯的路徑」，兩者都會靜默通過。呼叫端（例如 probe 快取的指紋計算）
    把它收成一個**可辨識的標記**，而不是當成「憑證不存在」。
    """


def _credential_row(prefix: str, executor: str, shape: CredentialShape) -> ExecutorCredential:
    for cred in EXECUTOR_CREDENTIALS:
        if cred.executor == executor and cred.shape is shape:
            return cred
    raise UnregisteredExecutorCredentialError(f"{prefix}:{executor}:{shape.value}")


def credential_asset_id(prefix: str, credential: ExecutorCredential) -> str:
    """`<帳號前綴>-<suffix>`。與 layout 無關 ⇒ 換帳號名不會改資產 id。"""
    return f"{prefix}-{credential.asset_suffix}"


def credential_rows() -> tuple[tuple[str, ExecutorCredential], ...]:
    """把兩軸展開成 `(帳號前綴, 該格的 executor 憑證)`。**唯一的展開點。**"""
    rows: list[tuple[str, ExecutorCredential]] = []
    for prefix, cells in CREDENTIALED_ACCOUNTS.items():
        for executor, shape in cells:
            rows.append((prefix, _credential_row(prefix, executor, shape)))
    return tuple(rows)


def credential_asset_ids(shape: CredentialShape | None = None) -> tuple[str, ...]:
    """本表產生的登記表資產 id（可依形狀過濾）。`_FILE_ASSET_IDS`／
    :data:`IN_PLACE_CONTENT_WRITE_ASSETS`／:data:`SYMLINK_ASSETS`／
    :data:`STICKY_JOB_WRITABLE_DIR_ASSETS` 全部由它導出。"""
    return tuple(
        credential_asset_id(prefix, cred)
        for prefix, cred in credential_rows()
        if shape is None or cred.shape is shape
    )


def enforcement_asset_id(prefix: str, credential: ExecutorCredential) -> str:
    """該格 enforcement 檔的登記表資產 id（`<帳號前綴>-<enforcement_asset_suffix>`）。"""
    if not credential.enforcement_asset_suffix:
        raise ValueError(f"{credential.executor}：這一格沒有 enforcement 檔")
    return f"{prefix}-{credential.enforcement_asset_suffix}"


def enforcement_rows() -> tuple[tuple[str, ExecutorCredential], ...]:
    """有 enforcement 檔的那些格（`(帳號前綴, 憑證)`）。#698 的 hooks 資產由它導出。"""
    return tuple(
        (prefix, cred) for prefix, cred in credential_rows() if cred.enforcement_leaf
    )


def enforcement_asset_ids() -> tuple[str, ...]:
    """全部 enforcement 檔的資產 id。**兩個帳號各一份，由同一條規則長出來。**"""
    return tuple(enforcement_asset_id(prefix, cred) for prefix, cred in enforcement_rows())


def credential_for(prefix: str, executor: str) -> ExecutorCredential:
    """該帳號在該 executor 上被核可的憑證形狀；沒有這一格就 raise（fail-closed）。"""
    for cell_prefix, cred in credential_rows():
        if cell_prefix == prefix and cred.executor == executor:
            return cred
    raise UnregisteredExecutorCredentialError(f"{prefix}:{executor}")


def _assert_shape_follows_enforcement_rule() -> None:
    """#698 的那條規則，在 **import 當下**強制。

    **規則**：executor 在 :data:`EXECUTOR_ENFORCEMENT_LEAVES` 上有一格 ⇒ 凡持有它
    登入態的帳號，那一格一律是 :attr:`CredentialShape.HOME_STICKY_TREE`。

    為什麼是 import 當下而不是一條測試：0818 的實測破口（#698）不是「有人寫錯一行」，
    是「兩個帳號的形狀**各自**被決定，於是其中一個先遷、另一個留在原地等著爆」。
    在展開點上強制，讓「只改一格」在**結構上做不到**——新增第五個帳號時漏改的症狀是
    模組載不起來，而不是三個月後的一次 R9 紅字。
    """
    for prefix, cred in credential_rows():
        want = EXECUTOR_ENFORCEMENT_LEAVES.get(cred.executor)
        if want and cred.shape is not CredentialShape.HOME_STICKY_TREE:
            raise ValueError(
                f"{prefix}×{cred.executor}：這個 executor 的狀態樹裡住著 root-owned 的 "
                f"`{want}`，因此該格必須是 HOME_STICKY_TREE（現況 {cred.shape.value}）。"
                "#698：非 sticky 的可寫樹裡，job 隨時 unlink／rename 掉那個檔——R9 T3.9 "
                "在 `cortex-reviewer-planner` 上實測攻破過一次，不再接受第二次。"
            )
        if want and cred.enforcement_leaf != want:
            raise ValueError(
                f"{prefix}×{cred.executor}：enforcement 檔名與 "
                f"EXECUTOR_ENFORCEMENT_LEAVES 不一致（{cred.enforcement_leaf!r} vs {want!r}）。"
            )
        if not want and cred.enforcement_leaf:
            raise ValueError(
                f"{prefix}×{cred.executor}：宣告了 enforcement 檔卻不在 "
                "EXECUTOR_ENFORCEMENT_LEAVES 上——規則的輸入只有那一張表。"
            )


_assert_shape_follows_enforcement_rule()


@dataclass(frozen=True)
class SystemProgram:
    """走**系統層**（發行版套件）的外部程式：不進部署樹，但仍是部署決定。"""

    name: str
    #: 取得方式（apt 套件名；非 apt 者寫來源形態）。
    source: str
    #: 誰需要它——用來讓 runbook 的驗證步驟知道要以**哪個帳號**實跑一次。
    required_by: tuple[str, ...]
    note: str


#: 走系統層的那一半（#640 裁決 (a) 的另一半，#661 補完）。判準：版本換掉幾乎不影響
#: 治理產出（通用 runtime 或純傳輸層）。它們仍是**部署決定**——某個 CLI 哪天提高下限
#: 時要一併升，否則會變成下一個無聲漂移點。
#:
#: **#661 之前這裡只有 `node` 一列**，而那是不完整的盤點：`srt` 在 Linux 上實際會去
#: exec `bwrap` 與 `socat`（doctor 的 `review-sandbox` probe 逐一跑它們的 `--version`），
#: Manager 則需要 `git`／`gh`。缺任何一支的症狀都不是「設定錯」而是「跑到一半才失敗」。
#:
#: **#666 的窮舉盤點又補上三支**，而三支都是「一直在用、只是從來沒被寫下來」的那種：
#: `bash`（每一支 job 的 `command[0]` 就是它——wrapper 是 `bash -c <script>`）、
#: `python3`（gate 宣告 `python3 -m pytest` 時解析到的是**系統層**那一支，不是部署
#: venv 的）、`systemctl`（B 案定案之後，Manager 派工的第一個動作就是 exec 它）。
#: 判準沒有改變——它們仍是「版本換掉幾乎不影響治理產出」的通用工具；改變的只是
#: 「盤點有沒有做完」。
SYSTEM_PROGRAMS: tuple[SystemProgram, ...] = (
    SystemProgram(
        "node", "apt: nodejs", ("codex", "copilot", "srt", "openspec"),
        note=(
            "通用 JS runtime，換版本幾乎不影響產出，因此不進部署樹；但版本本身仍是"
            "部署決定（目前 codex 宣告 `node >=16`）。所有 `needs_node` 的 toolchain "
            "程式都吃它。"
        ),
    ),
    SystemProgram(
        "git", "apt: git", ("manager", "builder", "reviewer-planner"),
        note="來源樹／per-job clone／baseline 解析全靠它；純工具，版本不影響治理判準。",
    ),
    SystemProgram(
        "gh", "apt: gh（GitHub CLI apt repo）", ("manager",),
        note=(
            "Manager 對 GitHub 的傳輸層（PR metadata、checks、merge），以及 preflight "
            "以 `--pr <N>` 取 PR 上下文時的來源。純傳輸層 ⇒ 系統層。"
        ),
    ),
    SystemProgram(
        "bwrap", "apt: bubblewrap", ("srt",),
        note=(
            "`srt` 在 Linux 上的 namespace 隔離實作。doctor 的 `review-sandbox` probe "
            "會實跑 `bwrap --version`，rc≠0 即 fail。隨發行版走 ⇒ 系統層。"
        ),
    ),
    SystemProgram(
        "socat", "apt: socat", ("srt",),
        note=(
            "`srt` 網路政策那一段的 socket 轉發。同樣由 `review-sandbox` probe 實跑 "
            "`socat -V` 驗證。"
        ),
    ),
    SystemProgram(
        "bash", "apt: bash（base system）", ("builder", "reviewer-planner", "manager"),
        note=(
            "**每一支 job 的 `command[0]` 就是它**（#666 窮舉盤點補上）。"
            "`launcher.build_wrapper_script()` 產的是一段 shell script，"
            "`launcher` 交給 runner 的 argv 是 `bash -c <script>`（reviewer 與降權 "
            "builder）或 `bash -lc <script>`（direct builder）；降權模式下 shim 的 "
            "`os.execvpe(command[0], …)` 執行的第一支程式因此是 `bash`。Manager 側的 "
            "exit 記帳 shell（`job_runner.build_manager_exit_recorder_argv`）同樣是 "
            "`bash -c`。\n"
            "**為什麼它一直沒被盤到**：它落在 `JOB_PATH_SYSTEM_TAIL` 的 `/bin` 裡、"
            "而且從來沒缺過，所以「沒被登記」與「不需要」在實機上長得一樣。這正是本"
            "族每一次的形態——把它寫下來的價值不在於它會缺，而在於它是 job 執行面的"
            "**第一段**：`ProtectSystem=strict` 或 `NoExecPaths` 之類的加固項哪天涵蓋"
            "到 `/bin`，症狀會是「每一個 job 都起不來且沒有輸出」。"
        ),
    ),
    SystemProgram(
        "python3", "apt: python3", ("gate", "srt", "manager"),
        note=(
            "**gate 宣告解析到的是這一支，不是部署 venv 的那一支**（#666）。"
            "`PSC_GATE_CMD_PYTEST=\"python3 -m pytest -q\"` 是相對名，由 gate 的 "
            "`PSC_GATE_PATH`（`<toolchain>/bin` ＋ `JOB_PATH_SYSTEM_TAIL`）解析 ⇒ "
            "`/usr/bin/python3`。gate unit 自己的 `ExecStart` 用的是 "
            "`gate_runner.DEFAULT_GATE_PYTHON`（`<venv>/bin/python3`），但那只涵蓋 "
            "ledger writer 本身；**operator 宣告的 gate 命令另外解析一次**。\n"
            "這是 #666 兩個漂移項之一的機械成因：pytest 裝在 operator 的 user "
            "site-packages 時，系統層的 `python3` 在 `ProtectHome=yes` 之後 import "
            "不到它 ⇒ 每張 build 卡的 ledger 為空 ⇒ 撞 #540 的 acceptance chain。"
            "系統層需要哪些 python 套件見 :data:`SYSTEM_PYTHON_DISTRIBUTIONS`。\n"
            "doctor 的 `review-sandbox` probe 也把它列進 "
            "`REVIEW_SANDBOX_EXECUTABLES`（`srt` 的沙箱 smoke test 拿它當被關的程式），"
            "同樣走 `PATH` 解析。"
        ),
    ),
    SystemProgram(
        "setfacl", "apt: acl", ("manager",),
        note=(
            "**per-job 工作區的可達性靠它落地**（#710）。Manager 建完 per-job clone 之後"
            "對**那一格**下 `setfacl -R -m u:<job 帳號>:rwX` ＋ default ACL——"
            "`chown` 給另一個使用者需要 `CAP_CHOWN`，而 Manager unit 的 "
            "`CapabilityBoundingSet=` 是空的；`setfacl` 由**目錄 owner** 執行，"
            "不需要任何 capability，因此這是 Manager 唯一做得到的授權動作。\n"
            "**它必須在 Manager 自己的 `PATH` 上**（`coordinator/job_workspace.py` 以 "
            "`which()` 解），解不到就是「每一個 builder job 都 "
            "`chdir` 不進自己的工作區」而不是單一 job 失敗——與 `systemctl` 那一列同"
            "一個等級的失效面。`/usr/bin/setfacl` 落在 Manager unit 的 `PATH` 尾段"
            "（`/usr/bin`）內。\n"
            "**0818 的部署陷阱之一就是這個套件缺席**（trust-root Phase 2b M1 的三個"
            "陷阱：sudoers 萬用 NOPASSWD／缺 `acl`／pipx 殘留）；缺它時整套具名 ACL "
            "授權——不只本票這一條——全部套不上去。`getfacl` 同套件出，驗證步驟"
            "（runbook 的 `getfacl` 判準）靠它。"
        ),
    ),
    SystemProgram(
        "systemctl", "apt: systemd", ("manager",),
        note=(
            "**B 案（模板 unit，0816 第三輪定案）的派工 client**（#666）。"
            "`job_runner.build_systemctl_start_argv()` ／ `gate_runner` 起 job 與 gate "
            "的方式就是 `systemctl start --wait <模板實例>`，`is-active` 查狀態亦然；"
            "polkit 規則放行的也正是這條路徑。Manager 以 system unit 跑，因此它必須"
            "落在 Manager 自己的 `PATH` 上——`job_runner` 是以 `which(\"systemctl\")` "
            "解的，解不到就是「降權派工整條不可用」而不是單一 job 失敗。\n"
            "**`systemd-run` 不另立一列**：那是附錄 B 的降級備援（A 案 transient unit），"
            "同一個 `systemd` 套件出的，升降級不會只有其中一支在場。"
        ),
    ),
)

#: 系統層程式的名字（既有名稱保留；值由 :data:`SYSTEM_PROGRAMS` 機械導出，避免兩份
#: 真相）。**#661 之前它是寫死的 `("node",)`**——那不是設計，是盤點沒做完。
TOOLCHAIN_SYSTEM_RUNTIMES: tuple[str, ...] = tuple(p.name for p in SYSTEM_PROGRAMS)

#: job `PATH` 的系統層尾段（toolchain 之後）。`node`（codex 的 runtime）、`git`、
#: wrapper 內的 `python3` 都在這裡。刻意不含任何 `sbin`。
JOB_PATH_SYSTEM_TAIL: tuple[str, ...] = ("/usr/local/bin", "/usr/bin", "/bin")

#: `PSC_PREFLIGHT_CMD` 指向的模組（#661）。**它住在 cortex 自己的套件裡**，因此隨
#: `/opt/cortex/venv` 一起是 root-owned 部署產物，不需要第二個檔案系統資產。
PREFLIGHT_ADAPTER_MODULE = "paulsha_cortex.preflight_ci"

#: preflight adapter 的 backend（typed argv 的第二段）。**cortex 不 import 它**——
#: adapter 只以 typed argv spawn `<venv python> -m <這個模組>`，未安裝時以清楚訊息
#: fail-closed。它是 `.project-policy.yml` 宣告的那個治理引擎（`policy-check` 發行
#: 版）自己提供的 CI-parity preflight 進入點。
PREFLIGHT_BACKEND_MODULE = "policy_check.preflight"

#: preflight backend 所在的 python 發行版。落點是**部署 venv**（既有 root-owned 資產）
#: 而不是 toolchain：它不是可執行檔，是一個 import 得到才有意義的 python 套件；且它的
#: 版本必須逐字等於 `.project-policy.yml` 的 `policy_version`（引擎自己會驗，對不上就
#: fail-closed），因此「裝哪一版」與 R-23 的 workflow pin 是同一個部署決定。
PREFLIGHT_BACKEND_DISTRIBUTION = "policy-check"


# ---------------------------------------------------------------------------
# python 發行版（#666）——**第四種**外部相依，前三張表都收不下它
#
# `TOOLCHAIN_PROGRAMS` 與 `SYSTEM_PROGRAMS` 的形狀都是「一個落在 `PATH` 上的可執行
# 檔」，而這一族不是：`pytest`／`PyYAML`／`policy-check` 是 **python 發行版**，
# `import` 或 `python3 -m` 得到才有意義，`command -v` 對它們一律無解。硬塞進
# `SYSTEM_PROGRAMS` 會讓「`TOOLCHAIN_SYSTEM_RUNTIMES` 的每一項都應該 `which` 得到」
# 這條既有性質變成假的——那是把盤點完整性拿去換掉一條真的不變式，正是 #661 對
# 「不要把 `srt` 併進 `EXECUTOR_TOOLS`」的同一條論證。
#
# **落在哪一個 interpreter 是關鍵，而且兩個都有人用**：
#
# - **系統層**（`/usr/bin/python3`）：operator 宣告的 gate 命令 `PSC_GATE_CMD_PYTEST=
#   "python3 -m pytest -q"` 是**相對名**，由 gate 的 `PSC_GATE_PATH` 解析，落在
#   `JOB_PATH_SYSTEM_TAIL` 的 `/usr/bin`。#666 的實機症狀就是這一格：pytest 只裝在
#   operator 的 user site-packages，`ProtectHome=yes` 之後系統層的 `python3` import
#   不到 ⇒ ledger 空 ⇒ 撞 #540 的 acceptance chain。
# - **部署 venv**（`<deploy_root>/venv`）：cortex 自己與 preflight backend 住的地方，
#   既有的 root-owned 部署資產，不需要新增檔案系統資產（#661 已裁決）。
# ---------------------------------------------------------------------------

#: :class:`PythonDistribution.interpreter` 的兩個值。
SYSTEM_INTERPRETER = "system"
DEPLOYMENT_VENV_INTERPRETER = "deployment-venv"


@dataclass(frozen=True)
class PythonDistribution:
    """一個 python 發行版的部署決定（#666）。不是可執行檔，因此不進前兩張表。"""

    #: PyPI 發行版名（`pip install` 用的那個名字）。
    name: str
    #: `import` ／ `python3 -m` 用的模組名（與發行版名常常不同，例如 PyYAML→yaml）。
    module: str
    #: 落在哪一個 interpreter：:data:`SYSTEM_INTERPRETER` 或
    #: :data:`DEPLOYMENT_VENV_INTERPRETER`。
    interpreter: str
    #: **版本要求本身**——這是「明示的部署決定」那一半。刻意寫成 requirement 字串而
    #: 不是解析出來的版本號：產生器是純函式，讀不到實機裝了哪一版；它能負責的是
    #: 「約束從哪裡來」，實際落定的版本由 runbook 的驗證步驟記錄並比對。
    requirement: str
    #: 上面那條約束的**宣告來源**（唯一真相在哪個檔）。
    declared_in: str
    #: 誰需要它（帳號／角色名，讓 runbook 知道要以哪個身分實跑驗證）。
    required_by: tuple[str, ...]
    note: str

    @property
    def module_invocation(self) -> tuple[str, ...]:
        """以 `-m` 執行時的 argv 尾段（可執行的那一族才有意義）。"""
        return ("-m", self.module)


#: **系統層** python 發行版（#666）。判準與 :data:`SYSTEM_PROGRAMS` 同一條：它們是
#: 通用工具，換版本不改變治理判準；但「gate 用哪一版 pytest 跑測試」仍必須是可稽核
#: 的部署決定，而不是跟著 operator 的 `pip install --user` 漂移。
#:
#: **這張表的存在條件是 operator 的 gate 宣告用相對名 `python3`**（見
#: :data:`GATE_COMMAND_DECLARATIONS`）。改宣告成部署 venv 的絕對路徑 interpreter，
#: 系統層這一份需求就整個消失——那是 operator 平面的裁決，不是產生器能替它做的，
#: 因此這裡記的是**目前這個部署決定的後果**，不是唯一可能的形態。
SYSTEM_PYTHON_DISTRIBUTIONS: tuple[PythonDistribution, ...] = (
    PythonDistribution(
        "pytest", "pytest", SYSTEM_INTERPRETER,
        requirement="pytest>=7",
        declared_in="pyproject.toml [project.optional-dependencies] test",
        required_by=("gate",),
        note=(
            "gate 執行身分實際跑的 test runner。#666 實機症狀：裝在 operator 的 "
            "`~/.local/lib/python3.12/site-packages`，`ProtectHome=yes` 之後 gate 身分"
            "讀不到——\n"
            "    $ sudo -u cortex-gate env HOME=<gate HOME> python3 -m pytest --version\n"
            "    /usr/bin/python3: No module named pytest\n"
            "後果不是「gate 失敗」而是**每張 build 卡的 ledger 為空**，接著整條 #540 "
            "的 acceptance chain 卡住，而錯誤只在 manager.log 裡。\n"
            "**落系統層而不是部署樹**：它是通用 test runner，不是「換版本會改變治理"
            "產出」的那一類（與 `node` 同一條理由，與模型 CLI 相反）。但**版本仍是"
            "部署決定**：約束由 `pyproject.toml` 的 `test` extra 宣告，實機解出來的"
            "版本必須記進 runbook 並與 operator 側比對——同一台機器上兩個 "
            "interpreter 各有一份 pytest 是常態，而版本分岔的症狀是「gate 判定與"
            "本機跑不一樣」，不是報錯。\n"
            "**加固面已實測**：CPython 不是 V8，`MemoryDenyWriteExecute=yes` 不影響"
            "它（與 #643 的 node 型 executor 相反），完整加固面下跑得完。"
        ),
    ),
    PythonDistribution(
        "PyYAML", "yaml", SYSTEM_INTERPRETER,
        requirement="PyYAML>=6",
        declared_in="pyproject.toml [project] dependencies",
        required_by=("gate",),
        note=(
            "**不是 pytest 的相依，是被測樹的相依**（#666 窮舉盤點；票上只點名了 "
            "pytest，但實機兩個是一起裝的，理由在這裡）。gate 命令的 cwd 是 gate "
            "自己那份工作樹副本，pytest 會把 rootdir 插進 `sys.path`，於是 "
            "`import paulsha_cortex` 解到**被驗的那棵樹**，而 cortex 的 runtime "
            "相依裡有 PyYAML。系統層的 `python3` 沒有它，收集階段就 ImportError ⇒ "
            "pytest exit code 落在 `2`（collection error）⇒ 依 #307 的判準**不會**被"
            "當成合格 RED，而是照一般規則記 failed。\n"
            "**這條的一般形式**：`PSC_GATE_CMD_*` 用系統 interpreter 時，"
            "**被治理 repo 的整組 runtime 相依**都成了系統層的部署決定。目前受治理的"
            "只有 cortex 自己（PyYAML 一項），治理面擴到別的 repo 時這一列要跟著長。"
        ),
    ),
)

#: **部署 venv** 裡的 python 發行版（#666 把既有的兩個散落常數收進同一張表）。
#: 落點是既有的 root-owned 部署資產（`<deploy_root>/venv`），因此**不新增任何檔案
#: 系統資產**——這與 #661 對 preflight backend 的裁決逐字相同。
DEPLOYMENT_PYTHON_DISTRIBUTIONS: tuple[PythonDistribution, ...] = (
    PythonDistribution(
        PREFLIGHT_BACKEND_DISTRIBUTION, PREFLIGHT_BACKEND_MODULE.split(".")[0],
        DEPLOYMENT_VENV_INTERPRETER,
        requirement=f"{PREFLIGHT_BACKEND_DISTRIBUTION}==<policy_version>",
        declared_in=".project-policy.yml policy_version（＋ R-23 的 workflow pin）",
        required_by=("manager",),
        note=(
            "preflight adapter 的 backend（#661）。版本必須逐字等於 "
            "`.project-policy.yml` 的 `policy_version`——引擎自己會驗，對不上 "
            "fail-closed；CI 端 R-23 另外釘住 workflow pin ⟷ `policy_version`，"
            "兩條遞移出「本機跑的引擎版本 == CI 跑的引擎版本」。"
        ),
    ),
    PythonDistribution(
        "PyYAML", "yaml", DEPLOYMENT_VENV_INTERPRETER,
        requirement="PyYAML>=6",
        declared_in="pyproject.toml [project] dependencies",
        required_by=("manager", "monitor"),
        note=(
            "cortex 自己唯一的 runtime 相依（deck／persona／model-identities 全是 "
            "YAML）。隨 `pip install paulsha-cortex` 進 venv，**與系統層那一份是兩個"
            "不同 interpreter 下的兩份**——列在這裡是為了讓「同一個發行版在兩個落點"
            "各有一份」這件事是明寫的，而不是靠讀者自己發現。"
        ),
    ),
)

#: 兩個落點的聯集——窮舉盤點的輸入之一。
PYTHON_DISTRIBUTIONS: tuple[PythonDistribution, ...] = (
    SYSTEM_PYTHON_DISTRIBUTIONS + DEPLOYMENT_PYTHON_DISTRIBUTIONS
)


#: operator 應該宣告的 gate 命令（`PSC_GATE_CMD_<NAME>` → typed argv，#666）。
#:
#: 與 `job_path_value()`／`preflight_command_value()` 同一個定位：**產生器出建議值、
#: operator 落進 root-owned 的 EnvironmentFile**，不是第二份執行期真相（執行期真相
#: 永遠是 `gate_ledger.load_gate_specs()` 讀到的那份 env）。
#:
#: 把它寫下來買到兩件既有形態買不到的事：
#:
#: 1. **gate 宣告的每一段都可以被機械對照到某張表**——`python3` 必須在
#:    :data:`SYSTEM_PROGRAMS` 上、`-m <module>` 必須在
#:    :data:`SYSTEM_PYTHON_DISTRIBUTIONS` 上。#666 的漂移正是這條不成立而沒人看得見。
#: 2. **覆蓋率**：doctor 的 `gate-declarations` probe 以 packaged deck 每張卡的
#:    `test_policy` 導出應驗 gate 集合，宣告沒涵蓋到就是 required fail。本表必須是
#:    那個集合的**超集**，否則照 runbook 裝出來的部署一開機 doctor 就是紅的。
GATE_COMMAND_DECLARATIONS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "pytest": ("python3", "-m", "pytest", "-q"),
})

#: gate 宣告的環境變數前綴。**與 `coordinator.gate_ledger.GATE_ENV_PREFIX` 是成對
#: 契約**（同 `DEFAULT_TEMPLATE_UNIT`／`JOB_PATH_ENV_BY_PRINCIPAL` 的既有模式：
#: `permgen` 與 `coordinator` 刻意不互相 import，改由契約測試釘住兩邊逐字相等）。
GATE_COMMAND_ENV_PREFIX = "PSC_GATE_CMD_"


#: 二分（**向後相容選項**，非預設）：builder 一個帳號，其餘 headless／Manager／
#: monitor 共用 cortex-svc。0816 第三輪裁決前的方案；已按此裝好的部署可續用，
#: 但新部署一律走 :data:`DEFAULT_SCHEME`（三分）。
TWO_WAY_SCHEME = UidScheme(
    scheme_id="two-way",
    account_of={
        Principal.MANAGER: "cortex-svc",
        Principal.MONITOR: "cortex-svc",
        Principal.REVIEWER: "cortex-svc",
        Principal.PLANNER: "cortex-svc",
        Principal.BUILDER: "cortex-builder",
        Principal.HEADLESS_HOOK: "cortex-builder",
        # #629：本方案沒有 gate 執行身分（明示，不是遺漏）。二分把 Manager／monitor／
        # reviewer／planner 全併在 `cortex-svc`，若把 gate 也併進去就等於讓 builder
        # 交出來的 `conftest.py` 在 durable state owner 的身分下執行——比 #629 要修的
        # 缺口更嚴重。因此二分**不提供 gate 執行面**：降權模式下 build 卡照
        # `require_ledger` fail closed（＝#629 之前的現況）。要用 gate 就升四分。
        Principal.GATE: ABSENT_ACCOUNT,
    },
    durable_state_owner="cortex-svc",
    deploy_account="root",
    # operator_account／external_reader_account 刻意留 None：它們是部署決定，
    # 由 CLI／env 於產生當下注入（#626）。寫死在這裡就是把部署決定編進程式碼。
)

#: 三分（**定案**）：把 cortex-svc 拆成 cortex-manager（durable state owner，持 spawn
#: 授權、不跑模型）與 cortex-reviewer-planner（reviewer＋planner 的模型 job 帳號，
#: 不持有 durable state、不持 spawn 授權）。**與二分共用同一套 policy，僅換 config。**
THREE_WAY_SCHEME = UidScheme(
    scheme_id="three-way",
    account_of={
        Principal.MANAGER: "cortex-manager",
        Principal.MONITOR: "cortex-manager",
        Principal.REVIEWER: "cortex-reviewer-planner",
        Principal.PLANNER: "cortex-reviewer-planner",
        Principal.BUILDER: "cortex-builder",
        Principal.HEADLESS_HOOK: "cortex-builder",
        # #629：同二分——三分也沒有第四個帳號可以承接 gate。三個候選逐一皆不可用
        # （見 `FOUR_WAY_SCHEME` 的說明），因此明示「本方案沒有這個角色」而不是併帳。
        Principal.GATE: ABSENT_ACCOUNT,
    },
    durable_state_owner="cortex-manager",
    deploy_account="root",
    # 同二分：operator／external reader 是部署決定，不寫死（#626）。
)

#: 四分（**#629 定案**）：三分再拆出 `cortex-gate`——operator 宣告的 gate 命令的執行
#: 身分。**與二分／三分共用同一套 policy，僅換 config。**
#:
#: ## 為什麼一定要是第四個帳號
#:
#: gate 命令跑在 **builder 完全掌控內容的工作樹**上，`pytest` 會載入該樹的
#: `conftest.py` 與 plugin ⇒ **執行 gate 的身分等於對 builder 開放任意程式碼執行**。
#: 既有的三個帳號逐一檢查，沒有一個能承接：
#:
#: - `cortex-builder`：模型自證自己的驗收證據，違反 #540 的 acceptance chain；
#: - `cortex-manager`：持 spawn 授權、擁有全部 Manager-owned durable state，是三分
#:   授權線的**內側**——這是 issue #629 本文的整段論證；
#: - `cortex-reviewer-planner`：它是寫 verdict 的那一個。把 gate 掛上去，被攻陷的
#:   builder 就能經由 gate 執行影響 reviewer 的帳號，等於把 #638／#639 剛關掉的
#:   「builder 對 verdict 通道的影響力」重新打開。
#:
#: ## 為什麼不能「不開帳號」
#:
#: 唯一認真的無新帳號候選是 systemd 的 `DynamicUser=yes`（每次起 unit 配一個暫時
#: UID，不必建帳號）。它**結構性地**不成立，理由不是偏好而是機制：本方案兩端都靠
#: **具名 POSIX ACL** 授權——`gate-ledger-spool` 那一格要給 gate `wx` 無 `r`，
#: `repo-worktree` 要給 gate `rX`——而 `setfacl -m u:<名>:…` 在**產生權限的當下**就
#: 要解析得到那個帳號；DynamicUser 的 UID 在 unit 起動前不存在、每次還不同，根本
#: 沒有可寫進 ACL 的名字。退而求其次的 `User=nobody` 更糟：那是一個與系統上任何
#: 其他服務共用的帳號，授它 Tier-0 spool 的寫入權等於把那條通道開給所有 `nobody`
#: 行程。
#:
#: ## 誠實邊界（spec §R3 有對應段落）
#:
#: 第四帳號買到的是**圍堵**，不是「ledger 不可偽造」。取得 ACE 的 builder 依然能
#: 影響**自己那一份** ledger 的內容——那是「執行對方交出來的測試碼」這件事本身的
#: 性質，換誰執行都一樣。它買到的是：那個 ACE 碰不到 Manager 的 durable state、
#: 碰不到 verdict 通道、碰不到別的 job 的工作樹、也沒有 spawn 授權。
FOUR_WAY_SCHEME = UidScheme(
    scheme_id="four-way",
    account_of={
        Principal.MANAGER: "cortex-manager",
        Principal.MONITOR: "cortex-manager",
        Principal.REVIEWER: "cortex-reviewer-planner",
        Principal.PLANNER: "cortex-reviewer-planner",
        Principal.BUILDER: "cortex-builder",
        Principal.HEADLESS_HOOK: "cortex-builder",
        Principal.GATE: "cortex-gate",
    },
    durable_state_owner="cortex-manager",
    deploy_account="root",
    # 同二分／三分：operator／external reader 是部署決定，不寫死（#626）。
)

SCHEMES: dict[str, UidScheme] = {
    TWO_WAY_SCHEME.scheme_id: TWO_WAY_SCHEME,
    THREE_WAY_SCHEME.scheme_id: THREE_WAY_SCHEME,
    FOUR_WAY_SCHEME.scheme_id: FOUR_WAY_SCHEME,
}

#: **定案方案**（#629；在此之前是 0816 第三輪裁決 A 的三分）。CLI／產生器未指定
#: scheme 時一律用這個——「預設就是最安全的那一個」是刻意的：要退回三分／二分必須
#: 顯式打出 `three-way`／`two-way`，打錯字不會靜默退回較寬鬆的方案（`SCHEMES` 查無即拒）。
#:
#: **三分／二分在 #629 之後會 fail-closed**，而且是刻意的：登記表已有兩個資產把
#: `GATE` 列為 writer，那兩個方案對它沒有帳號對應，`unresolved_principals()` 因此
#: 會指名它並讓 `plan_to_commands()` 一行都不輸出（#626 的既有出口）。這比「靜默把
#: gate 併到某個既有帳號」正確得多——併進去的每一種選法都是上面剛否決掉的那三條。
DEFAULT_SCHEME: UidScheme = FOUR_WAY_SCHEME
DEFAULT_SCHEME_ID: str = DEFAULT_SCHEME.scheme_id


# ---------------------------------------------------------------------------
# 權限模型
# ---------------------------------------------------------------------------

class OwnerClass(Enum):
    """資產的擁有類別，決定 owner 帳號來源。"""

    DEPLOYMENT = "deployment"        # enforcement plane：owner＝deploy/root，**全部 headless 唯讀**
    MANAGER_STATE = "manager-state"  # Manager-owned durable state：owner＝durable_state_owner
    JOB = "job"                      # job-visible：owner＝對應 job 帳號（或 runtime 逐案 chown）
    #: **root 擁有的容器，untrusted job 可寫其內容**（#698 方案 A：sticky 樹）。
    #:
    #: 它刻意**不是** `DEPLOYMENT`。`DEPLOYMENT` 的定義裡有一句「對全部 headless 唯讀」，
    #: 而那句話是好幾條不變式的依據（`test_builder_never_writes_manager_owned_or_deployment`
    #: ／`test_manager_owned_acls_are_read_only`）。sticky 樹**必須**讓 job 寫得進去，
    #: 把它塞進 `DEPLOYMENT` 等於就地把那句話改成「除了某些之外」——那些不變式會從
    #: 「機械成立」退化成「有例外清單」，而例外清單是會長大的。
    #:
    #: 本類別的性質是可以逐條寫下來的：owner＝root、帶 sticky bit、恰好一組 untrusted
    #: 執行身分持有具名 `rwx` **access** ACL（無 default ACL）；容器裡的 root-owned 檔
    #: 仍是 `DEPLOYMENT`，且 job 對它零寫入。
    STICKY_SHARED = "sticky-shared"


@dataclass(frozen=True)
class AclEntry:
    """單條 POSIX ACL（供跨帳號的精確授權；Manager-owned 上只會出現唯讀條目）。"""

    account: str
    perms: str          # "rX"（讀，dir 自動含 traverse）／"rwx"／"wx"
    default: bool = False  # 是否為 default ACL（dir 內新建物件繼承）
    #: 遞迴套用（`setfacl -R`）。**只給 per-job 工作區那一族**（#710）：那一格是
    #: Manager 用 `git clone` 建出來的一整棵樹，樹裡每個 inode 都由 Manager 以
    #: `UMask=0077` 建立 ⇒ 只在樹根下一條 ACL，job 進得去卻讀不到裡面任何東西。
    #: 其餘資產一律非遞迴（葉檔或空容器，遞迴只會多套到不該套的地方）。
    recursive: bool = False

    @property
    def writable(self) -> bool:
        return "w" in self.perms

    def render(self, path: str) -> str:
        flag = "-d -m" if self.default else "-m"
        if self.recursive:
            flag = f"-R {flag}"
        return f"setfacl {flag} u:{self.account}:{self.perms} {path}"


@dataclass(frozen=True)
class PermissionEntry:
    """單一資產路徑的目標權限（機械產生，不含任何實際 IO）。"""

    asset_id: str
    tier: str
    tree: str
    owner_class: OwnerClass
    owner: str
    group: str
    mode: int                     # 0o 值，僅 0o777 部分
    is_directory: bool
    #: 目標可寫帳號（含 owner 與 ACL 授寫者）——供不變式測試。
    writer_accounts: frozenset[str]
    reader_accounts: frozenset[str]
    acls: tuple[AclEntry, ...] = ()
    #: 本資產是一條 **symlink**（#685）。與 `is_directory` 互斥；決定 `commands()` 出
    #: `ln -sfn` ＋ `chown -h` 而不是裸 `chown`／`chmod`（後兩者在 Linux 上跟著 symlink
    #: 走，會改到**目標**而不是 symlink 本身）。由 :data:`SYMLINK_ASSETS` 機械導出。
    is_symlink: bool = False
    #: True＝容器的 per-child owner 由 launcher 在 spawn 時逐案 chown（如 worktree pool）。
    runtime_managed: bool = False
    #: 現況 writer（含 ANY_SAME_UID）——保留供對照，非目標。
    legacy_writers: tuple[str, ...] = ()
    rationale: str = ""
    #: 待 operator 拍板的未決點（例如部署路徑最終位置）。
    open_points: tuple[str, ...] = ()

    @property
    def mode_str(self) -> str:
        return format(self.mode, "04o")

    @property
    def sticky(self) -> bool:
        """本資產帶 sticky bit（`chmod +t`，#698）。

        由 `mode` 導出而不是另存一個布林欄位：兩份真相會漂移，而這一位的語意
        （「目錄可寫，但只刪得掉／改得掉名字的是自己的檔」）正是 `hooks.json` 住得進
        可寫樹的全部依據。
        """
        return bool(self.mode & STICKY_BIT)

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "tier": self.tier,
            "tree": self.tree,
            "owner_class": self.owner_class.value,
            "owner": self.owner,
            "group": self.group,
            "mode": self.mode_str,
            "is_directory": self.is_directory,
            "is_symlink": self.is_symlink,
            "writer_accounts": sorted(self.writer_accounts),
            "reader_accounts": sorted(self.reader_accounts),
            "acls": [
                {"account": a.account, "perms": a.perms, "default": a.default}
                for a in self.acls
            ],
            "runtime_managed": self.runtime_managed,
            "sticky": self.sticky,
            "legacy_writers": list(self.legacy_writers),
            "rationale": self.rationale,
            "open_points": list(self.open_points),
        }

    def effective_default_acls(self) -> tuple["AclEntry", ...]:
        """本資產**實際會落到磁碟上**的 default ACL 條目（#710）。

        `acls` 只存宣告面：目錄資產的 default 條目多數是 :meth:`commands` 依「dir 需
        同時設 access 與 default」那條規則**自動補**出來的，sticky 樹則明示不補。
        任何「這一項有沒有 default ACL」的判定都必須看這個結果而不是 `acls`——
        否則會得到與 operator 真的執行的那份 script 相反的答案，而那正是 #641／#710
        這一族「宣告與實機不一致」的形狀。
        """

        if self.is_symlink or not self.is_directory or self.sticky:
            return tuple(acl for acl in self.acls if acl.default)
        explicit = {acl.account for acl in self.acls if acl.default}
        derived = [acl for acl in self.acls if acl.default]
        derived += [
            AclEntry(acl.account, acl.perms, default=True, recursive=acl.recursive)
            for acl in self.acls
            if not acl.default and acl.account not in explicit
        ]
        return tuple(derived)

    def commands(self, path: str, link_target: str | None = None) -> list[str]:
        """產生本資產的 chown／chmod／setfacl 命令字串（**只回傳字串，不執行**）。

        `path` 由呼叫端提供（runbook 以 shell 變數帶入真實路徑）；未提供具體路徑時
        以清楚標記的 placeholder 呈現。

        **symlink 資產（#685）走另一組動詞，這不是風格選擇。** Linux 沒有 `lchmod`，
        而 `chown`／`chmod` 對 symlink 一律**跟著走**：對 `~/.gemini` 裸 `chown` 會把
        `cache/gemini` **那棵樹**的 owner 改掉，而那棵樹歸 job 帳號正是本形狀的全部
        重點。因此改出 `ln -sfn`（建立／重指，冪等）＋ `chown -h`（只改 symlink 自己）
        ，且**不出 `chmod`**——symlink 的 mode 位在 Linux 上沒有語意。
        """
        if self.is_symlink:
            target = link_target or f"<TARGET:{self.asset_id}>"
            return [
                f"ln -sfn {target} {path}",
                f"chown -h {self.owner}:{self.group} {path}",
            ]
        cmds = [
            f"chown {self.owner}:{self.group} {path}",
            f"chmod {self.mode_str} {path}",
        ]
        # #710：已**明示**宣告 default 條目的帳號不再自動補一條。自動那一條抄的是
        # access 的 perms，而 per-job 工作區那一族刻意兩者不同（access `rwX`／default
        # `rwx`——大寫 X 用在遞迴套用的既有檔上，default 沒有「既有檔」可參照）。
        # 少了這一行會產出兩條互相覆蓋的 default 條目，而**後寫的那條贏**。
        explicit_defaults = {acl.account for acl in self.acls if acl.default}
        for acl in self.acls:
            cmds.append(acl.render(path))
            # dir 需同時設 access 與 default ACL，讓新建物件繼承。
            #
            # ⛔ **sticky 樹是明示的例外（#698）**，而且這一行就是它的成敗。default ACL
            #    決定的是「**之後**在這個目錄裡新建的物件」的初值——包含 root 日後重放
            #    一次 `hooks.json` 的那一次。對 sticky 樹補上 `-d -m u:<job>:rwx` 等於
            #    宣告「root 放進去的每一個 enforcement 檔都自動交還給 job」，sticky
            #    擋得住 unlink／rename 也沒有用了（job 直接改內容）。
            #    因此這一族**只設 access ACL**。
            if (
                self.is_directory
                and not acl.default
                and not self.sticky
                and acl.account not in explicit_defaults
            ):
                cmds.append(
                    AclEntry(
                        acl.account, acl.perms, default=True, recursive=acl.recursive
                    ).render(path)
                )
        if self.sticky:
            cmds.append(
                "#   ⛔ 本目錄**刻意不設 default ACL**：它會讓 root 日後放進來的 "
                "enforcement 檔自動帶上 job 的 rwx（#698）。"
            )
            cmds.append(
                f"#   ✅ 驗證用 `getfacl {path}` 而不是 `ls -ld`——具名 ACL 存在時 mode "
                "的 group 位顯示的是 **ACL mask**（會顯示成 rwx），那不是 group 寫入權。"
            )
        return cmds


# ---------------------------------------------------------------------------
# 目錄／檔案推斷（登記表未編碼 file/dir，以 resolver 名與 asset_id 機械推斷）
# ---------------------------------------------------------------------------

_DIR_ASSET_TOKENS = (
    "tree", "root", "queue", "pool", "spool", "proposals", "outbox", "combos",
)
_DIR_ASSET_IDS = frozenset({
    "combo-card-override",       # <agents_root>/config/combos/ 目錄
    "skill-park-proposals",
    "digest-outbox",
    # 以下皆為 evidence／journal **目錄**（逐 slice／逐 run 一檔），asset_id 的
    # token heuristic 會誤判成單檔，故明列（路徑對照見 `PathLayout.asset_paths`）：
    "verification-evidence",        # <coordinator>/evidence/verification/
    "maintainer-attestation",       # <coordinator>/evidence/maintainer-review/
    "completion-record",            # <coordinator>/evidence/completion/
    "full-suite-evidence",          # <coordinator>/evidence/full-suite/
    "workflow-inputs",              # <coordinator>/evidence/workflow-inputs/
    "workflow-evidence",            # <coordinator>/evidence/workflow/
    "workflow-report-journal",      # <coordinator>/workflow-report-transactions/
    "engineering-outcome-outbox",   # <coordinator>/engineering-outcomes/<repo>.jsonl 的容器
    "gate-ledger",                  # <agents_root>/runtime/dispatch/（manager log_dir）
    "review-verdict-spool",         # <coordinator>/review-verdicts/<reviewer_job_id>/
    "commit-spool",                 # <coordinator>/commit-spool/<job-id>/
    "executor-toolchain",           # <deploy_root>/toolchain/（四個模型 CLI 的落點）
    # #698：codex 的 `$CODEX_HOME` 整棵（`<HOME>/.codex`）——root-owned ＋ sticky 的
    # **真目錄**。asset_id 的 token heuristic（`-state` 不在 `_DIR_ASSET_TOKENS`）會把
    # 它誤判成單檔，而誤判的後果不是排版問題：檔案資產的 RWP 會折算成**父目錄**＝
    # 整個 HOME，等於把 root-owned 的 HOME 開成可寫面。由表機械導出。
    *credential_asset_ids(CredentialShape.HOME_STICKY_TREE),
})
#: 登記表上是 **symlink** 的資產（#685／U-7）。
#:
#: 它們**不是目錄**（不得 `install -d`）也不是普通檔（不得裸 `chown`／`chmod`——兩者在
#: Linux 上都**跟著 symlink 走**，套在 `.gemini` 上會改到 `cache/gemini` 那棵樹本身的
#: owner，而那正是要保護的東西）。命令形態改由 `PermissionEntry.commands()` 出
#: `ln -sfn` ＋ `chown -h`，見該方法。
#:
#: 由 :func:`credential_asset_ids` 機械導出，不是手寫清單：往
#: :data:`CREDENTIALED_ACCOUNTS` 加一格 `HOME_REDIRECT_TREE`，這裡自動跟著長。
SYMLINK_ASSETS: frozenset[str] = frozenset(
    credential_asset_ids(CredentialShape.HOME_REDIRECT_TREE)
)

#: sticky 樹裡那個 **root-owned enforcement 葉檔**的資產 id（#698）。
#:
#: 它與其他葉檔在 `plan_to_commands()` 裡走**相反**的守衛：一般葉檔「不存在就跳過」，
#: 這一族「不存在就由 root 種一份」。理由是 sticky bit 的語意——它管「刪／改名別人的
#: 檔」，不管「建一個還不存在的檔」，因此**檔不存在時整個裁決是空的**。
ENFORCEMENT_LEAF_ASSETS: frozenset[str] = frozenset(enforcement_asset_ids())

#: 產生器種進 enforcement 位置的**最小合法內容**。
#:
#: 刻意是一份**空的** hooks 文件，不是 `paulsha_cortex/scripts/hooks/codex.json` 那份
#: relay hook：本檔的價值在於「這個位置由 root 佔住、job 換不掉」，不在於跑什麼。
#: 把 relay hook 種進 job 帳號會憑空多一條執行面（而且 job 帳號本來就寫不進
#: coordinator，每次 codex session 只會多一串失敗），與本票要收的洞方向相反。
#: 之後要放什麼是 root 的決定——換內容不需要動產生器，只要以 root 身分覆寫。
CODEX_HOOKS_SEED_CONTENT = '{\n  "hooks": {}\n}'

#: 登記表上是 **root-owned ＋ sticky ＋ job `rwx` ACL 的真目錄**的資產（#698）。
#:
#: 由 :func:`credential_asset_ids` 機械導出。這一族是 `build_entry()` 唯一會產出
#: **帶 sticky 位**與**唯一會在 DEPLOYMENT 類別下產出可寫 ACL** 的地方——兩件事都要
#: 有出處，因此集合本身也是導出的，不是手寫清單。
STICKY_JOB_WRITABLE_DIR_ASSETS: frozenset[str] = frozenset(
    credential_asset_ids(CredentialShape.HOME_STICKY_TREE)
)

_FILE_ASSET_IDS = frozenset({
    "control-daemon-lock",
    "control-status",
    # #666：`~/.config/gh/` 底下的兩個檔——那一層目錄必須維持 root-owned，`hosts.yml`
    # 與 `config.yml` 才能是**不同 owner** 的兩個檔。
    "manager-gh-credential",
    "manager-gh-config",
    # #640／#685／#698：憑證那一族**只有 sticky 樹是目錄**。file/dir 的判定直接決定
    # permgen 會不會把一整層 `install -d` 出來再 chown，因此三種形狀各自明列：
    #   - `IN_PLACE_FILE` 是單檔，父目錄必須維持 root-owned 才有「能改內容、不能增刪換」；
    #   - `HOME_REDIRECT_TREE` 是 **symlink**，`install -d` 會在它的位置建出一個真目錄，
    #     把整條導向 `cache` 的機制無聲換掉（見 :data:`SYMLINK_ASSETS`）；
    #   - `HOME_STICKY_TREE` 是**真目錄**，因此**不在**本集合，改列進 `_DIR_ASSET_IDS`。
    *(
        aid for aid in credential_asset_ids()
        if aid not in STICKY_JOB_WRITABLE_DIR_ASSETS
    ),
    # #698：enforcement 檔（codex 的 `hooks.json`）——兩個帳號各一份，由
    # `enforcement_asset_ids()` 導出。它是 sticky 樹裡那個 job 動不了的葉檔。
    *enforcement_asset_ids(),
})


def infer_is_directory(asset: TrustRootAsset) -> bool:
    """機械推斷資產是目錄或檔案。

    優先序：明列覆寫 → resolver 名後綴（`_path`＝檔、`_root`/`_dir`＝目錄）→
    asset_id token。path_resolver=None 的葉資產以 asset_id 推斷，**屬 heuristic**，
    runbook 標明 operator 應對 path_resolver=None 的葉逐一確認 file/dir。
    """
    if asset.asset_id in _DIR_ASSET_IDS:
        return True
    if asset.asset_id in _FILE_ASSET_IDS:
        return False
    if asset.path_resolver is not None:
        fn = asset.path_resolver.split(":", 1)[1]
        if fn.endswith("_path"):
            return False
        if fn.endswith("_root") or fn.endswith("_dir"):
            return True
    return any(tok in asset.asset_id for tok in _DIR_ASSET_TOKENS)


# ---------------------------------------------------------------------------
# owner class 分類 + policy
# ---------------------------------------------------------------------------

def classify_owner(asset: TrustRootAsset) -> OwnerClass:
    """把資產分到 DEPLOYMENT／MANAGER_STATE／JOB。

    - sticky 樹（:data:`STICKY_JOB_WRITABLE_DIR_ASSETS`）→ STICKY_SHARED。**這條排在
      最前面**：那一族的 `ingress_kind` 也是 `DEPLOYMENT_WRITE`（root 建立、root 擁有），
      若讓它落進 DEPLOYMENT，「deployment 對全部 headless 唯讀」那句話就不再為真。
    - enforcement plane（`DEPLOYMENT_WRITE`，或 writer 含 INSTALLER 的 bootstrap env）
      → DEPLOYMENT（owner＝root/deploy）。
    - control file queue（`CONTROL_FILE_QUEUE`）：登記表現況標為 job-visible（任何同
      UID 可建檔），但 spec §R4 明定其提交改走 Manager-owned authenticated socket、
      queue 目錄不再世界可寫——故目標 owner 收斂為 MANAGER_STATE（附 open point）。
    - Manager-owned 樹的其餘資產 → MANAGER_STATE。
    - 其餘 job-visible 樹 → JOB。
    """
    if asset.asset_id in STICKY_JOB_WRITABLE_DIR_ASSETS:
        return OwnerClass.STICKY_SHARED
    if asset.ingress_kind is IngressKind.DEPLOYMENT_WRITE:
        return OwnerClass.DEPLOYMENT
    if Principal.INSTALLER in asset.writers:
        # runtime bootstrap env：現況 installer 裸寫、無 mode——目標由 deploy 身分持有。
        return OwnerClass.DEPLOYMENT
    if asset.ingress_kind is IngressKind.CONTROL_FILE_QUEUE:
        return OwnerClass.MANAGER_STATE
    if asset.tree is TrustTree.MANAGER_OWNED:
        return OwnerClass.MANAGER_STATE
    return OwnerClass.JOB


def _dir_file_mode(is_dir: bool, owner_bits: int, group_bits: int, other_bits: int) -> int:
    """組出 mode；owner/group/other 各給 rwx 位（0-7）。dir 才有 x 意義。"""
    return (owner_bits << 6) | (group_bits << 3) | other_bits


def _mask_write(bits: int) -> int:
    """移除 write 位（用於確保 group/other 永不可寫）。"""
    return bits & ~0o2 & 0o7


#: sticky bit（`chmod +t`）。目錄語意：可寫，但**只有檔案 owner／目錄 owner／root**
#: 刪得掉或改得掉裡面的檔名。#698 的整套方案 A 就架在這一位上。
STICKY_BIT = 0o1000

#: setuid／setgid。**任何登記表資產都不得帶**，安全網無條件清掉——它們是提權位，
#: 而本產生器的整個目的是把提權面收斂到 root-owned 的 unit／shim 那一條線上。
_FORBIDDEN_SPECIAL_BITS = 0o6000


def build_entry(asset: TrustRootAsset, scheme: UidScheme) -> PermissionEntry:
    """對單一資產機械產生目標權限。純函式、無 IO。"""
    owner_class = classify_owner(asset)
    is_dir = infer_is_directory(asset)
    legacy = tuple(w.value for w in asset.writers)

    # 目標 reader 帳號（去掉 ANY_SAME_UID／未映射者）。
    reader_accounts = frozenset(
        a for a in (scheme.resolve(r) for r in asset.readers) if a is not None
    )

    open_points: list[str] = []
    acls: list[AclEntry] = []
    runtime_managed = False

    if owner_class is OwnerClass.DEPLOYMENT:
        owner = scheme.deploy_account
        # enforcement plane：owner（root）可寫，全部行程唯讀（spec §R3「全部行程唯讀」）。
        mode = _dir_file_mode(is_dir, 0o7 if is_dir else 0o6, 0o5 if is_dir else 0o4, 0o5 if is_dir else 0o4)
        writer_accounts = frozenset({owner})
        rationale = (
            "部署身分（root）擁有——enforcement plane（env／hooks），或 durable-state "
            "樹根（解析鏈即信任根，spec §1）：root 擁有使 headless／svc 皆無法 relink "
            "整棵樹；對全部 headless 唯讀，現況裸寫／group-writable 於此收斂。"
            "0816 裁決已定案路徑：部署樹＝/opt/cortex、bootstrap env 落 /opt/cortex/etc/、"
            "codex hooks 落 job 帳號 HOME 下的 root-owned .codex/（值見 PathLayout，勿手寫）。"
            "**凡 writer 只有部署身分的資產皆歸此類**，因此 #623 的三份 root-owned "
            ".gitconfig（builder／reviewer-planner／manager）也在其中：owner＝root 即代表"
            "**全部服務帳號（含 Manager）對這些檔唯讀**——ReadWritePaths 純由「誰可寫」"
            "導出，服務因此改不了自己的 git 設定（.gitconfig 可指定 core.fsmonitor／"
            "alias.* 這類會執行外部命令的鍵）。注意來源樹本身**不**歸此類：0817 裁決把"
            "它的 writer 改為 Manager（回收成果必須寫得進去），見 `repo-source-tree`。"
        )
        if asset.asset_id in ENFORCEMENT_LEAF_ASSETS:
            rationale += (
                "\n**#698：本檔住在一棵 job 可寫的 sticky 樹裡**（`*-codex-state`），"
                "因此它的保護不是「父目錄不可寫」而是三個動詞各自被擋："
                "unlink／rename 由 sticky 擋（非 owner 只動得了自己的檔）、改內容由本檔的 "
                "mode 擋（root:root 0644 ⇒ job 落在 `other` 位）；mount 層另有該 unit 的 "
                "`ReadOnlyPaths=` 這一條（`enforcement_read_only_paths()`）。"
                "\n⛔ **本檔必須存在**：sticky 不管「建一個還不存在的檔」，缺它時 job 直接"
                "`printf x > $HOME/.codex/hooks.json` 就會成功（R9 T3.9 的攻擊字面）。"
                "`plan_to_commands()` 因此對本族出 **create-if-absent**，而不是其他葉檔"
                "那條「不存在就跳過」。"
            )

    elif owner_class is OwnerClass.STICKY_SHARED:
        # #698 方案 A：executor 的狀態樹。owner 是 root（它承載 enforcement 檔——目錄
        # owner 對 sticky 免疫，所以這一層**不能**是 job 擁有），但整棵必須讓 job 寫得
        # 進去，否則 executor 起不來（#686）。兩者同時成立的唯一形態＝
        # **sticky ＋ 具名 rwx access ACL**。
        #
        # 授權對象由 `writers` ∩ `UNTRUSTED_EXECUTION_PRINCIPALS` 導出，與 JOB 那一支
        # 的規則同源——「哪個身分算不受信任的執行者」只有那一個集合說了算，這裡不另立
        # 第二條判準。
        owner = scheme.deploy_account
        sticky_writers = frozenset(
            a
            for a in (
                scheme.resolve(w)
                for w in asset.writers
                if w in UNTRUSTED_EXECUTION_PRINCIPALS
            )
            if a is not None
        )
        if not sticky_writers:
            raise ValueError(
                f"{asset.asset_id}：sticky 樹沒有任何 untrusted 執行身分當 writer"
                "——那它只是一個沒人寫得進去的 root 目錄，executor 會起不來（#686）。"
                "登記表的 writers 必須含該帳號的 job principal。"
            )
        # 目錄 rwx，group／other 唯讀＋traverse（安全網稍後會再確認一次無 w 位）。
        mode = _dir_file_mode(is_dir, 0o7, 0o5, 0o5) | STICKY_BIT
        for jacct in sorted(sticky_writers):
            acls.append(AclEntry(jacct, "rwx"))
        writer_accounts = frozenset({owner}) | sticky_writers
        rationale = (
            "**executor 狀態樹（#698 方案 A：sticky ＋ root-owned enforcement 檔）**："
            "owner＝root、mode 帶 sticky bit（`+t`），job 帳號以具名 **access** ACL "
            "取得 `rwx`。兩件在 #685 的形狀下互斥的事因此同時成立：(i) 整棵可寫 ⇒ "
            "codex 起得來（#686 實測 `$CODEX_HOME` 唯讀時連 in-process app-server 都"
            "初始化不了）；(ii) 樹裡的 root-owned `hooks.json` **刪不掉、改不掉名字、"
            "也改不了內容**——sticky 讓非 owner 只動得了自己的檔，而該檔的 mode 讓 "
            "job 落在 `other` 位。0818 的 R9 T3.9 就是在 (ii) 不成立的形狀下被實測"
            "攻破的（codex hooks 會執行命令 ⇒ 跨 job 持久化）。"
            "\n**owner 必須是 root**：目錄 owner 對 sticky 免疫（POSIX：目錄 owner 刪得掉"
            "裡面任何檔），把這一層交給 job 等於整條規則不存在。"
            "\n**group 寫入權仍然是零**（spec §R2 未放寬）：job 的寫入權走具名 ACL。"
            "注意 `ls -ld` 會把 group 位顯示成 `rwx`——那是 POSIX ACL 的 **mask**，"
            "不是 group 權限，驗證一律用 `getfacl`。"
            "\n**刻意不設 default ACL**：它會讓 root 日後補放的 enforcement 檔自動帶上"
            "job 的 `rwx`，等於把 (ii) 交還回去（見 `PermissionEntry.commands`）。"
            "\n**代價（R-6 不變）**：樹由 job 寫 ⇒ 樹裡的 **token 葉檔**仍可被該 job "
            "刪除或替換。那是刻意的——token 過期必須 refresh 得回來；sticky 保護的是"
            "root 擁有的那些檔。"
        )

    elif owner_class is OwnerClass.MANAGER_STATE:
        owner = scheme.durable_state_owner
        writer_accounts = {owner}
        # 基準 owner-only（dir 0700／file 0600）；跨帳號讀取一律走精確 ACL（唯讀）。
        mode = _dir_file_mode(is_dir, 0o7 if is_dir else 0o6, 0, 0)
        for racct in sorted(reader_accounts):
            if racct == owner:
                continue
            acls.append(AclEntry(racct, "rX" if is_dir else "r"))
        rationale = (
            "Manager-owned durable state：owner＝durable_state_owner，headless 零寫入；"
            "跨帳號讀取以 per-account 唯讀 ACL 精確授予（不開 group/other 寫入位）。"
        )
        # control file queue 現況未認證、任何同 UID 可寫——目標改由 Manager 持有、
        # 提交改走 R7 authenticated socket（spec §R4）。
        if asset.ingress_kind is IngressKind.CONTROL_FILE_QUEUE:
            rationale += " 提交通道 Phase 2 改為 Manager-owned socket（R7），queue 目錄不再世界可寫。"
            open_points.append(
                "control queue：確認 operator 提交改走 authenticated socket 後，"
                "requests/ 目錄可收斂為 owner-only（本表已如此產生）。"
            )
        writer_accounts = frozenset(writer_accounts)

    else:  # JOB
        # 「哪些 writer 算 untrusted producer」＝ `UNTRUSTED_EXECUTION_PRINCIPALS`
        # （#629 起含 `GATE`）。用它而不是逐項列舉，是為了讓「新增一個執行不受信任
        # 程式碼的身分」只需要改登記表那一個集合——漏改這裡的後果是**靜默 fail-open**
        # 的相反面：該身分的 `wx` ACL 不會被產生，它連自己那格 spool 都寫不進去，
        # 而症狀出現在部署當天而不是產生器。
        job_writers = frozenset(
            a
            for a in (
                scheme.resolve(w)
                for w in asset.writers
                if w in UNTRUSTED_EXECUTION_PRINCIPALS
            )
            if a is not None
        )
        trusted_owner = scheme.durable_state_owner
        workspace_reach = registry.per_job_named_acl_workspace_assets().get(asset.asset_id)

        if workspace_reach is not None:
            # #710：per-job **工作區**那一格。owner 維持 Manager，job 拿具名 ACL。
            #
            # 這一支存在的理由是一條**機制事實**，不是政策偏好：per-job 那一格是
            # Manager 用 `git clone` 建出來的，而把它 `chown` 給 job 帳號需要
            # `CAP_CHOWN`——Manager unit 的 `CapabilityBoundingSet=` 是空的。#623／#648
            # 宣稱的「整個 clone 由本 job 帳號擁有」因此不是漏寫一行，是**方案與降權
            # 模型衝突**；它在模板 unit 的註解裡活了兩個月，代價是 #710（builder job
            # 第一次由 daemon 經正規路徑派出來就死在 shim 的 `os.chdir()`）。
            #
            # `setfacl` 由**目錄 owner** 執行，不需要任何 capability ⇒ Manager 做得到。
            # 保留 owner 另外買到一件事：`gc`／`worktree_reclaim` 仍 `rmtree` 得掉整棵
            # 樹（那需要樹**內**的寫入權，交出 owner 等於讓工作區回收不了）。
            #
            # **遞迴是必要的**：樹裡每個 inode 都由 Manager 以 `UMask=0077` 建立，只在
            # 樹根下一條 ACL 的話 job 進得去、卻讀不到裡面任何東西。
            #
            # ⚠️ 這條**必須**下在 per-job 那一格。往 pool 根下 default ACL 會讓每個 job
            # 帳號進得去每個 job 的目錄——由
            # `_assert_job_workspace_reach_matches_the_plan()` 在 import 當下擋掉。
            owner = trusted_owner
            runtime_managed = True
            mode = _dir_file_mode(is_dir, 0o7 if is_dir else 0o6, 0, 0)
            workspace_account = scheme.resolve(workspace_reach.persona)
            if workspace_account is None:
                raise ValueError(
                    f"{asset.asset_id}：JOB_WORKSPACE_REACH 宣告的 persona "
                    f"{workspace_reach.persona.value} 在本方案沒有對應帳號——per-job "
                    "工作區的具名 ACL 因此產不出來，job 到了實機 `chdir` 不進去（#710）。"
                )
            acls.append(
                AclEntry(workspace_account, workspace_reach.access_perms, recursive=True)
            )
            acls.append(
                AclEntry(
                    workspace_account,
                    workspace_reach.default_perms,
                    default=True,
                    recursive=True,
                )
            )
            # 額外 reader（#629 的 `GATE` 就是這樣進來的）走**同一次** per-job setfacl
            # ——它與 builder 那條在本票之前一樣只存在於註解裡。清單由規則表給，而
            # **必須與登記表的 `readers` 對得上**：兩份各自列舉就是下一個 #641。
            declared_readers = {
                acct
                for acct in (scheme.resolve(r) for r in asset.readers)
                if acct is not None and acct not in {owner, workspace_account}
            }
            ruled_readers: dict[str, str] = {}
            for reader_principal, reader_perms in workspace_reach.extra_reader_perms:
                racct = scheme.resolve(reader_principal)
                if racct is None or racct in {owner, workspace_account}:
                    continue
                ruled_readers[racct] = reader_perms
            if set(ruled_readers) != declared_readers:
                raise ValueError(
                    f"{asset.asset_id}：JOB_WORKSPACE_REACH 的 extra_reader_perms "
                    f"{sorted(ruled_readers)} 與登記表 readers {sorted(declared_readers)} "
                    "對不上——per-job 那一格的每一條跨帳號授權都要同時在兩邊出現，"
                    "只有一邊的那條要嘛套不上去、要嘛沒有人宣告過（#641／#710）。"
                )
            for racct in sorted(ruled_readers):
                perms = ruled_readers[racct]
                acls.append(AclEntry(racct, perms, recursive=True))
                acls.append(AclEntry(racct, perms, default=True, recursive=True))
            writer_accounts = frozenset({owner, workspace_account})
            rationale = (
                "**per-job 工作區（#710）：owner＝Manager，job 帳號以具名 ACL 取得可寫面。**"
                "不是「整個 clone 由本 job 帳號擁有」——`chown` 給另一個使用者需要 "
                "`CAP_CHOWN`，而 Manager unit 帶 `CapabilityBoundingSet=`（空），那個形態"
                "結構上做不到（#623／#648 的宣稱在 unit 註解裡活了兩個月，零程式實作）。"
                "`setfacl` 由目錄 owner 執行、不需要任何 capability，且保留 owner 讓 "
                "`gc`／`worktree_reclaim` 仍 `rmtree` 得掉整棵樹。"
                "\n**ACL 遞迴套用**：樹由 Manager 以 `UMask=0077` 建出，只在根下一條 ACL "
                "的話 job 進得去卻讀不到裡面任何東西。access 用大寫 `X`（只有目錄與已可"
                "執行的檔拿到 `x`），default 用 `rwx`。"
                "\n⚠️ **只下在 per-job 那一格**：pool 根（`dispatch-worktree-pool`）是三個 "
                "job 帳號共用的容器，在它身上下 default ACL 會讓每個 job 帳號進得去每個 "
                "job 的目錄（裁決 10-2 當場歸零）。"
                "\n**驗證一律 `getfacl`**：`chmod` 會重寫 ACL mask，判準是 `mask::` 與 "
                "`#effective:`，不是「ACL 行存在」（runbook 4e-2b）。因此本項的命令順序是 "
                "`chown` → `chmod` → `setfacl`，執行期那一份（`coordinator/job_workspace.py:"
                "grant_workspace_acl`）在 setfacl 之後**不再 chmod**。"
            )
            open_points.append(
                f"{asset.asset_id}：per-job 那一格的 ACL 由 Manager 在 provision 當下"
                "套用（`coordinator/job_workspace.grant_workspace_acl`），setup 階段"
                "只登記形狀——路徑帶 `<job-id>`，部署當下還不存在。"
            )
        elif asset.ingress_kind is IngressKind.INTERPROCESS:
            # spool：trusted consumer 擁有並讀＋unlink，untrusted producer 只准 append。
            # Phase 2a 的 review verdict 通道（`review-verdict-spool`）走同一條政策：
            # 容器 owner 是 Manager（durable_state_owner）、mode 0700，reviewer 只拿
            # **write-only** ACL（`wx`，無 `r`——寫得進自己那格、讀不到他人 verdict），
            # builder 不在 writer 面故完全拿不到權限。這正是 spec 10-6「headless 可寫、
            # 不可讀不可改他人」的 per-job 單向語意。
            owner = trusted_owner
            mode = _dir_file_mode(is_dir, 0o7 if is_dir else 0o6, 0, 0)
            # owner 本身不需要 ACL（同帳號時 setfacl 只會是噪音；例如二分方案下
            # reviewer 與 Manager 併帳，此時 owner 位已涵蓋寫入權）。
            for pacct in sorted(a for a in job_writers if a != owner):
                acls.append(AclEntry(pacct, "wx" if is_dir else "w"))
            writer_accounts = frozenset({owner} | job_writers)
            rationale = (
                "job-visible spool：trusted consumer 擁有（讀＋消費），untrusted "
                "producer 僅以 ACL 授予 write（append），不得讀他人。"
            )
        elif len(job_writers) > 1:
            # 多 job persona 共享容器（worktree pool）：不得做成共寫目錄（會破 R2）。
            # 容器由 Manager 擁有，per-job 子目錄在 spawn 時逐案 chown 給該 job 帳號。
            owner = trusted_owner
            runtime_managed = True
            # 目錄容器：0701——others 僅 traverse 進自己被 chown 的子目錄，不可列目錄。
            # 檔案（如 per-job handoff manifest）：0600，owner-only；per-job owner 由
            # 降權啟動器在 spawn 時逐案 chown，容器層不預先開放。
            mode = _dir_file_mode(is_dir, 0o7 if is_dir else 0o6, 0, 0o1 if is_dir else 0)
            writer_accounts = frozenset({owner})  # 容器層僅 Manager 建子目錄
            rationale = (
                "job-visible 多 persona 容器：Manager 擁有容器，per-job worktree 於 "
                "spawn 時由降權啟動器 chown 給該 job 帳號——R2 在**子目錄粒度**強制"
                "（reviewer 與 builder 互不可寫）。容器層零 group/other 寫入。"
            )
            open_points.append(
                f"{asset.asset_id}：per-job 子目錄 chown 由 Phase 2 降權啟動器負責；"
                "本表只定容器層權限。"
            )
        else:
            # 單一 job writer：owner＝該 job 帳號，owner-only。
            #
            # #641：本分支原本無條件補一條「trusted reader（Manager）唯讀 ACL」，
            # 理由寫的是「交換面沿用 D2 git 讀」。那個交換面在 #637 之後已經不存在
            # ——builder 的成果走 bundle → `commit-spool` → Manager 從**檔案** fetch，
            # reviewer 的 verdict 走 `review-verdict-spool`。跨帳號讀取因此**只**在
            # 登記表真的宣告了非 owner 的 reader 時才產生；本族三項（`repo-worktree`
            # ／`review-verdict`／`work-items-yaml`）在 #641 之後都沒有，產出即為
            # owner-only、零 ACL。rationale 也跟著條件化——operator review 的是這一行，
            # 它不能在「這一項其實沒有任何跨帳號授權」時還宣稱有。
            owner = next(iter(job_writers), trusted_owner)
            mode = _dir_file_mode(is_dir, 0o7 if is_dir else 0o6, 0, 0)
            for racct in sorted(reader_accounts):
                if racct == owner:
                    continue
                acls.append(AclEntry(racct, "rX" if is_dir else "r"))
            writer_accounts = frozenset({owner})
            rationale = (
                "job-visible 單一 writer：owner＝對應 job 帳號可寫。"
            )
            if acls:
                rationale += (
                    "登記表宣告了非 owner 的 reader，故以 per-account 唯讀 ACL 精確授予"
                    "（每一條都必須在該資產的 note 指名真正的消費者——#641：成果交付"
                    "一律走 spool，「Manager 讀 job 的樹」不是合法理由）。"
                )
            else:
                rationale += (
                    "**owner-only、零跨帳號 ACL**：成果交付走 spool（builder→"
                    "`commit-spool` 的 bundle、reviewer→`review-verdict-spool` 的 "
                    "verdict），Manager 不讀 job 的樹（#637／#641）。"
                )

    # 安全網：group/other 一律不得帶 write 位（spec §R2「group 寫入權 MUST 移除」）。
    #
    # **#698 改了這一行的最後一段，而那是本票唯一動到 mode 管線的地方。** 舊寫法是
    # `mode & 0o700`——它保住 owner 位，卻連同 setuid／setgid **與 sticky** 一起吃掉。
    # sticky 被吃掉的後果不是「權限緊一點」：它是「目錄可寫但只能刪自己的檔」的**唯一**
    # 表達方式，沒有它就無法在一棵 job 可寫的樹裡放一個 job 動不了的 root-owned
    # `hooks.json`（#698 的方案 A）。#685 把這條列為「本票不做」的兩個理由之一。
    #
    # §R2 的不變式**一行都沒有放寬**：group／other 的 write 位仍然無條件清除，job 的
    # 寫入權一律走具名 ACL；新增的只有「sticky 通得過」。setuid／setgid 則從「被
    # `& 0o700` 順手吃掉」升級為**明文清除**——同樣的結果，但現在有出處、也擋得住
    # 日後有人把遮罩再放寬一位。
    group_bits = _mask_write((mode >> 3) & 0o7)
    other_bits = _mask_write(mode & 0o7)
    mode = (mode & (STICKY_BIT | 0o700) & ~_FORBIDDEN_SPECIAL_BITS) | (group_bits << 3) | other_bits

    return PermissionEntry(
        asset_id=asset.asset_id,
        tier=asset.tier.name,
        tree=asset.tree.value,
        owner_class=owner_class,
        owner=owner,
        group=scheme.group_of(owner),
        mode=mode,
        is_directory=is_dir,
        is_symlink=asset.asset_id in SYMLINK_ASSETS,
        writer_accounts=writer_accounts,
        reader_accounts=reader_accounts,
        acls=tuple(acls),
        runtime_managed=runtime_managed,
        legacy_writers=legacy,
        rationale=rationale,
        open_points=tuple(open_points),
    )


# ---------------------------------------------------------------------------
# 產生器入口
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PermissionPlan:
    """完整權限計畫（登記表全項）。"""

    scheme_id: str
    entries: tuple[PermissionEntry, ...]
    #: 產生本計畫的登記表切片。`PermissionEntry` 只留權限結論、不留 persona 面，
    #: 但 per-persona 的 ReadWritePaths 導出（`principals=` 過濾）需要回頭看
    #: writers／readers，故把輸入資產一併帶著走。手工組出的 plan 留空 tuple，
    #: 此時 `required_write_targets` 退回 `registry.ASSET_REGISTRY` 查表。
    assets: tuple[TrustRootAsset, ...] = ()
    #: 產生本計畫的方案**本體**。`scheme_id` 只是名字，查 `SCHEMES` 拿回來的是
    #: 模組層那個**未注入部署對應**的方案（#626 之後 operator／external reader 是
    #: 產生當下才注入的）——只憑 id 反查會把注入過的對應整個丟掉，自我檢查因此會
    #: 誤判。故一律把方案本身帶著走；`compare=False` 讓兩份同方案計畫仍然相等。
    scheme: "UidScheme | None" = field(default=None, compare=False, repr=False)

    @property
    def unresolved_principals(self) -> tuple[Principal, ...]:
        """本計畫用到、但方案沒有對應到帳號的 principal（空 tuple＝可安全輸出）。"""
        scheme = self.scheme or SCHEMES.get(self.scheme_id)
        if scheme is None:
            return ()
        return scheme.unresolved_principals(self.assets or registry.ASSET_REGISTRY)

    def by_id(self, asset_id: str) -> PermissionEntry:
        for e in self.entries:
            if e.asset_id == asset_id:
                return e
        raise KeyError(asset_id)

    def all_writable_accounts(self, entry: PermissionEntry) -> frozenset[str]:
        """entry 上所有實際可寫的帳號（owner＋ACL 授寫者）。"""
        accts = set(entry.writer_accounts)
        for acl in entry.acls:
            if acl.writable:
                accts.add(acl.account)
        return frozenset(accts)

    def to_dict(self) -> dict[str, object]:
        return {
            "scheme_id": self.scheme_id,
            "asset_count": len(self.entries),
            # 未對應的 principal 一律隨計畫一起出現：JSON 模式不會 raise（它是診斷
            # 用途），但**必須**看得出「這份計畫少了誰的授權」，否則就是靜默漏授。
            "unresolved_principals": [p.value for p in self.unresolved_principals],
            "entries": [e.to_dict() for e in self.entries],
        }


def generate_plan(
    scheme: UidScheme,
    assets: tuple[TrustRootAsset, ...] = registry.ASSET_REGISTRY,
) -> PermissionPlan:
    """對登記表每一項機械產生權限，回傳完整計畫（涵蓋無遺漏）。

    **本函式不 fail-closed**：計畫是資料，看得到未對應的 principal 反而有助診斷
    （`PermissionPlan.unresolved_principals`）。fail-closed 發生在
    :func:`plan_to_commands`——也就是「要輸出會被 `sh -e` 執行的命令」的那一刻。
    """
    entries = tuple(build_entry(a, scheme) for a in assets)
    return PermissionPlan(
        scheme_id=scheme.scheme_id,
        entries=entries,
        assets=tuple(assets),
        scheme=scheme,
    )


def _scheme_for(plan: PermissionPlan, scheme: UidScheme | None) -> UidScheme:
    """取本計畫該用的方案：顯式參數 > 計畫自帶 > `SCHEMES` 反查 > 預設。

    順序不能反：`SCHEMES[plan.scheme_id]` 拿到的是**未注入部署對應**的模組層方案。
    """
    if scheme is not None:
        return scheme
    if plan.scheme is not None:
        return plan.scheme
    return SCHEMES.get(plan.scheme_id, DEFAULT_SCHEME)


def assert_principals_resolved(plan: PermissionPlan, scheme: UidScheme) -> None:
    """輸出前的 fail-closed 閘：有任何未對應的 principal 即 raise，一行都不輸出。"""
    unresolved = scheme.unresolved_principals(plan.assets or registry.ASSET_REGISTRY)
    if unresolved:
        raise UnresolvedPrincipalError(unresolved, scheme.scheme_id)


#: 命令字串裡的 ACL 帳號（`setfacl … u:<name>:<perms> …`）。前置的 `(?<![\w-])`
#: 防止把 `menu:x:` 這種尾字為 `u` 的名字誤切成 `u:` 條目。
_ACL_ACCOUNT_RE = re.compile(r"(?<![\w-])u:([^:\s]+):")
#: `chown <owner>:<group> <path>`——owner／group 同樣必須是宣告過的帳號。
_CHOWN_ACCOUNT_RE = re.compile(r"(?<![\w-])chown\s+([^:\s]+):([^:\s]+)\s")


def unknown_accounts_in(lines: "list[str]", scheme: UidScheme) -> tuple[str, ...]:
    """輸出行裡出現、卻不在方案宣告帳號集合內的名字（空 tuple＝乾淨）。

    註解行**一併檢查**：per-job 資產是以註解形式輸出的，一個 phantom 帳號躲在
    `#   setfacl …` 裡照樣會被人複製貼上去執行。
    """
    declared = scheme.declared_accounts()
    seen: set[str] = set()
    for line in lines:
        for match in _ACL_ACCOUNT_RE.finditer(line):
            seen.add(match.group(1))
        for match in _CHOWN_ACCOUNT_RE.finditer(line):
            seen.update(match.groups())
    return tuple(sorted(seen - declared))


def assert_output_accounts_known(lines: "list[str]", scheme: UidScheme) -> None:
    """自我檢查：輸出裡每個帳號名都必須是本方案宣告過的，否則 raise。

    這條擋的是**未來**——新增一個 principal 而忘了進對應表時，字面角色名一漏進
    命令字串就 raise，不會再靜默產生一行 `sh -e` 下會炸掉的 `setfacl`（#626）。
    """
    unknown = unknown_accounts_in(lines, scheme)
    if unknown:
        raise UnknownAccountInOutputError(
            unknown, scheme.scheme_id, scheme.declared_accounts()
        )


def _placeholder_path(entry: PermissionEntry) -> str:
    """未提供真實路徑時的清楚標記 placeholder。"""
    return f"<PATH:{entry.asset_id}>"


#: per-job 路徑的標記 segment。帶此 segment 的資產由降權啟動器在 spawn 時逐案套用，
#: **不**在 setup 階段執行——命令因此以註解形式輸出（可讀、不可誤執行）。
PER_JOB_SEGMENT = "<job-id>"


def plan_to_commands(
    plan: PermissionPlan,
    path_of: Mapping[str, str] | None = None,
    layout: "PathLayout" = None,  # type: ignore[assignment]
    scheme: UidScheme | None = None,
) -> list[str]:
    """把計畫轉成 runbook 可引用的命令序列（**只產生字串，絕不執行**）。

    `path_of`：asset_id→真實路徑字串；未提供者以 placeholder 呈現，供 runbook 以
    shell 變數替換（`PathLayout.asset_paths()` 可一次提供全部真實路徑）。輸出含
    分節註解，方便 operator 對照登記表逐項核可；目錄資產會先出 `install -d`，
    使整份輸出成為一份可直接執行的 setup script。

    帶 `path_of`（真實路徑）時，輸出**尾端**另附一節由跨帳號 ACL 機械導出的父目錄
    traverse ACL（`derive_traverse_grants`，#620）——沒有它，葉節點 ACL 全部正確
    但路徑走不通。`layout`／`scheme` 只影響那一節（骨架目錄的 owner／mode 是判斷
    「這層是否已可 traverse」的輸入），未給時取 `DEFAULT_LAYOUT` 與 plan 的 scheme。

    **fail-closed（#626）**：方案裡有任何 principal 沒對應到真實 OS 帳號時，本函式
    raise :class:`UnresolvedPrincipalError` 而**不輸出任何一行**；輸出組完後再過一道
    :func:`assert_output_accounts_known` 自我檢查。理由見模組 docstring——半套用的
    權限樹比「產生器拒絕產出」危險得多。
    """
    scheme = _scheme_for(plan, scheme)
    assert_principals_resolved(plan, scheme)
    # #685：symlink 資產的目標由 layout 導出（`asset_paths()` 給的是 symlink 自己）。
    # 未給 layout 時退回 `DEFAULT_LAYOUT`，與下方 traverse 那一節同一個取法；未給
    # `path_of`（placeholder 模式）時 `commands()` 自己會出 `<TARGET:asset_id>`。
    link_of = (layout or DEFAULT_LAYOUT).symlink_targets() if path_of else None
    lines: list[str] = [
        f"# trust-root Phase 2b 權限套用命令（scheme={plan.scheme_id}）",
        "# 由 permgen 機械產生；operator 逐項 review 後手動 sudo 執行。",
        "# 帶 --paths 時路徑為 PathLayout 的真實絕對路徑；否則以 <PATH:asset_id> 呈現。",
        f"# 含 {PER_JOB_SEGMENT} 的資產屬 per-job（降權啟動器逐案套用），已註解不執行。",
    ]
    for e in plan.entries:
        path = (path_of or {}).get(e.asset_id) or _placeholder_path(e)
        per_job = PER_JOB_SEGMENT in path
        lines.append("")
        # rationale 可能是多行（#698 起有幾條是）。**每一行都要自己的 `#`**：這份輸出
        # 是 runbook 以 `sudo sh -e` 直接執行的 script，漏掉前綴的那幾行會被 shell 當成
        # 命令解析——症狀是整份 script 在一個看起來像散文的地方 `Syntax error`，而且
        # 那一行離真正的原因（某個資產的 rationale 換了行）非常遠。
        rationale_lines = e.rationale.splitlines() or [""]
        lines.append(
            f"# [{e.tier}] {e.asset_id} ({e.owner_class.value}) — {rationale_lines[0]}"
        )
        lines += [f"#   {line}" for line in rationale_lines[1:]]
        if e.runtime_managed:
            lines.append("#   注意：per-child owner 由降權啟動器逐案 chown（本節僅容器層）。")
        for op in e.open_points:
            lines.append(f"#   後續依賴：{op}")
        if per_job:
            lines.append("#   per-job：由降權啟動器在 spawn 時套用，setup 階段不執行。")
        link_target = (link_of or {}).get(e.asset_id)
        cmds = list(e.commands(path, link_target))
        if e.is_symlink:
            # #685：symlink 的守衛掛在**父目錄**上，不是自己身上——`ln` 是建立動作，
            # 「不存在就跳過」對它沒有意義；真正要跳過的情形是「本方案沒有這個帳號、
            # 那個 HOME 根本不存在」（二分方案下 `cortex-reviewer-planner` 就是如此）。
            # 這與葉檔的 `[ ! -e <檔> ]` 守衛是同一條原則的兩個位置。
            parent = _parent_dir(path)
            lines.append(
                f"#   symlink 守衛：{parent} 不存在時跳過（本方案沒有這個帳號的 HOME）。"
            )
            lines.append(
                "#   目標落在該帳號既有的 cache 底下 ⇒ 不新增任何可寫面；symlink 自己"
            )
            lines.append(
                "#   放在 root-owned 的 HOME 裡 ⇒ job 換不掉它的指向。"
            )
            cmds = [f"[ ! -e {parent} ] || {cmd}" for cmd in cmds]
        elif e.is_directory:
            # 目錄一定先建起來，後續 chown／chmod／setfacl 必然有對象。
            #
            # #698：sticky 樹的既有部署可能是**別的東西**（reviewer-planner 上是一條
            # 指向 `cache/codex` 的 symlink）。`install -d` 對「已存在的 symlink」不會
            # 報錯、也不會取代它——它會**跟著連結**去建目標目錄，於是 chown／chmod／
            # setfacl 全部套到 `cache/codex` 那棵 job-owned 的樹上，而現場看起來一切
            # 正常。遷移那一步（改成真目錄）必須由 operator 顯式做，見 runbook 4e-2b。
            if e.asset_id in STICKY_JOB_WRITABLE_DIR_ASSETS:
                lines.append(
                    f"#   ⚠️ 遷移守衛：{path} 若是既有部署留下的 **symlink**，下面這行"
                    "`install -d` 會跟著它去建目標、把權限套到錯的樹上（#698 的部署陷阱）。"
                )
                lines.append(
                    f"#      先確認形狀：`[ -L {path} ] && echo 'symlink——先跑 runbook "
                    "4e-2b 的遷移步驟，不要直接套用本節'`"
                )
                cmds.insert(0, f"install -d {path}")
                # 守衛必須排在 `install -d` **之前**：`install -d` 對既有 symlink 會
                # 跟著它去建目標，錯誤在那一行就已經造成了。
                cmds.insert(
                    0,
                    f"[ ! -L {path} ] || {{ echo '⛔ {path} 仍是 symlink，"
                    f"見 runbook 4e-2b 的遷移步驟' >&2; exit 1; }}",
                )
            else:
                cmds.insert(0, f"install -d {path}")
        elif e.asset_id in ENFORCEMENT_LEAF_ASSETS:
            # #698：enforcement 檔與其他葉檔**相反**——它不能用 `[ ! -e ] ||` 跳過。
            #
            # sticky bit 管的是「刪／改名**別人的**檔」，**不管「建一個還不存在的檔」**。
            # 因此這個檔不存在時，sticky 樹擋不住任何東西：job 直接 `printf x >
            # $HOME/.codex/hooks.json` 就成功了（那正是 R9 T3.9 的攻擊字面）。
            # 「跳過」在這裡不是保守，是**靜默把整個裁決變成空的**。
            #
            # 因此改成 create-if-absent：由 root 種一份最小、合法、**不含任何命令**的
            # 文件，再套上 root:root 0644。內容之後要換成什麼是 root 的決定；產生器
            # 只保證「這個位置永遠有一個 root 擁有的檔」。
            lines.append(
                f"#   ⛔ 本檔**必須存在**，否則 sticky 樹什麼也擋不住（job 建得出新檔）"
                "——因此這裡是 create-if-absent，不是「不存在就跳過」（#698）。"
            )
            lines.append(
                "#   內容是最小且**不含任何命令**的 hooks 文件：本檔的價值在於「這個位置"
                "由 root 佔住」，不在於跑什麼。要放真的 hook 是 root 之後的決定。"
            )
            seed = [f"[ -e {path} ] || cat > {path} <<'PSC_CODEX_HOOKS_EOF'"]
            seed += CODEX_HOOKS_SEED_CONTENT.splitlines()
            seed.append("PSC_CODEX_HOOKS_EOF")
            cmds = seed + cmds
        else:
            # 葉檔在 setup 當下多半尚未存在（由服務首次寫入時建立）。加 `[ ! -e ] ||`
            # 守衛：不存在就跳過（且在 `sh -e` 下不會中斷腳本），存在就套上目標權限。
            # 尚未存在也安全——容器目錄已是 owner-only，且 unit 的 UMask=0077 讓新檔
            # 出生即 0600。
            lines.append(
                f"#   葉檔守衛：{path} 尚未建立時跳過（服務以 UMask=0077 建立即符合目標）。"
            )
            cmds = [f"[ ! -e {path} ] || {cmd}" for cmd in cmds]
        for cmd in cmds:
            lines.append(f"#   {cmd}" if per_job else cmd)
    # 父層 traverse ACL 一律殿後——見 `traverse_commands` 的順序說明（chmod 會重寫
    # ACL mask）。placeholder 模式（未給 path_of）沒有真實路徑階層可推，故不出這節。
    lines += traverse_commands(
        derive_traverse_grants(plan, layout, scheme, path_of=path_of or {})
    )
    assert_output_accounts_known(lines, scheme)
    return lines


# ---------------------------------------------------------------------------
# Phase 2b：部署 layout（把登記表的抽象資產綁到目標主機的真實絕對路徑）
#
# operator 0816 第二輪裁決：durable state 落 `/var/lib/cortex`（worktree pool＝
# `/var/lib/cortex/worktree`）、Manager 部署落 `/opt/cortex`。本 layout 是那份裁決
# 的機器可讀形式——runbook 不再手寫路徑，全部從這裡取。
# ---------------------------------------------------------------------------

def _dedupe_scaffold(
    entries: tuple[tuple[str, str, str, int], ...],
) -> tuple[tuple[str, str, str, int], ...]:
    """同一個路徑只留第一次出現的那一筆（順序不變）。

    骨架清單是由**多條獨立規則**疊出來的（帳號 HOME／cache、憑證形狀各自的前置層），
    不同規則在某些 layout 下會指向同一層。去重讓 `install -d` 不會重複輸出，也讓
    「無多餘」那類等式測試不必為一個純顯示層的重複而放寬。

    ⚠️ **只留第一筆**，因此「同一條路徑被兩條規則以不同 mode 產出」會**靜默**取前者。
    #698 之後 `~/.codex` 是登記表資產（sticky 1755 ＋ ACL）而**不再**進骨架，正是為了
    避免骨架那份 0755 把 sticky 位蓋掉——那種漂移在輸出上看不出來。
    """
    seen: set[str] = set()
    kept: list[tuple[str, str, str, int]] = []
    for entry in entries:
        if entry[0] in seen:
            continue
        seen.add(entry[0])
        kept.append(entry)
    return tuple(kept)


@dataclass(frozen=True)
class ExtraWritePath:
    """非登記表資產、但服務身分確實需要寫的路徑（每條必須附理由）。

    這是「無多餘」等式的唯一合法例外通道：ReadWritePaths 由登記表機械導出，
    任何額外條目都必須在此明示宣告並說明理由，測試會強制理由非空。
    """

    path: str
    reason: str


@dataclass(frozen=True)
class PathLayout:
    """目標主機的絕對路徑 layout（0816 裁決值為預設）。"""

    agents_root: str = "/var/lib/cortex"
    worktree_root: str = "/var/lib/cortex/worktree"
    deploy_root: str = "/opt/cortex"
    instance: str = "cortex"
    #: 服務／job 帳號 HOME 的父目錄。**每個帳號的 HOME 由帳號名機械導出**
    #: （`home_of()`），不再是寫死的字面量——寫死會在換 scheme 時漂移：三分的
    #: Manager 帳號是 `cortex-manager`，HOME 卻還指著二分時代的 `/var/lib/cortex-svc`，
    #: unit 的 `Environment=HOME=` 與 scaffold 因此指向一個沒人擁有的目錄。
    home_root: str = "/var/lib"
    #: builder 的帳號名。只給 `asset_paths()` 用（`builder-gitconfig` 掛在
    #: builder HOME 下），因為 `asset_paths()` 刻意不吃 scheme——兩個 scheme 對 BUILDER
    #: 的映射相同。其餘所有帳號相關路徑一律由 scheme 現場導出。
    builder_account: str = "cortex-builder"
    #: reviewer＋planner 的 job 帳號名（`reviewer-planner-gitconfig` 掛在它的 HOME 下）。
    #: 與 `builder_account` 同一個理由存在，但**多一個前提**：兩個 scheme 對 REVIEWER／
    #: PLANNER 的映射**不同**——這裡取的是**定案的三分**。二分是向後相容選項，其
    #: reviewer／planner 與 Manager 併帳（`cortex-svc`）、且尚未經模板 unit 降權起 job，
    #: 因此那個部署形態下本資產不適用（產生的命令帶 `[ ! -e ] ||` 守衛，會直接跳過）。
    reviewer_planner_account: str = "cortex-reviewer-planner"
    #: Manager＋monitor 的服務帳號名（`manager-gitconfig` 掛在它的 HOME 下）。與上面
    #: 兩個同一個理由：`asset_paths()` 刻意不吃 scheme，而兩個 scheme 對 MANAGER 的映射
    #: **不同**（二分是 `cortex-svc`）——這裡同樣取**定案的三分**，二分下該資產不適用。
    manager_account: str = "cortex-manager"
    #: 本 instance 治理的來源 repo slug（`<repo_source_root>/<slug>`）。**部署決定**，
    #: 刻意留空：`.gitconfig` 需要**逐字**的 `safe.directory` 路徑（git 不吃目錄萬用
    #: 字元，見 `build_account_gitconfig`），猜不到就只能猜錯。未指定時
    #: `build_account_gitconfig()` fail-closed，比照 #626 的部署決定型 principal。
    source_repo_slugs: tuple[str, ...] = ()
    #: executor 憑證在帳號 HOME 下的相對路徑（登記表資產 `*-executor-credential`）。
    #: **部署決定**：預設對齊本部署的 `PSC_MANAGER_EXECUTOR=codex`（`~/.codex/auth.json`，
    #: 也就是 #640 實測的那一個）。換 executor 時只改這一個值——`asset_paths()`、
    #: 骨架目錄（那一層 root-owned 的父目錄）與 unit 的 `ReadWritePaths` 全部跟著動。
    executor_credential_relpath: str = ".codex/auth.json"
    #: per-job 路徑的 segment；system unit 模板用 `%i`（systemd instance 名）。
    job_segment: str = PER_JOB_SEGMENT

    def __post_init__(self) -> None:
        # 值會被接成絕對路徑並嵌進 root 執行的命令字串，因此在**建構**當下就驗形狀，
        # 不等到 operator `sudo` 執行（比照 `_validate_account_name`／`_validate_repo_slug`）。
        _validate_credential_relpath(self.executor_credential_relpath)

    # -- 衍生根 -------------------------------------------------------------
    @property
    def control_root(self) -> str:
        return f"{self.agents_root}/control"

    @property
    def coordinator_root(self) -> str:
        return f"{self.agents_root}/coordinator"

    @property
    def specs_root(self) -> str:
        return f"{self.agents_root}/specs"

    @property
    def monitor_state_root(self) -> str:
        return f"{self.agents_root}/monitor"

    @property
    def project_config_root(self) -> str:
        return f"{self.agents_root}/config/paulsha"

    @property
    def skill_registry_root(self) -> str:
        return f"{self.agents_root}/registry"

    @property
    def repo_source_root(self) -> str:
        """per-job clone 的**來源樹容器**（登記表資產 `repo-source-tree`，#623）。

        每個受治理 repo 一格：`<此根>/<slug>`，是 **working checkout**（不是 bare）——
        monitor 要掃工作樹裡的 `workstreams/*/todo.md`，bare 沒有工作樹；同一份 checkout
        因此兼作 monitor 的掃描目標與 job 的 clone 來源。

        掛在 `agents_root` 底下而不是部署樹（`/opt/cortex`）：它是**每個 instance 一份的
        資料**（隨 instance 治理的 repo 走），不是隨版本走的部署產物；換 layout 時它跟著
        durable state 樹搬，而不是跟著 venv 搬。
        """
        return f"{self.agents_root}/repos"

    def source_repo_paths(self) -> tuple[str, ...]:
        """已宣告的來源 repo 絕對路徑（`<repo_source_root>/<slug>`；未宣告即空）。"""
        return tuple(f"{self.repo_source_root}/{slug}" for slug in self.source_repo_slugs)

    def source_repo_safe_directories(self) -> tuple[str, ...]:
        """每個來源 repo 需要的 `safe.directory` 值——**工作樹根 ＋ `<root>/.git` 兩條**。

        實測（#623 複驗）：從**非 bare** 的來源 clone 時，git 檢查的是 `<repo>/.git`
        而**不是**工作樹根——

            fatal: detected dubious ownership in repository at
                   '/var/lib/cortex/repos/paulsha-cortex/.git'

        而 `git -C <repo> rev-parse`／`fetch` 這類對工作樹本身的操作，報的又是工作樹根：

            fatal: detected dubious ownership in repository at
                   '/var/lib/cortex/repos/paulsha-cortex'

        `safe.directory` 只認**逐字相等**的路徑（見 `build_account_gitconfig` 的說明），
        兩個位置就是兩條值，只給其中一條會讓另一半的操作在完全不同的時機才失敗。
        """
        entries: list[str] = []
        for path in self.source_repo_paths():
            entries.append(path)
            entries.append(f"{path}/.git")
        return tuple(entries)

    @property
    def run_root(self) -> str:
        return f"{self.agents_root}/run/{self.instance}"

    @property
    def dispatch_log_root(self) -> str:
        """Manager 的 job log_dir（`autonomy.py` 以相對 `runtime/dispatch/<slice>`
        推導，故由 unit 的 `WorkingDirectory` 決定落點）。gate ledger 住在這裡。"""
        return f"{self.agents_root}/runtime/dispatch"

    @property
    def commit_spool_root(self) -> str:
        """builder 成果回收的 bundle spool 根（登記表資產 `commit-spool`，#623／#634）。

        路徑與 `config.paths.commit_spool_root()` 是**成對契約**，由 `asset_paths()`
        供給權限計畫；本 property 只是給 unit 產生器引用的同一份字面量（比照
        `job_spec_spool_root`）。
        """
        return f"{self.coordinator_root}/commit-spool"

    @property
    def review_verdict_spool_root(self) -> str:
        """reviewer 寫、Manager 讀的 per-job verdict spool 根（登記表資產
        `review-verdict-spool`，#599／#638）。

        路徑與 `config.paths.review_verdict_spool_root()` 是**成對契約**，由
        `asset_paths()` 而非本 property 供給權限計畫；本 property 只是給 unit
        產生器引用的同一份字面量（比照 `commit_spool_root`／`job_spec_spool_root`）。
        """
        return f"{self.coordinator_root}/review-verdicts"

    def job_log_spool_root(self, principal: Principal) -> str:
        """該降權 principal 的 job log spool 根（登記表資產由 `JOB_LOG_SPOOLS` 導出）。

        路徑與 `config.paths.job_log_spool_root()` 是**成對契約**，由 `asset_paths()`
        供給權限計畫；本方法只是同一份字面量的另一個入口（比照
        :meth:`job_spec_spool_for`）。

        **掛在該 principal 既有的輸出通道底下是刻意的**（#686 design D3「不新開通道」／
        U-3 未裁決，#708 起適用於三個 principal）：`_minimize()` 因此把它從模板 unit 的
        `ReadWritePaths=` 吃掉，那個帳號的寫入面逐字不變，default ACL 自動繼承。
        理由全文見 `config/paths.py:job_log_spool_root` 與該資產的 note。

        **通道根不在這裡重算**——它由 :meth:`asset_paths` 上那條通道資產的值供給，
        因此「掛在既有通道底下」是查表的結果，不是又一個手寫前綴（手寫前綴漂掉的
        症狀是 permgen 對一條路徑出 ACL、runtime 寫另一條，兩邊都不報錯）。
        """
        spool = registry.job_log_spool_for(principal)
        return f"{self.asset_paths()[spool.channel_asset_id]}/{spool.dirname}"

    @property
    def planning_job_log_spool_root(self) -> str:
        """planning job 寫、Manager 讀的輸出通道根（登記表資產
        `planning-job-log-spool`，#686；#708 起是 :data:`registry.JOB_LOG_SPOOLS`
        的一列）。回傳值逐字不變。"""
        return self.job_log_spool_root(Principal.REVIEWER)

    @property
    def planning_scratch_root(self) -> str:
        """planning job 的 per-invocation **唯讀** scratch pool 根（登記表資產
        `planning-scratch-pool`，#686）。

        它**不**出現在任何 job 模板 unit 的 `ReadWritePaths=`——那是登記表 writer 面
        只有 Manager 的機械後果，不是這裡的字面決定（見該資產的 note）。
        """
        return f"{self.coordinator_root}/planning-scratch"

    @property
    def gate_ledger_spool_root(self) -> str:
        """gate 寫、Manager 讀的 per-job ledger spool 根（登記表資產
        `gate-ledger-spool`，#629）。

        路徑與 `config.paths.gate_ledger_spool_root()` 是**成對契約**，由
        `asset_paths()` 而非本 property 供給權限計畫；本 property 只是給 unit
        產生器引用的同一份字面量（比照 `commit_spool_root`）。
        """
        return f"{self.coordinator_root}/gate-ledger-spool"

    @property
    def gate_worktree_root(self) -> str:
        """gate 執行身分的拋棄式工作區 pool 根（登記表資產 `gate-worktree-pool`，#629）。

        與 `config.paths.gate_worktree_root()` 成對契約。掛在 `agents_root` 底下而不是
        `worktree_root`：那個 pool 的容器已為 job persona 的三個帳號套好 ACL，把一個
        **不同帳號**的樹混進同一棵容器只會讓「誰進得了哪一格」變成要逐格推敲的事。
        """
        return f"{self.agents_root}/gate-worktree"

    @property
    def job_spec_spool_root(self) -> str:
        """per-principal spec spool 的**容器**（登記表資產 `job-spec-spool`）。

        路徑與 `config.paths.job_spec_spool_root()` 是**成對契約**，由 `asset_paths()`
        而非本 property 供給權限計畫；本 property 只是給 unit／shim 產生器引用的同一份
        字面量。

        **#657 起沒有任何 job 讀得到這一層**：它 owner-only 0700、零跨帳號 ACL，
        降權帳號只會拿到機械導出的 `--x` traverse。spec 落在
        :meth:`job_spec_spool_for` 的子 spool。
        """
        return f"{self.coordinator_root}/job-specs"

    def job_spec_spool_for(self, principal: Principal) -> str:
        """#657：**該降權 principal 專屬**的 spec spool（`<容器>/<principal>`）。

        與 `config.paths.job_spec_spool_for()` 是**成對契約**（登記表資產
        `job-spec-spool-<principal>`）。模板 unit 的
        `Environment=PSC_JOB_SPEC_SPOOL=` 用的就是本函式——「哪個身分讀哪個 spool」
        因此是 root-owned unit 檔上可逐字稽核的一行。
        """
        return f"{self.job_spec_spool_root}/{principal.value}"

    @property
    def bin_root(self) -> str:
        """部署樹的可執行檔目錄（root-owned）——降權 shim 住這裡。"""
        return f"{self.deploy_root}/bin"

    @property
    def toolchain_root(self) -> str:
        """四個模型 executor 的部署樹落點（登記表資產 `executor-toolchain`，#640）。

        掛在 `deploy_root` 而不是 `agents_root`：它是**隨版本走的部署產物**（哪一版的
        模型 CLI），不是隨 instance 治理的資料——與 venv／shim 同一棵樹、同一個 owner
        （root）、同一條升級路徑。
        """
        return f"{self.deploy_root}/toolchain"

    @property
    def toolchain_bin(self) -> str:
        """job 的 `PATH` 上那一段——四個 executor 的進入點都在這裡。"""
        return f"{self.toolchain_root}/bin"

    @property
    def toolchain_lib(self) -> str:
        """需要整包搬的 CLI（npm 套件樹）的落點；`bin/` 內的進入點指進來。"""
        return f"{self.toolchain_root}/lib"

    def job_path_value(self) -> str:
        """job 應該拿到的 `PATH`（＝ Manager 端 `PSC_BUILDER_PATH` 的值，#640）。

        **toolchain 必須排在最前面**：系統層可能另有一份同名但舊很多的 CLI（實機盤點
        到的兩份 `codex` 差 100 個以上小版本），排在後面就會被系統那份蓋掉，而症狀是
        「跑得起來、但跑的不是你以為的版本」——比 `command not found` 難查得多。

        尾段給的是系統層：`node`（codex 的 runtime）、`git`、`python3`（wrapper 內的
        gate ledger writer）都在那裡。刻意**不含** `sbin`——job 不需要任何管理工具。
        """
        return ":".join((self.toolchain_bin,) + JOB_PATH_SYSTEM_TAIL)

    def job_home_value(self, account: str) -> str:
        """job 應該拿到的 `HOME`（＝ Manager 端 `PSC_<ROLE>_HOME` 的值，#685／#686）。

        **它與 PATH 是同一個形狀的缺口**（#679）：模板模式下 `cortex-job-shim` 以
        `os.execvpe(command[0], command, job_env)` 把環境**整份換掉**，因此 unit 上那行
        `Environment=HOME=` 到不了模型行程；`HOME` 只能來自 spec 的 env，而那份 env 由
        `job_runner.build_job_env()` 從 Manager 端的 `PSC_<ROLE>_HOME` 取。實機 0818：
        `PSC_BUILDER_HOME` **有**宣告，`PSC_REVIEWER_HOME`／`PSC_GATE_HOME` **沒有** ⇒
        降權 planning 的 agy 死在
        `resolving log directory: getting home directory: $HOME is not defined`（#686）。

        **這條與憑證面是綁在一起的**：#685 登記的三份登入態全部以 `$HOME` 為根
        （`~/.codex`／`~/.gemini`／`~/.claude` 三條 symlink），`HOME` 解不到時它們在 job
        內**一條都不存在**——codify 出來的路徑會全部落空，而症狀（`Not logged in`／
        `$HOME is not defined`）與「憑證沒放好」長得一模一樣。產生器出這個值，是為了讓
        operator 落進 root-owned EnvironmentFile 的那一行有**單一來源**，不必手抄。

        本函式只**出值**，不改 `job_runner` 的 fail-open/closed 語意——那屬於獨立票
        （#686 已把理由寫進 `build_job_env()` 的 docstring）。
        """
        return self.home_of(account)

    def preflight_command_value(self) -> str:
        """`PSC_PREFLIGHT_CMD` 應該拿到的值（#661）。

        形態是 **typed argv、絕對路徑的部署 venv interpreter ＋ `-m <module>`**：

        - **typed argv**：`coordinator/preflight.py` 的 `_validate_typed_command()`
          會拒絕 shell wrapper，doctor 也把它列為一個獨立的失敗類別；
        - **絕對路徑**：`load_preflight_command()` 對絕對路徑會實際檢查
          `is_file() and os.access(X_OK)`，缺件在 doctor 就看得到，而不是等到 ship
          當下才 `command not found`；走 `PATH` 則要看 unit 注入了什麼；
        - **部署 venv 的 interpreter**：`ProtectHome=yes` 之後 operator HOME 底下的
          任何東西都不可達（#661 的原症狀：舊值 `~/.local/bin/cortex-preflight-ci`
          是個 shell wrapper，它指向的 backend 也在 `/home` 底下——**兩層都不可達**）。
          `/opt/cortex/venv` 是既有的 root-owned 部署樹，job／服務唯讀＋可執行，
          與 `executor-toolchain` 同一類，因此模組**天生**落在受保護面內。
        """
        return f"{self.venv_root}/bin/python3 -m {PREFLIGHT_ADAPTER_MODULE}"

    def gate_command_env(self) -> dict[str, str]:
        """`PSC_GATE_CMD_<NAME>` 的建議值（#666）。

        由 :data:`GATE_COMMAND_DECLARATIONS` 機械導出，**不接受第二份手寫清單**。
        值刻意是相對名（`python3`）而不是絕對路徑，理由與 `preflight_command_value()`
        **相反**，因此值得寫清楚：

        - preflight 的 backend 是**部署產物**（隨 cortex 進同一個 venv），落點由部署
          決定，寫絕對路徑才驗得到「缺件在 doctor 就看得見」；
        - gate 命令跑的是**被驗那棵樹的測試**，用的是 gate 的 `PSC_GATE_PATH`
          （`<toolchain>/bin` ＋ :data:`JOB_PATH_SYSTEM_TAIL`）解析出來的系統
          interpreter。這條路徑上的 python 需要哪些套件，見
          :data:`SYSTEM_PYTHON_DISTRIBUTIONS`。

        **改成部署 venv 的絕對 interpreter 是一個 operator 可以做、但本產生器不會替
        它做的裁決**：那會讓系統層那兩個 python 套件的需求整個消失，同時也讓 gate 跑
        的 pytest 版本與 cortex 自己的部署版本綁在一起。兩邊各有取捨，因此這裡只出
        「目前這個部署決定」的值。
        """
        return {
            f"{GATE_COMMAND_ENV_PREFIX}{name.upper()}": " ".join(argv)
            for name, argv in GATE_COMMAND_DECLARATIONS.items()
        }

    @property
    def job_shim(self) -> str:
        """降權 job 模板 unit 的固定 `ExecStart=`（root-owned，內容由 permgen 產）。"""
        return f"{self.bin_root}/cortex-job-shim"

    @property
    def venv_root(self) -> str:
        return f"{self.deploy_root}/venv"

    @property
    def exec_start(self) -> str:
        return f"{self.venv_root}/bin/cortex service run"

    @property
    def monitor_exec_start(self) -> str:
        """monitor 的 `ExecStart=`。

        形態刻意與 Manager 同一種：**部署 venv 裡的 `cortex` console script ＋ 一個
        既有的 CLI verb**，而不是 `python -m paulsha_cortex.monitor`。理由見
        `build_monitor_unit()` 的 docstring 與 unit 內註解。
        """
        return f"{self.venv_root}/bin/cortex monitor"

    @property
    def env_file(self) -> str:
        return f"{self.deploy_root}/etc/{self.instance}-manager.env"

    # -- 帳號→HOME／cache（由帳號名機械導出，換 scheme 不會漂移）-------------
    def home_of(self, account: str) -> str:
        """該帳號的 HOME。HOME 本身 root-owned（見 `scaffold_directories`）。"""
        return f"{self.home_root}/{account}"

    def cache_of(self, account: str) -> str:
        """該帳號 HOME 底下唯一可寫的那一層（git／gh／uv 的 cache）。"""
        return f"{self.home_of(account)}/cache"

    def codex_hooks_dir_of(self, account: str) -> str:
        """該帳號的 `~/.codex`。root-owned（#698 起再加 sticky）——job 不得替換 hooks。

        **#698 之後這一層同時是 codex 的 `$CODEX_HOME` 整棵**（登記表資產
        `<前綴>-codex-state`），因此值由憑證表導出、不再是寫死的 `.codex`：
        `PathLayout.executor_credential_relpath` 換掉時它必須跟著換，否則 hooks 會落在
        一個 codex 根本不看的目錄裡——**一個不會報錯的 enforcement 缺口**。
        表上查無的帳號（二分的 `cortex-svc`）退回既有字面值。
        """
        try:
            credential = credential_for(
                self.credential_prefix_of(account), PRIMARY_CREDENTIAL_EXECUTOR
            )
        except UnregisteredExecutorCredentialError:
            return f"{self.home_of(account)}/.codex"
        return f"{self.home_of(account)}/{self.credential_relpath_of(credential)}"

    def credential_relpath_of(self, credential: ExecutorCredential) -> str:
        """該憑證**登記節點**在帳號 HOME 底下的相對落點（`None` 的那一列由本 layout 供給）。

        `relpath is None` 的那一列（＝ :data:`PRIMARY_CREDENTIAL_EXECUTOR`）的值來自
        `executor_credential_relpath` 這個部署決定。#698 起它要依形狀切一刀：

        - `IN_PLACE_FILE`：登記的節點就是**那個檔**（`.codex/auth.json`）；
        - `HOME_STICKY_TREE`：登記的節點是**放那個檔的那一層**（`.codex`）——樹與葉
          因此是同一個部署決定的 head 與 tail，不可能各改一半，而
          「換 primary executor 只改一個值」（#640）仍然逐字成立。
        """
        if credential.relpath is not None:
            return credential.relpath
        relpath = self.executor_credential_relpath
        if credential.shape is CredentialShape.IN_PLACE_FILE:
            return relpath
        # `_validate_credential_relpath` 保證至少兩段，因此這一刀必定切得出 head。
        return relpath.rsplit("/", 1)[0]

    def credential_token_relpath_of(self, credential: ExecutorCredential) -> str:
        """真正承載 token 的**葉檔**在帳號 HOME 底下的相對落點。

        `relpath is None` 的那一列直接回 `executor_credential_relpath` 全值——它本來
        就是「那個檔在哪裡」的部署決定；再從 `token_leaf` 拼一次會是第二份真相
        （operator 把它改成 `.claude/credentials.json` 時會拼出 `.claude/auth.json`）。
        """
        if credential.relpath is None:
            return self.executor_credential_relpath
        if credential.token_leaf:
            return f"{credential.relpath}/{credential.token_leaf}"
        return credential.relpath

    def enforcement_relpath_of(self, credential: ExecutorCredential) -> str:
        """該格 enforcement 檔（codex 的 `hooks.json`）在 HOME 底下的相對落點（#698）。"""
        if not credential.enforcement_leaf:
            raise ValueError(f"{credential.executor}：這一格沒有 enforcement 檔")
        return f"{self.credential_relpath_of(credential)}/{credential.enforcement_leaf}"

    def executor_credential_of(
        self, account: str, executor: str = PRIMARY_CREDENTIAL_EXECUTOR,
    ) -> str:
        """該帳號**該 executor** 的登入態落點（#640 裁決 (b)／#685 U-5 擴表）。

        **簽章多了 `executor`，預設值是 :data:`PRIMARY_CREDENTIAL_EXECUTOR`**——單參數
        的既有呼叫端（票 C 的 probe 快取指紋、runbook、測試）行為逐字不變，而需要表達
        「同一個帳號的第二／第三份憑證」的呼叫端多給一個名字即可。

        回傳值的**形狀跟著 (account, executor) 這一格走**（:data:`CREDENTIALED_ACCOUNTS`）：

        - `HOME_STICKY_TREE`（{builder, reviewer-planner}×codex，#698）：回傳的是那棵
          **root-owned ＋ sticky 的真目錄**（`~/.codex`），token 葉檔在它底下
          （`credential_token_path_of()`），root-owned 的 `hooks.json` 也在它底下。
        - `HOME_REDIRECT_TREE`（reviewer-planner×{agy, claude}）：回傳的是那條
          **root-owned symlink**（`~/.gemini`／`~/.claude`），真正的 token 葉檔在它
          底下（`credential.token_leaf`）。
        - `IN_PLACE_FILE`：回傳的是**那個檔**。#698 之後憑證表上沒有這個形狀的格子
          （兩個 codex 都已改走 sticky 樹），保留是因為它仍是
          :data:`IN_PLACE_CONTENT_WRITE_ASSETS` 的語意來源。

        **帳號不在表上時**（`cortex-gate` 不跑模型；二分方案的 `cortex-svc` 沒有登記表
        憑證資產）退回 #640 的單一部署決定 `executor_credential_relpath`——那正是本函式
        在 #685 之前對**任何**帳號的行為，因此「這個帳號有沒有憑證骨架」這類既有查詢
        逐字不變。**但只對 primary executor 退回**：問一個未登記帳號的第二／第三份憑證
        沒有正確答案，猜一條路徑的後果是「指紋盯著一個不存在的檔、unit 放行一條錯的
        路徑」，兩者都會靜默通過 ⇒ raise :class:`UnregisteredExecutorCredentialError`。
        """
        try:
            prefix = self.credential_prefix_of(account)
        except UnregisteredExecutorCredentialError:
            if executor != PRIMARY_CREDENTIAL_EXECUTOR:
                raise
            return f"{self.home_of(account)}/{self.executor_credential_relpath}"
        credential = credential_for(prefix, executor)
        return f"{self.home_of(account)}/{self.credential_relpath_of(credential)}"

    def executor_credential_dir_of(
        self, account: str, executor: str = PRIMARY_CREDENTIAL_EXECUTOR,
    ) -> str:
        """憑證登記節點的**父目錄**。

        - `IN_PLACE_FILE`：憑證檔的父目錄，**必須 root-owned**——裁決 (b) 的性質全部
          落在這一層。
        - `HOME_REDIRECT_TREE`／`HOME_STICKY_TREE`：回傳的是登記節點的父目錄（＝HOME）。
          對 sticky 樹而言，「必須 root-owned」的那一層是**樹自己**，不是這裡回的 HOME
          ——HOME 仍然 root-owned，但那擋的是「換掉整棵樹」，不是「換掉樹裡的 hooks」。
        """
        return _parent_dir(self.executor_credential_of(account, executor))

    def credential_token_path_of(
        self, account: str, executor: str = PRIMARY_CREDENTIAL_EXECUTOR,
    ) -> str:
        """真正承載 token 的那個**葉檔**（`HOME_REDIRECT_TREE` 時是樹裡的一格）。

        與 :meth:`executor_credential_of` 的差別只有一個消費者，但那個差別是必要的：
        登記表要的是「要保護／要放行的那個節點」（symlink 或單檔），而**指紋**要的是
        「refresh 之後會變的那個檔」。拿 symlink 或目錄去 `stat` 只看得到目錄本身的
        mtime——token 就地覆寫時它不變，於是「憑證換了」偵測不到（票 C 的
        `test_cache_invalidated_by_credential_fingerprint` 釘的正是這條）。

        `token_leaf` 為空（`IN_PLACE_FILE`）時兩者相同。
        """
        try:
            credential = credential_for(self.credential_prefix_of(account), executor)
        except UnregisteredExecutorCredentialError:
            return self.executor_credential_of(account, executor)
        return f"{self.home_of(account)}/{self.credential_token_relpath_of(credential)}"

    def credential_target_of(self, account: str, credential: ExecutorCredential) -> str:
        """`HOME_REDIRECT_TREE` 的 symlink 目標絕對路徑（恆在該帳號的 `cache` 底下）。"""
        if credential.cache_target is None:
            raise ValueError(f"{credential.executor}：不是 HOME_REDIRECT_TREE，沒有目標")
        return f"{self.cache_of(account)}/{credential.cache_target}"

    def credential_accounts(self) -> Mapping[str, str]:
        """登記表資產 id 的**帳號前綴** → OS 帳號名。**只有這裡把前綴接回帳號欄位。**

        前綴是資產 id 的一部分（因此與帳號改名無關），OS 帳號名則是部署決定欄位。
        兩者的對應寫在這一支函式裡，`asset_paths()`／`scaffold_directories()` 都從這裡取。
        """
        return MappingProxyType({
            "builder": self.builder_account,
            "reviewer-planner": self.reviewer_planner_account,
        })

    def credential_prefix_of(self, account: str) -> str:
        """OS 帳號名 → 資產 id 前綴（反查）。查不到即 fail-closed。"""
        for prefix, name in self.credential_accounts().items():
            if name == account:
                return prefix
        raise UnregisteredExecutorCredentialError(account)

    def credential_placements(self) -> tuple[tuple[str, str, str, ExecutorCredential], ...]:
        """per-(account, executor) 表的**絕對路徑展開**：
        `(asset_id, account, 絕對路徑, 該格的憑證形狀)`。

        `asset_paths()`／`scaffold_directories()`／`symlink_targets()`／unit 註解全部由
        本函式導出——加一格憑證只改 :data:`CREDENTIALED_ACCOUNTS`，產生器一行都不必動。
        """
        accounts = self.credential_accounts()
        rows: list[tuple[str, str, str, ExecutorCredential]] = []
        for prefix, credential in credential_rows():
            account = accounts[prefix]
            rows.append((
                credential_asset_id(prefix, credential),
                account,
                f"{self.home_of(account)}/{self.credential_relpath_of(credential)}",
                credential,
            ))
        return tuple(rows)

    def enforcement_placements(self) -> tuple[tuple[str, str, str, ExecutorCredential], ...]:
        """#698 的 enforcement 檔展開：`(asset_id, account, 絕對路徑, 該格的憑證)`。

        與 :meth:`credential_placements` 同源、同形，因此「兩個帳號各一份 hooks」是
        **一條規則長出來的兩列**，不是兩處手寫的路徑。`asset_paths()` 由它導出。
        """
        accounts = self.credential_accounts()
        rows: list[tuple[str, str, str, ExecutorCredential]] = []
        for prefix, credential in enforcement_rows():
            account = accounts[prefix]
            rows.append((
                enforcement_asset_id(prefix, credential),
                account,
                f"{self.home_of(account)}/{self.enforcement_relpath_of(credential)}",
                credential,
            ))
        return tuple(rows)

    def symlink_targets(self) -> dict[str, str]:
        """symlink 型資產 → 它應該指向的絕對路徑（:data:`SYMLINK_ASSETS` 的值那一半）。"""
        return {
            asset_id: self.credential_target_of(account, credential)
            for asset_id, account, _path, credential in self.credential_placements()
            if credential.shape is CredentialShape.HOME_REDIRECT_TREE
        }

    def gitconfig_of(self, account: str) -> str:
        """該帳號的 `~/.gitconfig`。root-owned——job 不得替換自己的 git 設定（#623）。

        git 只在 `$HOME/.gitconfig`（global scope，屬 git 的 *protected configuration*）
        認 `safe.directory`，因此位置由帳號的 HOME 決定，與 `~/.codex` 同一個模式。
        """
        return f"{self.home_of(account)}/.gitconfig"

    def declared_accounts(self) -> tuple[str, ...]:
        """本 layout 以**部署決定欄位**寫死的帳號名（#666）。

        `asset_paths()` 刻意不吃 scheme，因此幾個掛在帳號 HOME 下的資產只能由這三個
        欄位導出路徑，而它們取的是**定案的三分／四分**。二分把 Manager／reviewer／
        planner 併進 `cortex-svc`，於是這三條路徑在二分部署裡不存在——「哪些資產在
        本方案不適用」因此要拿這張清單去對 `UidScheme.declared_accounts()`，見
        :func:`inapplicable_home_anchored_assets`。
        """
        return (self.builder_account, self.reviewer_planner_account, self.manager_account)

    def gh_config_dir_of(self, account: str) -> str:
        """該帳號的 `gh` 設定目錄（`~/.config/gh`）。**必須 root-owned**（#666）。

        **為什麼是這條路徑而不是別條**：`gh` 依序看 `$GH_CONFIG_DIR` →
        `$XDG_CONFIG_HOME/gh` → `$HOME/.config/gh`。產生出來的 unit 設 `HOME=` 與
        `XDG_CACHE_HOME=`，**刻意不設 `XDG_CONFIG_HOME=`**，因此解析結果就是這一條。
        哪天有人在 unit 或 EnvironmentFile 補上 `XDG_CONFIG_HOME`，憑證的實際落點會
        跟著搬走而登記表不會知道——那是**無聲**的漂移（`gh auth status` 會變成
        「未登入」，兩個 github provider 一起 `degraded`，而檔案還好端端躺在原處）。

        與 `codex_hooks_dir_of()`／`executor_credential_dir_of()` 同一個模式：目錄
        root-owned，底下才放得下「檔案 job/服務-owned、目錄 root-owned」那組性質。
        """
        return f"{self.home_of(account)}/.config/gh"

    def gh_credential_of(self, account: str) -> str:
        """該帳號的 `gh` **憑證**檔（`~/.config/gh/hosts.yml`）。由該帳號擁有、0600。

        `hosts.yml` 是 `gh` 唯一寫回 token 的檔（`gh auth login`／`refresh` 就地覆寫
        它）。因此它與 #640 裁決 (b) 的 `auth.json` 同一族：**檔案**由使用它的帳號
        擁有（token 過期要 refresh 得回來），**放它的目錄維持 root-owned**。
        """
        return f"{self.gh_config_dir_of(account)}/hosts.yml"

    def gh_settings_of(self, account: str) -> str:
        """該帳號的 `gh` **非憑證**設定（`~/.config/gh/config.yml`）。root-owned、0644。

        它與 `hosts.yml` **刻意不同 owner**，而且不是疏漏：`config.yml` 放的是
        editor／pager／aliases／`prompt` 這類偏好，其中 **`aliases` 可以宣告 `!`
        開頭的 shell alias**——讓服務帳號改得了它，等於把一條「Manager 自己就能把
        任意命令掛進每一次 `gh` 呼叫」的執行面交還給它，與 `.gitconfig` 維持
        root-owned 的理由（`core.fsmonitor`／`alias.*`）**逐字相同**。它不承載 token、
        也不需要被寫回，因此 root-owned、對服務帳號唯讀就夠。
        """
        return f"{self.gh_config_dir_of(account)}/config.yml"

    @property
    def builder_home(self) -> str:
        return self.home_of(self.builder_account)

    @property
    def builder_cache(self) -> str:
        return self.cache_of(self.builder_account)

    def with_job_segment(self, segment: str) -> "PathLayout":
        """換掉 per-job segment（system unit 模板用 `%i`）。

        以 `dataclasses.replace` 而非逐欄位重建：欄位表只有一份，新增欄位不會在這裡
        被靜默重設回預設值（那會讓 job layout 與 setup layout 指向不同的樹）。
        """
        return replace(self, job_segment=segment)

    def with_source_repo_slugs(self, slugs: "tuple[str, ...] | list[str]") -> "PathLayout":
        """回傳帶上來源 repo 宣告的新 layout（本體 frozen，不就地改）。

        slug 會被逐字嵌進 root 產生的 `.gitconfig`，因此在**產生階段**就驗形狀，
        不等到 operator 落檔（比照帳號名的 `_validate_account_name`）。
        """
        checked = tuple(str(s) for s in slugs)
        for slug in checked:
            _validate_repo_slug(slug)
        return replace(self, source_repo_slugs=checked)

    # -- 資產→路徑 ----------------------------------------------------------
    def asset_paths(self) -> dict[str, str]:
        """登記表每一項 asset_id → 目標主機絕對路徑（涵蓋全部、無多餘）。"""
        a = self.agents_root
        c = self.coordinator_root
        ctl = self.control_root
        mon = self.monitor_state_root
        reg = self.skill_registry_root
        wt = self.worktree_root
        job = f"{wt}/{self.job_segment}"
        paths: dict[str, str] = {
            "runtime-agents-tree": a,
            "control-root-tree": ctl,
            "coordinator-root-tree": c,
            "dispatch-specs-tree": self.specs_root,
            "runtime-run-tree": self.run_root,
            "project-config-tree": self.project_config_root,
            "coverage-shadow-telemetry": f"{c}/coverage-shadow",
            "monitor-state-tree": mon,
            "monitor-work-items-snapshot": f"{mon}/work-items.snapshot.json",
            "monitor-github-sync-cursor": f"{mon}/github-issue-sync.json",
            "monitor-event-spool": f"{mon}/event-spool",
            "skill-registry-tree": reg,
            "skill-usage-ledger": f"{reg}/skill_usage.jsonl",
            "skill-park-state": f"{reg}/skill_park.json",
            "skill-park-proposals": f"{reg}/skill_park_proposals",
            "control-request-queue": f"{ctl}/requests",
            "control-done-queue": f"{ctl}/done",
            "control-status": f"{ctl}/status.json",
            "control-daemon-lock": f"{ctl}/manager.lock",
            # #623：per-job clone 的來源樹（容器；每個受治理 repo 一格 <此根>/<slug>）。
            "repo-source-tree": self.repo_source_root,
            "builder-gitconfig": self.gitconfig_of(self.builder_account),
            "reviewer-planner-gitconfig": self.gitconfig_of(self.reviewer_planner_account),
            "manager-gitconfig": self.gitconfig_of(self.manager_account),
            # #640：四個模型 executor 的部署樹落點。
            "executor-toolchain": self.toolchain_root,
            # #685（#672 票 D／U-4／U-5）：per-(account, executor) 憑證表的展開。
            # 逐項寫死換成由 `CREDENTIALED_ACCOUNTS` × `EXECUTOR_CREDENTIALS` 導出——
            # 「機制是 per-account 的、登記表只登記其中一份」那個結構性缺口（#666 的
            # deferred 第 1／3 項）就是逐項寫死造成的。
            **{
                asset_id: path
                for asset_id, _account, path, _credential in self.credential_placements()
            },
            # #666：Manager 的 gh 登入態。**兩個檔刻意不同 owner**——`hosts.yml` 承載
            # token（要 refresh ⇒ 服務帳號擁有），`config.yml` 是偏好設定（可宣告
            # `!` shell alias ⇒ 必須 root-owned 唯讀）。
            "manager-gh-credential": self.gh_credential_of(self.manager_account),
            "manager-gh-config": self.gh_settings_of(self.manager_account),
            "repo-worktree": job,
            "dispatch-worktree-pool": wt,
            "jobs-registry": f"{c}/jobs.json",
            "review-verdict": f"{job}/.psc-review-verdict.json",
            # Phase 2a 受控通道（PR #599）：<coordinator>/review-verdicts/<reviewer_job_id>/
            "review-verdict-spool": self.review_verdict_spool_root,
            # #623／#634 成果回收的 bundle spool：<coordinator>/commit-spool/<job-id>/
            "commit-spool": self.commit_spool_root,
            # #686（#672 票 E）：降權 planning job 的唯讀 scratch pool。它**不**進任何
            # job 模板 unit 的 RWP（writer 面只有 MANAGER）——U-2「scratch 唯讀」因此
            # 是機械導出的。三個 principal 的 **log** spool 由本 dict 之後那一段依
            # `registry.JOB_LOG_SPOOLS` 導出（#708）。
            "planning-scratch-pool": self.planning_scratch_root,
            # Phase 2b 方案 B（0816 第三輪 A+B）：模板 unit 的 per-job 執行規格。
            # #657：容器 ＋ per-principal 子 spool。子項由登記表的同一張
            # `DOWNGRADED_JOB_PRINCIPALS` 導出，不逐項寫死——asset_paths() 漏一項的
            # 症狀是 `plan_to_commands()` 對該資產用 placeholder 路徑，等於漏授。
            "job-spec-spool": self.job_spec_spool_root,
            **{
                registry.job_spec_spool_asset_id(principal): self.job_spec_spool_for(
                    principal
                )
                for principal in registry.DOWNGRADED_JOB_PRINCIPALS
            },
            # #629 gate 執行身分：拋棄式工作區 pool ＋ ledger 單向 spool。
            # 工作區 pool 登記的是**容器**（不帶 per-job segment）：它只有一個 writer
            # ＝`cortex-gate`，因此容器本身就 owner-only 0700，per-job 那一格由 gate
            # 自己建、自己重建——不像 `dispatch-worktree-pool` 要 Manager 逐案 chown 給
            # 三個不同帳號。少一個 runtime-managed 的環節就少一個會漏掉的環節。
            "gate-worktree-pool": self.gate_worktree_root,
            "gate-ledger-spool": self.gate_ledger_spool_root,
            "verification-evidence": f"{c}/evidence/verification",
            "maintainer-attestation": f"{c}/evidence/maintainer-review",
            "completion-record": f"{c}/evidence/completion",
            "full-suite-evidence": f"{c}/evidence/full-suite",
            "workflow-inputs": f"{c}/evidence/workflow-inputs",
            "workflow-evidence": f"{c}/evidence/workflow",
            "gate-ledger": self.dispatch_log_root,
            "delivery-journal": f"{c}/delivery-journal.json",
            "provider-backoff": f"{c}/provider-rate-limit-backoff.json",
            # #684：planning probe 的跨 tick 快取。Manager-owned 葉檔，**不**列入
            # 任何 job 模板 unit 的 RWP（登記表的 writers/readers 只有 MANAGER，
            # 因此 `required_write_targets()` 對 job 帳號機械地不會產出這一條）。
            "planning-probe-cache": f"{c}/planning-probe-cache.json",
            "workflow-report-journal": f"{c}/workflow-report-transactions",
            "digest-outbox": f"{c}/digest/outbox",
            "engineering-outcome-outbox": f"{c}/engineering-outcomes",
            "model-identity-overlay": f"{self.project_config_root}/model-identities.yaml",
            "combo-card-override": f"{a}/config/combos",
            "handoff-manifest": f"{job}/.psc-handoff.json",
            "runtime-bootstrap-env": self.env_file,
            "work-items-yaml": f"{job}/.cortex/work-items.yaml",
            # **#698：hooks 不再只有 builder 那一份，也不再是寫死的路徑。**
            #
            # #685 之後這裡的註解寫的是「只有 builder 這一份，因為 reviewer／planner 的
            # `~/.codex` 是導進 `cache` 的 job-owned 樹、hooks 放進去擋不住替換」。0818
            # 的 R9 T3.9 把那句話的**後果**量了出來：該帳號真的植入了 `hooks.json`，而
            # codex hooks 會執行命令 ⇒ 跨 job 持久化（#698）。
            #
            # 方案 A 讓那個限制消失：兩個帳號的 `$CODEX_HOME` 都是 root-owned ＋ sticky
            # 的真目錄，因此**兩份 hooks 都登記得起來**，而且是由
            # `enforcement_placements()`（＝憑證表那條規則）長出來的兩列，不是兩處手寫。
            **{
                asset_id: path
                for asset_id, _account, path, _cred in self.enforcement_placements()
            },
        }
        # #708：三個降權 principal 的 job log spool。**掛在既有通道底下這件事在這裡
        # 是結構事實，不是又一個手寫前綴**——前綴直接取自本 dict 上那條通道資產的值，
        # 因此通道路徑改一次，三條 log 路徑自動跟著改；而「通道」是不是真的是那個
        # principal 今天就寫得進去的落點，由
        # `_assert_job_log_spools_hang_off_existing_channels()` 在 import 當下強制。
        for spool in registry.JOB_LOG_SPOOLS:
            paths[spool.asset_id] = f"{paths[spool.channel_asset_id]}/{spool.dirname}"
        return paths

    # -- 非資產骨架目錄 -----------------------------------------------------
    def scaffold_directories(self, scheme: UidScheme) -> tuple[tuple[str, str, str, int], ...]:
        """`(path, owner, group, mode)`：不屬任何登記表資產、但必須先存在的父目錄。

        原則：**凡是保護資產的父目錄，一律 root 擁有**——父目錄可寫者能 unlink／
        rename 子物件，因此把 root-owned 檔放進 svc-owned 目錄等於沒保護。
        """
        svc = scheme.durable_state_owner
        root = scheme.deploy_account
        g = scheme.group_of
        # 每個 scheme 解析得到的帳號都要有 HOME／cache——**由 scheme 導出，不是列舉**。
        # 二分下這是 {cortex-svc, cortex-builder}（與改動前逐字相同）；三分下自動多出
        # `cortex-reviewer-planner`，不必在這裡補一行（補一行正是上一版漏掉它的原因）。
        service_accounts = [svc] + sorted(scheme.headless_accounts() - {svc})
        # 跑模型的 job 帳號還要一個 root-owned 的 ~/.codex（hooks 不得被 job 替換）。
        # **來源是 `model_job_accounts()` 而不是 `headless_accounts()`**（#629）：
        # `cortex-gate` 也是不受信任的執行帳號、也要 HOME／cache，但它不跑模型 CLI，
        # 給它 `~/.codex` 與一份 executor 憑證只會多兩個沒有消費者的 root-owned 目錄。
        job_accounts = sorted(scheme.model_job_accounts() - {svc})
        account_dirs: list[tuple[str, str, str, int]] = []
        for account in service_accounts:
            account_dirs.append((self.home_of(account), root, g(root), 0o755))
            account_dirs.append((self.cache_of(account), account, g(account), 0o700))
        # #685：憑證那一族的骨架**由 per-(account, executor) 表導出**，不再逐項寫死。
        # 三種形狀各自要先存在的東西不同，而「哪個帳號是哪種形狀」是表上的一格：
        #
        #   - `IN_PLACE_FILE`：憑證檔的**父目錄**必須 root-owned（#640 裁決 (b) 的性質
        #     全部落在這一層——job 因此改得了內容，卻建不了新檔、刪不掉）。
        #   - `HOME_REDIRECT_TREE`：symlink 自己由權限計畫的 `ln -sfn` 落位；骨架要先
        #     建的是它的**目標**——那一格落在該帳號的 `cache` 底下，由**該帳號**擁有
        #     0700。這一層不建的話，`~/.gemini` 會是一條懸空 symlink，而建目錄的
        #     syscall 對懸空 symlink 回 `EEXIST`（**不是**「建出目標」）——executor 於是
        #     死在一個與權限完全無關的錯誤上。
        #   - `HOME_STICKY_TREE`（#698）：**這一格不進骨架**。它自己就是登記表資產
        #     （目錄型），`plan_to_commands()` 會出 `install -d` ＋ `chown` ＋ `chmod
        #     1755` ＋ `setfacl`。在這裡再建一次會是第二份真相，而且是**會漂移的那種**
        #     ——骨架這一層不知道 sticky 位與 ACL，去重（`_dedupe_scaffold`）只留第一筆，
        #     於是先出現的骨架版本會把權限計畫的版本蓋掉，sticky 靜默消失。
        #
        # 三者的保護都不是寫在這裡的字面量：symlink 換不掉是因為 HOME 那一層 root-owned
        # （上面那一行），sticky 樹換不掉也是同一個理由，而目標不新增可寫面是因為它在
        # `cache` 裡（`_minimize()` 會吃掉）。
        for _asset_id, account, path, credential in self.credential_placements():
            if account not in job_accounts:
                # 本方案沒有這個帳號（二分把 reviewer／planner 併進 `cortex-svc`），
                # 或它不跑模型（`cortex-gate`）——都不該生出這一層。
                continue
            if credential.shape is CredentialShape.IN_PLACE_FILE:
                account_dirs.append((_parent_dir(path), root, g(root), 0o755))
            elif credential.shape is CredentialShape.HOME_REDIRECT_TREE:
                account_dirs.append((
                    self.credential_target_of(account, credential),
                    account, g(account), 0o700,
                ))
        # 表上查無的 job 帳號（二分的 `cortex-svc`）維持 #640 的既有形態不變：它沒有
        # 登記表憑證資產，但骨架那兩層 root-owned 目錄照舊——本票不改二分部署。
        #
        # **#698 之後表上有登記的帳號在這裡什麼都不做**：它們的 `~/.codex` 就是
        # `HOME_STICKY_TREE` 那個登記表資產本身（見上一段的第三點）。
        for account in job_accounts:
            try:
                self.credential_prefix_of(account)
            except UnregisteredExecutorCredentialError:
                account_dirs.append(
                    (f"{self.home_of(account)}/.codex", root, g(root), 0o755)
                )
                account_dirs.append((
                    _parent_dir(f"{self.home_of(account)}/{self.executor_credential_relpath}"),
                    root, g(root), 0o755,
                ))
        # #666：**durable state owner 才需要** `~/.config/gh` 那兩層。`gh` 依序看
        # `$GH_CONFIG_DIR` → `$XDG_CONFIG_HOME/gh` → `$HOME/.config/gh`，而產生出來的
        # unit 只設 `HOME=` 與 `XDG_CACHE_HOME=`（**刻意不設 `XDG_CONFIG_HOME=`**），
        # 因此落點就是這一條。兩層都 root-owned：底下要放得下「`hosts.yml` 服務帳號
        # 擁有、`config.yml` root-owned」這組不同 owner 的檔，而那組性質全部建立在
        # 「目錄沒有 `w` 位給服務帳號」上。
        #
        # **job 帳號刻意沒有這一層**：job unit 帶 `Environment=GH_TOKEN=`／
        # `GITHUB_TOKEN=` 把 token 清空，GitHub 寫入一律由 Manager 代理（D1 outbox）。
        # 為 job 建出 gh 設定目錄等於替一條被明確關掉的通道預留位置。
        account_dirs.append((f"{self.home_of(svc)}/.config", root, g(root), 0o755))
        account_dirs.append((self.gh_config_dir_of(svc), root, g(root), 0o755))
        return _dedupe_scaffold((
            # 部署樹（enforcement plane）：全 root，對 svc／builder 唯讀。
            (self.deploy_root, root, g(root), 0o755),
            (f"{self.deploy_root}/etc", root, g(root), 0o755),
            # 降權 shim 的家：root-owned、對 svc／job 唯讀。模板 unit 的 ExecStart=
            # 指向這裡，因此持 spawn 授權的帳號也改不了 job 實際執行的第一支程式。
            (self.bin_root, root, g(root), 0o755),
            (self.venv_root, root, g(root), 0o755),
            # durable state 樹的 root-owned 骨架（svc 不得 relink 這幾層）。
            (f"{self.agents_root}/config", root, g(root), 0o755),
            (f"{self.agents_root}/run", root, g(root), 0o755),
            (f"{self.agents_root}/runtime", root, g(root), 0o755),
            # svc 自己建得出來、但先建好可讓權限一次到位的中間層。
            (f"{self.coordinator_root}/evidence", svc, g(svc), 0o700),
            (f"{self.coordinator_root}/digest", svc, g(svc), 0o700),
            # job spec spool 不在此列：它已是登記表資產（`job-spec-spool`），權限由
            # `plan_to_commands()` 依登記表機械產出（owner-only ＋ job 帳號唯讀 ACL），
            # 在骨架再寫一次會變成第二份真相。
            # 服務／job 帳號 HOME：root 擁有（job 不得替換自己的 ~/.codex），只開
            # cache 子目錄。清單由 scheme 導出，見上方 `account_dirs`。
            *account_dirs,
        ))

    # -- 額外可寫路徑（非登記表資產，須附理由）------------------------------
    def manager_extra_write_paths(self, account: str) -> tuple[ExtraWritePath, ...]:
        # 註：job spec spool 曾經是這裡的一條 extra（`<agents_root>/jobs/<id>/run.sh`）。
        # 0816 第三輪 A+B 把它升格為登記表資產 `job-spec-spool`，因此改由
        # `required_write_targets()` 機械導出——例外通道少一條，等式多涵蓋一項。
        return (
            ExtraWritePath(
                self.cache_of(account),
                f"服務帳號 {account} 的 HOME 快取（git/gh/uv）；HOME 本身 root-owned，只開這一層。",
            ),
        )

    def monitor_extra_write_paths(self, account: str) -> tuple[ExtraWritePath, ...]:
        """monitor unit 的額外可寫路徑。

        三分 UID 方案表寫的是「`cortex-manager`：Manager ＋ **monitor**」——同一個
        帳號、同一個 HOME，因此這裡**刻意複用** `manager_extra_write_paths()` 而不
        另開一條例外：monitor 的 `git`／`gh`／`uv` 快取就是 Manager 的那一個
        `XDG_CACHE_HOME`，寫成兩條會讓「例外通道」看起來比實際多一條。
        """
        return self.manager_extra_write_paths(account)

    def job_extra_write_paths(self, account: str) -> tuple[ExtraWritePath, ...]:
        # 帳號由呼叫端（`build_job_unit` 的 principal）給：M2 要為 reviewer/planner
        # 開第二個模板 unit 時，這裡不必改一行——換 principal 即換帳號。
        return (
            ExtraWritePath(
                self.cache_of(account),
                f"job 帳號 {account} 的 HOME 快取（git/gh/uv）；HOME 與 ~/.codex 兩層**目錄**"
                "皆 root-owned 不可替換（#698 起 ~/.codex 另帶 sticky，裡面的 root-owned "
                "hooks.json 因此 job 也動不了；樹本身的可寫面由登記表資產導出，不走本例外）。",
            ),
        )


DEFAULT_LAYOUT = PathLayout()


def asset_paths(layout: PathLayout = DEFAULT_LAYOUT) -> dict[str, str]:
    """模組層便利函式（CLI 與 runbook 引用）。"""
    return layout.asset_paths()




# ---------------------------------------------------------------------------
# ReadWritePaths 的機械導出
# ---------------------------------------------------------------------------

def _parent_dir(path: str) -> str:
    head = path.rsplit("/", 1)[0]
    return head or "/"


def _is_within(child: str, parent: str) -> bool:
    """`child` 是否落在 `parent` 之內（含相等）——純字串前綴判定，無 IO。"""
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def _minimize(paths: set[str]) -> tuple[str, ...]:
    """去掉被其他條目涵蓋的子路徑，回傳排序後的最小覆蓋集合。"""
    kept = [
        p for p in paths
        if not any(other != p and _is_within(p, other) for other in paths)
    ]
    return tuple(sorted(set(kept)))


def _assert_job_log_spools_hang_off_existing_channels() -> None:
    """#708 那條規則的**另一半**，在 import 當下強制。

    registry 那一半管「三個 principal 一格都不能少」；本函式管「掛的是不是**既有**
    通道」——那才是「可寫面逐字不變、零部署動作」這句話的來源。逐條檢查：

    1. `channel_asset_id` 真的是一個已登記的資產（打錯字 ⇒ `asset_paths()` 會 KeyError，
       但那個 traceback 指的是產生器內部，不是「你少宣告了一條通道」）；
    2. 該通道資產的 writer 面**在帳號層**本來就含這一列的 writer。判準刻意是**帳號**
       而不是 principal：`review-verdict-spool` 的 writer 宣告是 `REVIEWER`，而 planning
       這條通道的 writer 是 `PLANNER`——三分／四分下兩者是同一個 OS 帳號，產出的 ACL
       逐字相同，而「不新增可寫面」講的正是那個帳號的可寫面。少了這一條，「掛在既有
       通道底下」會退化成「掛在一條這個帳號其實寫不進去的路徑底下」——那不是省一條
       RWP，是**新開一條**，而且是沒有人宣告過的那種；
    3. log spool 的路徑**嚴格落在**通道路徑之內（且不等於它）。這是 `_minimize()` 會
       把它吃掉的**充要條件**：吃不掉就代表模板 unit 的 `ReadWritePaths=` 多了一行，
       「逐字不變、零部署動作」當場失效；
    4. writer 與 principal 在 :data:`DEFAULT_SCHEME` 下解到**同一個帳號**。reviewer 那
       一列的 writer 是 `PLANNER`（登記表講的是「誰在用這條通道」），兩者在三分／四分
       下併帳；哪天不併帳了，這一條會當場翻紅而不是產出一組對不上帳號的 ACL。

    為什麼是 import 當下而不是一條測試：理由與 registry 那一半、與 #698 的
    :func:`_assert_shape_follows_enforcement_rule` 逐字相同——要讓「只修一格」在
    **結構上做不到**，檢查就必須在展開點上，而不是在一個可以被 `-k` 跳過的測試裡。
    """

    index = {a.asset_id: a for a in registry.ASSET_REGISTRY}
    paths = DEFAULT_LAYOUT.asset_paths()
    for spool in registry.JOB_LOG_SPOOLS:
        channel = index.get(spool.channel_asset_id)
        if channel is None:
            raise ValueError(
                f"{spool.asset_id}：channel_asset_id {spool.channel_asset_id!r} 不在"
                "登記表上——job log 只能掛在**既有**的輸出通道底下（#708）。"
            )
        writer_account = DEFAULT_SCHEME.resolve(spool.writer)
        channel_accounts = {
            acct
            for acct in (DEFAULT_SCHEME.resolve(w) for w in channel.writers)
            if acct is not None
        }
        if writer_account is None or writer_account not in channel_accounts:
            raise ValueError(
                f"{spool.asset_id}：{spool.writer.value}（帳號 {writer_account}）不在 "
                f"{spool.channel_asset_id} 的 writer 帳號面 {sorted(channel_accounts)} 上"
                "——那條通道對這個帳號本來就不開放，"
                "掛上去不是「省一條 ReadWritePaths」，是**新開一條沒有人宣告過的寫入面**"
                "（#686 design D3／U-3：新開 job→Manager 的寫入面未決、待 operator 裁決）。"
            )
        child, parent = paths[spool.asset_id], paths[spool.channel_asset_id]
        if child == parent or not _is_within(child, parent):
            raise ValueError(
                f"{spool.asset_id}：路徑 {child} 沒有嚴格落在通道 {parent} 之內，"
                "`_minimize()` 因此吃不掉它 ⇒ 模板 unit 的 `ReadWritePaths=` 會多一行，"
                "「可寫面逐字不變、零部署動作」不再成立（#708）。"
            )
        if DEFAULT_SCHEME.resolve(spool.writer) != DEFAULT_SCHEME.resolve(spool.principal):
            raise ValueError(
                f"{spool.asset_id}：writer {spool.writer.value} 與 principal "
                f"{spool.principal.value} 在 {DEFAULT_SCHEME.scheme_id} 下不是同一個帳號"
                "——ACL 會產在一個帳號上、模板 unit 卻以另一個帳號執行（#657 的失效模式）。"
            )


_assert_job_log_spools_hang_off_existing_channels()


def _assert_job_workspace_reach_matches_the_plan() -> None:
    """#710 那條規則的**另一半**，在 import 當下強制。

    registry 那一半管「三個 principal 一格都不能少、每一列自洽」；本函式管
    **宣告的機制與權限計畫是不是同一回事**——那才是「每個 job 都進得去自己的工作區」
    這句話的來源。三種形態各驗各的，逐條：

    ``POOL_OWNED_BY_JOB``（gate）
        pool 資產的 owner **必須就是**該帳號。owner 位是它唯一的可達性來源，
        owner 一旦漂走（例如有人把 pool 改成 Manager-owned「比照 dispatch pool」），
        gate 就再也建不出自己的副本，而症狀是 gate 全部失敗、不是產生器紅字。

    ``INHERITED_DEFAULT_ACL``（reviewer／planner）
        每一棵 pool 資產都**必須**帶該帳號的 **default** ACL，且 perms 涵蓋宣告值。
        Manager 在裡面建的每一格靠它繼承；少了它，「reviewer 進得去自己的 review
        worktree」就退化成一句沒有出處的宣稱。

    ``PER_JOB_NAMED_ACL``（builder）
        三條一起驗：

        1. per-job 資產真的帶該帳號的具名 **access** ACL（＝執行期那條 setfacl 有
           出處，不是 coordinator 裡的一個字面量）；
        2. per-job 資產的路徑**嚴格落在** pool 之內且帶 :data:`PER_JOB_SEGMENT`
           ——沒有 `<job-id>` 就代表它不是 per-job 的，那條 ACL 會套到整個 pool；
        3. ⚠️ **pool 根上不得有任何 job 帳號的 default ACL**。這是本票兩個硬性
           注意事項的第一個：pool 根的 default ACL 會讓**每個** job 帳號進得去
           **每個** job 的目錄，裁決 10-2 的 per-job 隔離當場歸零。把它放在 import
           當下而不是一條測試，是因為「順手往 pool 根加一條 default ACL 讓它過」
           正是這個缺陷最可能的**修法**——那個動作必須讓模組載不起來。
    """

    index = {a.asset_id: a for a in registry.ASSET_REGISTRY}
    paths = DEFAULT_LAYOUT.asset_paths()
    plan = generate_plan(DEFAULT_SCHEME)
    job_accounts = set(DEFAULT_SCHEME.headless_accounts())
    for reach in registry.JOB_WORKSPACE_REACH:
        account = DEFAULT_SCHEME.resolve(reach.persona)
        if account is None:  # pragma: no cover - DEFAULT_SCHEME 涵蓋三個降權角色
            raise ValueError(
                f"{reach.principal.value}：persona {reach.persona.value} 在 "
                f"{DEFAULT_SCHEME.scheme_id} 下沒有帳號（#710）。"
            )
        for pool_id in reach.pool_asset_ids:
            if pool_id not in index:
                raise ValueError(
                    f"{reach.principal.value}：pool 資產 {pool_id!r} 不在登記表上"
                    "——工作區只能落在**已登記**的樹底下（#710）。"
                )
            pool_entry = plan.by_id(pool_id)
            if reach.reach is registry.WorkspaceReach.POOL_OWNED_BY_JOB:
                if pool_entry.owner != account:
                    raise ValueError(
                        f"{reach.principal.value}：{pool_id} 的 owner 是 "
                        f"{pool_entry.owner}，不是 {account}——`POOL_OWNED_BY_JOB` 的"
                        "可達性只來自 owner 位，owner 一漂走 job 就建不出自己那一格"
                        "（#710）。"
                    )
            elif reach.reach is registry.WorkspaceReach.INHERITED_DEFAULT_ACL:
                inherited = [
                    acl
                    for acl in pool_entry.effective_default_acls()
                    if acl.account == account
                ]
                if not inherited:
                    raise ValueError(
                        f"{reach.principal.value}：{pool_id} 沒有 {account} 的 "
                        "**default** ACL——Manager 在裡面建的每一格靠它繼承，少了它 "
                        "job `chdir` 不進自己的工作區（#710 的原症狀形狀）。"
                    )
                missing = set(reach.access_perms) - set(
                    "".join(acl.perms for acl in inherited)
                )
                if missing:
                    raise ValueError(
                        f"{reach.principal.value}：{pool_id} 的 default ACL 缺 "
                        f"{sorted(missing)}（宣告需要 {reach.access_perms!r}）——#710。"
                    )
            else:  # PER_JOB_NAMED_ACL
                leaked = [
                    acl
                    for acl in pool_entry.effective_default_acls()
                    if acl.account in job_accounts
                ]
                if leaked:
                    raise ValueError(
                        f"⛔ {pool_id}（{reach.principal.value} 的 pool 根）上出現 job "
                        f"帳號的 default ACL {[(a.account, a.perms) for a in leaked]}"
                        "——那會讓**每個** job 帳號進得去**每個** job 的目錄，per-job "
                        "隔離當場歸零（裁決 10-2）。可達性必須下在 per-job 那一格"
                        f"（{reach.per_job_asset_id}），不是 pool 根（#710）。"
                    )
        if reach.reach is not registry.WorkspaceReach.PER_JOB_NAMED_ACL:
            continue
        leaf_id = reach.per_job_asset_id or ""
        if leaf_id not in index:
            raise ValueError(
                f"{reach.principal.value}：per-job 資產 {leaf_id!r} 不在登記表上（#710）。"
            )
        leaf_entry = plan.by_id(leaf_id)
        granted = [
            acl
            for acl in leaf_entry.acls
            if not acl.default and acl.account == account and acl.perms == reach.access_perms
        ]
        if not granted:
            raise ValueError(
                f"{reach.principal.value}：{leaf_id} 沒有 {account} 的具名 access ACL "
                f"{reach.access_perms!r}——執行期那條 `setfacl` 因此沒有出處（#710）。"
            )
        leaf_path = paths[leaf_id]
        if PER_JOB_SEGMENT not in leaf_path:
            raise ValueError(
                f"{reach.principal.value}：{leaf_id} 的路徑 {leaf_path} 不帶 "
                f"{PER_JOB_SEGMENT!r}——那代表它不是 per-job 的一格，那條遞迴 ACL 會套到"
                "整個 pool（#710）。"
            )
        for pool_id in reach.pool_asset_ids:
            pool_path = paths[pool_id]
            if leaf_path == pool_path or not _is_within(leaf_path, pool_path):
                raise ValueError(
                    f"{reach.principal.value}：{leaf_id}（{leaf_path}）沒有嚴格落在 "
                    f"{pool_id}（{pool_path}）之內——#710。"
                )


_assert_job_workspace_reach_matches_the_plan()


#: `_assert_git_workspace_trust_matches_the_gitconfig()` 的探針 layout。
#:
#: `DEFAULT_LAYOUT.source_repo_slugs` 刻意留空（來源 repo 是部署決定，猜不到就只能
#: 猜錯，見 :class:`UnresolvedSourceRepoError`），因此拿它去產 `.gitconfig` 只會
#: raise，斷言會變成一句空話。改以一個**代表性 slug** 產一份真的檔來驗形狀：要擋的
#: 漂移（有人往 `[safe]` 塞萬用字元或塞 pool 根）與 slug 叫什麼無關。
_GIT_TRUST_PROBE_LAYOUT = replace(DEFAULT_LAYOUT, source_repo_slugs=("psc-git-trust-probe",))


def _assert_git_workspace_trust_matches_the_gitconfig() -> None:
    """#712 那條規則的**另一半**，在 import 當下強制。

    registry 那一半管「三個 principal 一格都不能少，且與 `JOB_WORKSPACE_REACH` 對
    『誰建那一格』的宣告一致」；本函式管**那份靜態 `.gitconfig` 真的沒有涵蓋工作區**
    ——也就是把「per-job env 是必要的」這句話從論證升級成斷言。

    這正是本票的原始缺陷形狀：`builder-gitconfig` 的 note 宣稱它涵蓋 per-job clone，
    產生器實際只出來源樹那兩條，而**兩者之間沒有任何東西在比對**。於是那則宣稱活了
    兩個月，直到 #710 把 ACL 補上、builder job 第一次真的跑起來才炸出來（#696 的同型，
    本票是第三個實例）。

    逐條驗：

    1. **`[safe]` 一律不得出現萬用字元。** 產生器的註解逐字寫著「字面 `*` 等於對這個
       帳號整個關掉該保護——那是 opt-out，不是授權」，但那條紀律在本票之前只是註解。
       這是本票最可能的**壞修法**（「加個 `*` 讓它過」），必須讓模組載不起來。
    2. **`[safe]` 不得出現任何工作區 pool／per-job 資產的路徑。** 第二個壞修法是
       「把 pool 根加進靜態檔」——它一來對 per-job 那一格沒有用（git 只認逐字相等的
       路徑，實測：只給來源樹的兩條時 linked worktree 仍被拒），二來若真的生效就是
       整個 pool 的放行，per-job 的逐格語意當場歸零。
    3. **`PER_JOB_ENV` 的工作區路徑必須真的帶動態段**——沒有動態段就代表它其實是個
       部署期常數，那時「靜態檔裝不下」這個前提不成立，整條規則要重新論證。
    4. **`OWNED_BY_JOB` 的 pool 根 owner 必須就是該帳號**：那是它「不需要放行」的
       **唯一**來源。#710 已從可達性的角度驗過同一件事，這裡從 git 信任的角度再驗
       一次是刻意的——兩條性質在同一個事實上，錯誤訊息卻要各自指向自己的那一票。
    """

    plan = generate_plan(DEFAULT_SCHEME)
    paths_by_asset = _GIT_TRUST_PROBE_LAYOUT.asset_paths()
    workspace_paths: set[str] = set()
    for reach in registry.JOB_WORKSPACE_REACH:
        for pool_id in reach.pool_asset_ids:
            workspace_paths.add(paths_by_asset[pool_id])
        if reach.per_job_asset_id:
            workspace_paths.add(paths_by_asset[reach.per_job_asset_id])

    for trust in registry.JOB_GIT_WORKSPACE_TRUST:
        account = DEFAULT_SCHEME.resolve(trust.persona)
        if account is None:  # pragma: no cover - DEFAULT_SCHEME 涵蓋三個降權角色
            raise ValueError(
                f"{trust.principal.value}：persona {trust.persona.value} 在 "
                f"{DEFAULT_SCHEME.scheme_id} 下沒有帳號（#712）。"
            )
        reach = registry.job_workspace_reach_for(trust.principal)
        if trust.trust is registry.GitWorkspaceTrust.OWNED_BY_JOB:
            for pool_id in reach.pool_asset_ids:
                pool_entry = plan.by_id(pool_id)
                if pool_entry.owner != account:
                    raise ValueError(
                        f"{trust.principal.value}：git 信任宣告 "
                        f"`{trust.trust.value}`，但 {pool_id} 的 owner 是 "
                        f"{pool_entry.owner} 而不是 {account}——「job 自己建、自己擁有"
                        "」就不成立了，git 會開始看到跨 owner，而**執行期零動作**的"
                        "形態接不住那件事（#712）。"
                    )
            continue
        # --- PER_JOB_ENV ---
        for pool_id in reach.pool_asset_ids:
            pool_path = paths_by_asset[pool_id]
            leaf_path = (
                paths_by_asset[reach.per_job_asset_id] if reach.per_job_asset_id else None
            )
            dynamic = leaf_path if leaf_path is not None else pool_path
            if leaf_path is None and PER_JOB_SEGMENT not in dynamic:
                # reviewer 那一列沒有 per-job 資產（那一格不是獨立資產，靠繼承），
                # 工作區是 pool 底下 Manager 逐次開出來的**一格**——因此「動態」的
                # 證據是「pool 根本身不是工作區」，而它由下一段的 disjoint 檢查涵蓋。
                continue
            if leaf_path is not None and PER_JOB_SEGMENT not in leaf_path:
                raise ValueError(
                    f"{trust.principal.value}：per-job 資產 "
                    f"{reach.per_job_asset_id}（{leaf_path}）不帶 {PER_JOB_SEGMENT!r}"
                    "——那代表工作區其實是個部署期常數，「靜態 .gitconfig 裝不下它」"
                    "這個前提不成立，#712 那條規則要重新論證。"
                )
        asset_id = ACCOUNT_GITCONFIG_ASSETS.get(trust.persona) or ACCOUNT_GITCONFIG_ASSETS.get(
            trust.principal
        )
        if asset_id is None:
            # 這個 persona 沒有靜態 `.gitconfig`（今天沒有這種降權角色；gate 的
            # `gate-gitconfig` 仍是 deferred，而 gate 走的是 OWNED_BY_JOB 那一支）。
            # 沒有靜態檔就沒有「靜態檔宣稱涵蓋了什麼」的問題，本段無事可驗。
            continue
        gitconfig = build_account_gitconfig(
            DEFAULT_SCHEME, _GIT_TRUST_PROBE_LAYOUT, trust.persona
        )
        for entry in gitconfig.safe_directories:
            if "*" in entry:
                raise ValueError(
                    f"⛔ {asset_id} 的 `[safe] directory = {entry}` 帶萬用字元——git 只"
                    "認逐字相等的路徑或字面 `*`（實測 git 2.43：`<repos>/*` 仍被拒），"
                    "而字面 `*` 等於對 "
                    f"{account} 整個關掉 dubious-ownership 保護：**那是 opt-out，不是"
                    "授權**（#623／#712）。per-job 那一格走 spec 的 env，不走這份檔。"
                )
            if entry in workspace_paths:
                raise ValueError(
                    f"⛔ {asset_id} 的 `[safe] directory = {entry}` 是一棵**工作區**"
                    "（pool 根或 per-job 資產）——靜態檔放行工作區有兩個問題：對逐 job "
                    "的那一格沒有效果（git 只認逐字相等的路徑，per-job 路徑帶動態段），"
                    "而真的生效的那部分是**整個 pool** 的放行，逐 job 語意當場歸零"
                    "（#712）。這份檔只放來源樹。"
                )


# 呼叫刻意排在 `build_account_gitconfig()` 之後（本檔尾端的 gitconfig 那一節）——
# 本函式要拿一份**真的產出來的** `.gitconfig` 去驗，不是驗一組常數。


def principal_needs_write(
    asset: TrustRootAsset,
    principals: frozenset[Principal],
) -> bool:
    """該組 persona 是否需要對此資產可寫——**只看登記表，沒有第二條規則**。

    兩種「需要寫」，都直接讀自登記表欄位：

    1. **直接 writer**：persona 出現在 `asset.writers`。
    2. **單向 spool 的 trusted consumer**：`IngressKind.INTERPROCESS` 的 reader。
       消費＝讀完即 unlink（`monitor-event-spool` 的 note 就是這麼寫的：「別的
       行程寫入、monitor 消費即消失」），而 unlink 需要對**容器目錄**的寫入權。
       少了這條，monitor unit 會拿到一個「讀得到卻刪不掉」的 spool——事件會被
       重複消費，spool 只增不減，正是 #622 描述的那個沒有消費端的狀態。

    這條規則是**帳號過濾之外的第二層**：三分方案把 Manager 與 monitor 映到同一個
    `cortex-manager` 帳號，單看帳號兩者的可寫面完全相同；要讓 monitor unit 真的
    比 Manager 窄，必須回到 persona 這一層來切。
    """
    if any(p in principals for p in asset.writers):
        return True
    if asset.ingress_kind is IngressKind.INTERPROCESS:
        return any(p in principals for p in asset.readers)
    return False


#: 「只改內容、不增刪換」的葉檔資產（#640 裁決 (b)）。
#:
#: 一般規則把檔案資產折算成**父目錄**——因為要「建立／取代」一個檔，必須對父目錄可寫。
#: 這一族刻意**不**具備建立／取代的能力：目錄維持 root-owned（`scaffold_directories`），
#: job 只是把自己擁有的那一個檔就地改寫（`O_TRUNC` 覆寫，例如 token refresh）。因此
#: `ReadWritePaths` 只掛在**檔案本身**，父目錄連 mount 層都不開放可寫——「檔案
#: job-owned、目錄 root-owned」這條性質因此在**檔案系統**與 **systemd mount** 兩層
#: 同時成立，而不是只靠其中一層。
#:
#: 代價（裁決刻意接受）：以「暫存檔 ＋ rename 原子替換」形式 refresh 的 CLI 會失敗，
#: 因為那需要在同目錄建檔。診斷方式見 Phase 2b runbook 第 4e 步。
#:
#: **#685：憑證那一半改由 per-(account, executor) 表導出，不是手寫清單。** 往
#: :data:`CREDENTIALED_ACCOUNTS` 加一格 `IN_PLACE_FILE`，這裡自動跟著長；加一格
#: `HOME_REDIRECT_TREE` 則**刻意不進**——那一族的 RWP 由 `cache` 涵蓋（見
#: :class:`CredentialShape`），掛在 symlink 上只會讓 systemd 去 bind-mount 一條連結。
#:
#: **#698：憑證那一半目前導出為空集，而那是一個有結論的狀態、不是遺漏。** 兩個 job
#: 帳號的 codex 都已改走 `HOME_STICKY_TREE`（整棵目錄進 RWP），因此本集合現在只剩
#: `manager-gh-credential` 一項。`IN_PLACE_FILE` 這個形狀**刻意保留**：它仍是本集合
#: 的語意來源，而且 `manager-gh-credential` 逐條同構——差別只在 Manager 那一份不在
#: 憑證表上（它不是 executor 登入態）。
#:
#: **sticky 樹換到什麼、付出什麼**（誠實記錄，不要在別處被讀成「一樣安全」）：
#: `IN_PLACE_FILE` 讓 `hooks.json` 同時被 **DAC** 與 **systemd mount** 兩層擋住；
#: sticky 樹只剩 DAC 那一層（整棵樹必須進 RWP，codex 才起得來）。換到的是 codex
#: **真的跑得起來**，以及 reviewer／planner 那一格「hooks 根本擋不住」的整個消失。
#: DAC 那一層擋的是三個動詞（unlink／rename／改內容），三個都由 OS 強制，符合 spec
#: §R2「不得依賴 mode 0400 作為唯一手段」——sticky 不是 mode 位的自我約束，它約束的
#: 是**非 owner**。
IN_PLACE_CONTENT_WRITE_ASSETS: frozenset[str] = frozenset({
    *credential_asset_ids(CredentialShape.IN_PLACE_FILE),
    # #666：Manager 的 gh token。與上一條同形，但**洩漏面不同級**，這點不得混談：
    # #640 的憑證是給 **job 帳號**的，job unit 另有 `Environment=GH_TOKEN=` 把 token
    # 清空、成果一律走 spool 由 Manager 代理推送；這一份是給 **Manager** 的，而
    # Manager 是 durable state owner——它的 token 推得動 PR、關得掉 issue、改得了
    # label。折算成父目錄會讓 Manager unit 的 mount 層開放整個 `~/.config/gh`，
    # 連 root-owned 的 `config.yml`（可宣告 `!` shell alias）一起可寫，等於把「改不了
    # 自己的 gh 設定」這條性質從兩層拆成零層。
    "manager-gh-credential",
})


#: 登記表上仍列該 persona 為 writer、但降權 job unit **刻意不放行**的資產（#615 M2）。
#:
#: 唯一一項是 `review-verdict`——reviewer worktree 內的
#: `.psc-review-verdict.json`。它正是 spec 背景 §3 認定的**最短攻擊路徑**（同 UID 下
#: builder 可代寫 reviewer 的 verdict），Phase 2a 已把權威通道整個換成
#: `review-verdict-spool`：`manager._review_verdict_source()` 對任何帶
#: `review_verdict_channel == "spool"` 標記的 job **只**認 spool 落點，而 Phase 2b
#: 部署派出的每一個 reviewer job 都帶那個標記。因此在模板 unit 上放行這條路徑，
#: 買到的是**零**（沒有消費者），付出的卻有兩項：
#:
#: 1. **語意**：等於在 OS 邊界上把一條已除役的 verdict 寫入面重新打開；
#: 2. **可用性**：那條路徑是 `<worktree pool>/%i`，而 reviewer 的工作樹**不在** pool
#:    底下（它是 Manager provision 的 review worktree）。systemd 對不存在的
#:    `ReadWritePaths=` 目標會讓 unit 直接起不來——放行反而讓每個 reviewer job 起不來。
#:
#: 這條是**除役宣告**，不是例外通道：登記表仍完整記錄 `review-verdict` 的存在與它的
#: writer（過渡期 legacy fallback 還要讀它），只是「Phase 2b 的 job unit 不再為它開
#: 寫入面」。加一項到這裡必須同時附上「誰是它今天的消費者」的答案。
RETIRED_JOB_WRITE_ASSETS: frozenset[str] = frozenset({
    "review-verdict",
})


def home_anchored_account(layout: PathLayout, path: str) -> str | None:
    """`path` 掛在**哪一個 layout 帳號欄位**的 HOME 底下（否則回 `None`）。純字串。

    比對來源刻意只有 :meth:`PathLayout.declared_accounts`（三個部署決定欄位），
    **不是**「`home_root` 底下的第一段」——durable state 樹本身就住在
    `<home_root>/cortex`，用字串切段會把整棵樹誤判成某個叫 `cortex` 的帳號的 HOME。
    """
    for account in layout.declared_accounts():
        home = layout.home_of(account)
        if path == home or path.startswith(home.rstrip("/") + "/"):
            return account
    return None


def inapplicable_home_anchored_assets(
    plan: PermissionPlan,
    layout: PathLayout,
) -> tuple[tuple[str, str], ...]:
    """`(asset_id, path)`：掛在**本方案不存在的帳號** HOME 下的資產（#666）。

    登記表資產是 1:1 綁到一條絕對路徑的，而幾個 HOME-anchored 資產的路徑由
    :class:`PathLayout` 的**部署決定欄位**（`manager_account`／
    `reviewer_planner_account`）導出，那幾個欄位取的是**定案的三分／四分**；二分
    方案把 Manager／reviewer／planner 全併進 `cortex-svc`，於是同一條路徑在二分
    部署裡**根本不存在**。這在登記表既有的 note 裡已經寫成「二分下該資產不適用」，
    而權限那一半也早就以 `[ ! -e ] || …` 守衛表達了它。

    **本函式補的是 `ReadWritePaths` 那一半**：systemd 對不存在的 `ReadWritePaths=`
    目標會讓 unit **直接起不來**，因此「不適用」不能只在權限面成立。#642 當時的處置
    是「乾脆不登記第二份憑證」（`builder-codex-state` 的 note 有整段論證）；
    #666 要登記 Manager 的 gh 憑證時同一個陷阱又出現一次，因此把「不適用」做成一條
    **可列舉的機械規則**，而不是每次都用「不要登記」繞過去。

    刻意做成可列舉而不是靜默過濾：靜默扣掉一條 RWP 與「漏授一條 RWP」在輸出上長得
    一樣，而後者的症狀是 job 跑到一半 EROFS。要看它扣了什麼，呼叫這個函式。
    """
    scheme = plan.scheme or SCHEMES.get(plan.scheme_id)
    if scheme is None:
        return ()
    declared = scheme.declared_accounts()
    paths = layout.asset_paths()
    found: list[tuple[str, str]] = []
    for entry in plan.entries:
        path = paths.get(entry.asset_id)
        if path is None:
            continue
        anchor = home_anchored_account(layout, path)
        if anchor is not None and anchor not in declared:
            found.append((entry.asset_id, path))
    return tuple(sorted(found))


def required_write_targets(
    plan: PermissionPlan,
    layout: PathLayout,
    account: str,
    principals: frozenset[Principal] | None = None,
    retired: frozenset[str] | None = None,
) -> dict[str, str]:
    """`asset_id → 該帳號必須可寫的目標路徑`（檔案取其父目錄）。

    ProtectSystem=strict 下整個檔案系統唯讀；要**建立／取代**一個檔，必須對其
    父目錄可寫，故檔案資產一律折算成父目錄。這就是「ReadWritePaths 由登記表機械
    導出」的全部規則——唯一的例外是 :data:`IN_PLACE_CONTENT_WRITE_ASSETS`
    （只改內容、不增刪換的葉檔），它們掛在檔案本身而**不**折算成父目錄。

    `principals` 給定時再套一層 persona 過濾（見 `principal_needs_write`）：同一
    帳號上跑多個 persona 時（三分的 `cortex-manager`＝Manager＋monitor），每個
    unit 只拿自己那一份，而不是帳號的全集。`None`＝不過濾（帳號全集，Manager
    unit 與 job 模板 unit 的既有行為）。

    `retired`（#615）給定時再扣掉一組**已除役**的 asset_id（見
    :data:`RETIRED_JOB_WRITE_ASSETS`）。`None`＝不扣（Manager／monitor unit 的既有
    行為）——除役宣告只適用於降權 job 的模板 unit。
    """
    targets: dict[str, str] = {}
    paths = layout.asset_paths()
    index = {a.asset_id: a for a in (plan.assets or registry.ASSET_REGISTRY)}
    # #666：本方案不存在的帳號 HOME 底下的資產一律不進 RWP——systemd 對不存在的
    # `ReadWritePaths=` 目標會讓 unit 起不來（見 `inapplicable_home_anchored_assets`）。
    inapplicable = {aid for aid, _path in inapplicable_home_anchored_assets(plan, layout)}
    for entry in plan.entries:
        if account not in plan.all_writable_accounts(entry):
            continue
        if entry.asset_id in inapplicable:
            continue
        if retired is not None and entry.asset_id in retired:
            continue
        if principals is not None:
            asset = index.get(entry.asset_id)
            if asset is None or not principal_needs_write(asset, principals):
                continue
        path = paths[entry.asset_id]
        if entry.is_directory or entry.asset_id in IN_PLACE_CONTENT_WRITE_ASSETS:
            targets[entry.asset_id] = path
        else:
            targets[entry.asset_id] = _parent_dir(path)
    return targets


def read_write_paths(
    plan: PermissionPlan,
    layout: PathLayout,
    account: str,
    extras: tuple[ExtraWritePath, ...] = (),
    principals: frozenset[Principal] | None = None,
    retired: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """該帳號 unit 的 `ReadWritePaths=` 最小覆蓋集合（登記表導出 − 除役 ∪ 明示 extras）。"""
    wanted = set(required_write_targets(plan, layout, account, principals, retired).values())
    wanted |= {e.path for e in extras}
    return _minimize(wanted)


def enforcement_read_only_paths(
    layout: PathLayout,
    account: str,
    read_write: tuple[str, ...],
) -> tuple[str, ...]:
    """該帳號 unit 的 `ReadOnlyPaths=`：落在自己 RWP 之內的 enforcement 檔（#698）。

    **為什麼需要這一條，而不是「sticky 就夠了」**：sticky 樹整棵必須進
    `ReadWritePaths=`（codex 才起得來），而 `hooks.json` 就住在那棵樹裡——於是 mount 層
    對它是可寫的，只剩 DAC（sticky ＋ 檔案 mode）擋著。#640 的 `IN_PLACE_FILE` 形態
    在這一點上是**兩層**（`IN_PLACE_CONTENT_WRITE_ASSETS` 的整段說明就是這件事），
    本形狀若不補這一條就是**淨退一層**。

    systemd 依路徑長度排序套用這些 bind mount，因此巢狀在 `ReadWritePaths=` 之內的
    `ReadOnlyPaths=` 會後套、覆蓋掉外層的可寫性。**沒有 `-` 前綴是刻意的**：目標不存在
    時 unit 直接起不來，而那正是要的行為——`hooks.json` 缺席時 sticky 擋不住任何東西
    （job 建得出新檔），一個「能植入 hooks 的 job」不該起得來。這與同一份 unit 對
    `ReadWritePaths` 既有的 fail-closed 立場逐字一致。

    只回傳**落在該帳號 RWP 之內**的那些：不在 RWP 內的 enforcement 檔本來就寫不到
    （mount 層已唯讀），再掛一條只會多一個「目標不存在就起不來」的失敗面。
    """
    home = layout.home_of(account)
    wanted = {
        path
        for _asset_id, acct, path, _cred in layout.enforcement_placements()
        if acct == account and any(_is_within(path, rwp) for rwp in read_write)
    }
    # 防呆：路徑必須真的掛在該帳號 HOME 底下（換 scheme／改 layout 時不會靜默跑掉）。
    return tuple(sorted(p for p in wanted if _is_within(p, home)))


def read_write_path_owners(
    plan: PermissionPlan,
    layout: PathLayout,
    account: str,
    extras: tuple[ExtraWritePath, ...] = (),
    principals: frozenset[Principal] | None = None,
    retired: frozenset[str] | None = None,
) -> dict[str, tuple[str, ...]]:
    """每條 ReadWritePaths → 它涵蓋的 asset_id（或 `extra:<reason>`），供逐條註解。"""
    targets = required_write_targets(plan, layout, account, principals, retired)
    result: dict[str, tuple[str, ...]] = {}
    for rwp in read_write_paths(plan, layout, account, extras, principals, retired):
        covered = sorted(aid for aid, t in targets.items() if _is_within(t, rwp))
        covered += [f"extra:{e.reason}" for e in extras if _is_within(e.path, rwp)]
        result[rwp] = tuple(covered)
    return result


# ---------------------------------------------------------------------------
# 父目錄 traverse ACL 的機械導出（#620）
#
# POSIX 要求路徑上**每一層**都帶 `x`（search）位才走得到葉節點。葉節點的跨帳號 ACL
# 再精確，只要中間任何一層是 `0700 <別人>`，實際結果就是 `Permission denied`——而且
# 錯誤訊息指的是那個父目錄，與真正缺的那條授權**不在同一層**，極難診斷（Phase 2b
# 實機兩條正向路徑同時全斷即為此）。
#
# 因此 traverse 權**必須與葉節點 ACL 同源機械導出**，不能留給 runbook 手補：它是
# 「正向路徑成立」的必要條件，漏掉會讓整套降權部署看起來「裝好了但 job 全部失敗」。
# ---------------------------------------------------------------------------

#: 父層 traverse ACL 的 perms。**必須是 `--x` 而不是 `r-x`**：只給 search，不給
#: 列目錄。builder 因此走得到 `<monitor>/event-spool`，卻列不出 `coordinator/`
#: 底下還有哪些 Manager 資產——「能走到自己那格」與「看得見別人有哪些格」是兩件事，
#: 這裡只授前者。
TRAVERSE_PERMS = "--x"


@dataclass(frozen=True)
class DirectoryFacts:
    """某目錄在**目標狀態**下的 owner／group／mode／具名 ACL（推導 traverse 用）。

    來源有二，合起來就是本產生器對「路徑上每一層長什麼樣」的全部知識：
    登記表資產的 `PermissionEntry`（目錄型）與 `PathLayout.scaffold_directories()`。
    不在其中的目錄一律保守視為**不可 traverse**（fail-closed：寧可多產一條 `--x`，
    也不要漏掉一條而讓正向路徑靜默斷掉）。
    """

    path: str
    owner: str
    group: str
    mode: int
    #: account → ACL perms。只收 access ACL；default ACL 決定的是**子物件**的初值，
    #: 不影響「能不能走過這個目錄本身」。
    acl_perms: Mapping[str, str] = field(default_factory=dict)
    #: `asset:<asset_id>` 或 `scaffold`——供診斷時指出這層是誰定的。
    source: str = ""


def directory_facts(
    plan: PermissionPlan,
    layout: "PathLayout" = None,  # type: ignore[assignment]
    scheme: UidScheme | None = None,
    path_of: Mapping[str, str] | None = None,
) -> dict[str, DirectoryFacts]:
    """路徑→目標狀態（骨架目錄先鋪底，登記表資產覆蓋其上）。純函式、無 IO。"""
    layout = layout if layout is not None else DEFAULT_LAYOUT
    scheme = _scheme_for(plan, scheme)
    paths = dict(path_of) if path_of is not None else layout.asset_paths()
    facts: dict[str, DirectoryFacts] = {}
    for path, owner, group, mode in layout.scaffold_directories(scheme):
        facts[path] = DirectoryFacts(
            path=path, owner=owner, group=group, mode=mode, source="scaffold"
        )
    for entry in plan.entries:
        if not entry.is_directory:
            continue
        path = paths.get(entry.asset_id)
        if not path:
            continue
        facts[path] = DirectoryFacts(
            path=path,
            owner=entry.owner,
            group=entry.group,
            mode=entry.mode,
            acl_perms={a.account: a.perms for a in entry.acls if not a.default},
            source=f"asset:{entry.asset_id}",
        )
    return facts


def can_traverse(
    facts: DirectoryFacts | None,
    account: str,
    scheme: UidScheme,
) -> bool:
    """該帳號在目標狀態下能否 search 進這個目錄。

    依 POSIX ACL 的判定順序：owner 條目優先；具名 user 條目一旦存在就**取代**
    group／other 位（不是疊加——`r--` 的具名條目會擋掉 other 的 x）；都沒有才看
    group、最後看 other。未知目錄回 `False`（fail-closed）。
    """
    if facts is None:
        return False
    if facts.owner == account:
        return bool(facts.mode & 0o100)
    perms = facts.acl_perms.get(account)
    if perms is not None:
        # setfacl 的 `X` ＝「目錄才給 x」；本函式的對象都是目錄，故等同 x。
        return "x" in perms or "X" in perms
    if facts.group == scheme.group_of(account):
        return bool(facts.mode & 0o010)
    return bool(facts.mode & 0o001)


def managed_roots(facts: Mapping[str, DirectoryFacts]) -> tuple[str, ...]:
    """本 layout 管理的樹根（沒有其他已知目錄是其祖先者）。

    traverse 推導往上走到這裡為止。再往上（`/var/lib`、`/var`、`/`）是發行版標準的
    root-owned 0755，不歸本產生器管——對那幾層下 `setfacl` 是越權，也毫無必要。
    """
    return _minimize(set(facts))


def _ancestors_within(path: str, roots: tuple[str, ...]) -> tuple[str, ...]:
    """`path` 由內而外的祖先目錄，只取仍落在管理樹內者。"""
    chain: list[str] = []
    current = _parent_dir(path)
    while any(_is_within(current, r) for r in roots):
        chain.append(current)
        parent = _parent_dir(current)
        if parent == current:
            break
        current = parent
    return tuple(chain)


@dataclass(frozen=True)
class TraverseGrant:
    """單條父層 traverse ACL：`setfacl -m u:<account>:--x <path>`。"""

    path: str
    account: str
    #: 需要這條才走得到的葉資產（一個中間目錄常同時服務多個葉）。
    required_by: tuple[str, ...] = ()

    @property
    def acl(self) -> AclEntry:
        """對應的 ACL 條目。**永遠 access-only**（`default=False`）：default ACL 會讓
        該目錄底下**新建的每個物件**都繼承這條授權，等於把一條 traverse 洩漏成整棵
        子樹的授權——與「不可列目錄、不可讀他人資產」的目的正好相反。"""
        return AclEntry(self.account, TRAVERSE_PERMS)

    def render(self) -> str:
        return self.acl.render(self.path)

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "account": self.account,
            "perms": TRAVERSE_PERMS,
            "required_by": list(self.required_by),
        }


def derive_traverse_grants(
    plan: PermissionPlan,
    layout: "PathLayout" = None,  # type: ignore[assignment]
    scheme: UidScheme | None = None,
    path_of: Mapping[str, str] | None = None,
) -> tuple[TraverseGrant, ...]:
    """對每個授了跨帳號 ACL 的資產，沿路徑往上補齊該帳號的 traverse 權。

    規則只有一條，沒有第二條：**葉節點被授權的帳號，其路徑上每一層都必須可 search**。
    已經允許該帳號 traverse 的中間層（owner 相符、other 帶 x、或既有 ACL 已帶 x）
    一律跳過——不重複產生。回傳依 `(path, account)` 排序，確保輸出決定性。
    """
    layout = layout if layout is not None else DEFAULT_LAYOUT
    scheme = _scheme_for(plan, scheme)
    paths = dict(path_of) if path_of is not None else layout.asset_paths()
    facts = directory_facts(plan, layout, scheme, paths)
    roots = managed_roots(facts)

    needed: dict[tuple[str, str], set[str]] = {}
    for entry in plan.entries:
        path = paths.get(entry.asset_id)
        if not path:
            continue
        for acl in entry.acls:
            # default ACL 只決定子物件初值；owner 自己不需要 ACL。
            if acl.default or acl.account == entry.owner:
                continue
            for ancestor in _ancestors_within(path, roots):
                if can_traverse(facts.get(ancestor), acl.account, scheme):
                    continue
                needed.setdefault((ancestor, acl.account), set()).add(entry.asset_id)
    return tuple(
        TraverseGrant(path=p, account=a, required_by=tuple(sorted(ids)))
        for (p, a), ids in sorted(needed.items())
    )


def unreachable_hops(
    plan: PermissionPlan,
    layout: "PathLayout" = None,  # type: ignore[assignment]
    scheme: UidScheme | None = None,
    *,
    account: str,
    asset_id: str,
    path_of: Mapping[str, str] | None = None,
    grants: tuple[TraverseGrant, ...] | None = None,
) -> tuple[str, ...]:
    """套用導出的 traverse 授權後，該帳號**仍**走不過去的中間層（空 tuple＝整條通）。

    這就是 #620 的驗收條件本身：葉節點 ACL 正確 **≠** 路徑走得通。
    """
    layout = layout if layout is not None else DEFAULT_LAYOUT
    scheme = _scheme_for(plan, scheme)
    paths = dict(path_of) if path_of is not None else layout.asset_paths()
    facts = directory_facts(plan, layout, scheme, paths)
    if grants is None:
        grants = derive_traverse_grants(plan, layout, scheme, paths)
    granted: dict[str, set[str]] = {}
    for grant in grants:
        granted.setdefault(grant.path, set()).add(grant.account)

    blocked: list[str] = []
    for ancestor in _ancestors_within(paths[asset_id], managed_roots(facts)):
        if account in granted.get(ancestor, set()):
            continue
        if can_traverse(facts.get(ancestor), account, scheme):
            continue
        blocked.append(ancestor)
    return tuple(blocked)


def account_can_reach(
    plan: PermissionPlan,
    layout: "PathLayout" = None,  # type: ignore[assignment]
    scheme: UidScheme | None = None,
    *,
    account: str,
    asset_id: str,
    path_of: Mapping[str, str] | None = None,
    grants: tuple[TraverseGrant, ...] | None = None,
) -> bool:
    """該帳號套完產生器輸出後是否走得到這個資產（整條路徑鏈皆可 search）。"""
    return not unreachable_hops(
        plan, layout, scheme,
        account=account, asset_id=asset_id, path_of=path_of, grants=grants,
    )


def traverse_commands(grants: tuple[TraverseGrant, ...]) -> list[str]:
    """把 traverse 授權轉成命令序列（**只產生字串，絕不執行**）。

    這一節必須排在整份 script 的**最後**：`chmod` 在有 ACL 的物件上會把 group 位
    寫進 ACL **mask**，先 `setfacl` 再 `chmod 0700` 會讓所有具名條目的有效權限被
    mask 成空——順序反了不會報錯，只會靜默失效。
    """
    if not grants:
        return []
    lines = [
        "",
        "# ===== 父目錄 traverse ACL（由上方跨帳號 ACL 機械導出，#620）=====",
        "# POSIX 要求路徑上每一層都有 x（search）位；葉節點 ACL 再精確，中間只要有一層",
        "# 是 0700 <別人>，整條就走不通，而錯誤訊息還指在父目錄（與缺的授權不同層）。",
        f"# perms 固定為 {TRAVERSE_PERMS}：**只給 traverse、不給列目錄**——帳號走得到自己",
        "# 那格，卻列不出該目錄底下還有哪些別人的資產。",
        "# 一律只設 access ACL、**不設 default ACL**：default 會讓底下新建的每個物件都",
        "# 繼承這條授權，等於把一條 traverse 放大成整棵子樹的授權。",
        "# 本節排在最後是必要的：chmod 會重寫 ACL mask，先 setfacl 再 chmod 會讓具名",
        "# 條目被 mask 成空（靜默失效，不會報錯）。",
    ]
    for grant in grants:
        per_job = PER_JOB_SEGMENT in grant.path
        lines.append(f"#   走得到：{', '.join(grant.required_by)}")
        if per_job:
            lines.append("#   per-job：由降權啟動器在 spawn 時套用，setup 階段不執行。")
            lines.append(f"#   {grant.render()}")
        else:
            lines.append(grant.render())
    return lines


# ---------------------------------------------------------------------------
# systemd unit 產生（Manager／monitor system unit ＋ 降權 job 模板 unit）
#
# 三份 unit 共用同一份加固表（`_HARDENING`）與同一套 ReadWritePaths 導出
# （`read_write_path_owners`）；差別只在 `User=`／`ExecStart=`／persona 過濾。
# ---------------------------------------------------------------------------

#: 加固指令 →（值, 為何）。逐項附註解是 spec §R3 的可審查性要求。
_HARDENING: tuple[tuple[str, str, str], ...] = (
    ("NoNewPrivileges", "yes",
     "提權天花板：exec 後不得取得新特權，setuid 二進位／file capabilities 全部失效。"),
    ("CapabilityBoundingSet", "",
     "清空 capability 上界——服務永不具 root 能力（裁決：cortex 任何元件永不具 root）。"),
    ("AmbientCapabilities", "",
     "不夾帶任何 ambient capability；CAP_SETUID 路線已被裁決排除。"),
    ("ProtectSystem", "strict",
     "整個檔案系統唯讀，只有下方 ReadWritePaths 例外——/opt/cortex 部署樹因此唯讀。"),
    ("ProtectHome", "yes",
     "/home、/root、/run/user 一律不可見：state 已全數搬離 HOME，任何殘留的 HOME "
     "路徑必須立刻失敗，而不是靜默沿用舊樹。"),
    ("PrivateTmp", "yes",
     "私有 /tmp、/var/tmp：切斷經共用 tmp 的跨 persona 檔案交換與 symlink 攻擊。"),
    ("PrivateDevices", "yes",
     "只掛最小 /dev；封掉 raw device 與 /dev/mem 這類旁路。"),
    ("ProtectProc", "invisible",
     "看不到其他 UID 的 /proc/<pid>——直接封 R9 族 4 的 environ／mem 讀取。"),
    ("ProcSubset", "pid",
     "/proc 只保留 pid 子集，隱藏 /proc/kcore 等核心介面。"),
    ("ProtectControlGroups", "yes",
     "cgroup 樹唯讀：不可經 cgroup 改寫資源或逃逸 unit 界線。"),
    ("ProtectKernelModules", "yes", "禁止載入／卸載核心模組。"),
    ("ProtectKernelTunables", "yes", "/proc/sys、/sys 唯讀，禁止改核心參數。"),
    ("ProtectKernelLogs", "yes", "禁讀 kmsg，避免經核心日誌側錄他人資料。"),
    ("ProtectClock", "yes", "禁止改系統時鐘——時間是 evidence 排序不變式的輸入。"),
    ("ProtectHostname", "yes", "禁止改 hostname（稽核紀錄的主機標識）。"),
    ("RestrictSUIDSGID", "yes",
     "禁止建立 setuid/setgid 檔——關掉自製提權助手這條路。"),
    ("RestrictNamespaces", "yes",
     "禁止建立 namespace：user namespace 是 unprivileged 提權的常見起點。"),
    ("RestrictRealtime", "yes", "禁 realtime 排程，避免 DoS 宿主。"),
    ("RestrictAddressFamilies", "AF_UNIX AF_INET AF_INET6",
     "只留 unix socket 與 IP：封掉 AF_NETLINK／AF_PACKET 等旁路。"),
    ("LockPersonality", "yes", "鎖定執行域，禁止切換 personality 規避 seccomp。"),
    ("MemoryDenyWriteExecute", "yes",
     "禁 W+X 記憶體，封 JIT 型 shellcode。※ 若 Python C-extension（ctypes "
     "trampoline）或任何 JS runtime 啟動失敗，這是第一嫌疑：先單獨註解本行複測"
     "（#643 實測 V8 的 Runtime_CompileLazy 會直接崩）。"),
    ("SystemCallArchitectures", "native",
     "只允許原生 ABI，封掉經 32-bit compat 介面規避 seccomp。"),
    ("SystemCallFilter", "@system-service @sandbox",
     "seccomp 白名單：只留一般服務所需 syscall。※ #673 實機量到它會過濾掉 "
     "`pkey_alloc`（V8 啟動時會叫），但**不放寬**：處置在下一行的過濾語意，"
     "不在白名單。"
     "※ **`@sandbox` 是 #714 加的，而它與「放寬」方向相反**："
     "`@sandbox`＝`landlock_create_ruleset`／`landlock_add_rule`／"
     "`landlock_restrict_self`／`seccomp` 四支，能力上限是**讓呼叫者把自己關得更緊**"
     "——它們拿不到任何本來拿不到的資源。少了它，executor 自帶的內層沙箱裝不上"
     "（#714 實機：codex 的 legacy landlock 路徑在 `SeccompInstall … EPERM` 上 panic），"
     "於是 job 只剩 systemd 這一層。**加在全域而不是某份剖面**：這正是 "
     ":data:`PROFILE_LOCKED_KEYS` 那條理由要的形態（白名單的變動必須是一次全域可稽核"
     "的決定），因此 `SystemCallFilter` 至今仍是鎖定鍵、沒有任何剖面分岔它。"
     "需求由 :data:`TOOLCHAIN_PROGRAMS` 的 `inner_sandbox` 機械導出，"
     "import 時由 `_validate_inner_sandbox_support()` 強制——把 `@sandbox` 刪掉，"
     "這個模組**載不起來**。"),
    ("SystemCallErrorNumber", "EPERM",
     "被過濾的 syscall 回 EPERM 而非 SIGSYS——失敗可觀測，不是無聲當掉。"
     "※ **本行承重，刪掉會讓六份 job unit 上的 codex／copilot 同時靜默死**"
     "（#673 實機：systemd 預設的 SECCOMP_RET_KILL_PROCESS 會在 V8 叫 "
     "`pkey_alloc` 時當場殺掉 node，rc=1、stdout 與 stderr 皆空）。它**不放行"
     "任何 syscall**，被擋的照樣擋，因此不是放寬。要動它必須先讀 "
     "`SECCOMP_FATALITY_KEY` 那一段——import 時有強制檢查。"),
    ("RemoveIPC", "yes", "服務結束即清掉該 UID 的 IPC 物件，不留跨 job 殘留。"),
    ("KeyringMode", "private", "私有 kernel keyring：不共用、不繼承金鑰。"),
    ("UMask", "0077",
     "新建檔預設 0600／目錄 0700，與權限產生器的 owner-only 基準一致。"),
)


# ---------------------------------------------------------------------------
# per-executor 加固剖面（#643，operator 裁決＝方向 2）
#
# 問題：`MemoryDenyWriteExecute=yes` 與 JS runtime 天生互斥（V8 的 JIT 必須 W+X），
# 而預設 executor 正是 node 型的 `codex`。三個方向裡 operator 選了「per-executor 剖面」
# ——node 型 job 走一份**只在這一項**放寬的 unit，原生執行檔型維持嚴格。
#
# 這個設計成立的前提只有一條：**剖面不可由 job 自選**。若 job 能選到寬鬆那份，
# 它就退化成「全域移除 MDWE」。守法（三層，缺一即退化）：
#
#   1. 剖面由 **executor** 決定，而 executor 是 Manager 的 dispatch 決定
#      （`SubprocessLauncher(executor=...)`，job spec 產生之前就固定了）；
#   2. 對應表由 :data:`EXECUTOR_TOOLS` 的 `needs_node` **機械導出**，未知 executor
#      fail-closed（見 :func:`executor_hardening_profile`）——不得預設落到寬鬆那份；
#   3. 兩份 unit 都是 root-owned、`User=`／`ExecStart=` 寫死；呼叫端只能
#      `systemctl start <那兩個名字之一>@<id>.service`，選不了執行的第一支程式，
#      也塞不進任何屬性（polkit 規則的 unit pattern 只列舉這兩個字幹）。
#
# **兩份 unit 共用同一張 `_HARDENING` 表**，只以 `overrides` 分岔：日後往加固表加一
# 項時兩份自動同時拿到，不存在「改一份忘另一份」。
# ---------------------------------------------------------------------------

#: 加固表中**唯一**允許被剖面分岔的鍵。寫成常數而非散在各處：想放寬第二項就必須改
#: 這裡，而這裡同時是測試的比對基準——「順手多放寬一項」不會靜默通過。
PROFILE_DIVERGENCE_KEYS: frozenset[str] = frozenset({"MemoryDenyWriteExecute"})


class UnknownExecutorProfileError(ValueError):
    """未知 executor 的剖面查詢——**fail-closed**，絕不落到寬鬆那份。"""


@dataclass(frozen=True)
class HardeningProfile:
    """job 模板 unit 的加固剖面（`_HARDENING` ＋ 一組受限覆寫）。"""

    profile_id: str
    #: unit 字幹的後綴。strict＝空字串，因此既有的 `cortex-job@.service` 逐字不變。
    unit_suffix: str
    #: 對 `_HARDENING` 的覆寫。鍵必須同時屬於 `_HARDENING` 與 `PROFILE_DIVERGENCE_KEYS`
    #: （import 時由 `_validate_hardening_profiles()` 強制，打錯字不會靜默變成 no-op）。
    overrides: Mapping[str, str]
    #: 這份剖面為誰而設、為什麼。
    rationale: str
    #: 這份剖面**放棄**的防護。誠實標註：產物、spec 與 runbook 都引用它。
    accepted_loss: tuple[str, ...] = ()

    def effective(self) -> dict[str, str]:
        """本剖面實際生效的加固表（鍵集合與 `_HARDENING` 恆等）。"""

        table = {key: value for key, value, _why in _HARDENING}
        table.update(self.overrides)
        return table


#: 嚴格剖面（**預設**）：完整加固表，一項不減。原生執行檔型 executor
#: （`claude`／`agy`）在此剖面下實測全部可用，因此它們沒有理由降級。
STRICT_PROFILE = HardeningProfile(
    profile_id="strict",
    unit_suffix="",
    overrides={},
    rationale=(
        "原生 ELF executor（不依賴 JS runtime）的 job。完整加固面，含 "
        "MemoryDenyWriteExecute=yes。"
    ),
)

#: 放寬剖面：**只**放寬 `MemoryDenyWriteExecute`，其餘 26 項逐字不變。
JIT_PROFILE = HardeningProfile(
    profile_id="jit",
    unit_suffix="-jit",
    overrides={"MemoryDenyWriteExecute": "no"},
    rationale=(
        "node 型 executor（`EXECUTOR_TOOLS` 的 `needs_node`）的 job。V8 的 JIT 必須有 "
        "W+X 記憶體，MemoryDenyWriteExecute=yes 下 Runtime_CompileLazy 直接崩（#643 "
        "實機量測）——這一項與 JS runtime 天生互斥，不是設定錯誤。"
    ),
    accepted_loss=(
        "本剖面的 job **失去 MemoryDenyWriteExecute 這層防護**：取得任意程式碼執行的"
        "攻擊者可在本 job 內配置 W+X 記憶體，JIT 型 shellcode（不落地、不經 exec、"
        "因此也不觸發 NoNewPrivileges／SystemCallFilter 之外的任何一層）在此可行。",
        "換來的是保住 codex／copilot 兩個 provider——即 §R5／§R8 的 independence_domain "
        "仍有可選空間。這是**付了代價的取捨**，不是沒有代價。",
        "其餘 26 項加固（NoNewPrivileges／CapabilityBoundingSet 空／ProtectSystem=strict"
        "／SystemCallFilter 等）在本剖面下**逐項不變**：W+X 只讓攻擊者在自己這個 UID "
        "的位址空間內執行程式碼，跨 UID／跨檔案系統／提權的那幾層完全沒有鬆動。",
    ),
)

#: 全部剖面。polkit 的 unit pattern 與測試的比對基準都由這裡導出。
HARDENING_PROFILES: tuple[HardeningProfile, ...] = (STRICT_PROFILE, JIT_PROFILE)
HARDENING_PROFILES_BY_ID: Mapping[str, HardeningProfile] = MappingProxyType(
    {profile.profile_id: profile for profile in HARDENING_PROFILES}
)

#: 未指定時一律用**嚴格**那份。「預設就是最安全的那一個」與 `DEFAULT_SCHEME` 同一條
#: 原則：要放寬必須顯式打出來，打錯字不會靜默退回較寬鬆的剖面。
DEFAULT_HARDENING_PROFILE: HardeningProfile = STRICT_PROFILE


def _validate_hardening_profiles() -> None:
    """import 時的結構檢查：覆寫鍵必須真的存在、且在允許分岔的白名單內。"""

    known = {key for key, _value, _why in _HARDENING}
    missing = sorted(PROFILE_DIVERGENCE_KEYS - known)
    if missing:
        raise ValueError(f"PROFILE_DIVERGENCE_KEYS 指到 _HARDENING 沒有的鍵: {missing}")
    suffixes: set[str] = set()
    for profile in HARDENING_PROFILES:
        bad = sorted(set(profile.overrides) - known)
        if bad:
            raise ValueError(
                f"剖面 {profile.profile_id} 覆寫了 _HARDENING 沒有的鍵 {bad}"
                "——打錯字會變成一個看起來有效、實際毫無作用的覆寫。"
            )
        outside = sorted(set(profile.overrides) - PROFILE_DIVERGENCE_KEYS)
        if outside:
            raise ValueError(
                f"剖面 {profile.profile_id} 覆寫了 PROFILE_DIVERGENCE_KEYS 以外的鍵 "
                f"{outside}——擴大分岔面必須是顯式決定。"
            )
        if profile.unit_suffix and not re.fullmatch(r"-[a-z0-9]+", profile.unit_suffix):
            # 字幹會被拼進 polkit 的 regex（原字串，不 escape），因此形狀必須受限。
            raise ValueError(f"剖面 {profile.profile_id} 的 unit_suffix 形狀不合法")
        if profile.unit_suffix in suffixes:
            raise ValueError(f"剖面 unit_suffix 重複: {profile.unit_suffix!r}")
        suffixes.add(profile.unit_suffix)


_validate_hardening_profiles()


#: executor → 剖面 id。**由 `EXECUTOR_TOOLS` 機械導出，不是第二張清單**：改 executor
#: 的形態只改那張表一列，這裡跟著動。
EXECUTOR_HARDENING_PROFILE: Mapping[str, str] = MappingProxyType(
    {
        tool.name: (JIT_PROFILE if tool.needs_node else STRICT_PROFILE).profile_id
        for tool in EXECUTOR_TOOLS
    }
)


def executor_hardening_profile(executor: str) -> HardeningProfile:
    """executor 名 → 加固剖面。**未知 executor 一律拒絕，不回傳任何剖面。**

    fail-closed 的方向很重要：這裡不是「不確定就給嚴格的」，而是「不確定就拒絕」。
    給嚴格的表面上安全，實際會讓一個從未被盤點過的 node 型 CLI 在真實加固面下靜默
    起不來（症狀是空輸出，離原因很遠——#643 就是這樣被埋掉的）；而如果反過來預設
    給寬鬆的，整個 per-executor 設計當場退化成「全域移除 MDWE」。兩邊都不可接受，
    因此唯一正確的行為是要求它先被登記進 :data:`EXECUTOR_TOOLS`。
    """

    name = str(executor or "").strip()
    profile_id = EXECUTOR_HARDENING_PROFILE.get(name)
    if profile_id is None:
        raise UnknownExecutorProfileError(
            f"未知的 executor {executor!r}，無法決定加固剖面"
            f"（已登記：{sorted(EXECUTOR_HARDENING_PROFILE)}）。新增 executor 必須先進 "
            "permgen.EXECUTOR_TOOLS 並標明 needs_node——剖面不得靠猜，也不得預設"
            "落到放寬的那一份。"
        )
    return HARDENING_PROFILES_BY_ID[profile_id]


# ---------------------------------------------------------------------------
# seccomp 過濾語意（#673）——與加固剖面**正交**的第二個維度
#
# #643 的剖面只有一個軸：「這個 executor 要不要 W+X 記憶體」（`needs_node` ⇒
# `MemoryDenyWriteExecute`）。#673 在實機量到第二個軸，而它**不是**剖面問題：
#
#   `SystemCallFilter=@system-service` 會過濾掉 `pkey_alloc`（x86_64 syscall 330，
#   systemd 歸在 `@pkey`，而 `@pkey` 不在 `@system-service` 的 16 個子集合裡）。
#   V8 啟動時會叫它。在 systemd **預設**的過濾語意（`SECCOMP_RET_KILL_PROCESS`）下，
#   node 當場被 SIGSYS 殺掉——rc=1、stdout 與 stderr **皆空**；在
#   `SystemCallErrorNumber=EPERM` 下，V8 收到 `EPERM` 走 fallback，一切正常。
#   （kernel audit 直接證據：`type=1326 … comm="node" sig=31 syscall=330
#   code=0x80000000`；全程沒有任何一筆 `landlock_*`／`seccomp` 的 record。）
#
# 因此承重的不是「放不放行那支 syscall」，而是「被過濾時致不致命」。兩者在安全面
# 上差很多：放行 `pkey_alloc` 是**放寬過濾器**（多一支 syscall 可用）；
# `SystemCallErrorNumber=EPERM` **不放行任何 syscall**——被擋的照樣擋，只是回錯誤碼
# 而非殺行程。#673 的量測結論因此是**不放寬**：`SystemCallFilter=@system-service`
# 在八份 unit（六份 job 模板 ＋ manager ＋ monitor）上逐字不動。
#
# **為何不把這個維度塞進 `needs_node`**（那樣兩件事會繼續混在一起）：
#   1. **適用面比剖面大**。剖面只覆蓋 `EXECUTOR_TOOLS`；`openspec` 是
#      `SERVICE_TOOLS`，跑在 **Manager unit** 上——那一格沒有剖面，卻同樣撞
#      `pkey_alloc`（#673 實機 audit 確認）。綁在剖面上會整個漏掉 Manager 面。
#   2. **處置方向相反**。`needs_node` 導向「換一份放寬的剖面」；本維度導向「一個
#      所有剖面都**不得**分岔的鎖定鍵」。
#   3. **證據等級不同**。`filtered_syscalls` 每一列都有 audit record 背書，不是
#      「它是 node，所以大概會」。
#
# 落法沿用 permgen 既有做法：事實填在既有那張登記表（`ToolchainProgram.
# filtered_syscalls`）上一格，需求由它**機械導出**，import 時強制——不另立第二張
# 人工對照表，也不靠 review 記得。
# ---------------------------------------------------------------------------

#: 加固表中決定「被 `SystemCallFilter=` 擋掉的 syscall 致不致命」的那一個鍵。
SECCOMP_FATALITY_KEY: str = "SystemCallErrorNumber"

#: 剖面**永遠不得**分岔的鍵：seccomp 白名單本身，與它的過濾語意。
#:
#: 白名單在此是因為「放寬 syscall」必須是全域可稽核的一次決定，不能變成某份剖面
#: 偷偷多開一支；過濾語意在此是因為它是**全部** node 型程式的存活條件，per-profile
#: 分岔等於讓其中一份剖面靜默地殺掉 codex／copilot。
#:
#: 與 :data:`PROFILE_DIVERGENCE_KEYS` 恆為互斥（import 時強制）：想分岔其中任一項，
#: 必須先把它從這裡拿掉——而那是一個看得見的 diff。
PROFILE_LOCKED_KEYS: frozenset[str] = frozenset(
    {"SystemCallFilter", SECCOMP_FATALITY_KEY}
)


def seccomp_filter_is_fatal(table: Mapping[str, str]) -> bool:
    """該加固表下，被過濾的 syscall 會不會**殺掉行程**。

    systemd 的語意：`SystemCallErrorNumber=` 未設或設為空 ⇒ 預設動作是
    `SECCOMP_RET_KILL_PROCESS`（SIGSYS）；設成 errno 名或數字 ⇒ 該 syscall 回那個
    錯誤碼、行程續跑。
    """

    return not str(table.get(SECCOMP_FATALITY_KEY, "") or "").strip()


@dataclass(frozen=True)
class FilteredSyscallSurface:
    """一支**量到會撞被過濾 syscall** 的程式 × 它實際跑的加固面上的過濾語意。"""

    program: str
    #: 實機量到的、被 `@system-service` 擋掉的 syscall。
    syscalls: tuple[str, ...]
    #: 剖面 id（executor）或 :data:`MANAGER_SURFACE`（非 executor 的消費者面）。
    surface: str
    #: 該面上被過濾的 syscall 是否致命——**為真即是缺陷**。
    fatal: bool
    detail: str


def _surface_hardening_table(surface: str) -> tuple[dict[str, str], str]:
    """加固面名 → 該面生效的加固表。與 :func:`_surface_allows_wx` 同一組判準。"""

    if surface == MANAGER_SURFACE:
        return {key: value for key, value, _why in _HARDENING}, MANAGER_SURFACE
    profile = executor_hardening_profile(surface)
    return profile.effective(), f"executor {surface} ⇒ 剖面 {profile.profile_id}"


def filtered_syscall_surfaces() -> tuple[FilteredSyscallSurface, ...]:
    """全部 `filtered_syscalls` 非空的程式 × 它跑的每一個加固面（機械導出）。

    executor 走自己的剖面；非 executor 走 `consumed_by` 指到的消費者面（executor
    名 ⇒ 該 executor 的剖面；:data:`MANAGER_SURFACE` ⇒ Manager／monitor unit 的
    加固表）。**不另立清單**：改一支程式的形態只改 :data:`TOOLCHAIN_PROGRAMS`
    上那一列，這裡跟著動。
    """

    executor_names = {tool.name for tool in EXECUTOR_TOOLS}
    findings: list[FilteredSyscallSurface] = []
    for tool in TOOLCHAIN_PROGRAMS:
        if not tool.filtered_syscalls:
            continue
        surfaces = (
            (tool.name,) if tool.name in executor_names else tuple(tool.consumed_by)
        )
        if not surfaces:
            # `consumed_by` 空的非 executor 無從得知它跑在哪一面——那是登記表的漏，
            # 不是可以省略的一格。fail-closed 交給 `_validate_seccomp_tolerance`。
            findings.append(
                FilteredSyscallSurface(
                    program=tool.name,
                    syscalls=tool.filtered_syscalls,
                    surface="",
                    fatal=True,
                    detail="未登記 consumed_by，無法判定它跑在哪一份加固面上",
                )
            )
            continue
        for surface in surfaces:
            table, label = _surface_hardening_table(surface)
            fatal = seccomp_filter_is_fatal(table)
            findings.append(
                FilteredSyscallSurface(
                    program=tool.name,
                    syscalls=tool.filtered_syscalls,
                    surface=surface,
                    fatal=fatal,
                    detail=(
                        f"{label}（{SECCOMP_FATALITY_KEY}="
                        f"{table.get(SECCOMP_FATALITY_KEY, '') or '（未設＝致命）'}）"
                    ),
                )
            )
    return tuple(findings)


def _validate_seccomp_tolerance() -> None:
    """import 時強制 #673 的不變式。三條，缺一即讓 import 炸掉。"""

    overlap = sorted(PROFILE_LOCKED_KEYS & PROFILE_DIVERGENCE_KEYS)
    if overlap:
        raise ValueError(
            f"PROFILE_LOCKED_KEYS 與 PROFILE_DIVERGENCE_KEYS 重疊: {overlap}"
            "——鎖定鍵的意思就是任何剖面都不得分岔它。"
        )
    known = {key for key, _value, _why in _HARDENING}
    missing = sorted(PROFILE_LOCKED_KEYS - known)
    if missing:
        raise ValueError(
            f"PROFILE_LOCKED_KEYS 指到 _HARDENING 沒有的鍵: {missing}"
            "——鎖一個不存在的鍵不會擋住任何東西。"
        )
    broken = [item for item in filtered_syscall_surfaces() if item.fatal]
    if broken:
        detail = "；".join(
            f"{item.program}（{', '.join(item.syscalls)}）在 {item.detail}"
            for item in broken
        )
        raise ValueError(
            "#673 不變式失守：下列程式實機量到會撞被 `SystemCallFilter=` 過濾掉的 "
            f"syscall，而它們的執行面是**致命**過濾語意 ⇒ 會靜默死（rc=1、無輸出）：{detail}。"
            f"處置是把該面的 `{SECCOMP_FATALITY_KEY}` 設成一個 errno（現行為 EPERM），"
            "**不是**把那些 syscall 加進 `SystemCallFilter=` 白名單——後者是放寬，前者不是。"
        )


_validate_seccomp_tolerance()


# ---------------------------------------------------------------------------
# 內層沙箱的執行條件（#714）——與剖面正交的第三個維度，處置是**全域**、方向相反
# ---------------------------------------------------------------------------

#: 加固表中決定「executor 自帶的內層沙箱裝不裝得上」的那一個鍵。
#:
#: 與 :data:`SECCOMP_FATALITY_KEY` 是同一張表上的兩個鄰居，但問的是不同的問題：
#: 那一個問「被擋的 syscall 會不會殺掉行程」，這一個問「內層沙箱要用的 syscall 有沒有
#: 被放行」。兩者都指向 `SystemCallFilter=` 這一族，因此都在 :data:`PROFILE_LOCKED_KEYS`
#: 的守備範圍內——**處置一律全域，不得由剖面分岔**。
INNER_SANDBOX_SYSCALL_KEY: str = "SystemCallFilter"


@dataclass(frozen=True)
class InnerSandboxSurface:
    """一支**自帶內層沙箱**的程式 × 它實際跑的加固面上，那一層裝不裝得上（#714）。"""

    program: str
    kind: str
    #: 剖面 id（executor）或 :data:`MANAGER_SURFACE`。
    surface: str
    #: 該形態宣告需要、而該面**沒有放行**的 syscall 群組（空 tuple＝裝得上）。
    missing_groups: tuple[str, ...]
    detail: str

    @property
    def satisfied(self) -> bool:
        return not self.missing_groups


def executor_inner_sandbox(executor: str) -> InnerSandboxSpec | None:
    """executor 名 → 它自帶的內層沙箱形態（無則 ``None``）；**未知 executor 一律拒絕**。

    fail-closed 的方向與 :func:`executor_hardening_profile` 逐字相同，理由也相同：
    「不確定就回 ``None``」看起來安全，實際會讓一個沒被盤點過的 executor 在真實加固面
    下**每一條命令都失敗**，而症狀（模型回 `needs_human`）離原因四層遠——那正是 #714
    被埋掉 30 分鐘的方式。
    """

    name = str(executor or "").strip()
    for tool in EXECUTOR_TOOLS:
        if tool.name == name:
            return tool.inner_sandbox
    raise UnknownExecutorProfileError(
        f"未知的 executor {executor!r}，無法決定內層沙箱形態"
        f"（已登記：{sorted(t.name for t in EXECUTOR_TOOLS)}）。新增 executor 必須先進 "
        "permgen.EXECUTOR_TOOLS，並在真實加固面下量一次它的內層沙箱（#714）。"
    )


def _surface_allows_syscall_groups(
    surface: str, groups: Sequence[str]
) -> tuple[tuple[str, ...], str]:
    """該加固面的 `SystemCallFilter=` 少了哪幾個群組（由既有產生器導出，不另抄一份）。"""

    table, label = _surface_hardening_table(surface)
    allowed = str(table.get(INNER_SANDBOX_SYSCALL_KEY, "") or "").split()
    missing = tuple(group for group in groups if group not in allowed)
    return missing, f"{label}（{INNER_SANDBOX_SYSCALL_KEY}={' '.join(allowed) or '（未設）'}）"


def inner_sandbox_surfaces() -> tuple[InnerSandboxSurface, ...]:
    """全部宣告了 `inner_sandbox` 的程式 × 它跑的每一個加固面（機械導出）。

    與 :func:`filtered_syscall_surfaces` 逐條同型（executor 走自己的剖面、非 executor
    走 `consumed_by` 指到的消費者面），**不另立清單**：改一支程式的內層沙箱形態只改
    :data:`TOOLCHAIN_PROGRAMS` 上那一列，這裡跟著動。
    """

    executor_names = {tool.name for tool in EXECUTOR_TOOLS}
    findings: list[InnerSandboxSurface] = []
    for tool in TOOLCHAIN_PROGRAMS:
        spec = tool.inner_sandbox
        if spec is None:
            continue
        surfaces = (
            (tool.name,) if tool.name in executor_names else tuple(tool.consumed_by)
        )
        if not surfaces:
            findings.append(
                InnerSandboxSurface(
                    program=tool.name,
                    kind=spec.kind,
                    surface="",
                    missing_groups=spec.syscall_groups,
                    detail="未登記 consumed_by，無法判定它跑在哪一份加固面上",
                )
            )
            continue
        for surface in surfaces:
            missing, label = _surface_allows_syscall_groups(
                surface, spec.syscall_groups
            )
            findings.append(
                InnerSandboxSurface(
                    program=tool.name,
                    kind=spec.kind,
                    surface=surface,
                    missing_groups=missing,
                    detail=label,
                )
            )
    return tuple(findings)


def _validate_inner_sandbox_support() -> None:
    """import 時強制 #714 的不變式。兩條，缺一即讓 import 炸掉。

    1. 每一個宣告了 `inner_sandbox` 的程式，在它**每一個**執行面上都必須放行該形態
       宣告的 syscall 群組；
    2. `INNER_SANDBOX_SYSCALL_KEY` 必須留在 :data:`PROFILE_LOCKED_KEYS` 上——處置是
       **全域**放行，剖面分岔它就退化成「某一份剖面偷偷多開一支 syscall」，而那正是
       #673 立那把鎖的理由。

    為什麼在 import 當下而不是一條測試：#714 的破口不是「有人寫錯一行」，是「內層
    沙箱的執行條件從來不是機器可讀的」——於是它只能在實機上以「模型跑的每一條命令都
    `exit 1`」的形式出現，而那個症狀離原因四層遠。
    """

    if INNER_SANDBOX_SYSCALL_KEY not in PROFILE_LOCKED_KEYS:
        raise ValueError(
            f"{INNER_SANDBOX_SYSCALL_KEY} 不在 PROFILE_LOCKED_KEYS 上——內層沙箱的"
            "執行條件是**全域**一次決定（#714/#673），不得由剖面分岔。"
        )
    broken = [item for item in inner_sandbox_surfaces() if not item.satisfied]
    if broken:
        detail = "；".join(
            f"{item.program} 的 {item.kind} 內層沙箱在 {item.detail} 少了 "
            f"{', '.join(item.missing_groups)}"
            for item in broken
        )
        raise ValueError(
            "#714 不變式失守：下列 executor 自帶的內層沙箱在它實際跑的加固面上**裝不上**"
            f"⇒ job 內每一條 shell 命令都會失敗（實機症狀是模型回 `needs_human`）：{detail}。"
            f"處置是把缺的群組加進 `_HARDENING` 的 `{INNER_SANDBOX_SYSCALL_KEY}`（全域，"
            "一次可稽核的決定），**不是**替某個 executor 生一份放寬的剖面。"
        )


_validate_inner_sandbox_support()


@dataclass(frozen=True)
class NodeExecutionSurface:
    """一支 `needs_node` 的**非 executor** 程式 × 它實際跑在哪個加固面上（#661）。"""

    program: str
    #: executor 名，或 :data:`MANAGER_SURFACE`。
    surface: str
    #: 執行它的 unit 目前是否允許 W+X 記憶體（＝`MemoryDenyWriteExecute` 不是 `yes`）。
    allows_wx: bool
    detail: str


def _surface_allows_wx(surface: str) -> tuple[bool, str]:
    """該執行面的 `MemoryDenyWriteExecute` 實際值（由既有產生器導出，不另抄一份）。"""

    if surface == MANAGER_SURFACE:
        table = {key: value for key, value, _why in _HARDENING}
        value = table["MemoryDenyWriteExecute"]
        return value != "yes", f"{MANAGER_SURFACE}（MemoryDenyWriteExecute={value}）"
    profile = executor_hardening_profile(surface)
    value = profile.effective()["MemoryDenyWriteExecute"]
    return value != "yes", (
        f"executor {surface} ⇒ 剖面 {profile.profile_id}"
        f"（MemoryDenyWriteExecute={value}）"
    )


def node_execution_surfaces() -> tuple[NodeExecutionSurface, ...]:
    """把「node 程式實際跑在誰的加固面上」這句話機械化（#661）。

    **這是 #643 的剖面推導看不到的那一格。** #643 把「哪個 job 要放寬 W+X」由
    `EXECUTOR_TOOLS.needs_node` 機械導出，而那條推導的唯一輸入是 **executor 名**——
    它涵蓋「被 dispatch 直接執行的那一支是不是 node」，**不涵蓋**「那一支在執行途中
    再 exec 出來的 node 程式」。#661 的完整盤點正好撞出兩個這種格子：

    - `srt`：由 `claude`（原生 ELF ⇒ **strict** 剖面）在 review 時 exec；
    - `openspec`：由 **Manager 的 system unit** 在 ship 時 exec。

    兩者所在的 unit 目前都是 `MemoryDenyWriteExecute=yes`，而 #643 已在實機量到 V8
    的 `Runtime_CompileLazy` 在該項下直接崩。因此這兩格**預期會失敗**——但這是
    **OS 層語意**，本 repo 的測試環境沒有那個加固面，不得在這裡宣稱已驗證。本函式
    只負責讓它們**可列舉、不會靜默消失**；實機量測步驟在 runbook 第 4e 步，裁決
    （放寬哪一面、放寬到什麼程度）屬 operator，見 #643 的先例：量到才改，且不得
    就地放寬。
    """

    surfaces: list[NodeExecutionSurface] = []
    for tool in SERVICE_TOOLS:
        if not tool.needs_node:
            continue
        for consumer in tool.consumed_by:
            allows, detail = _surface_allows_wx(consumer)
            surfaces.append(
                NodeExecutionSurface(tool.name, consumer, allows, detail)
            )
    return tuple(surfaces)


def unresolved_node_execution_surfaces() -> tuple[NodeExecutionSurface, ...]:
    """`node_execution_surfaces()` 裡**執行面仍禁 W+X** 的那些（＝已知會失敗的組合）。"""

    return tuple(surface for surface in node_execution_surfaces() if not surface.allows_wx)


# ---------------------------------------------------------------------------
# 窮舉盤點（#666）：降權帳號在完整加固面下跑完一個 run 需要碰到的**全部**外部相依
#
# ## 為什麼要有這一節
#
# `#640`（executor toolchain ＋ job 憑證）、`#661`／`#664`（`srt`／`openspec`／
# preflight backend）、`#666`（`pytest`／Manager 的 gh 憑證）——**同一族的第一到第五
# 個成員**，每一次都是「症狀出現才補一項」。症狀還一次比一次遠：從 `rc=127`，到
# doctor 一個看不出原因的 FAIL，到「ledger 空 ⇒ 每張 build 卡在採信階段被拒」。
#
# 前三張表（:data:`EXECUTOR_TOOLS`／:data:`SERVICE_TOOLS`／:data:`SYSTEM_PROGRAMS`）
# 各自都是完整的——但它們回答的是「**這一類**東西有哪些」，沒有任何一個地方回答
# 「跑完一個 run 需要碰到的東西有哪些」。這一節就是後者：**判準改成從 run 反推**，
# 而不是等下一個症狀。
#
# ## 它怎麼防止自己過期
#
# 兩個方向都釘住，缺任一邊測試就紅：
#
# - :func:`uncovered_run_dependencies`：盤點列出的每一項都必須**真的**落在某張表或
#   某個登記表資產上（不是字串相等，是回頭去那張表／`ASSET_REGISTRY` 裡查）；
# - :func:`unlisted_roster_entries`：反過來，每張表上的每一項、以及每一個掛在帳號
#   HOME 下的登記表資產，都必須被盤點列到。往 `SERVICE_TOOLS` 加一支程式而忘了說明
#   它在 run 的哪一段被誰碰到，測試會紅。
#
# 已知**還沒有**歸宿的那些不塞進主表充數，改由 :func:`deferred_run_dependencies`
# 列舉（比照 :func:`unresolved_node_execution_surfaces` 的先例：不做裁決、不放寬、
# 但也不讓它靜默消失）。
# ---------------------------------------------------------------------------

class DependencyKind(Enum):
    """外部相依的種類——決定它應該落在哪一張表。"""

    #: 部署樹 `<toolchain>/bin`（版本會影響治理產出 ⇒ 可稽核的部署決定）。
    TOOLCHAIN_PROGRAM = "toolchain-program"
    #: 發行版套件（通用 runtime／傳輸層／基礎工具）。
    SYSTEM_PROGRAM = "system-program"
    #: python 發行版——**不是可執行檔**，`import`／`-m` 得到才有意義（#666 新增的
    #: 第四類；前三張表的形狀都收不下它，見 :class:`PythonDistribution`）。
    PYTHON_DISTRIBUTION = "python-distribution"
    #: 帳號 HOME 下的**憑證**檔（登記表資產；檔案該帳號擁有、目錄 root-owned）。
    CREDENTIAL = "credential"
    #: 帳號 HOME 下的**非憑證**設定（登記表資產；一律 root-owned 唯讀）。
    ACCOUNT_CONFIG = "account-config"


class RunStage(Enum):
    """一個 run 的各段——用來回答「這項相依是在哪一步被碰到的」。"""

    DISPATCH = "dispatch"        # Manager 決定派工並起 job
    MODEL_CALL = "model-call"    # job 帳號實際呼叫模型 CLI
    REVIEW = "review"            # reviewer 在 sandbox 內覆核
    GATE = "gate"                # gate 執行身分重跑 operator 宣告的命令
    SHIP = "ship"                # preflight／archive／推送
    MONITOR = "monitor"          # monitor 的輪詢與 provider


@dataclass(frozen=True)
class RunDependency:
    """窮舉盤點的一列：**誰**在 run 的**哪一段**碰到**什麼**，以及它登記在哪。"""

    name: str
    kind: DependencyKind
    #: 哪些 principal 在**完整加固面下**會碰到它（不是「誰理論上可能用到」）。
    principals: tuple[Principal, ...]
    stages: tuple[RunStage, ...]
    #: 涵蓋它的那張表的名字，或登記表資產的 `asset_id`。**這個欄位會被真的拿去查**
    #: （見 :func:`uncovered_run_dependencies`），寫錯字就是紅的。
    covered_by: str
    note: str

    @property
    def key(self) -> tuple[str, str]:
        """盤點內的唯一鍵。**名字本身不夠**——同一個 python 發行版（PyYAML）在系統層
        與部署 venv 各有一份，那是兩個不同的部署決定，不能被去重掉。"""
        return (self.name, self.covered_by)


#: **窮舉盤點的本體**（#666）。順序＝一個 run 走過的順序，方便對著 runbook 讀。
RUN_EXTERNAL_DEPENDENCIES: tuple[RunDependency, ...] = (
    # ---- DISPATCH：Manager 起 job -----------------------------------------
    RunDependency(
        "systemctl", DependencyKind.SYSTEM_PROGRAM,
        (Principal.MANAGER,), (RunStage.DISPATCH,),
        covered_by="SYSTEM_PROGRAMS",
        note="B 案的派工 client：`systemctl start --wait <模板實例>`。#666 補進表。",
    ),
    RunDependency(
        "setfacl", DependencyKind.SYSTEM_PROGRAM,
        (Principal.MANAGER,), (RunStage.DISPATCH,),
        covered_by="SYSTEM_PROGRAMS",
        note=(
            "#710：per-job 工作區的具名 ACL。Manager 建完 clone 後對**那一格**下 "
            "`setfacl -R`，job 才 `chdir` 得進自己的工作區——`chown` 需要 `CAP_CHOWN`，"
            "Manager 沒有。解不到它＝每一個 builder job 都起不來。"
        ),
    ),
    RunDependency(
        "manager-gitconfig", DependencyKind.ACCOUNT_CONFIG,
        (Principal.MANAGER, Principal.MONITOR), (RunStage.DISPATCH, RunStage.SHIP),
        covered_by="manager-gitconfig",
        note=(
            "來源樹的 `safe.directory`（#623）。Manager 對 root-owned 的來源樹跑 "
            "`fetch`／`rev-parse`／`branch -f`，缺它就是每一次 git 都 dubious ownership。"
        ),
    ),
    RunDependency(
        "git", DependencyKind.SYSTEM_PROGRAM,
        (Principal.MANAGER, Principal.MONITOR, Principal.BUILDER,
         Principal.REVIEWER, Principal.PLANNER),
        (RunStage.DISPATCH, RunStage.MODEL_CALL, RunStage.SHIP, RunStage.MONITOR),
        covered_by="SYSTEM_PROGRAMS",
        note=(
            "per-job clone、bundle 產出與回收、monitor 的 mirror 掃描、ship 段的 "
            "push／ls-remote 全靠它。**gate 刻意不在 principals 內**——那一格是未決的，"
            "見 `deferred_run_dependencies()`。"
        ),
    ),
    # ---- MODEL_CALL：job 帳號呼叫模型 --------------------------------------
    RunDependency(
        "bash", DependencyKind.SYSTEM_PROGRAM,
        (Principal.MANAGER, Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER),
        (RunStage.DISPATCH, RunStage.MODEL_CALL),
        covered_by="SYSTEM_PROGRAMS",
        note=(
            "**每一支 job 的 `command[0]`**（wrapper 是 `bash -c <script>`），"
            "以及 Manager 側的 exit 記帳 shell。#666 補進表。"
        ),
    ),
    RunDependency(
        "codex", DependencyKind.TOOLCHAIN_PROGRAM,
        (Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER),
        (RunStage.MODEL_CALL,),
        covered_by="EXECUTOR_TOOLS",
        note="dispatch 直接執行的模型 CLI；本部署的 `PSC_MANAGER_EXECUTOR` 預設值。",
    ),
    RunDependency(
        "claude", DependencyKind.TOOLCHAIN_PROGRAM,
        (Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER, Principal.MANAGER),
        (RunStage.MODEL_CALL, RunStage.REVIEW),
        covered_by="EXECUTOR_TOOLS",
        note=(
            "**Manager 也在 principals 內，而且不是筆誤**：`planning_runtime` 的 JSON "
            "呼叫是在 **Manager 行程內**直接 exec `claude` 的（不是派一個降權 job），"
            "因此它跑在 Manager unit 的加固面上、讀的是 Manager HOME。那條路徑的登入態"
            "目前沒有登記表資產，見 `deferred_run_dependencies()`。"
        ),
    ),
    RunDependency(
        "copilot", DependencyKind.TOOLCHAIN_PROGRAM,
        (Principal.BUILDER,), (RunStage.MODEL_CALL,),
        covered_by="EXECUTOR_TOOLS",
        note="shell script，但內部再 exec node（#643 實機量測）⇒ 走 `jit` 剖面。",
    ),
    RunDependency(
        "agy", DependencyKind.TOOLCHAIN_PROGRAM,
        (Principal.PLANNER, Principal.MANAGER), (RunStage.MODEL_CALL, RunStage.DISPATCH),
        covered_by="EXECUTOR_TOOLS",
        note=(
            "planning 的 canonical executor。Manager 另外會跑 `agy models`（"
            "`model_identities` 的 live probe ＋ doctor 的 `agy` probe），因此它同樣"
            "要在 Manager 的 `PATH` 上。"
        ),
    ),
    RunDependency(
        "node", DependencyKind.SYSTEM_PROGRAM,
        (Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER, Principal.MANAGER),
        (RunStage.MODEL_CALL, RunStage.REVIEW, RunStage.SHIP),
        covered_by="SYSTEM_PROGRAMS",
        note="全部 `needs_node` 的 toolchain 程式共用的系統層 runtime。",
    ),
    RunDependency(
        "builder-codex-state", DependencyKind.CREDENTIAL,
        (Principal.BUILDER,), (RunStage.MODEL_CALL,),
        covered_by="builder-codex-state",
        note=(
            "**job 帳號**的 provider 憑證＋codex 狀態樹（#640 裁決 (b) → #698 方案 A）。"
            "洩漏面＝一次模型呼叫的額度——job unit 另有 `Environment=GH_TOKEN=` 把 GitHub "
            "token 清空，成果一律走 `commit-spool` 由 Manager 代理推送。**與 "
            "`manager-gh-credential` 不同級**。\n"
            "**#698 起形狀是 root-owned ＋ sticky 的整棵樹**（不再是單檔 `auth.json`）："
            "#686 實測 codex 需要 `$CODEX_HOME` 整棵可寫，只放行單檔時它連起都起不來。"
        ),
    ),
    RunDependency(
        "reviewer-planner-codex-state", DependencyKind.CREDENTIAL,
        (Principal.REVIEWER, Principal.PLANNER), (RunStage.MODEL_CALL, RunStage.REVIEW),
        covered_by="reviewer-planner-codex-state",
        note=(
            "#685／#698：planner／reviewer 帳號的 codex 登入態＋狀態樹（`~/.codex`，"
            "root-owned ＋ sticky 的真目錄）。**不是一個 `auth.json`**——#686 實測 "
            "codex 需要 `$CODEX_HOME` 整棵可寫，只放行單檔時它連起都起不來。"
        ),
    ),
    RunDependency(
        "reviewer-planner-agy-state", DependencyKind.CREDENTIAL,
        (Principal.PLANNER,), (RunStage.MODEL_CALL,),
        covered_by="reviewer-planner-agy-state",
        note=(
            "#685／U-7：agy 的可寫狀態樹（`~/.gemini` → `cache/gemini`）。planning 的異質 "
            "planner 就是它——0818 在完整 unit 沙箱下實測 rc=0、輸出逐位元等於 expected。"
            "principals 只列 PLANNER 是因為 reviewer 的 executor 是 `claude`；帳號共用，"
            "因此 ACL 面上兩者不可分（三分方案的既有性質）。"
        ),
    ),
    RunDependency(
        "reviewer-planner-claude-state", DependencyKind.CREDENTIAL,
        (Principal.REVIEWER, Principal.PLANNER), (RunStage.MODEL_CALL, RunStage.REVIEW),
        covered_by="reviewer-planner-claude-state",
        note=(
            "#685：reviewer 的**預設 executor** 是 `claude`，而這個帳號從 M2（#615）起"
            "就沒有 claude 登入態——#686 的矩陣裡它是「CLI rc=0、卻回 `Not logged in`」"
            "的那一列。缺它時「reviewer 已降權」買到的是一個跑不動的 job。"
        ),
    ),
    RunDependency(
        "builder-codex-hooks", DependencyKind.ACCOUNT_CONFIG,
        (Principal.BUILDER,), (RunStage.MODEL_CALL,),
        covered_by="builder-codex-hooks",
        note=(
            "enforcement plane：root-owned，job 不得替換自己的 hooks（#623）。"
            "#698 起它住在 root-owned ＋ sticky 的 `~/.codex` 裡——那棵樹整個可寫"
            "（codex 才起得來），但這個檔 job 刪不掉、改不掉名字、也改不了內容。"
        ),
    ),
    RunDependency(
        "reviewer-planner-codex-hooks", DependencyKind.ACCOUNT_CONFIG,
        (Principal.REVIEWER, Principal.PLANNER), (RunStage.MODEL_CALL, RunStage.REVIEW),
        covered_by="reviewer-planner-codex-hooks",
        note=(
            "#698：這一份在 #685～#697 之間是 `deferred_run_dependencies()` 的 **U-9**"
            "——「codex 要整棵可寫」與「樹裡放得住 root-owned 檔」在當時的形狀下互斥，"
            "而 0818 的 R9 T3.9 實測證明不做的代價是**該帳號能植入 hooks.json** ⇒ 跨 job "
            "持久化。方案 A（sticky）讓兩件事同時成立，本項因此從 deferred 升為登記表資產。"
            "**它與 builder 那一份由同一條規則長出來**（`enforcement_placements()`）。"
        ),
    ),
    RunDependency(
        "builder-gitconfig", DependencyKind.ACCOUNT_CONFIG,
        (Principal.BUILDER,), (RunStage.MODEL_CALL,),
        covered_by="builder-gitconfig",
        note=(
            "**來源樹**那兩條逐字 `safe.directory`（`<repos>/<slug>` 與其 `.git`，"
            "#623）；root-owned（`alias.*`／`core.fsmonitor` 會執行外部命令）。"
            "\n⛔ **不涵蓋 per-job clone**——本項的 note 從 #623 起逐字寫著「per-job "
            "clone 的 safe.directory」，而產生器實際只出來源樹那兩條：per-job clone 是 "
            "`<pool>/<job-id>`，路徑動態，靜態檔裝不下（萬用字元已被實測否決，見 "
            "`build_account_gitconfig`）。那則宣稱**反向說謊了兩個月**，代價是 #712："
            "ACL 補上之後 builder job 真的跑起來，然後死在 `fatal: detected dubious "
            "ownership`。per-job 那一格改由 spec 的 env 逐 job 放行（登記表 "
            "`JOB_GIT_WORKSPACE_TRUST`），**與本項不衝突、也不互相取代**。"
        ),
    ),
    RunDependency(
        "reviewer-planner-gitconfig", DependencyKind.ACCOUNT_CONFIG,
        (Principal.REVIEWER, Principal.PLANNER), (RunStage.REVIEW,),
        covered_by="reviewer-planner-gitconfig",
        note=(
            "同 `builder-gitconfig` 的**來源樹**那兩條，掛在 reviewer／planner 共用的"
            "那個帳號 HOME 下。\n⛔ **同樣不涵蓋 review worktree**：那棵樹是 "
            "`git worktree add --detach` 在 `<repos>/<slug>/.psc-review-worktrees/` 底下"
            "開的 linked worktree，路徑動態，而且實測 git 2.43 只給來源樹那兩條時，對 "
            "worktree **自己的路徑**仍是 dubious ownership（git 查的是工作樹的路徑）。"
            "本項舊 note 只寫「同 `builder-gitconfig`」，於是連同那則錯誤宣稱一起繼承"
            "了（#712／#696）。"
        ),
    ),
    # ---- REVIEW：reviewer 在 sandbox 內覆核 --------------------------------
    RunDependency(
        "srt", DependencyKind.TOOLCHAIN_PROGRAM,
        (Principal.REVIEWER,), (RunStage.REVIEW,),
        covered_by="SERVICE_TOOLS",
        note=(
            "Claude review sandbox 的強制面（#661）。由 `claude` 在執行途中 exec ⇒ "
            "跑在 `strict` 剖面上，那一格仍未決，見 `unresolved_node_execution_surfaces()`。"
        ),
    ),
    RunDependency(
        "bwrap", DependencyKind.SYSTEM_PROGRAM,
        (Principal.REVIEWER,), (RunStage.REVIEW,),
        covered_by="SYSTEM_PROGRAMS",
        note="`srt` 在 Linux 上的 namespace 隔離實作；doctor 會實跑 `--version`。",
    ),
    RunDependency(
        "socat", DependencyKind.SYSTEM_PROGRAM,
        (Principal.REVIEWER,), (RunStage.REVIEW,),
        covered_by="SYSTEM_PROGRAMS",
        note="`srt` 網路政策那一段的 socket 轉發。",
    ),
    RunDependency(
        "python3", DependencyKind.SYSTEM_PROGRAM,
        (Principal.GATE, Principal.REVIEWER, Principal.MANAGER),
        (RunStage.GATE, RunStage.REVIEW),
        covered_by="SYSTEM_PROGRAMS",
        note=(
            "**gate 宣告 `python3 -m pytest` 時解析到的是這一支**（系統層），不是部署 "
            "venv 的那一支——#666 兩個漂移項之一的機械成因。`srt` 的沙箱 smoke test 也"
            "拿它當被關的程式。#666 補進表。"
        ),
    ),
    # ---- GATE：gate 執行身分重跑 operator 宣告的命令 -----------------------
    RunDependency(
        "pytest", DependencyKind.PYTHON_DISTRIBUTION,
        (Principal.GATE,), (RunStage.GATE,),
        covered_by="SYSTEM_PYTHON_DISTRIBUTIONS",
        note=(
            "#666 漂移項 1。裝在 operator 的 user site-packages 時 gate 身分讀不到 ⇒ "
            "每張 build 卡的 ledger 為空 ⇒ 撞 #540 的 acceptance chain。版本約束由 "
            "`pyproject.toml` 的 `test` extra 宣告（明示的部署決定）。"
        ),
    ),
    RunDependency(
        "PyYAML", DependencyKind.PYTHON_DISTRIBUTION,
        (Principal.GATE,), (RunStage.GATE,),
        covered_by="SYSTEM_PYTHON_DISTRIBUTIONS",
        note=(
            "**被測樹的 runtime 相依**，不是 pytest 的（#666 窮舉盤點才撞出來）。缺它"
            "的症狀是 pytest exit code `2`（collection error），依 #307 的判準不會被誤"
            "判成合格 RED，但整張卡照樣過不了。"
        ),
    ),
    # ---- SHIP：preflight／archive／推送 ------------------------------------
    RunDependency(
        "openspec", DependencyKind.TOOLCHAIN_PROGRAM,
        (Principal.MANAGER,), (RunStage.SHIP,),
        covered_by="SERVICE_TOOLS",
        note=(
            "`openspec archive -y`／`validate --strict` 都是**採信判準**（#661），因此"
            "版本進部署樹。由 Manager 的 system unit exec ⇒ 那一格同樣仍未決。"
        ),
    ),
    RunDependency(
        PREFLIGHT_BACKEND_DISTRIBUTION, DependencyKind.PYTHON_DISTRIBUTION,
        (Principal.MANAGER,), (RunStage.SHIP,),
        covered_by="DEPLOYMENT_PYTHON_DISTRIBUTIONS",
        note=(
            "preflight 的 backend（#661）。落點是既有的部署 venv ⇒ **不新增檔案系統"
            "資產**；版本必須逐字等於 `.project-policy.yml` 的 `policy_version`。"
        ),
    ),
    RunDependency(
        "PyYAML", DependencyKind.PYTHON_DISTRIBUTION,
        (Principal.MANAGER, Principal.MONITOR),
        (RunStage.DISPATCH, RunStage.SHIP, RunStage.MONITOR),
        covered_by="DEPLOYMENT_PYTHON_DISTRIBUTIONS",
        note=(
            "cortex 自己唯一的 runtime 相依，隨 venv 走。**與系統層那一份是兩個不同 "
            "interpreter 下的兩份**，因此在盤點裡也是兩列（`RunDependency.key` 用 "
            "`(name, covered_by)` 正是為了這個）。"
        ),
    ),
    RunDependency(
        "gh", DependencyKind.SYSTEM_PROGRAM,
        (Principal.MANAGER, Principal.MONITOR), (RunStage.SHIP, RunStage.MONITOR),
        covered_by="SYSTEM_PROGRAMS",
        note="Manager 對 GitHub 的傳輸層；monitor 的兩個 github provider 也直接跑它。",
    ),
    RunDependency(
        "manager-gh-credential", DependencyKind.CREDENTIAL,
        (Principal.MANAGER, Principal.MONITOR), (RunStage.SHIP, RunStage.MONITOR),
        covered_by="manager-gh-credential",
        note=(
            "#666 漂移項 2。**洩漏面與 job 憑證不同級**：Manager 是 durable state "
            "owner，這個 token 推得動 PR、關得掉 issue、merge 得了分支。"
        ),
    ),
    RunDependency(
        "manager-gh-config", DependencyKind.ACCOUNT_CONFIG,
        (Principal.MANAGER, Principal.MONITOR), (RunStage.SHIP, RunStage.MONITOR),
        covered_by="manager-gh-config",
        note=(
            "同目錄下的非憑證設定，**owner 刻意與 `hosts.yml` 不同**：`aliases` 可宣告 "
            "`!` shell alias ⇒ 必須 root-owned 唯讀（與三份 `.gitconfig` 同一條理由）。"
        ),
    ),
)


#: 盤點的 `covered_by` 可以指向哪些**名冊**（其餘視為登記表 `asset_id`）。
_DEPENDENCY_ROSTERS: Mapping[str, Callable[[], frozenset[str]]] = MappingProxyType({
    "EXECUTOR_TOOLS": lambda: frozenset(t.name for t in EXECUTOR_TOOLS),
    "SERVICE_TOOLS": lambda: frozenset(t.name for t in SERVICE_TOOLS),
    "SYSTEM_PROGRAMS": lambda: frozenset(p.name for p in SYSTEM_PROGRAMS),
    "SYSTEM_PYTHON_DISTRIBUTIONS": lambda: frozenset(
        d.name for d in SYSTEM_PYTHON_DISTRIBUTIONS
    ),
    "DEPLOYMENT_PYTHON_DISTRIBUTIONS": lambda: frozenset(
        d.name for d in DEPLOYMENT_PYTHON_DISTRIBUTIONS
    ),
})


def home_anchored_asset_ids(layout: "PathLayout | None" = None) -> frozenset[str]:
    """掛在**帳號 HOME** 底下的登記表資產 id（＝憑證／per-account 設定那一族）。

    由 `asset_paths()` × :func:`home_anchored_account` 機械導出，**不是手寫清單**：
    新增一個掛在帳號 HOME 下的資產而沒有把它列進 :data:`RUN_EXTERNAL_DEPENDENCIES`，
    :func:`unlisted_roster_entries` 就會非空。
    """
    resolved = layout if layout is not None else DEFAULT_LAYOUT
    return frozenset(
        asset_id
        for asset_id, path in resolved.asset_paths().items()
        if home_anchored_account(resolved, path) is not None
    )


def uncovered_run_dependencies() -> tuple[RunDependency, ...]:
    """盤點裡 `covered_by` **查不到東西**的那些列（正常應為空）。

    這不是字串比對：名冊型的 `covered_by` 會回頭去那張 tuple 裡找同名項，資產型的會
    去 `registry.ASSET_REGISTRY` 找 `asset_id`。把一項從表上拿掉、或打錯資產 id，
    這裡就會非空。
    """
    known_assets = {a.asset_id for a in registry.ASSET_REGISTRY}
    missing: list[RunDependency] = []
    for dep in RUN_EXTERNAL_DEPENDENCIES:
        roster = _DEPENDENCY_ROSTERS.get(dep.covered_by)
        if roster is not None:
            if dep.name not in roster():
                missing.append(dep)
            continue
        if dep.covered_by not in known_assets or dep.name != dep.covered_by:
            missing.append(dep)
    return tuple(missing)


def unlisted_roster_entries(layout: "PathLayout | None" = None) -> tuple[str, ...]:
    """**反方向**：名冊／登記表上有、但窮舉盤點沒列到的項目（正常應為空）。

    回傳 `"<名冊或 registry>:<名字>"`。這條是本節真正的價值所在——它讓「往
    `SERVICE_TOOLS` 加一支程式」與「說明它在 run 的哪一段被誰碰到」變成同一件事，
    而不是兩件可以只做一半的事。#640→#661→#666 每一次的形態都是後者只做了一半。
    """
    listed = {dep.key for dep in RUN_EXTERNAL_DEPENDENCIES}
    orphans: list[str] = []
    for roster_name, members in _DEPENDENCY_ROSTERS.items():
        for name in sorted(members()):
            if (name, roster_name) not in listed:
                orphans.append(f"{roster_name}:{name}")
    for asset_id in sorted(home_anchored_asset_ids(layout)):
        if (asset_id, asset_id) not in listed:
            orphans.append(f"registry:{asset_id}")
    return tuple(orphans)


@dataclass(frozen=True)
class DeferredDependency:
    """盤點撞到、但**尚未有歸宿**的相依（#666）。不做裁決，只讓它不會靜默消失。"""

    name: str
    kind: DependencyKind
    principals: tuple[Principal, ...]
    #: 為什麼它現在沒有登記表資產／沒有產生器步驟。
    reason: str
    #: 實機上會怎麼表現出來（症狀，不是推測）。
    symptom: str
    #: 誰要裁決、往哪裡走。
    disposition: str


#: **窮舉盤點撞到的未決項**（#666）。比照 :func:`unresolved_node_execution_surfaces`
#: 的先例：本 PR **不做裁決、不放寬任何一面**，只讓它們可列舉——落位計畫會把它們印在
#: 輸出裡，測試釘住目前的內容，補上或悄悄拿掉都會讓測試變紅。
#:
#: 四項的共同形態是「**per-account 的機制已經就緒，但登記表只登記了其中一份**」——
#: 與 #640 對 `builder-codex-state`（當時叫 `builder-executor-credential`）的處置逐條
#: 同構（那一條的 note 有完整
#: 論證），差別只在那時候 M2 還沒落地、現在落地了。
_DEFERRED_RUN_DEPENDENCIES: tuple[DeferredDependency, ...] = (
    DeferredDependency(
        "gate-gitconfig", DependencyKind.ACCOUNT_CONFIG,
        (Principal.GATE,),
        reason=(
            "`ACCOUNT_GITCONFIG_ASSETS` 只有 BUILDER／REVIEWER／MANAGER 三個 principal"
            "——#629 開出 `cortex-gate` 時沒有一併考慮它需不需要 git 設定。"
        ),
        symptom=(
            "gate 把 builder 的樹**複製**到自己的拋棄式工作區後才跑命令，因此複本由 "
            "gate 自己擁有、多數情況不會撞 dubious ownership。真正會撞的是「gate 命令"
            "碰到 `.git`」的情形（`setuptools-scm`、`git describe`、需要 git 的 pytest "
            "plugin）。**目前 `PSC_GATE_CMD_PYTEST` 沒有這種相依，因此這是預防面而不是"
            "現行症狀**——寫在這裡是為了不讓它變成第六個「症狀出現才補」。"
        ),
        disposition="等第一個需要 git 的 gate 宣告出現時再補，或 operator 主動裁決。",
    ),
    # **U-9（`reviewer-planner-codex-hooks`）已於 #698 結案，因此本項消失。**
    #
    # 它從 #685 起掛在這裡，理由是「(i) codex 起得來（$CODEX_HOME 整棵可寫）與 (ii) 同棵
    # 樹裡有一個 job 換不掉的 root-owned `hooks.json` **互斥**」，並記下兩個不做的理由：
    # 要改 permgen 的 mode 管線（`_mask_write`／`mode & 0o700` 會吃掉 sticky），以及
    # 「codex 能不能在一個它**不擁有**的 `$CODEX_HOME` 下跑起來」沒有實機證據。
    #
    # #698 把兩個理由都消掉了：
    #   - mode 管線已能表達 sticky（`build_entry()` 尾端的安全網改成
    #     `& (STICKY_BIT | 0o700)`，§R2 的 group／other 不可寫一行未放寬）；
    #   - 「不擁有的 `$CODEX_HOME`」已在**完整模板 unit 加固面**下實測（runbook 第
    #     4e-2b 步的 planning 探針矩陣，兩個帳號各一輪，逐格記路徑與版本字串）。
    #
    # 於是 (i) 與 (ii) 不再互斥，兩份 hooks 都成為登記表資產（`*-codex-hooks`），
    # 由 `enforcement_placements()` 從**同一條規則**長出來——這正是「deferred 該怎麼
    # 結案」的形態：不是刪一列，是那一列描述的張力真的不存在了。
    # #687（#672 票 F）：`manager-claude-credential` 已移除。
    #
    # 它從來不是「還沒補的憑證」，而是「Manager 在 direct 模式下自己 exec `claude`」
    # 這件事的登記表投影。票 F 的裁決是 **planning 一律走降權 job**：四分部署的
    # `PSC_JOB_RUNNER=systemd-template` 使 `planning_runtime._select_planning_invoker()`
    # 恆回 `JobPlanningInvoker`，模型 CLI 只在 `cortex-reviewer-job@` 實例內、以
    # `cortex-reviewer-planner` 執行。Manager 不再 exec 任何 executor ⇒ 它不需要
    # 任何 executor 憑證 ⇒ 本項**消失**，而不是被登記成一格資產。
    #
    # **這條裁決不是「刪一列」，它有可驗證的內容**：`_select_planning_invoker` 對
    # `systemd-run`（A 案）與任何非法值 fail-closed、**不退回 in-process**——因此
    # 「Manager 又開始跑模型」這件事不可能靜默發生。direct 模式的程式碼逐字保留
    # （開發機、離線重現），它在四分部署上不是一個可達的組態。
    #
    # 票 D（#685）刻意沒刪，理由是「切換尚未發生，現在刪等於宣稱一件還沒成立的
    # 事」。票 F 讓它成立了：實機一輪 define 的每一次模型呼叫都落成一個模板 unit
    # 實例（PR #687 的 E2E evidence）。
)


def deferred_run_dependencies() -> tuple[DeferredDependency, ...]:
    """窮舉盤點撞到、尚未有歸宿的相依（#666）。純函式；內容由測試釘住。"""
    return _DEFERRED_RUN_DEPENDENCIES


#: **實際具備啟動面降權的 job principal**（＝各有一組 root-owned 模板 unit 的角色）。
#:
#: - `BUILDER`（M1，#603／#584）：`cortex-job@.service`／`cortex-job-jit@.service`。
#: - `REVIEWER`（M2，#615）：`cortex-reviewer-job@.service`／
#:   `cortex-reviewer-job-jit@.service`，`User=cortex-reviewer-planner`。
#:
#: **`PLANNER` 刻意不在表內，而且不是遺漏**：三分方案把 reviewer 與 planner 映到
#: **同一個帳號**（`cortex-reviewer-planner`），而模板 unit 的唯一內容差異就是
#: `User=`／HOME／`ReadWritePaths=`——那三者都由帳號決定。為 planner 再產一份逐字
#: 相同、只是名字不同的 unit，等於多一個要同步維護的放行面（polkit pattern 也會多
#: 一個字幹），卻換不到任何隔離。`REVIEWER` 在這裡是**那個帳號的代表 principal**，
#: 由 :data:`JOB_PRINCIPAL_PERSONAS` 明載它代表誰。
#:
#: **本表是「unit／spool 產得出哪幾份」，不是「哪些執行路徑真的走上它」（#687）。**
#: 這兩件事在 #615～#686 之間**分岔了三個月**：本表從 #615 起就宣稱 reviewer／planner
#: 這一格已降權，而 planner 的 define／brainstorm 當時仍在 Manager 行程內跑模型
#: （#672）——因為它走的是 `planning_runtime`，不是 `SubprocessLauncher`。
#: 產生器面永遠不會發現這件事：unit 產得出來，只是沒有人拿它起 job。
#: 「執行路徑走不走上這份 unit」的答案在
#: `coordinator/planning_runtime._select_planning_invoker()` 與
#: `coordinator/launcher.SubprocessLauncher._job_role()` 兩處，不在本表。
#:
#: - `GATE`（#629）：`cortex-gate-job@.service`／`cortex-gate-job-jit@.service`，
#:   `User=cortex-gate`。它不跑模型，跑的是 operator 宣告的 gate 命令——但那些命令
#:   載入的是 builder 工作樹裡的 `conftest.py`／plugin，所以在「必須被關進盒子」這件
#:   事上與前兩者同級。
#:
#: 這張表同時是 polkit unit pattern 的字幹來源（見 :func:`job_unit_pattern`）：
#: 放行面 = 這張表 × :data:`HARDENING_PROFILES`，兩者都是列舉，沒有萬用字元。
#:
#: **#657：真相搬進 `registry`，本名只是別名。** 這張表同時決定「登記表有哪些
#: per-principal spec spool 資產」（`registry.job_spec_spool_asset_id`），而
#: `permgen` import `registry`（反向不成立）——留在這裡會讓登記表得從產生器 import
#: 回來。搬過去之後兩邊仍是**同一個 tuple 物件**，不是兩份會漂移的清單。
DOWNGRADED_JOB_PRINCIPALS: tuple[Principal, ...] = registry.DOWNGRADED_JOB_PRINCIPALS

#: 每個 job 角色的 `PATH` 覆寫變數名。與 `coordinator/job_runner.JOB_ROLE_CONFIG`
#: 的 `path_env` 是**成對契約**（同 `DEFAULT_TEMPLATE_UNIT` 的既有模式：permgen 與
#: job_runner 刻意不互相 import，改由契約測試釘住兩邊逐字相等）。產生的 unit 內註解
#: 要指出「真正的 PATH 來自哪個 Manager 端變數」，那個名字必須跟著角色走——寫死
#: `PSC_BUILDER_PATH` 會讓 reviewer 的 unit 指到一個對它無效的變數。
JOB_PATH_ENV_BY_PRINCIPAL: Mapping[Principal, str] = MappingProxyType(
    {
        Principal.BUILDER: "PSC_BUILDER_PATH",
        Principal.REVIEWER: "PSC_REVIEWER_PATH",
        Principal.GATE: "PSC_GATE_PATH",
    }
)

#: 每個 job 角色的 `HOME` 覆寫變數名（#685／#686）。與 `job_runner.JOB_ROLE_CONFIG` 的
#: `home_env` 是**成對契約**，與上面那張 PATH 表逐條同構、同一個理由：模板模式下 shim
#: 以 `os.execvpe` 整份換掉環境，unit 的 `Environment=HOME=` 到不了模型，`HOME` 只能
#: 來自 spec 的 env。
#:
#: **本表對 #685 是必要的，不是順手加的**：三份登入態（`~/.codex`／`~/.gemini`／
#: `~/.claude`）全部以 `$HOME` 為根，`HOME` 沒宣告時它們在 job 內一條都解不到——而
#: 症狀（`Not logged in`／`$HOME is not defined`）與「憑證沒放好」長得一模一樣。
JOB_HOME_ENV_BY_PRINCIPAL: Mapping[Principal, str] = MappingProxyType(
    {
        Principal.BUILDER: "PSC_BUILDER_HOME",
        Principal.REVIEWER: "PSC_REVIEWER_HOME",
        Principal.GATE: "PSC_GATE_HOME",
    }
)

#: 每份模板 unit 服務的 persona 家族（代表 principal → 實際會以該 unit 起跑的 persona）。
#: 記錄在這裡而不是散在註解裡：`cortex-reviewer-job@.service` 同時是 planner 的 unit，
#: 這件事必須是機器可讀的，否則「planner 的降權在哪」只能靠讀 commit 訊息回答。
#: 產生每份模板 unit 的 CLI 旗標（`python -m paulsha_cortex.trust_root unit …`）。
#: 產出的 unit 檔頭會印出「重跑用哪一行」，那一行必須跟著角色走——寫死 `--job` 會讓
#: operator 照抄之後拿到 builder 的 unit 覆蓋掉 reviewer／gate 的那一份。
JOB_UNIT_CLI_FLAG: Mapping[Principal, str] = MappingProxyType(
    {
        Principal.BUILDER: "--job",
        Principal.REVIEWER: "--review-job",
        Principal.GATE: "--gate-job",
    }
)

JOB_PRINCIPAL_PERSONAS: Mapping[Principal, frozenset[Principal]] = MappingProxyType(
    {
        Principal.BUILDER: frozenset({Principal.BUILDER, Principal.HEADLESS_HOOK}),
        Principal.REVIEWER: frozenset({Principal.REVIEWER, Principal.PLANNER}),
        # #629：gate 不代表任何 persona——它就是它自己。這一格不是佔位符，是斷言：
        # 「有沒有哪個 persona 其實是以 gate 帳號起跑的」必須是機器可讀的 **否**。
        Principal.GATE: frozenset({Principal.GATE}),
    }
)


def downgraded_job_principals(scheme: UidScheme) -> tuple[Principal, ...]:
    """本方案**真的會落檔**的那一組降權 job principal（依表順序）。

    :data:`DOWNGRADED_JOB_PRINCIPALS` 是「本系統支援哪些降權角色」；本函式是「這個
    部署有哪些」。兩者在四分方案下相同，在二分／三分下差一項——那兩個方案對
    `GATE` 明示 :data:`ABSENT_ACCOUNT`（#629），因此不會有 `cortex-gate-job@.service`
    這份 unit 檔。

    **為什麼 polkit pattern 必須用這一支而不是那張表**：pattern 是放行面。放行一個
    在本機**不存在**的 unit 名不會讓任何 job 起得來（unit 檔不在就是不在），但它會
    讓「這條規則授權了什麼」與「這台機器上實際有什麼」對不起來——而那條規則的全部
    價值就在於它可以被逐字讀懂。同理 `build_job_unit()` 對未映射的 principal 直接
    raise：產生一份 `User=` 空白的 unit 比不產生危險得多。
    """

    return tuple(p for p in DOWNGRADED_JOB_PRINCIPALS if scheme.resolve(p) is not None)


def _as_principals(
    principals: "Principal | Sequence[Principal]",
) -> tuple[Principal, ...]:
    """把「單一 principal」與「一組 principal」正規化成 tuple（順序即輸出順序）。"""

    if isinstance(principals, Principal):
        return (principals,)
    ordered = tuple(principals)
    if not ordered:
        raise ValueError("至少要給一個 principal——空集合會產出一條放行面為空的 pattern")
    return ordered


def job_unit_stem(
    layout: "PathLayout" = None,  # type: ignore[assignment]
    principal: Principal = Principal.BUILDER,
    profile: HardeningProfile = DEFAULT_HARDENING_PROFILE,
) -> str:
    """降權 job 模板 unit 的字幹（不含 `@.service`）。

    **M2（#615，reviewer/planner 啟動面降權）已由此擴充點落地**：傳入
    `Principal.REVIEWER` 即得 `cortex-reviewer-job`，unit 名、`User=`、
    `Environment=HOME=`／`XDG_CACHE_HOME=`、`ReadWritePaths=` 全部跟著 scheme 導出，
    `build_job_unit()`／`build_polkit_rule()`／`build_job_shim()` 三支產生器**一行
    都沒有改**（M2 只改了「預設涵蓋哪些 principal」與 RWP 的除役集合）。
    builder 的字幹維持 `cortex-job`（與 `coordinator/job_runner` 的
    `TEMPLATE_UNIT_PREFIX` ＋ polkit pattern 成對契約），逐字不變。

    `profile` 是 **#643 的第二個擴充點**：加固剖面不同 ⇒ 必須是不同的 unit 檔
    （加固指令寫在檔案裡，一個模板只有一份），因此字幹尾端掛剖面後綴。嚴格剖面的
    後綴是空字串，`cortex-job@.service` 這個既有名字逐字不變。

    **#629 由同一個擴充點再加一個角色**：`Principal.GATE` → `cortex-gate-job`
    （`User=cortex-gate`）。產生器同樣一行都沒有改。
    """
    layout = layout if layout is not None else DEFAULT_LAYOUT
    if principal is Principal.BUILDER:
        return f"{layout.instance}-job{profile.unit_suffix}"
    return f"{layout.instance}-{principal.value}-job{profile.unit_suffix}"


@dataclass(frozen=True)
class SystemdUnit:
    """產生出來的 unit：**只有內容字串與結構化欄位，沒有任何寫檔／執行**。"""

    unit_name: str
    install_path: str
    account: str
    exec_start: str
    environment_file: str | None
    read_write_paths: tuple[str, ...]
    content: str
    #: 生效的加固剖面 id（#643）。Manager／monitor unit 恆為 `strict`。
    hardening_profile: str = DEFAULT_HARDENING_PROFILE.profile_id
    #: 巢狀在 `read_write_paths` 之內、被重新收回唯讀的路徑（#698 的 enforcement 檔）。
    read_only_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "unit_name": self.unit_name,
            "install_path": self.install_path,
            "account": self.account,
            "exec_start": self.exec_start,
            "environment_file": self.environment_file,
            "read_write_paths": list(self.read_write_paths),
            "read_only_paths": list(self.read_only_paths),
            "hardening_profile": self.hardening_profile,
            "content": self.content,
        }


def _wrap_comment(text: str, prefix: str = "# ", width: int = 78) -> list[str]:
    """把一段（可能很長的）中文說明折成多行註解。

    `textwrap` 只在空白處斷行，對中文等於不折——產出的 unit 會出現數百字元的單行
    註解，`systemctl cat` 讀起來完全失去可審查性。這裡改以顯示寬度計（CJK 算 2），
    可在任意字元處斷行。
    """

    lines: list[str] = []
    current: list[str] = []
    used = len(prefix)
    for char in text:
        size = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + size > width and current:
            lines.append((prefix + "".join(current)).rstrip())
            current = []
            used = len(prefix)
            if char == " ":
                # 斷在空白處時不把它帶到下一行的行首。
                continue
        current.append(char)
        used += size
    if current:
        lines.append((prefix + "".join(current)).rstrip())
    return lines or [prefix.rstrip()]


def _hardening_lines(
    profile: HardeningProfile = DEFAULT_HARDENING_PROFILE,
) -> list[str]:
    """展開加固表。**所有 unit 都走這一支**，剖面只以 `overrides` 分岔。

    刻意不接受任意 override mapping：唯一的合法覆寫來源是 :data:`HARDENING_PROFILES`
    ——那張表已在 import 時驗過「鍵存在且在 `PROFILE_DIVERGENCE_KEYS` 內」。開一個
    自由的 override 參數等於讓任何呼叫端就地放寬一項加固而不被任何檢查看到。
    """

    lines: list[str] = []
    for key, value, why in _HARDENING:
        effective = profile.overrides.get(key, value)
        # 折行（#673）：加固表的理由已長到單行數百字元，`systemctl cat` 讀起來完全
        # 失去可審查性——而「這一行為什麼在這裡」正是這份 root-owned 檔存在的理由。
        # `_wrap_comment` 就是為此而寫的（見它的 docstring），這裡沿用同一支。
        lines += _wrap_comment(why)
        if effective != value:
            lines += _wrap_comment(
                f"※ 剖面覆寫（profile={profile.profile_id}）：嚴格剖面為 "
                f"{key}={value}，本剖面改為 {key}={effective}。**這是本檔與 "
                f"strict 剖面唯一的差異**；理由與接受的代價見檔頭「加固剖面」段。"
            )
        lines.append(f"{key}={effective}")
    return lines


def _rwp_lines(
    owners: Mapping[str, tuple[str, ...]],
    read_only: tuple[str, ...] = (),
) -> list[str]:
    lines = [
        "# --- ReadWritePaths：由 R1 登記表機械導出（permgen），勿手擴 ---",
        "# 每條後面列出它涵蓋的登記表資產；新增 durable state 時改登記表、重跑產生器。",
    ]
    for path, covered in owners.items():
        lines.append(f"#   涵蓋：{', '.join(covered) if covered else '（無）'}")
        lines.append(f"ReadWritePaths={path}")
    if read_only:
        lines += [
            "",
            "# --- ReadOnlyPaths：巢狀在上面某條 RWP **之內**、被收回唯讀的 enforcement 檔 ---",
            "# （#698）codex 的 $CODEX_HOME 整棵必須可寫（executor 才起得來），但樹裡的",
            "# root-owned hooks.json 不該連 mount 層都是可寫的。systemd 依路徑排序套用這些",
            "# bind mount，巢狀的唯讀條目後套 ⇒ 覆蓋外層的可寫性。",
            "# **刻意沒有 `-` 前綴**：目標不存在時本 unit 直接起不來——那正是要的行為，",
            "# 因為 sticky bit 不管「建一個還不存在的檔」，hooks.json 缺席時 job 植得進去，",
            "# 而一個植得進 hooks 的 job 不該起得來（與上方 RWP 的 fail-closed 立場一致）。",
        ]
        for path in read_only:
            lines.append(f"ReadOnlyPaths={path}")
    return lines


def build_manager_unit(
    scheme: UidScheme,
    layout: PathLayout = DEFAULT_LAYOUT,
    plan: PermissionPlan | None = None,
) -> SystemdUnit:
    """Manager 的 system-level unit（`User=<durable_state_owner>`）。"""
    plan = plan or generate_plan(scheme)
    account = scheme.durable_state_owner
    group = scheme.group_of(account)
    extras = layout.manager_extra_write_paths(account)
    owners = read_write_path_owners(plan, layout, account, extras)
    unit_name = f"{layout.instance}-manager.service"

    body = [
        f"# {'/etc/systemd/system/' + unit_name}",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}）——勿手改；改登記表後重跑：",
        f"#   python3 -m paulsha_cortex.trust_root unit {scheme.scheme_id} --manager",
        "",
        "[Unit]",
        "Description=cortex Manager (trust-root Phase 2b, system-level)",
        "Documentation=file://docs/superpowers/runbooks/trust-root-phase2b-setup.md",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "# 受信任服務身分。Manager 永不以 root 執行——root 操作只由 operator 手動 sudo。",
        f"User={account}",
        f"Group={group}",
        "",
        "# 部署樹在 root-owned 樹內，對本服務唯讀（ProtectSystem=strict 再加一層）：",
        "# 改寫 verifier／注入 sitecustomize.py／.pth 皆 EACCES（spec §R3）。",
        f"ExecStart={layout.exec_start}",
        "# 相對 log_dir（runtime/dispatch/<slice>）由此解析，必須落在 ReadWritePaths 內。",
        f"WorkingDirectory={layout.agents_root}",
        "",
        "# --- per-job clone 的來源樹（登記表 repo-source-tree，#623）：本服務**可寫** ---",
        f"#   {layout.repo_source_root}/<slug>"
        f"（{account} 擁有 0700，PSC_REPO_ROOT 指向它）。",
        "# 0817 裁決推翻了本票初版的 root-owned：`git fetch` 必須把 FETCH_HEAD 寫進**目標",
        "# repo**，而 #634 的成果回收正是 fetch 進來源樹；provision 那半邊的 `git branch -f`",
        "# 也是對來源樹的寫入。「Manager 唯讀」與「Manager 回收成果」互斥，取後者。",
        "# 隔離沒有變弱：不受信任的是 job 帳號，它們對這棵樹只有唯讀 ACL；而 Manager 本來",
        "# 就擁有 gate ledger／evidence／jobs.json——多這一棵樹不改變攻擊面。",
        f"# 跨擁有者的 git 操作由 {layout.gitconfig_of(account)} 的 safe.directory 放行",
        "# （root-owned、本帳號唯讀；登記表資產 manager-gitconfig，內容同樣由 permgen 產）。",
        "",
        "# EnvironmentFile 無 '-' 前綴＝fail-closed：檔案缺席即拒絕啟動，",
        "# MUST NOT 靜默落回 $HOME/.agents 預設（spec §R3 Scenario「刪除 EnvironmentFile」）。",
        f"EnvironmentFile={layout.env_file}",
        "# HOME 由 unit 指定；HOME 本身 root-owned，只有 cache 子目錄可寫。",
        "# 路徑由帳號名導出（`layout.home_of`）——換 scheme 時不會停在舊帳號的樹上。",
        f"Environment=HOME={layout.home_of(account)}",
        f"Environment=XDG_CACHE_HOME={layout.cache_of(account)}",
        "",
        "# --- 加固（spec §R3；逐項附理由供審查）---",
    ]
    body += _hardening_lines()
    body += [""]
    body += _rwp_lines(owners)
    body += [
        "",
        "Restart=on-failure",
        "RestartSec=5s",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    return SystemdUnit(
        unit_name=unit_name,
        install_path=f"/etc/systemd/system/{unit_name}",
        account=account,
        exec_start=layout.exec_start,
        environment_file=layout.env_file,
        read_write_paths=tuple(owners.keys()),
        content="\n".join(body) + "\n",
    )


#: monitor unit 的 persona 過濾集合。ReadWritePaths 只從這組 persona 在登記表上的
#: writer／spool-consumer 面導出——**這是 monitor unit 比 Manager unit 窄的機制**。
MONITOR_PRINCIPALS: frozenset[Principal] = frozenset({Principal.MONITOR})


def build_monitor_unit(
    scheme: UidScheme,
    layout: PathLayout = DEFAULT_LAYOUT,
    plan: PermissionPlan | None = None,
) -> SystemdUnit:
    """monitor 的 system-level unit（`User=<durable_state_owner>`，與 Manager 同帳號）。

    ## 為什麼 `User=` 與 Manager 同一個帳號

    三分方案的 UID 表寫的就是「`cortex-manager`：Manager ＋ monitor」（見
    :data:`THREE_WAY_SCHEME`）。裁決的判準是「injection 可達的任何進程都不得持有
    spawn 授權」——monitor **不跑任何模型程式碼**，與 Manager 同屬那條線的內側；
    而 `/var/lib/cortex/monitor` 是 `0700 cortex-manager`，monitor 若以別的身分跑
    就根本寫不進自己的 state 樹（#622 的實機症狀正是如此：舊 `--user` monitor 以
    操作者身分跑，只能靜默地繼續寫舊的 `~/.agents` 樹）。

    ## 為什麼 ReadWritePaths 必須比 Manager 窄

    同帳號**不代表**同可寫面。`required_write_targets()` 只按帳號過濾時，monitor
    會拿到 Manager 的全集（`coordinator/`、`specs/`、`control/`、`worktree/`…），
    等於把 monitor 的任何 bug／被餵入的惡意 GitHub 內容，變成對整棵 durable state
    的寫入面。因此這裡多帶一層 persona 過濾（:data:`MONITOR_PRINCIPALS`），讓
    ReadWritePaths 只由 **monitor 這個 persona 在登記表上的資產**導出：

    - `monitor-state-tree`／`monitor-work-items-snapshot`／`monitor-github-sync-cursor`
      （`writers=(MONITOR,)`）
    - `monitor-event-spool`（`INTERPROCESS` spool 的 trusted consumer——消費＝unlink，
      需要容器目錄的寫入權；#622 的「有生產端沒消費端」就是這一格）
    - `runtime-run-tree`（`writers=(MANAGER, MONITOR)`——monitor 的 unix socket）

    其餘全部落在 monitor 的 persona 面之外，因此**機械地**不會出現在這份 unit 上。
    要讓某條回來，唯一的辦法是改登記表把 monitor 登記成該資產的 writer／consumer，
    然後重跑產生器——沒有手擴的入口。

    ## ExecStart 的形態（#618／PR #619 對齊）

    與 Manager 同形：`<venv>/bin/cortex <verb>`，這裡的 verb 是**既有**的
    `cortex monitor`（`cli.py` 轉發到 `paulsha_cortex.monitor.__main__:main`，不帶
    `--once` 即長駐 `ProjectMonitorService.run_forever()`，符合 `Type=simple`）。
    #618 的教訓是「ExecStart 契約只存在於產生器端、CLI 沒跟上」，因此本 PR 一併加了
    契約鎖測試把兩端綁住；差別在 Manager 那次必須**補**一個 verb，monitor 這次
    verb 早就在（`cortex monitor` 是 README 與 `cli.py` 的公開介面），所以只補鎖。

    刻意**不用** `<venv>/bin/python -m paulsha_cortex.monitor`：那條路繞過 console
    script，等於在部署樹裡開第二種進入點形態——`cortex service run` 與
    `python -m ...` 之後會各自漂移，而 R-16 的 CLI help 對齊面只看得到前者。
    """
    plan = plan or generate_plan(scheme)
    account = scheme.durable_state_owner
    group = scheme.group_of(account)
    extras = layout.monitor_extra_write_paths(account)
    owners = read_write_path_owners(
        plan, layout, account, extras, principals=MONITOR_PRINCIPALS
    )
    unit_name = f"{layout.instance}-monitor.service"

    body = [
        f"# {'/etc/systemd/system/' + unit_name}",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}）——勿手改；改登記表後重跑：",
        f"#   python3 -m paulsha_cortex.trust_root unit {scheme.scheme_id} --monitor",
        "#",
        "# monitor 與 Manager 同帳號（UID 方案表：cortex-manager＝Manager ＋ monitor），",
        "# 但**可寫面不同**：下方 ReadWritePaths 只由 monitor persona 在 R1 登記表上的",
        "# writer／spool-consumer 面導出，是 Manager unit 的真子集。",
        "",
        "[Unit]",
        "Description=cortex Monitor (trust-root Phase 2b, system-level)",
        "Documentation=file://docs/superpowers/runbooks/trust-root-phase2b-setup.md",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "# 受信任服務身分。monitor 不跑任何模型程式碼，故與 Manager 同屬授權線內側；",
        f"# 也唯有同帳號才寫得進 0700 {account} 的 {layout.monitor_state_root}。",
        f"User={account}",
        f"Group={group}",
        "",
        "# 與 Manager 同形態的進入點：部署 venv 的 console script ＋ 既有 CLI verb",
        "# （`cortex monitor` → paulsha_cortex.monitor.__main__:main；不帶 --once 即長駐）。",
        "# 不寫 `python -m paulsha_cortex.monitor`——那會在部署樹裡開第二種進入點形態，",
        "# 與 `cortex service run` 各自漂移（#618 就是產生器與 CLI 單邊漂移的後果）。",
        f"ExecStart={layout.monitor_exec_start}",
        "# 落在 durable state 樹根（root-owned、對本服務唯讀）——monitor 的掃描目標一律",
        "# 由 config 指定絕對路徑，這裡只要一個必然存在、不隨 operator HOME 漂移的 cwd。",
        f"WorkingDirectory={layout.agents_root}",
        "",
        "# --- monitor 的掃描目標（登記表 repo-source-tree，#623）：**唯讀** ---",
        f"#   {layout.repo_source_root}/<slug>——**working checkout**（不是 bare），因為",
        "# monitor 掃的是工作樹裡的檔案（workstreams/*/todo.md…），bare 沒有工作樹。",
        "# ProtectSystem=strict 下讀是預設允許的，故不需要任何指令；而下方 ReadWritePaths",
        "# **不含**它——monitor 在登記表上不是它的 writer，掃描本來就只需要讀。",
        "# 注意這一條是 **persona 過濾**的成果，不是帳號的：0817 裁決後 Manager 是這棵樹的",
        "# writer，而 monitor 與 Manager 同帳號——檔案層兩者權限相同，是這份 unit 少了那條",
        "# ReadWritePaths 才讓 monitor 真的寫不進去。要拿回來只能改登記表，沒有手擴的入口。",
        "",
        "# EnvironmentFile 無 '-' 前綴＝fail-closed：檔案缺席即拒絕啟動，",
        "# MUST NOT 靜默落回 $HOME/.agents 預設。這正是 #622 的核心——舊 --user monitor",
        "# 的 PSC_MONITOR_STATE_ROOT 指著舊樹，起回來只會雙寫。與 Manager 共用同一份",
        "# instance-scoped env，兩個服務因此不可能解析到不同的 durable state 樹。",
        f"EnvironmentFile={layout.env_file}",
        "# HOME 由 unit 指定；HOME 本身 root-owned，只有 cache 子目錄可寫。",
        "# 路徑由帳號名導出（`layout.home_of`）——換 scheme 時不會停在舊帳號的樹上。",
        f"Environment=HOME={layout.home_of(account)}",
        f"Environment=XDG_CACHE_HOME={layout.cache_of(account)}",
        "",
        "# --- 加固（與 Manager unit 逐項同一份表；集合比對有測試守著，不得單邊漂移）---",
    ]
    body += _hardening_lines()
    body += [""]
    body += _rwp_lines(owners)
    body += [
        "",
        "Restart=on-failure",
        "RestartSec=10s",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    return SystemdUnit(
        unit_name=unit_name,
        install_path=f"/etc/systemd/system/{unit_name}",
        account=account,
        exec_start=layout.monitor_exec_start,
        environment_file=layout.env_file,
        read_write_paths=tuple(owners.keys()),
        content="\n".join(body) + "\n",
    )


def _job_unit_credential_lines(job_layout: "PathLayout", account: str) -> list[str]:
    """模板 unit 裡「本帳號有哪幾份登入態、各是什麼形狀」那一段（#685）。

    逐格由 `credential_placements()` 導出，**不是寫死的一段文字**：加一格憑證之後
    unit 的註解會自己長出來，而不是變成一份與登記表不同步的說明。
    """
    lines: list[str] = []
    for _asset_id, cred_account, path, credential in job_layout.credential_placements():
        if cred_account != account:
            continue
        if credential.shape is CredentialShape.IN_PLACE_FILE:
            lines += [
                f"#   [{credential.executor}] {path}",
                "#     單檔：**檔案**由本 job 帳號擁有（0600，token 過期可就地 refresh），",
                "#     **放它的目錄維持 root-owned** ⇒ 本帳號建不了新檔、刪不掉、也換不掉",
                "#     同目錄下的 root-owned hooks.json。下方 ReadWritePaths 只掛**那一個",
                "#     檔**，不是它的父目錄（明示的例外，見 IN_PLACE_CONTENT_WRITE_ASSETS）。",
                "#     憑證尚未落位時本 unit 會**起不來**（systemd 對不存在的",
                "#     ReadWritePaths 目標報錯）——刻意的 fail-closed：沒有登入態的 job",
                "#     本來就做不了事，在 exec 前失敗比走到呼叫模型那一步才 rc=127 好查。",
                "#     ⚠️ #686 實測：codex 需要 $CODEX_HOME **整棵**可寫，因此這個形狀下的",
                "#     codex 在本 unit 內**起不來**（見 runbook 4e-3、deferred 的 U-9）。",
            ]
        elif credential.shape is CredentialShape.HOME_STICKY_TREE:
            hooks = f"{path}/{credential.enforcement_leaf}"
            lines += [
                f"#   [{credential.executor}] {path}（root-owned ＋ sticky，#698 方案 A）",
                "#     可寫狀態樹：目錄由 **root** 擁有、帶 sticky bit（`chmod +t`），本",
                "#     帳號以一條具名 **access** ACL（`u:<本帳號>:rwx`）取得整棵的寫入權。",
                "#     ⇒ executor 起得來（#686：$CODEX_HOME 唯讀時 codex 連 in-process",
                "#       app-server 都初始化不了），而且——",
                f"#     ⇒ {hooks}（root:root 0644）本帳號**動不了**：",
                "#       unlink／rename 被 sticky 擋（非 owner 只動得了自己的檔）、改內容",
                "#       被它自己的 mode 擋（本帳號落在 `other` 位）。",
                "#     0818 的 R9 T3.9 就是在**沒有**這兩層時被實測攻破的（#698）——codex",
                "#     hooks 會執行命令 ⇒ 那是跨 job 持久化，不只是少一層防護。",
                "#     ⚠️ group 寫入權仍然是零（spec §R2 未放寬）；`ls -ld` 顯示的 group 位",
                "#       是 POSIX ACL 的 **mask**，驗證一律用 `getfacl`。",
                "#     ⚠️ 那個 hooks 檔**必須先存在**——sticky 不管「建一個還不存在的檔」。",
                "#       缺它時 R9 T3.9 會以「建得出新檔」翻紅（正確的紅字）。",
                "#     代價（R-6）：樹裡的 **token 葉檔**仍由本帳號擁有、仍可被它刪除或",
                "#     替換。那是刻意的——token 過期必須 refresh 得回來。",
            ]
        else:
            target = job_layout.credential_target_of(account, credential)
            leaf = credential.token_leaf or "（無單一 token 葉檔）"
            lines += [
                f"#   [{credential.executor}] {path} -> {target}",
                f"#     可寫狀態樹（token 葉檔：{leaf}）。symlink 由 root 擁有、放在",
                "#     root-owned 的 HOME 裡 ⇒ 本帳號**換不掉它的指向**；目標落在本帳號",
                "#     既有的 cache 底下 ⇒ **下方 ReadWritePaths 逐字不變、不新增任何",
                "#     可寫面**（那條 cache 本來就在，_minimize() 會吃掉子路徑）。",
                "#     代價（R-6）：這棵樹由本帳號擁有，樹裡的 token 葉檔因此**可被本",
                "#     帳號刪除或替換**——換到的是 executor 起不起得來。同一棵樹裡不得",
                "#     再放 root-owned 的 enforcement 檔（見 U-9）。",
            ]
    if not lines:
        lines.append("#   （本帳號在表上沒有任何登入態——它不跑模型 CLI。）")
    return lines


def _job_unit_workspace_lines(
    job_layout: "PathLayout", principal: Principal, account: str
) -> list[str]:
    """模板 unit 的「本 job 的工作區長什麼樣」那一段，由 :data:`registry.JOB_WORKSPACE_REACH` 導出（#710）。

    **這一段在 #710 之前是三份 unit 逐字共用的一塊硬寫死註解**，內容是 builder 的
    故事（「`git clone` 到 `<pool>/%i`，整個 clone 由本 job 帳號擁有，已在下方 RWP
    內」）。對 reviewer 與 gate 那兩份 unit，那段話**每一個子句都是假的**：它們的
    工作區不在 pool 底下、也不在自己的 `ReadWritePaths=` 裡。而對 builder 自己，
    「由本 job 帳號擁有」同樣不成立——Manager 沒有 `CAP_CHOWN`，那個動作從未發生過。

    陳舊的宣稱會**反向說謊**（#696）：讀到那段話的人會去找一個不存在的 chown 步驟，
    或者相信一條不存在的可達性。因此這一段改為由規則表逐 principal 產生——三份 unit
    的內容從此**必然不同**，而且各自等於它那一列宣告的機制。
    """

    reach = registry.job_workspace_reach_for(principal)
    lines = [
        f"# --- 工作區可達性（登記表 JOB_WORKSPACE_REACH：{reach.reach.value}，#710）---",
        "# shim 在**降權之後**才 `os.chdir(spec['working_directory'])`——那一步走不進去，",
        "# job 就死在它做任何事之前（#710 實機：`[Errno 13] Permission denied`）。",
        "# 「進得去」需要 mount 層（下方 ReadWritePaths／ProtectSystem）**與** DAC 層",
        "# 同時成立；本節講的是 DAC 那一半。",
    ]
    if reach.reach is registry.WorkspaceReach.PER_JOB_NAMED_ACL:
        lines += [
            f"#   {job_layout.repo_source_root}/<slug> → `git clone --no-hardlinks` 到",
            f"#   {job_layout.worktree_root}/%i。",
            f"#   那一格的 owner 是 **Manager**（不是本帳號），本帳號以**具名 ACL** 取得",
            f"#   `{reach.access_perms}`（＋ default `{reach.default_perms}`），由 Manager 在",
            "#   provision 當下遞迴套上（`coordinator/job_workspace.grant_workspace_acl`）。",
            "#   ⛔ **不是**「整個 clone 由本 job 帳號擁有」——那句話從 #623／#648 起寫在",
            "#      這裡，但沒有任何程式實作它，而且結構上做不到：`chown` 給另一個使用者",
            "#      需要 `CAP_CHOWN`，本部署的 Manager unit 帶 `CapabilityBoundingSet=`",
            "#      （空）。代價是 #710（builder job 第一次由 daemon 經正規路徑派出來就",
            "#      死在 chdir）。`setfacl` 由目錄 owner 執行、不需要任何 capability。",
            "#   ⚠️ ACL 下在 **per-job 那一格**，不在 pool 根：pool 根是三個 job 帳號共用",
            "#      的容器（0701），在它身上下 default ACL 會讓每個 job 帳號進得去每個",
            "#      job 的目錄（裁決 10-2 當場歸零）。",
            "#   ⚠️ 驗證一律 `getfacl`，判準是 `mask::` 與 `#effective:`——`chmod` 會重寫",
            "#      ACL mask，「ACL 行存在」證明不了有效權限（runbook 4e-2b）。",
            "#   來源樹（登記表 repo-source-tree）的 owner 是 Manager（0817 裁決），job",
            "#   帳號只拿到唯讀 ACL：讀得到、寫不進去，共用 object store 那條「builder 能",
            "#   寫 Manager 的樹」的路因此在 git 這一層就不存在；下方 ReadWritePaths",
            "#   **不含**來源樹。",
            f"#   **來源樹**那兩條逐字路徑由 {job_layout.gitconfig_of(account)} 的",
            "#   safe.directory 放行（root-owned、本帳號唯讀；登記表資產，內容由 permgen",
            "#   產生）。⛔ **那份靜態檔蓋不到上面這一格**——per-job clone 的路徑帶 `%i`，",
            "#      每個 job 不同，而 git 的 safe.directory 只認逐字相等的路徑（實測 git",
            "#      2.43：`<repos>/*` 仍被拒；字面 `*` 是 opt-out 不是授權）。這一格走",
            "#      下面那一節（#712）。",
        ]
    elif reach.reach is registry.WorkspaceReach.INHERITED_DEFAULT_ACL:
        lines += [
            "#   本帳號的工作區**不在** worktree pool 底下（那是 builder 的），而是由",
            "#   Manager 在下列既有的樹裡逐次開出來：",
        ]
        lines += [f"#     - 登記表 {pool_id}" for pool_id in reach.pool_asset_ids]
        lines += [
            f"#   那幾棵樹的**根**已帶本帳號的 default ACL（`{reach.access_perms}`，由登記表的",
            "#   readers 機械導出），Manager 在裡面建的每一格因此自動繼承 ⇒ 可達性已經成立，",
            "#   **本節不新增任何 ACL、也沒有任何執行期動作**。",
            "#   ⛔ 下方 ReadWritePaths **不含**這些樹：本帳號對工作區唯讀是刻意的——交付",
            "#      通道是 review-verdict-spool／dispatch-specs-tree，給工作區寫入權會把",
            "#      #628／#639 關掉的「被驗方在自己的工作區裡產生自己的證據」重新打開。",
            f"#   **來源樹**那兩條逐字路徑由 {job_layout.gitconfig_of(account)} 的",
            "#   safe.directory 放行（root-owned、本帳號唯讀；登記表資產，內容由 permgen",
            "#   產生）。⛔ **那份靜態檔蓋不到上面那幾格**——review worktree 的路徑帶動態",
            "#      段，而且它是一棵 linked worktree：實測 git 2.43，只給來源樹那兩條時",
            "#      對 worktree 自己的路徑仍是 `fatal: detected dubious ownership`。",
            "#      這一格走下面那一節（#712）。",
        ]
    else:  # POOL_OWNED_BY_JOB
        lines += [
            f"#   本帳號的工作區 pool（登記表 {reach.pool_asset_ids[0]}）的 **owner 就是",
            f"#   {account} 自己**（0700、零 ACL），per-job 那一格由本帳號在執行期自己建",
            "#   （`gate_ledger.snapshot_worktree()` 的目錄樹複製）⇒ 它天生擁有自己",
            "#   產出的每一個 inode。可達性在部署當下一次成立，執行期零動作、零 ACL。",
            "#   被驗的那棵樹（builder 的 clone）另以 `rX` 具名 ACL 授予，與 builder 的",
            "#   可寫面由**同一次** per-job setfacl 一起落地（登記表 repo-worktree 的",
            "#   readers 宣告了本帳號，#629）。",
        ]
    lines += _job_unit_git_trust_lines(job_layout, principal, account)
    return lines


def _job_unit_git_trust_lines(
    job_layout: "PathLayout", principal: Principal, account: str
) -> list[str]:
    """模板 unit 的「本 job 怎麼對自己的工作區跑 git」那一段，由
    :data:`registry.JOB_GIT_WORKSPACE_TRUST` 導出（#712）。

    為什麼要**另外**一段而不是併進工作區那一段：那是**兩層**，而 #712 就是「其中
    一層通了、另一層沒通」的實機證據——#710 把 ACL 補上、`getfacl` 實機確認
    `user:cortex-builder:rwx`／`mask::rwx` 之後，builder job 真的跑起來了，然後死在
    `fatal: detected dubious ownership`。把兩層寫成同一段，下一個讀的人就會以為
    「權限對了 ⇒ git 就跑得動」，而那正是本票要消滅的推論。
    """

    trust = registry.job_git_workspace_trust_for(principal)
    lines = [
        "",
        f"# --- 工作區的 git 信任（登記表 JOB_GIT_WORKSPACE_TRUST：{trust.trust.value}，#712）---",
        "# git 的 dubious-ownership 是 **owner 判準，不是權限判準**：ACL／mask 全部正確",
        "# 也照樣整個 repo 被拒（#712 實機：ACL 生效之後才露出這一條）。",
    ]
    if trust.trust is registry.GitWorkspaceTrust.PER_JOB_ENV:
        lines += [
            "#   本帳號的工作區由 **Manager** 建 ⇒ owner 是 Manager、本帳號是另一個 uid",
            "#   ⇒ git 必然看到跨 owner。放行**逐 job**下：Manager 算出這一格的絕對路徑，",
            "#   隨 spec 的 env 交給 shim（`GIT_CONFIG_COUNT=1`／",
            "#   `GIT_CONFIG_KEY_0=safe.directory`／`GIT_CONFIG_VALUE_0=<這一格>`），",
            "#   `coordinator/job_runner.git_workspace_trust_env()`。",
            "#   ⛔ 那條 env **只放行 `safe.directory` 一個鍵**（寫端與讀端共用",
            "#      `job_runner._reject_unsafe_git_config()`）：`GIT_CONFIG_*` 與 `git -c`",
            "#      同級，`alias.*`／`core.fsmonitor` 經它塞進來會**執行外部命令**——那正是",
            f"#      {job_layout.gitconfig_of(account)} 必須 root-owned 的理由，本管道不得",
            "#      成為它的繞法。",
            "#   ⛔ 也**不得**改成往靜態 .gitconfig 加一條萬用字元：實測 git 2.43，",
            "#      `<repos>/*` 仍被拒，而字面 `*` 等於對這個帳號整個關掉該保護（opt-out，",
            "#      不是授權）。",
        ]
    else:
        lines += [
            "#   per-job 那一格由**本帳號自己**建（目錄樹複製）⇒ 它擁有自己產出的每一個",
            "#   inode（含 `.git`）⇒ git 看到的 owner 就是當下的 uid ⇒ **零動作、零 env**。",
            "#   這條靠的是 pool 根的 owner 位；owner 一旦漂走（例如「比照 dispatch pool」",
            "#   改成 Manager-owned），git 信任會跟著工作區可達性一起塌 —— registry 的",
            "#   import 期斷言擋的就是那個漂移。",
        ]
    return lines


def build_job_unit(
    scheme: UidScheme,
    layout: PathLayout = DEFAULT_LAYOUT,
    principal: Principal = Principal.BUILDER,
    plan: PermissionPlan | None = None,
    profile: HardeningProfile = DEFAULT_HARDENING_PROFILE,
) -> SystemdUnit:
    """降權 job 的**模板** unit（`cortex-job@.service`）。

    這是降權/提權分界線的另一半：`User=` 在 root-owned 的 unit 檔裡**硬寫死**，
    呼叫端（Manager）只能給 instance 名，**無法選擇 UID、無法夾帶任何屬性**。
    polkit 規則只放行這個模板的實例（見 `build_polkit_rule`）。

    `ExecStart=` 同樣固定：永遠是 root-owned 的 shim（`build_job_shim()` 產出），
    per-job 的命令／worktree／env／log 路徑改由 Manager-owned 的 spec spool
    （登記表資產 `job-spec-spool`）傳遞，shim 讀完才 exec。

    **為什麼不用 `StandardOutput=append:<log>`**：`file:`／`append:` 的目標檔是由
    **PID 1（root）** 在降權之前開啟的；路徑裡只要有任何一段由 Manager 帳號掌控
    （spec spool 或 log 目錄都是），Manager 就能在該位置放一個 symlink 讓 root 對
    任意檔案 append——那是把「Manager 不具 root」這條裁決整個賣掉。模板是**單一
    靜態檔**、per-job 的 log 路徑又必須維持 harvest 既有的
    `<log_dir>/<slice>.jsonl`（`%i` 推不出來），兩者無法同時成立。因此 log 導引改由
    **shim 在已降權之後**依 spec 的 `log_path` 自行接管（見 `coordinator/job_shim.py`），
    unit 這層只留 journal 給 shim 讀 spec 失敗時的診斷。

    **`profile`（#643）**：加固剖面。同一個 principal 會產出**兩份** unit——
    `cortex-job@.service`（strict）與 `cortex-job-jit@.service`（jit）——兩份共用
    同一張 `_HARDENING` 表，只在 `PROFILE_DIVERGENCE_KEYS` 那一項分岔。哪個 job 用
    哪一份由 **executor** 決定（:func:`executor_hardening_profile`），而 executor 是
    Manager 的 dispatch 決定；job 自己（spec 也好、worktree 內容也好）碰不到這個選擇。

    **`principal`（#615 M2）**：`BUILDER` ＋ `REVIEWER` 兩個角色各兩份剖面＝**四份**
    unit（見 :data:`DOWNGRADED_JOB_PRINCIPALS`）。四份共用**同一張** `_HARDENING` 表
    與**同一條** `ReadWritePaths` 導出規則——角色之間的全部差異都是「帳號」帶出來的
    （`User=`／`Group=`／HOME／cache／登記表上該帳號的可寫面），本函式沒有任何一行
    `if principal is …`。planner 不另產一份：它與 reviewer 同帳號，見
    :data:`JOB_PRINCIPAL_PERSONAS`。
    """
    plan = plan or generate_plan(scheme)
    account = scheme.resolve(principal)
    if account is None:
        raise ValueError(f"principal 未映射到帳號: {principal}")
    group = scheme.group_of(account)
    # per-job 路徑在模板 unit 中以 systemd 的 %i 表示。
    job_layout = layout.with_job_segment("%i")
    extras = job_layout.job_extra_write_paths(account)
    owners = read_write_path_owners(
        plan, job_layout, account, extras, retired=RETIRED_JOB_WRITE_ASSETS
    )
    stem = job_unit_stem(layout, principal, profile)
    unit_name = f"{stem}@.service"
    profile_users = sorted(
        name
        for name, profile_id in EXECUTOR_HARDENING_PROFILE.items()
        if profile_id == profile.profile_id
    )
    # 產生本檔的旗標必須跟著角色走：註解裡寫著「重跑用 --job」卻產出 reviewer／gate
    # 的 unit，會讓 operator 照抄之後拿到**另一份** unit 覆蓋掉這一份。
    unit_flag = JOB_UNIT_CLI_FLAG[principal]

    body = [
        f"# {'/etc/systemd/system/' + unit_name}",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}, profile={profile.profile_id}）",
        "# ——勿手改；重跑：",
        f"#   python3 -m paulsha_cortex.trust_root unit {scheme.scheme_id} {unit_flag}"
        + (f" --profile {profile.profile_id}" if profile is not DEFAULT_HARDENING_PROFILE else ""),
        "#",
        "# 降權/提權分界線：User= 在本 root-owned 檔內硬寫死。Manager"
        f"（{scheme.durable_state_owner}）",
        f"# 只能 `systemctl start {stem}@<id>.service`，**不能**選 UID、不能傳屬性。",
        "#",
        f"# === 加固剖面：{profile.profile_id}（#643 per-executor 剖面）===",
    ]
    if principal is Principal.GATE:
        # gate 不跑任何模型 CLI，列 executor 名單只會誤導（讀的人會以為這份 unit 是
        # 給 `agy`／`claude` 用的）。剖面來源也不同：operator 平面，不是 executor。
        body += _wrap_comment(
            "適用對象：gate 執行身分（#629）——它不跑模型 CLI，因此沒有「適用 "
            "executor」可言。剖面由 operator 的 PSC_GATE_HARDENING_PROFILE 決定"
            "（宣告 gate 命令與宣告它需要哪份剖面是同一個人、同一個平面的決定）；"
            "宣告 node 型 gate（npm test）時必須顯式打出 jit，否則 V8 會直接崩。"
        )
    else:
        body += [
            f"# 適用 executor：{'、'.join(profile_users) if profile_users else '（無）'}",
        ]
    body += _wrap_comment(f"理由：{profile.rationale}")
    for loss in profile.accepted_loss:
        body += _wrap_comment(f"⚠ 本剖面接受的代價：{loss}")
    body += [
        "# 剖面**不由 job 決定**：模型 job 的對應表由 permgen.EXECUTOR_TOOLS 的",
        "# needs_node 機械導出，executor 則是 Manager 的 dispatch 決定；gate 的剖面",
        "# 由 operator 平面決定（#629）。兩者共同點是 job spec 結構性禁止攜帶任何",
        "# 剖面欄位（job_runner.SPEC_FORBIDDEN_KEYS，寫端與讀端各擋一次）。",
        "",
        "[Unit]",
        "Description=cortex headless job %i (downgraded, trust-root Phase 2b, "
        f"hardening={profile.profile_id})",
        f"After={layout.instance}-manager.service",
        "# job 為一次性：結束即回收 unit 狀態，不留可被重用的殘骸。",
        "# `CollectMode` 是 **[Unit] 的鍵**，不是 [Service] 的（#645 附帶修正）——放錯段",
        "# systemd 只會 `Unknown key name 'CollectMode' in section 'Service', ignoring.`，",
        "# 於是「失敗的 instance 自動回收」這個用意整個沒生效，失敗殘骸會一直掛在",
        "# `systemctl list-units --failed` 上、擋住同名 instance 的下一次 start。",
        "CollectMode=inactive-or-failed",
        "",
        "[Service]",
        "Type=exec",
        "# 硬寫死的 job 身分——這行是整套降權的唯一 UID 來源。",
        f"User={account}",
        f"Group={group}",
        "",
        "# ExecStart 也是固定的：永遠是 root-owned 的 shim，呼叫端連命令列都給不了。",
        "# per-job 執行規格由 Manager 原子寫入 spec spool（Manager-owned，job 帳號唯讀）：",
        f"#   {job_layout.job_spec_spool_for(principal)}/%i.json",
        "# **本 principal 專屬的 spool**（#657）：容器 <…>/job-specs/ 對本帳號只有",
        "# traverse（--x），讀得到的只有自己這一格。shim 是 systemd 套完上面的 User=",
        "# **之後**才執行的，它以 job 身分讀 spec——所以「這個身分讀得到哪個 spool」",
        "# 必須是這份 root-owned unit 上可逐字稽核的一行，不是一組共用目錄的 ACL 交集。",
        "# job 因此無法改寫自己的命令列，也無法為下一個 job 埋伏。",
        f"ExecStart={job_layout.job_shim} %i",
        "# 工作目錄：shim 會依 spec 的 working_directory 再 chdir 到該 job 的 worktree；",
        "# 這裡只給恆存在的 pool 根（0701＝可 traverse、不可列目錄），避免 unit 因",
        "# per-job 目錄尚未建立而在 exec 前就失敗（那會讓 log 裡沒有任何線索）。",
        f"WorkingDirectory={job_layout.worktree_root}",
    ]
    body += _job_unit_workspace_lines(job_layout, principal, account)
    body += [
        "# --- 成果回收（登記表 commit-spool，#623／#634）---",
        f"#   {job_layout.commit_spool_root}/%i/：job 在**自己的** clone `git bundle create`",
        "# 寫進這一格，Manager 再從那個 bundle **檔案** fetch——Manager 全程不碰 job 的樹。",
        "# 權限是 `wx` 無 `r`：寫得進自己那格、讀不到別人的 bundle。producer 只有 builder，",
        "# 因此這條只會出現在 builder 的模板 unit（RWP 由登記表機械導出，不是寫死的）。",
        "# --- verdict 通道（登記表 review-verdict-spool，#599／#638）---",
        f"#   {job_layout.review_verdict_spool_root}/<reviewer job id>/：reviewer 把 verdict",
        "# 寫進自己那一格，Manager 收割後把**目錄**封口。同樣是 `wx` 無 `r`，因此它只會",
        "# 出現在 reviewer／planner 的模板 unit——builder 完全不在該資產的 writer 面，",
        "# 「builder 代寫 reviewer 的 verdict」這條 spec §3 最短攻擊路徑在 OS 層被關掉。",
        "# --- executor toolchain（登記表 executor-toolchain，#640）---",
        f"#   {job_layout.toolchain_root}：四個模型 CLI 的落點，root-owned 0755——",
        "# 本 job 帳號**唯讀＋可執行**。ProtectSystem=strict 讓 /opt 唯讀，但唯讀只擋",
        "# 寫入，讀取與執行完全不受影響；下方 ReadWritePaths 因此機械地不含它",
        "# （writer 只有部署身分）。",
        "# --- PATH：**兩層都補**（#679）---",
        "# #679 之前這份 unit 刻意不寫 PATH，理由是「shim 以 execvpe(argv[0], argv,",
        "# spec['env']) 整份換掉環境，寫在 unit 上會被丟掉」。那個理由對了一半、也因此",
        "# 錯得很貴：spec 的 env 確實是 job 的完整環境，但產生那份 env 的",
        "# `job_runner.build_job_env()` 當時是 **fail-open** 的——Manager 端沒宣告",
        "# PSC_*_PATH 就整個不寫 PATH 這個鍵，execvpe 於是退回 os.defpath",
        "# （`:/bin:/usr/bin`），`codex` 靜默解到系統層那份舊 CLI。實機部署的",
        "# EnvironmentFile 三個變數一個都沒有，於是三個角色全中。",
        "# 現在是兩層：",
        "#   1. spec 的 env——`build_job_env()` 對未宣告 fail-closed（不再靜默省略）；",
        "#   2. **這一行**——shim 在 spec 的 env 沒有 PATH 時，改用本 unit 給的這一份",
        "#      （root-owned、可逐字稽核），兩層都缺才 fail-closed。",
        "# 第 2 層不是 fail-open：它退回的是**更可信**的來源（root-owned unit），不是",
        "# 猜一個預設值。它同時涵蓋「手工組 spec 繞過產生器」（#645 的同型前例）。",
        f"Environment=PATH={job_layout.job_path_value()}",
        "# 值與 Manager 端 root-owned EnvironmentFile 裡的這個變數同源（job 改不了）：",
        f"#   {JOB_PATH_ENV_BY_PRINCIPAL[principal]}={job_layout.job_path_value()}",
        "# toolchain 排最前面是必要的：系統層可能另有一份同名但舊很多的 CLI（實機盤點",
        "# 到兩份 codex 差 100 個以上小版本），排後面會被它蓋掉，而症狀是「跑得起來但",
        "# 版本不是你以為的那個」。",
        "# ⚠️ 驗「job 會解到哪一份 CLI」的檢查**不得自帶 PATH**（#679 的核心教訓）：",
        "#    `unit_replica_properties()` 會把上面這一行機械帶進 --property=，複本因此",
        "#    連「production 供應什麼／不供應什麼」都一起複製。再加一個 --setenv=PATH=",
        "#    就等於驗證環境供應了 production 不供應的東西，結構上永遠驗不出這個缺陷。",
        "# --- executor 登入態（登記表的 per-(account, executor) 憑證表，#640／#685）---",
        "# 形狀由 permgen.CREDENTIALED_ACCOUNTS × EXECUTOR_CREDENTIALS 決定，**不是**",
        "# 本檔的字面量；本帳號被核可的每一格逐條列在下面。",
    ]
    body += _job_unit_credential_lines(job_layout, account)
    body += [
        "# ⚠️ 三條 symlink 的路徑**全部以 $HOME 為根**，而模板模式下 shim 以",
        "#    os.execvpe 整份換掉環境 ⇒ 下面那行 Environment=HOME= **到不了模型**",
        "#    （#686 實機更正）。HOME 只能來自 spec 的 env，也就是 Manager 端",
        f"#    root-owned EnvironmentFile 的 {JOB_HOME_ENV_BY_PRINCIPAL[principal]}：",
        f"#      {JOB_HOME_ENV_BY_PRINCIPAL[principal]}={job_layout.job_home_value(account)}",
        "#    未宣告時症狀是 `$HOME is not defined`／`Not logged in`——與「憑證沒放好」",
        "#    長得一模一樣，因此憑證面與這一行**必須一起成立**（runbook 第 5-5c 步）。",
        "# shim 讀 spec 的唯一合法來源：這一行在 root-owned 的 unit 檔裡，",
        "# 因此持 spawn 授權的帳號也改不掉 spec 要從哪個目錄讀。shim 對未設此",
        "# 變數的情況 fail-closed（不猜、不落回 $HOME 推導的預設）。",
        f"Environment=PSC_JOB_SPEC_SPOOL={job_layout.job_spec_spool_for(principal)}",
        "# job 永不取得 gh token：GitHub 寫入由 Manager 代理（D1 outbox）。",
        "Environment=GH_TOKEN=",
        "Environment=GITHUB_TOKEN=",
        f"Environment=HOME={job_layout.home_of(account)}",
        f"Environment=XDG_CACHE_HOME={job_layout.cache_of(account)}",
        "",
        f"# --- 加固（與 Manager 同一張 _HARDENING 表；剖面={profile.profile_id}）---",
        "# 兩份 job unit 共用這張表，只在下方以 ※ 標出的那一項分岔；",
        "# 日後往表裡加一項，兩份 unit 會自動同時拿到。",
    ]
    body += _hardening_lines(profile)
    body += [""]
    read_only = enforcement_read_only_paths(job_layout, account, tuple(owners.keys()))
    body += _rwp_lines(owners, read_only)
    body += [
        "",
        "# job 為一次性，不自動重啟（`CollectMode` 在上方 [Unit] 段）。",
        "Restart=no",
        "# 刻意**不**用 StandardOutput=append:<log>——那個檔由 PID 1（root）在降權前開啟，",
        "# 路徑中只要有一段由 Manager 帳號掌控就成了 root-follows-symlink 的提權面。",
        "# job 的 JSONL log 由 shim 在**已降權之後**依 spec 的 log_path 自行接管；",
        "# 這裡的 journal 只承接 shim 讀 spec 失敗（尚未接管前）的診斷輸出。",
        "StandardOutput=journal",
        "StandardError=journal",
    ]
    return SystemdUnit(
        unit_name=unit_name,
        install_path=f"/etc/systemd/system/{unit_name}",
        account=account,
        exec_start=f"{job_layout.job_shim} %i",
        environment_file=None,
        read_write_paths=tuple(owners.keys()),
        read_only_paths=read_only,
        content="\n".join(body) + "\n",
        hardening_profile=profile.profile_id,
    )


# ---------------------------------------------------------------------------
# 降權 shim（root-owned，模板 unit 的固定 ExecStart）
# ---------------------------------------------------------------------------

#: shim 真正的實作模組。**刻意不是 heredoc 產出的一大段程式碼**：shim 要做的事
#: （驗 instance 名／驗 spec 檔不是 symlink／驗 schema／接管 log／chdir／execve）
#: 每一條都是可測的邏輯，塞進字串就只剩「字串比對」這種驗收方式。把邏輯放進
#: repo 內的模組，它跟其他程式碼一樣被單元測試、被 lint、被 review；permgen 只
#: 產出那支 3 行的 root-owned 啟動 stub。兩者都落在 root-owned 的部署樹裡，
#: 「job 改不了自己執行的第一支程式」這條性質完全不變。
JOB_SHIM_MODULE = "paulsha_cortex.coordinator.job_shim"


@dataclass(frozen=True)
class ShimScript:
    """產生出來的 shim stub：**只有內容字串**，本模組不寫任何系統路徑。"""

    install_path: str
    interpreter: str
    module: str
    mode: int
    owner: str
    group: str
    content: str

    @property
    def mode_str(self) -> str:
        return format(self.mode, "04o")

    def to_dict(self) -> dict[str, object]:
        return {
            "install_path": self.install_path,
            "interpreter": self.interpreter,
            "module": self.module,
            "mode": self.mode_str,
            "owner": self.owner,
            "group": self.group,
            "content": self.content,
        }

    def commands(self) -> list[str]:
        """安裝命令字串（**只回傳字串，不執行**）。"""
        return [
            f"chown {self.owner}:{self.group} {self.install_path}",
            f"chmod {self.mode_str} {self.install_path}",
        ]


def build_job_shim(
    scheme: UidScheme = DEFAULT_SCHEME,
    layout: PathLayout = DEFAULT_LAYOUT,
) -> ShimScript:
    """產生 `<deploy_root>/bin/cortex-job-shim` 的內容（root-owned 啟動 stub）。

    stub 只做一件事：以部署 venv 的 interpreter 執行 :data:`JOB_SHIM_MODULE`，把
    模板 unit 傳進來的 `%i`（instance 名）原封不動交過去。**不解析參數、不組命令、
    不碰 spec 檔**——所有判斷都在那個模組裡，這裡沒有可被注入的表面。

    interpreter 寫成部署 venv 的絕對路徑而不是 `/usr/bin/env python3`：後者會走
    job 帳號的 `PATH`，等於讓 job 決定用哪個 interpreter 執行 root-owned 的 shim。
    """
    account = scheme.deploy_account
    interpreter = f"{layout.venv_root}/bin/python3"
    body = [
        "#!/bin/sh",
        f"# {layout.job_shim}",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}）——勿手改；重跑：",
        f"#   python3 -m paulsha_cortex.trust_root shim {scheme.scheme_id}",
        "#",
        "# root-owned、mode 0755：Manager 與 job 帳號皆唯讀。這是模板 unit 固定的",
        f"# ExecStart=，因此持 spawn 授權的帳號也換不掉 job 執行的第一支程式。",
        "#",
        f"# $1 ＝ systemd 模板實例名（%i）。spec 由此推導：",
        f"#   $PSC_JOB_SPEC_SPOOL/$1.json（Manager 寫、job 唯讀）",
        "# spool 根**只**來自模板 unit 的 Environment=PSC_JOB_SPEC_SPOOL=，而那是",
        f"#   {layout.job_spec_spool_root}/<principal>（#657：一個降權身分一格）。",
        "# 本 shim 對三個角色逐字相同——身分與 spool 都由 root-owned 的 unit 決定。",
        "set -eu",
        f'exec "{interpreter}" -m {JOB_SHIM_MODULE} "$@"',
    ]
    return ShimScript(
        install_path=layout.job_shim,
        interpreter=interpreter,
        module=JOB_SHIM_MODULE,
        mode=0o755,
        owner=account,
        group=scheme.group_of(account),
        content="\n".join(body) + "\n",
    )


# ---------------------------------------------------------------------------
# 服務／job 帳號的 root-owned `.gitconfig`（per-job clone 的必要條件，#623）
# ---------------------------------------------------------------------------

#: 需要一份 root-owned `.gitconfig` 的 persona → 登記表 asset_id。
#: reviewer 與 planner 共用同一個帳號（三分定案），故共用同一份檔，由 REVIEWER 代表；
#: Manager 與 monitor 同樣共用一個帳號與 HOME，由 MANAGER 代表。
ACCOUNT_GITCONFIG_ASSETS: Mapping[Principal, str] = {
    Principal.BUILDER: "builder-gitconfig",
    Principal.REVIEWER: "reviewer-planner-gitconfig",
    Principal.MANAGER: "manager-gitconfig",
}

#: 各 persona 對應的 CLI 旗標（`trust_root gitconfig … <flag>`）。與
#: :data:`ACCOUNT_GITCONFIG_ASSETS` 同一張表導出，產出的「重跑這行」註解因此不會漂移。
ACCOUNT_GITCONFIG_FLAGS: Mapping[Principal, str] = {
    Principal.BUILDER: "--builder",
    Principal.REVIEWER: "--reviewer-planner",
    Principal.MANAGER: "--manager",
}

#: `.gitconfig` 的 mode。**0644 而非 0600**：檔案 root 擁有、讀取的帳號要讀得到，
#: 與 `*-codex-hooks`（同樣 root-owned、同樣落在帳號 HOME 下）逐位元相同。
ACCOUNT_GITCONFIG_MODE = 0o644


class UnresolvedSourceRepoError(ValueError):
    """layout 沒宣告任何來源 repo——`.gitconfig` 產不出有用的 `safe.directory`（#623）。

    fail-closed 而不是輸出一份空的 `[safe]` 段：空的檔案照樣裝得起來、服務照樣起得來，
    然後**每一個 job 在第一次 `git clone` 時失敗**（`fatal: detected dubious ownership`），
    而症狀出現的位置離原因很遠——正是 #623 要消滅的那一類缺口。
    """

    def __init__(self, layout_hint: str) -> None:
        super().__init__(
            "permgen 拒絕產生 .gitconfig：layout 未宣告任何來源 repo slug。\n"
            f"來源樹容器：{layout_hint}\n"
            "  指定：--source-repo <slug>（可重複）  或  env PSC_SOURCE_REPO_SLUGS=<slug>[,<slug>…]\n"
            "\n"
            "為何不能省略、也不能用萬用字元：git 的 `safe.directory` 只認**逐字相等**的\n"
            "路徑或字面 `*`（實測 git 2.43：`<repos>/*` 仍被拒），而字面 `*` 等於對這個\n"
            "帳號整個關掉 dubious-ownership 保護——那是 opt-out，不是授權。\n"
            "slug 即來源樹底下的目錄名：`<repo_source_root>/<slug>`（例如 repo 的名字）。"
        )


@dataclass(frozen=True)
class GitConfigFile:
    """產生出來的 `.gitconfig`：**只有內容字串與結構化欄位**，本模組不寫任何路徑。"""

    install_path: str
    #: 讀這個檔的帳號（`$HOME` 就是它的 HOME）。
    account: str
    owner: str
    group: str
    mode: int
    #: 被授信的來源 repo 絕對路徑（逐字寫進 `safe.directory`）。
    safe_directories: tuple[str, ...]
    content: str

    @property
    def mode_str(self) -> str:
        return format(self.mode, "04o")

    def to_dict(self) -> dict[str, object]:
        return {
            "install_path": self.install_path,
            "account": self.account,
            "owner": self.owner,
            "group": self.group,
            "mode": self.mode_str,
            "safe_directories": list(self.safe_directories),
            "content": self.content,
        }

    def commands(self) -> list[str]:
        """安裝命令字串（**只回傳字串，不執行**）。"""
        return [
            f"chown {self.owner}:{self.group} {self.install_path}",
            f"chmod {self.mode_str} {self.install_path}",
        ]


def build_account_gitconfig(
    scheme: UidScheme = DEFAULT_SCHEME,
    layout: PathLayout = DEFAULT_LAYOUT,
    principal: Principal = Principal.BUILDER,
) -> GitConfigFile:
    """產生某個帳號 HOME 下那份 root-owned `.gitconfig` 的內容（#623）。

    三份同構的產物共用本函式（見 :data:`ACCOUNT_GITCONFIG_ASSETS`）：兩個 job 帳號
    各一份，**Manager 帳號一份**。Manager 那份不是「順手也給一個」——它與 job 那兩份
    是同一條必要性，只是操作方向相反（見下）。

    ## 為什麼這個檔是必要的

    #623 裁決把 job 工作區從 `git worktree` 改成 **per-job 完整 clone**（實測：共用
    git object store 與三分隔離互斥——builder 要 commit 就得能寫 object store）。
    來源樹（登記表資產 `repo-source-tree`）與讀它的帳號**不同 owner**時，git 的
    dubious-ownership 保護會直接擋下操作：

        fatal: detected dubious ownership in repository at '<來源樹>/<slug>'

    唯一的解是 `safe.directory`，而它**必須由 root 放進該帳號的 HOME**——那些 HOME
    都是 root-owned，帳號自己放不了這個檔。這正是登記表既有的 `*-codex-hooks`
    （root-owned、在帳號 HOME 下）同一個模式，不需要新概念。

    ## 為什麼每個 repo 是**兩條**值

    實測：從**非 bare** 的來源 clone 時 git 檢查的是 `<repo>/.git`，而 `git -C <repo>`
    這類對工作樹本身的操作報的是工作樹根。只給一條會讓另一半的操作在完全不同的時機
    才失敗。兩條的推導在 `PathLayout.source_repo_safe_directories()`。

    ## 為什麼逐個列出來源 repo，而不是 `<repos>/*` 或 `*`

    - git 的 `safe.directory` **不支援目錄萬用字元**（實測 git 2.43：值寫成
      `<repos>/*` 時 clone 仍被拒），只認逐字相等的路徑或字面 `*`；
    - 字面 `*` 等於對這個帳號整個關掉該保護——那是 opt-out，不是授權，且會讓「這個
      帳號被授信的來源有哪些」這件事在檔案裡完全看不出來。

    因此來源 repo 清單是**部署決定**，由 `layout.source_repo_slugs` 於產生當下注入
    （比照 #626 的部署決定型 principal），未宣告即 :class:`UnresolvedSourceRepoError`。

    ## ⛔ 這份檔涵蓋的**只有來源樹**（#712 更正）

    本函式的產物是 `<repos>/<slug>` 與 `<repos>/<slug>/.git` 那兩條逐字路徑，如此
    而已。它**不涵蓋、也裝不下** job 自己的工作區：

    - builder 的 per-job clone 是 `<worktree pool>/<job-id>`；
    - reviewer 的 review worktree 是 `<repos>/<slug>/.psc-review-worktrees/<…>`
      （而且是 linked worktree——實測 git 2.43，只給來源樹那兩條時，對 worktree
      **自己的路徑**仍是 `fatal: detected dubious ownership`）。

    兩者的路徑都帶動態段，而上一節那條「只認逐字相等或字面 `*`」正是它裝不下的
    理由。`builder-gitconfig`／`reviewer-planner-gitconfig` 兩則 `RunDependency`
    的 note 曾逐字宣稱涵蓋 per-job clone，而那是假的——代價是 #712（#710 的 ACL
    補上、builder job 真的跑起來之後，才在 `git bundle create` 上炸出來）。

    per-job 那一格改由**每次派工的 spec env** 逐 job 放行（登記表
    `registry.JOB_GIT_WORKSPACE_TRUST` ↔
    `coordinator/job_runner.git_workspace_trust_env()`），與本檔**並存、不互相取代**：
    來源樹是部署期常數，工作區是逐 job 的。
    """
    account = scheme.resolve(principal)
    if account is None:
        raise ValueError(f"principal 未映射到帳號: {principal}")
    if not layout.source_repo_paths():
        raise UnresolvedSourceRepoError(layout.repo_source_root)
    safe_dirs = layout.source_repo_safe_directories()
    install_path = layout.gitconfig_of(account)
    asset_id = ACCOUNT_GITCONFIG_ASSETS.get(principal, "builder-gitconfig")
    flag = ACCOUNT_GITCONFIG_FLAGS.get(principal, "--builder")
    slug_args = " ".join(f"--source-repo {slug}" for slug in layout.source_repo_slugs)
    body = [
        f"# {install_path}",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}）——勿手改；重跑：",
        f"#   python3 -m paulsha_cortex.trust_root gitconfig {scheme.scheme_id} "
        f"{flag} {slug_args}",
        "#",
        f"# 登記表資產 `{asset_id}`：root 擁有、mode "
        f"{format(ACCOUNT_GITCONFIG_MODE, '04o')}、{account} **唯讀**。",
        "# HOME 本身也是 root-owned，因此該帳號既改不了這個檔，也放不了自己的版本",
        "# ——.gitconfig 可指定 core.fsmonitor／alias.* 這類會執行外部命令的鍵。",
        "#",
        "# 為什麼需要它：#623 把 job 工作區從 git worktree 改為 per-job 完整 clone；",
        "# 來源樹與讀它的帳號不同 owner 時，git 的 dubious-ownership 保護會擋下操作",
        "# （fatal: detected dubious ownership in repository at ...）。",
        "#",
        "# 為什麼每個 repo 兩條：從**非 bare** 來源 clone 時 git 檢查的是 <repo>/.git，",
        "# 而 `git -C <repo> …` 報的是工作樹根——兩個位置就是兩條逐字的值。",
        "#",
        "# 為什麼逐個列出而不是萬用字元：git 的 safe.directory 只認逐字相等的路徑或",
        "# 字面 `*`（實測 git 2.43：`<repos>/*` 仍被拒），而字面 `*` 等於對這個帳號",
        "# 整個關掉該保護——那是 opt-out，不是授權。",
        "#",
        "# ⛔ 涵蓋範圍**只有下列來源樹路徑**（#712）：job 自己的工作區（builder 的",
        "# per-job clone、reviewer 的 review worktree）路徑帶動態段，這份靜態檔按上面",
        "# 那條規則就裝不下它們。那一格由每次派工的 spec env 逐 job 放行",
        "# （registry.JOB_GIT_WORKSPACE_TRUST ↔ job_runner.git_workspace_trust_env()），",
        "# 與本檔並存、不互相取代。",
        "[safe]",
    ]
    body += [f"\tdirectory = {path}" for path in safe_dirs]
    return GitConfigFile(
        install_path=install_path,
        account=account,
        owner=scheme.deploy_account,
        group=scheme.group_of(scheme.deploy_account),
        mode=ACCOUNT_GITCONFIG_MODE,
        safe_directories=safe_dirs,
        content="\n".join(body) + "\n",
    )


_assert_git_workspace_trust_matches_the_gitconfig()


# ---------------------------------------------------------------------------
# executor toolchain 的落位計畫（#640）
# ---------------------------------------------------------------------------

def build_toolchain_plan(
    scheme: UidScheme = DEFAULT_SCHEME,
    layout: PathLayout = DEFAULT_LAYOUT,
) -> list[str]:
    """產生部署樹 toolchain 的落位步驟（**只回傳字串，絕不執行**）。

    分工：**權限**由登記表經 `plan_to_commands()` 產出（`executor-toolchain` 那一節），
    本函式產的是**內容落位**——哪一支程式用哪種方式搬進 `<deploy_root>/toolchain`，
    比照 `build_job_shim()`／`build_account_gitconfig()` 的定位。

    **#661：涵蓋範圍由「四個 executor」擴為 `TOOLCHAIN_PROGRAMS`**（executor ∪ 非
    executor）。原本的盤點漏掉 `srt` 與 `openspec`，兩者同樣住在 operator 的 HOME
    底下、`ProtectHome=yes` 之後同樣不可達，而症狀分別是 doctor 的 `review-sandbox`
    FAIL 與 ship 段的 archive 失敗。

    來源路徑刻意留成 shell 變數：那是 operator 機器上的位置（nvm 樹／`~/.local/bin`），
    產生器猜不到也不該猜。但**來源的判準**是固定的，寫進輸出裡：一律取 operator
    **實際在用的那一份**（`command -v` 解出來的），不是另外裝一份系統的。
    """
    tail = ", ".join(JOB_PATH_SYSTEM_TAIL)
    lines = [
        f"# {layout.toolchain_root} —— 部署樹 toolchain（登記表資產 executor-toolchain）",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}）——勿手改；重跑：",
        f"#   python3 -m paulsha_cortex.trust_root toolchain {scheme.scheme_id}",
        "#",
        "# ===== 裁決 (a)（#640）＋ 完整盤點（#661）=====",
        "# 通用 runtime／傳輸層走**系統層**（換版本幾乎不影響治理產出）；版本會影響",
        "# 治理產出的程式落進部署樹，因為那必須是一個**可稽核的部署決定**，而不是跟著",
        "# operator 自己的環境漂移。#661 把後者由「四個模型 CLI」補成完整盤點：",
        f"#   executor（dispatch 直接執行）："
        + "、".join(t.name for t in EXECUTOR_TOOLS),
        f"#   非 executor（由別人 exec，但同樣是 job／服務跑得起來的必要條件）："
        + "、".join(t.name for t in SERVICE_TOOLS),
        "#",
        "# ===== 來源一律取 operator 實際在用的那一份 =====",
        "# **不要** `npm install -g` 另裝一份系統的。實機盤點：同一台機器上系統層的",
        "# codex 是 0.42.0、operator 實際在用的是 0.147.0（差 100 個以上小版本）——",
        "# 照「系統層有什麼就用什麼」，job 會跑一份 operator 從未判讀過的版本，而",
        "# 症狀是「跑得起來但結果對不上」，不是 `command not found`。",
        "#",
        "# ===== 系統層程式（不進部署樹，但仍是部署決定）=====",
    ]
    for program in SYSTEM_PROGRAMS:
        lines += [
            f"#   {program.name}（{program.source}）— 需要它的是："
            + "、".join(program.required_by),
            f"#     {program.note}",
        ]
    lines += [
        "#   版本本身仍是**部署決定**——某個 CLI 哪天提高下限時要一併升，否則它會變成",
        "#   下一個無聲漂移點。",
        "",
        "# --- 目錄骨架（權限與登記表那一節逐位元相同）---",
        f"install -d -o {scheme.deploy_account} -g {scheme.group_of(scheme.deploy_account)}"
        f" -m 0755 {layout.toolchain_root}",
        f"install -d -o {scheme.deploy_account} -g {scheme.group_of(scheme.deploy_account)}"
        f" -m 0755 {layout.toolchain_bin}",
        f"install -d -o {scheme.deploy_account} -g {scheme.group_of(scheme.deploy_account)}"
        f" -m 0755 {layout.toolchain_lib}",
    ]
    for tool in TOOLCHAIN_PROGRAMS:
        lines += [
            "",
            f"# --- {tool.name}（{tool.shape.value}"
            + ("；**需要系統層 node**" if tool.needs_node else "")
            + f"）---",
        ]
        if tool.consumed_by:
            for consumer in tool.consumed_by:
                _, detail = _surface_allows_wx(consumer)
                lines.append(f"#   執行面：{detail}")
        else:
            profile = executor_hardening_profile(tool.name)
            lines.append(
                f"#   加固剖面：{profile.profile_id} ⇒ "
                f"{job_unit_stem(layout, Principal.BUILDER, profile)}@<id>.service（#643）"
            )
        # note 可能是多行（#661 起有實測輸出要照抄）——逐行加註解前綴，否則計畫裡會
        # 出現既不是註解也不是命令的行，落地時被當成命令貼進 shell。
        lines += [f"#   {segment}" for segment in tool.note.split("\n")]
        lines += [
            f'#   SRC="$(readlink -f "$(command -v {tool.name})")"   '
            "# operator 實際在用的那一份",
        ]
        if tool.copy_tree:
            lines += [
                "#   整包搬（單搬進入點會缺 node_modules）：先找出套件根，再整棵複製——",
                f'#     PKG="$(cd "$(dirname "$SRC")/.." && pwd)"',
                f'#     cp -a "$PKG" {layout.toolchain_lib}/{tool.name}',
                f"#     ln -sfn {layout.toolchain_lib}/{tool.name}/<套件內的進入點>"
                f" {layout.toolchain_bin}/{tool.name}",
                "#   落定後確認進入點的 shebang 解得開：`head -n 1` 應為 "
                "`#!/usr/bin/env node`，且 `command -v node` 落在系統層。",
                f"#   ⚠️ `{layout.toolchain_bin}/{tool.name}` **必須是指進 lib/ 的 "
                "symlink**，不是把進入點複製出來的單檔（#661 實測）：ESM 的相對 "
                "import、以及「從 `which()` 往上找 `package.json`」這類套件根解析，"
                "靠的都是 `readlink -f` 之後落在套件樹裡的那條路徑。",
            ]
        else:
            lines += [
                f'#     cp -a "$SRC" {layout.toolchain_bin}/{tool.name}',
            ]
    unresolved = unresolved_node_execution_surfaces()
    if unresolved:
        lines += [
            "",
            "# ===== ⚠️ 已知的 W+X 衝突（#661 盤點結果，尚待 operator 裁決）=====",
            "# 下列程式是 node（V8 的 JIT 需要 W→X），但**執行它的那個 unit 目前仍是**",
            "# `MemoryDenyWriteExecute=yes`。#643 已在實機量到 V8 的 Runtime_CompileLazy",
            "# 在該項下直接崩，症狀是**空輸出**而不是報錯——離原因很遠。",
            "# 這一格 #643 的剖面推導看不到：那條推導的唯一輸入是 executor 名，涵蓋不了",
            "# 「executor 在執行途中再 exec 出來的 node 程式」。",
        ]
        for surface in unresolved:
            lines.append(f"#   {surface.program} ← {surface.detail}")
        lines += [
            "# 實機量測步驟見 runbook 第 4e 步（systemd-run 帶該 unit 的關鍵 property）。",
            "# **量到才改，且不得就地放寬**——回報 issue 由 operator 裁決（#643 的先例）。",
        ]
    lines += [
        "",
        "# --- 統一收權（root 擁有、全部 job／服務帳號唯讀＋可執行）---",
        f"chown -R {scheme.deploy_account}:{scheme.group_of(scheme.deploy_account)}"
        f" {layout.toolchain_root}",
        f"chmod -R u=rwX,go=rX {layout.toolchain_root}",
        "",
        "# ===== job 的 PATH =====",
        "# toolchain **必須排在最前面**：系統層可能另有一份同名但舊很多的 CLI，排後面",
        "# 就會被它蓋掉。尾段給的是系統層（" + tail + "）——node／git／python3 在那裡；",
        "# 刻意不含任何 sbin。值寫進 Manager 端 root-owned 的 EnvironmentFile：",
        f"#   PSC_BUILDER_PATH={layout.job_path_value()}",
        "",
        "# ===== delivery preflight（#661）=====",
        "# preflight 的 backend **不是**可執行檔而是一個 python 套件，因此它的落點不是",
        f"# toolchain 而是既有的部署 venv（{layout.venv_root}，同樣 root-owned、job／服務",
        "# 唯讀）。版本必須逐字等於 .project-policy.yml 的 policy_version——引擎自己會驗，",
        "# 對不上就 fail-closed；那與 R-23 的 workflow pin 是同一個部署決定。",
        f"#   {layout.venv_root}/bin/pip install "
        f"'{PREFLIGHT_BACKEND_DISTRIBUTION}==<policy_version>'",
        "# 值寫進 Manager 端 root-owned 的 EnvironmentFile：",
        f"#   PSC_PREFLIGHT_CMD=\"{layout.preflight_command_value()}\"",
    ]
    lines += _system_python_lines(layout)
    lines += _manager_gh_credential_lines(scheme, layout)
    lines += _dependency_inventory_lines()
    return lines


def _system_python_lines(layout: PathLayout) -> list[str]:
    """系統層 python 發行版的落位段（#666 漂移項 1）。"""
    # 刻意先組成字串再放進 list：巢狀 f-string ＋ 同款引號是 PEP 701（3.12+）語法，
    # 而本 repo 的 `requires-python` 從 3.10 起——CI 的 3.10／3.11 會直接 SyntaxError。
    declarations = " / ".join(
        '{}="{}"'.format(key, value)
        for key, value in layout.gate_command_env().items()
    )
    lines = [
        "",
        "# ===== 系統層 python 發行版（#666 漂移項 1：pytest）=====",
        "# **這一段不是 toolchain，也不是系統層可執行檔**——它是第四種相依：python",
        "# 發行版，`import`／`-m` 得到才有意義，`command -v` 對它一律無解。",
        "#",
        "# **為什麼落在系統層而不是部署 venv**：operator 宣告的 gate 命令用的是相對名",
        f"#   {declarations}",
        "# 而 gate 的 PSC_GATE_PATH 尾段是系統層 ⇒ `python3` 解到 /usr/bin/python3。",
        "# gate unit 自己的 ExecStart 用的是部署 venv 的 interpreter，但那只涵蓋 ledger",
        "# writer 本身；**operator 宣告的命令另外解析一次**。兩者是不同的 interpreter。",
        "#",
        "# ⚠️ 裝在 operator 的 user site-packages（`pip install --user`）是**不夠的**：",
        "#   ProtectHome=yes 之後 /home 整個不可見——",
        "#     $ sudo -u <gate 帳號> env HOME=<gate HOME> python3 -m pytest --version",
        "#     /usr/bin/python3: No module named pytest",
        "#   而症狀不是「gate 失敗」，是**每張 build 卡的 ledger 為空** ⇒ 撞 #540 的",
        "#   acceptance chain ⇒ 交付全部卡住，錯誤只在 manager.log 裡。",
    ]
    for dist in SYSTEM_PYTHON_DISTRIBUTIONS:
        lines += [
            "",
            f"# --- {dist.name}（module `{dist.module}`；interpreter＝{dist.interpreter}）---",
            f"#   版本約束：{dist.requirement}   ← 宣告在 {dist.declared_in}",
        ]
        lines += [f"#   {segment}" for segment in dist.note.split("\n")]
        lines += [
            f"#     sudo pip install --break-system-packages '{dist.requirement}'",
            "#   ✅ 版本是**明示的部署決定**：裝完把解出來的版本記進 runbook，並與",
            "#      operator 側逐字比對——同一台機器上兩個 interpreter 各有一份是常態，",
            "#      而版本分岔的症狀是「gate 判定與本機跑不一樣」，不是報錯：",
            f"#     python3 -m {dist.module} --version   # 系統層（gate 實際用的那一份）",
        ]
    lines += [
        "",
        "# ✅ 在**完整加固面下**實測（`sudo -u` 沒有 unit 的加固面，兩者可能不同結果）：",
        "#   systemd-run --uid=<gate 帳號> … --property=MemoryDenyWriteExecute=yes \\",
        "#     /usr/bin/python3 -m pytest -q",
        "#   期望 rc=0。CPython **不是** V8——MDWE 對它沒有影響（與 #643 的 node 型",
        "#   executor 相反），因此這一條在完整加固面下就應該過，不必放寬任何一項。",
    ]
    return lines


def _manager_gh_credential_lines(scheme: UidScheme, layout: PathLayout) -> list[str]:
    """Manager 的 gh 憑證落位段（#666 漂移項 2）。"""
    account = scheme.durable_state_owner
    root = scheme.deploy_account
    group = scheme.group_of(root)
    cred = layout.gh_credential_of(layout.manager_account)
    settings = layout.gh_settings_of(layout.manager_account)
    return [
        "",
        "# ===== Manager 的 gh 憑證（#666 漂移項 2；登記表 manager-gh-credential）=====",
        "# 形態沿用 #640 裁決 (b)：**目錄 root-owned、憑證檔服務帳號 owned**。兩層",
        "# 目錄由 `trust_root scaffold` 產出（本計畫不重複產），權限由登記表經",
        "# `trust_root permissions … --commands` 產出。這裡只出**內容落位**。",
        "#",
        "# ⚠️ **兩個檔刻意不同 owner，這不是疏漏**——下一個讀到這裡的人最可能做的事",
        "#    就是把兩個都設成同一種，因此把理由寫在計畫裡：",
        f"#      {cred}",
        f"#        → {account}:{scheme.group_of(account)} 0600"
        "   ← `gh` 唯一寫回 token 的檔，要 refresh 得回來",
        f"#      {settings}",
        f"#        → {root}:{group} 0644"
        "   ← 非憑證；但 `aliases` 可宣告 `!` shell alias ⇒ 唯讀",
        "#    後者與三份 `.gitconfig` 維持 root-owned 是**同一條理由**（`core.fsmonitor`",
        "#    ／`alias.*` 同樣會執行外部命令）。",
        "#",
        "# ⚠️ **與 #640 的 job 憑證形狀相同、洩漏面不同級，不要混為一談**：#640 那一份",
        "#    是給 **job 帳號**的模型 provider 憑證（job unit 另有 `Environment=GH_TOKEN=`",
        "#    把 GitHub token 清空，成果一律走 spool 由 Manager 代理推送）；這一份是給",
        "#    **Manager** 的，而 Manager 是 durable state owner——這個 token 推得動 PR、",
        "#    關得掉 issue、merge 得了分支。因此它只掛在 durable state owner 的 HOME 下，",
        "#    job 帳號刻意沒有 `~/.config/gh` 那一層目錄。",
        "#",
        "# 🔧 落位（來源取 operator 實際在用的那一份）：",
        f'#     sudo install -o {account} -g {scheme.group_of(account)} -m 0600 \\',
        f'#       "$HOME/.config/gh/hosts.yml" {cred}',
        f'#     sudo install -o {root} -g {group} -m 0644 \\',
        f'#       "$HOME/.config/gh/config.yml" {settings}',
        "#",
        "# ✅ 驗證要**以該身分實測**，不是只驗檔案存在：",
        f'#     sudo -u {account} env HOME={layout.home_of(layout.manager_account)} \\',
        "#       gh auth status",
        "#     期望：以 fleet 正式身分登入成功（不是 `You are not logged into any "
        "GitHub hosts.`）",
        "# ✅ 不變式「改得了內容、建不了新檔」（與 #640 的憑證逐條同構）：",
        f'#     sudo -u {account} sh -c \'printf "" >> {cred}\'        # 期望：OK',
        f'#     sudo -u {account} sh -c \'touch {_parent_dir(cred)}/x\' # 期望：Permission denied',
        f'#     sudo -u {account} sh -c \'rm -f {settings}\'            # 期望：Permission denied',
    ]


def _dependency_inventory_lines() -> list[str]:
    """窮舉盤點的摘要 ＋ 未決項（#666）。"""
    lines = [
        "",
        "# ===== 窮舉盤點（#666）=====",
        "# 判準：**降權帳號在完整加固面下，跑完一個 run 需要碰到的所有外部程式與憑證**",
        "#（而不是「這一類東西有哪些」——前者才是 #640→#661→#666 每次漏掉的那個問題）。",
        "# 機器可讀形式在 permgen.RUN_EXTERNAL_DEPENDENCIES，兩個方向都有測試釘住：",
        "#   uncovered_run_dependencies()  盤點列到但表上查無 ⇒ 必須是空的",
        "#   unlisted_roster_entries()     表上有但盤點沒列到 ⇒ 必須是空的",
        f"# 目前共 {len(RUN_EXTERNAL_DEPENDENCIES)} 項，逐段列出：",
    ]
    for stage in RunStage:
        rows = [d for d in RUN_EXTERNAL_DEPENDENCIES if stage in d.stages]
        if not rows:
            continue
        lines.append(f"#   [{stage.value}]")
        for dep in rows:
            who = "／".join(p.value for p in dep.principals)
            lines.append(
                f"#     {dep.name}  ({dep.kind.value}; {who}) ← {dep.covered_by}"
            )
    deferred = deferred_run_dependencies()
    if deferred:
        lines += [
            "#",
            "# ===== ⚠️ 盤點撞到、**尚未有歸宿**的相依（不做裁決，只讓它不會消失）=====",
        ]
        for item in deferred:
            who = "／".join(p.value for p in item.principals)
            lines += [
                f"#   {item.name}  ({item.kind.value}; {who})",
                f"#     症狀：{item.symptom.splitlines()[0]}",
                f"#     處置：{item.disposition}",
            ]
        lines += [
            "#   完整理由：permgen.deferred_run_dependencies()。與 #661 的",
            "#   unresolved_node_execution_surfaces() 同一個定位——**量到／裁決之前不動**。",
        ]
    return lines


# ---------------------------------------------------------------------------
# polkit 規則產生（授權面嚴格收窄）
# ---------------------------------------------------------------------------

#: 唯一被授權的 systemd polkit action。
POLKIT_ACTION = "org.freedesktop.systemd1.manage-units"
#: 唯一被授權的 verb（起／停 job；reload、mask、set-property 等一律拒）。
POLKIT_ALLOWED_VERBS: tuple[str, ...] = ("start", "stop")


class PolkitPlan(Enum):
    """降權的兩個方案。**0816 第三輪裁決：B 定案**（`TEMPLATE` 為預設）。

    - `TEMPLATE`（B，**定案／預設**）：root-owned 模板 unit（`<instance>-job@.service`）
      把 `User=` 與 `ExecStart=` 都硬寫死，polkit 只放行該模板的實例。**「降到哪個
      帳號」「執行哪支程式」因此都由 OS 強制**，`plan_residual_risk()` 回傳空 tuple。
      Manager 端對應的啟動器模式是 `PSC_JOB_RUNNER=systemd-template`
      （`coordinator/job_runner.py`）。
    - `TRANSIENT`（A，保留為對照／過渡）：Manager 以 `systemd-run` 起 transient unit
      （`PSC_JOB_RUNNER=systemd-run`）。polkit 能收窄的**只有**呼叫者 UID ＋ unit 名
      前綴；`User=`／`--uid=` **不在** polkit detail 內（#603 實測），故「只能降到 job
      帳號」這一半只能由 Manager 端封閉的 argv 產生器在 code level 保證——那正是本次
      改採 B 案的原因，殘餘風險見 `plan_residual_risk`。
    """

    TRANSIENT = "transient"
    TEMPLATE = "template"


#: 明確被拒的特權 unit 屬性——列在規則檔開頭，讓審查者一眼看到邊界在哪。
POLKIT_FORBIDDEN_PROPERTIES: tuple[str, ...] = (
    "User=root",
    "User=<任何非 job 帳號>",
    "AmbientCapabilities=",
    "CapabilityBoundingSet=",
    "PrivateUsers=",
    "SystemCallFilter=",
    "ExecStart=（任意 argv）",
)


@dataclass(frozen=True)
class PolkitRule:
    """產生出來的 polkit 規則：**只有內容字串**，本模組不寫任何系統路徑。"""

    install_path: str
    plan: PolkitPlan
    subject_account: str
    #: 本規則放行的模板會降到的**全部**目標帳號（#615 起可能不只一個）。
    target_accounts: tuple[str, ...]
    unit_pattern: str
    allowed_verbs: tuple[str, ...]
    content: str
    #: 本方案在 OS 層**未**強制的部分（空 tuple＝無殘餘）。
    residual_risks: tuple[str, ...] = ()

    @property
    def target_account(self) -> str:
        """第一個目標帳號。

        #615 之前一條規則只服務一個 principal，這個欄位是純量；現在一條規則同時
        涵蓋 builder 與 reviewer/planner 兩份模板，純量已經表達不了全貌。保留它是
        為了既有呼叫端（runbook 的摘要輸出）不必同步改，**但任何「這條規則授權了
        誰」的判斷都應該讀 `target_accounts`**。
        """

        return self.target_accounts[0]

    def to_dict(self) -> dict[str, object]:
        return {
            "install_path": self.install_path,
            "plan": self.plan.value,
            "subject_account": self.subject_account,
            "target_account": self.target_account,
            "target_accounts": list(self.target_accounts),
            "unit_pattern": self.unit_pattern,
            "allowed_verbs": list(self.allowed_verbs),
            "residual_risks": list(self.residual_risks),
            "content": self.content,
        }


#: A 方案的 transient unit 名前綴——與 `coordinator/job_runner.UNIT_NAME_PREFIX`
#: 是**成對契約**：改任一邊都必須同步改另一邊，否則 polkit 會拒掉所有 job。
def transient_unit_prefix(layout: "PathLayout") -> str:
    return f"{layout.instance}-job-"


def job_unit_stems(
    layout: "PathLayout" = None,  # type: ignore[assignment]
    principals: "Principal | Sequence[Principal]" = Principal.BUILDER,
) -> tuple[str, ...]:
    """這些 principal 的**全部**模板字幹（principal × 加固剖面），依表順序。

    `principals` 收單一 principal 或一組：前者是既有呼叫端（builder 一族），後者是
    #615 之後真正落檔的集合（:data:`DOWNGRADED_JOB_PRINCIPALS`）。**兩層都是列舉**
    ——字幹數 = principal 數 × 剖面數，沒有任何一層是萬用字元。
    """

    layout = layout if layout is not None else DEFAULT_LAYOUT
    return tuple(
        job_unit_stem(layout, principal, profile)
        for principal in _as_principals(principals)
        for profile in HARDENING_PROFILES
    )


def job_unit_pattern(
    layout: "PathLayout" = None,  # type: ignore[assignment]
    plan: PolkitPlan = PolkitPlan.TEMPLATE,
    principals: "Principal | Sequence[Principal]" = DOWNGRADED_JOB_PRINCIPALS,
) -> str:
    """被授權的 unit 名 regex（錨定）。

    **字幹段是一個列舉的交替，不是萬用字元**，而且是**兩層**列舉：

    - `principal`（#615 M2）：`cortex-job`（builder）與 `cortex-reviewer-job`
      （reviewer＋planner），由 :data:`DOWNGRADED_JOB_PRINCIPALS` 導出；
    - 加固剖面（#643）：每份剖面各有一份 root-owned 模板檔，因此各有一個字幹後綴
      （空字串 / `-jit`），由 :data:`HARDENING_PROFILES` 導出。

    兩層都是**具名模板的列舉**：前後都錨定，instance 段的字元類逐字未變，`^` 與 `@`
    之間不允許任何未列舉的字幹。放行面因此從「兩個具名模板」變成「四個具名模板」，
    **不是**「任意 unit」——四份 unit 檔全部 root-owned、`User=`／`ExecStart=` 都寫死，
    呼叫端能選的只是「哪一份具名模板」。

    **為什麼仍然只有一條 `polkit.addRule`**（#643 立下、#615 沿用）：第二條規則會把
    subject／action／verb／明細缺席四個檢查複製一份，變成兩個要同步維護的放行出口，
    而「全檔只有一個 `return polkit.Result.YES`」正是這份規則檔的可審查性性質。擴充
    字幹段保留了單一出口。

    刻意**不**用 `re.escape()`：產出的字串會原樣嵌進 polkit 的 JS regex 字面量，而
    `re.escape` 會把 `-` escape 成 `\\-`（JS 的 unicode 模式視為錯誤）。字幹形狀改由
    `_validate_hardening_profiles()` 與 `layout.instance` 的既有約束保證。
    """
    layout = layout if layout is not None else DEFAULT_LAYOUT
    if plan is PolkitPlan.TRANSIENT:
        return r"^" + layout.instance + r"-job-[a-z0-9][a-z0-9._-]{0,62}\.service$"
    stems = job_unit_stems(layout, principals)
    alternation = stems[0] if len(stems) == 1 else "(?:" + "|".join(stems) + ")"
    return r"^" + alternation + r"@[a-z0-9][a-z0-9._-]{0,62}\.service$"


def plan_residual_risk(plan: PolkitPlan, scheme: UidScheme) -> tuple[str, ...]:
    """本方案在 OS 層未強制的部分（誠實標註，runbook 與 PR 皆引用）。"""
    if plan is PolkitPlan.TEMPLATE:
        return ()
    svc = scheme.durable_state_owner
    # 只列會跑模型的 persona——它們是「被攻陷」的實際入口。
    same_uid = sorted(
        {
            p.value
            for p in (Principal.REVIEWER, Principal.PLANNER)
            if scheme.resolve(p) == svc
        }
    )
    risks = [
        f"polkit 的 {POLKIT_ACTION} 只暴露 unit 名稱，**不暴露 User=／--uid=**；"
        f"授權後 systemd 會照請求的任意 User= 起 unit。「只能降到 job 帳號」這一半"
        f"由 Manager 端封閉的 argv 產生器在 code level 保證，OS 層未強制。",
        f"因此**與 {svc} 同 UID 的任何行程**都持有這個 grant，可請求任意 User= 的"
        f" transient unit（含 User=root）。",
    ]
    if same_uid:
        risks.append(
            f"在 {scheme.scheme_id} 方案下，跑模型的 {'／'.join(same_uid)} 與 {svc} 同帳號，"
            f"故其中任一被攻陷即取得上一條的能力——這正是「是否提前三分」要衡量的東西。"
        )
    return tuple(risks)


def build_polkit_rule(
    scheme: UidScheme,
    layout: "PathLayout" = None,  # type: ignore[assignment]
    plan: PolkitPlan = PolkitPlan.TEMPLATE,
    principals: "Principal | Sequence[Principal] | None" = None,
) -> PolkitRule:
    """產生降權授權的 polkit 規則內容（A／B 兩方案共用同一套產生邏輯）。

    兩案的授權面都收窄到「`<svc>` 對特定 unit 名 pattern 的 start/stop」，且
    **unit／verb 明細缺席即拒**；差別在「降到哪個帳號」由誰強制（見 `PolkitPlan`）。

    `principals`（#615 M2）預設就是**實際落檔的那一組**
    （:data:`DOWNGRADED_JOB_PRINCIPALS`）——預設值等於部署現實是刻意的：預設若停在
    builder，`build_polkit_rule(scheme)` 產出的內容會與機器上那一份不同，而那正是
    「產生器與部署漂移」的起點。要只看單一 principal 的放行面（測試／對照用）必須
    顯式打出來。
    """
    layout = layout if layout is not None else DEFAULT_LAYOUT
    svc = scheme.durable_state_owner
    # `None`＝依本方案導出（#629）。預設不再是那張**支援表**，而是「這個部署真的會有
    # 哪幾份 unit」——二分／三分沒有 gate 帳號，規則就不該提 `cortex-gate-job@`。
    ordered = _as_principals(
        downgraded_job_principals(scheme) if principals is None else principals
    )
    targets: list[str] = []
    for principal in ordered:
        target = scheme.resolve(principal)
        if target is None:
            raise ValueError(f"principal 未映射到帳號: {principal}")
        if target not in targets:
            targets.append(target)
    pattern = job_unit_pattern(layout, plan, ordered)
    verbs = POLKIT_ALLOWED_VERBS
    verb_check = " && ".join(f'verb !== "{v}"' for v in verbs)
    residual = plan_residual_risk(plan, scheme)

    if plan is PolkitPlan.TRANSIENT:
        headline = (
            f"// 方案 A（transient unit）：{svc} 可 start/stop 名為\n"
            f"//   {transient_unit_prefix(layout)}<job 片段>-<sha8>.service 的 transient unit。\n"
            f"// unit 名前綴與 coordinator/job_runner.UNIT_NAME_PREFIX 是**成對契約**——\n"
            f"// 改任一邊都必須同步改另一邊，否則所有 job 會被 polkit 拒掉（fail-closed）。\n"
            f"//\n"
            f"// ===== 本方案在 OS 層未強制的部分（務必知悉）=====\n"
            + "\n".join(f"// - {r}" for r in residual)
            + "\n//\n"
            f"// 要把這一半也搬到 OS 層，改用方案 B（root-owned 模板 unit，User= 寫死）：\n"
            f"//   python3 -m paulsha_cortex.trust_root polkit {scheme.scheme_id} --template\n"
        )
    else:
        stems = job_unit_stems(layout, ordered)
        profile_lines = "".join(
            f"//     - {job_unit_stem(layout, principal, p)}@<id>.service"
            f"（User={scheme.resolve(principal)}，剖面 {p.profile_id}："
            f"{'完整加固表' if not p.overrides else '、'.join(f'{k}={v}' for k, v in sorted(p.overrides.items())) + '，其餘逐項同 strict'}）\n"
            for principal in ordered
            for p in HARDENING_PROFILES
        )
        headline = (
            f"// 方案 B（root-owned 模板 unit）：{svc} 只能 start/stop 下列**具名模板**的實例：\n"
            + profile_lines
            + f"// {len(stems)} 份模板檔都在 /etc/systemd/system/ 由 root 擁有，\n"
            f"// 內容硬寫死 User={'／'.join(targets)}、NoNewPrivileges=yes、"
            f"CapabilityBoundingSet=（空），\n"
            f"// 以及固定的 ExecStart={layout.job_shim} %i（root-owned shim）。\n"
            f"// per-job 參數走 Manager-owned spec spool（{layout.job_spec_spool_root}/<principal>/<id>.json，\n"
            f"// job 帳號唯讀）——{svc} 給得出參數，但給不出 UID、也給不出命令列。\n"
            f"// 因此 {svc} **無法選擇 job 的 UID**，也**無法夾帶任何特權屬性**：\n"
            + "\n".join(f"//     - {p}" for p in POLKIT_FORBIDDEN_PROPERTIES)
            + "\n"
            f"// 這些屬性全部只存在於 root-owned 的模板檔裡，呼叫端連提都提不了。\n"
            f"//\n"
            f"// ===== 為什麼字幹段是一個交替（兩層列舉）=====\n"
            f"// (a) **加固剖面**（#643）：加固指令寫在 unit 檔裡，一個模板只有一份 ⇒\n"
            f"//     兩種剖面必然是兩個檔、兩個名字。\n"
            f"// (b) **job 角色**（#615 M2）：builder 與 reviewer／planner 是**不同的 UID**，\n"
            f"//     而 User= 同樣寫死在 unit 檔裡 ⇒ 同樣必然是不同的檔、不同的名字。\n"
            f"// 上面的 pattern 因此是**列舉的交替**（{'、'.join(stems)}），\n"
            f"// 不是萬用字元：`^` 與 `@` 之間不允許任何未列舉的字幹，instance 段的字元類\n"
            f"// 一字未改。{svc} 選得了「哪一份模板」，但每一份都是 root-owned、User= 都寫死，\n"
            f"// 也都不含任何可由呼叫端注入的東西——能選的只是「哪個 job 帳號、多一項或少\n"
            f"// 一項加固」。放寬的那一項（MemoryDenyWriteExecute）擋的是**本 job 自己位址\n"
            f"// 空間內**的 W+X，不是跨 UID 的邊界；而帳號的選擇本身不構成提權——四份模板\n"
            f"// 的 User= 全部是無 sudo、無 root、彼此互不可寫的降權服務帳號，沒有任何一份\n"
            f"// 比 {svc} 自己更有權限。真正決定用哪一份的是 persona ＋ executor（都是\n"
            f"// Manager 的 dispatch 決定），job 側完全碰不到。\n"
            f"//\n"
            f"// ===== 為什麼 transient unit 在本方案下一律拒 =====\n"
            f"// StartTransientUnit 的 polkit 檢查**不帶 unit 屬性明細**（規則只看得到\n"
            f"// action id，看不到 User=／AmbientCapabilities=／ExecStart=）。放行 transient\n"
            f"// unit 就等於允許 {svc} 傳 User=root。下方「unit／verb 明細缺席即拒」與\n"
            f"// 只認 `@` 實例名的 pattern 一起把這條路關死。\n"
        )

    content = f"""// /etc/polkit-1/rules.d/49-{layout.instance}-downgrade.rules
// 由 permgen 機械產生（scheme={scheme.scheme_id}, plan={plan.value}）——勿手改；重跑：
//   python3 -m paulsha_cortex.trust_root polkit {scheme.scheme_id} --{plan.value}
//
// ===== 這是 cortex 的降權/提權分界線 =====
{headline}//
// ===== 審查者的一眼結論 =====
// 唯一的放行出口需要同時滿足：
//   (1) subject 是 {svc}；(2) action 是 {POLKIT_ACTION}；
//   (3) unit／verb 明細存在；(4) verb ∈ {{{", ".join(verbs)}}}；
//   (5) unit 名匹配 {pattern}
// 任一不成立即拒絕。函式只有最後一行放行。

polkit.addRule(function(action, subject) {{
    if (subject.user !== "{svc}") {{
        // 不干涉 operator／其他帳號的既有授權（交回 polkit 預設）。
        return polkit.Result.NOT_HANDLED;
    }}
    if (action.id !== "{POLKIT_ACTION}") {{
        // {svc} 的其他 polkit action 一律拒（含 login1／hostname1／systemd1 其他面）。
        return polkit.Result.NO;
    }}
    var unit = action.lookup("unit");
    var verb = action.lookup("verb");
    if (!unit || !verb) {{
        // 明細缺席就無從判斷，一律拒（fail-closed）。
        return polkit.Result.NO;
    }}
    if ({verb_check}) {{
        return polkit.Result.NO;
    }}
    if (!/{pattern}/.test(unit)) {{
        // 只有上述 pattern 的 job unit；{layout.instance}-manager.service 等一律拒。
        return polkit.Result.NO;
    }}
    return polkit.Result.YES;
}});
"""
    return PolkitRule(
        install_path=f"/etc/polkit-1/rules.d/49-{layout.instance}-downgrade.rules",
        plan=plan,
        subject_account=svc,
        target_accounts=tuple(targets),
        unit_pattern=pattern,
        allowed_verbs=verbs,
        content=content,
        residual_risks=residual,
    )


def transient_unit_properties(
    scheme: UidScheme,
    layout: "PathLayout" = None,  # type: ignore[assignment]
    principal: Principal = Principal.BUILDER,
    plan: PermissionPlan | None = None,
    profile: HardeningProfile = DEFAULT_HARDENING_PROFILE,
) -> tuple[str, ...]:
    """A 方案的 `--property=` 建議清單（與 B 方案模板 unit 同源，機械產生）。

    `job_runner` 目前只送 `NoNewPrivileges=yes`；本函式把同一套加固表與**由登記表
    導出的 ReadWritePaths** 展開成 `systemd-run --property=` 形式，供 operator 在
    A 方案下逐條加固，或作為「A 與 B 的加固面是否等價」的對照表。
    """
    layout = layout if layout is not None else DEFAULT_LAYOUT
    plan = plan or generate_plan(scheme)
    account = scheme.resolve(principal)
    if account is None:
        raise ValueError(f"principal 未映射到帳號: {principal}")
    job_layout = layout.with_job_segment("%i")
    effective = profile.effective()
    props = [f"--property={key}={effective[key]}" for key, _value, _why in _HARDENING]
    for rwp in read_write_paths(
        plan,
        job_layout,
        account,
        job_layout.job_extra_write_paths(account),
        retired=RETIRED_JOB_WRITE_ASSETS,
    ):
        props.append(f"--property=ReadWritePaths={rwp}")
    return tuple(props)


# ---------------------------------------------------------------------------
# 從**已落檔的 unit** 機械導出 systemd-run 的 --property= 清單（#673）
#
# 為什麼不能用上面的 `transient_unit_properties()` 代替：那一支是從**產生器**展開
# 的，它回答的是「permgen 現在會產出什麼」。runbook 要驗的是另一個命題——「磁碟上
# 那份 unit 現在是什麼」。兩者會漂移（runbook 自己就記著「產生器修好 ≠ 已落檔的
# unit 跟著更新，#643 的教訓」），而漂移正是要被驗出來的東西。
#
# 為什麼不能手抄子集：#638（單 UID 讓 ACL 斷言真空）、#657（同型）、#673 是同一族
# 事故的第一、二、三次。#673 特別值得記：它的複本抄了 `SystemCallFilter=` 卻漏抄
# `SystemCallErrorNumber=EPERM`，於是複本比 production **更嚴格**，量出一個
# production 沒有的 rc=1——**手抄子集的假綠與假紅一樣會發生**，方向不由人選。
#
# 因此本函式的契約是「全帶，不選」：`[Service]` 段除了明示排除的執行面指令
# （`ExecStart=` 之類，探針要換掉它）以外**全部**帶進 `--property=`。新增一項加固
# 時 runbook 不必改、也不會漏——這就是它與 grep 白名單的差別。
# ---------------------------------------------------------------------------

#: 複製加固面時**不該**帶進 `systemd-run` 的 `[Service]` 指令。
#:
#: 判準是「這一項描述的是**跑什麼／怎麼收尾**，不是**在什麼條件下跑**」。刻意寫成
#: 排除表而非允許表：允許表漏一項＝複本比 production 弱一項且沒人看得見，排除表漏
#: 一項＝探針多帶一個無害的屬性、最壞情況是 `systemd-run` 當場報錯（看得見）。
UNIT_REPLICA_EXCLUDED_KEYS: frozenset[str] = frozenset({
    "ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost",
    "ExecReload", "ExecCondition",
    "Type", "Restart", "RestartSec", "RemainAfterExit", "GuessMainPID",
    "PIDFile", "BusName", "NotifyAccess", "WatchdogSec",
    "TimeoutStartSec", "TimeoutStopSec", "TimeoutSec", "RuntimeMaxSec",
    "KillMode", "KillSignal", "SuccessExitStatus", "FinalKillSignal",
    "StandardInput", "StandardOutput", "StandardError", "SyslogIdentifier",
})


class UnitReplicaDriftError(ValueError):
    """已落檔的 unit 少了加固表上的鍵——複本會**比 production 弱**，一律拒絕產出。

    這是本函式存在的唯一理由：靜默產出一份少幾條的清單，就是把 #673 再演一次。
    """


def _unit_service_directives(unit_text: str) -> list[tuple[str, str]]:
    """把一份 unit 的 `[Service]` 段讀成 (key, value) 序列（保留重複鍵的順序）。"""

    directives: list[tuple[str, str]] = []
    section: str | None = None
    pending: str | None = None
    for raw in unit_text.splitlines():
        line = raw.rstrip("\n")
        if pending is not None:
            # 續行：systemd 以行尾 `\` 續行，值以空白接續。
            merged = pending + " " + line.strip()
            if merged.endswith("\\"):
                pending = merged[:-1].rstrip()
                continue
            pending = None
            key, _, value = merged.partition("=")
            directives.append((key.strip(), value.strip()))
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        header = re.fullmatch(r"\[([^\]]+)\]", stripped)
        if header is not None:
            section = header.group(1)
            continue
        if section != "Service" or "=" not in stripped:
            continue
        if stripped.endswith("\\"):
            pending = stripped[:-1].rstrip()
            continue
        key, _, value = stripped.partition("=")
        directives.append((key.strip(), value.strip()))
    return directives


def unit_replica_properties(
    unit_text: str,
    *,
    instance: str = "probe",
    require_hardening: bool = True,
) -> tuple[str, ...]:
    """一份**已落檔**的 unit → `systemd-run --property=` 的**完整**清單。

    runbook 與測試共用這一支，因此「真實加固面」在兩邊是同一個定義。

    :param instance: 模板 unit 的 `%i` 代入值（探針用的假 job id）。
    :param require_hardening: 產出前強制比對 :data:`_HARDENING` 的每一個鍵都在
        unit 裡。**預設開啟**——關掉它就回到「複本可能比 production 弱」的世界。
    :raises UnitReplicaDriftError: 落檔的 unit 少了加固鍵，或值裡留有本函式展不開的
        systemd specifier（展不開就代表複本與 unit 不同義，寧可炸掉）。
    """

    props: list[str] = []
    seen: set[str] = set()
    for key, value in _unit_service_directives(unit_text):
        if key in UNIT_REPLICA_EXCLUDED_KEYS:
            continue
        # `%%` 是 systemd 的跳脫（＝一個字面 `%`），**必須先抽走**：否則 `%%i` 會被
        # 下一步當成 specifier 展開成 `%<instance>`，而它的正確結果是字面 `%i`。
        # 這種錯法不會報錯，只會讓探針的加固面與 unit 悄悄不同——正是本票要根除的那類。
        sentinel = "\x00PCT\x00"
        expanded = value.replace("%%", sentinel).replace("%i", instance)
        # 未知 specifier 的檢查必須在**還原跳脫之前**：還原後的字面 `%i` 長得跟
        # specifier 一模一樣，先還原會把合法的 `%%i` 誤判成展不開的 specifier。
        leftover = re.search(r"%[a-zA-Z]", expanded)
        expanded = expanded.replace(sentinel, "%")
        if leftover is not None:
            raise UnitReplicaDriftError(
                f"{key}= 的值含本函式展不開的 systemd specifier "
                f"{leftover.group(0)!r}：{value!r}。複本與 unit 不同義時**不產出**"
                "——請先把該 specifier 加進展開規則，不要讓探針帶著一個字面上的 % 跑。"
            )
        seen.add(key)
        props.append(f"--property={key}={expanded}")
    if require_hardening:
        missing = [key for key, _value, _why in _HARDENING if key not in seen]
        if missing:
            raise UnitReplicaDriftError(
                f"已落檔的 unit 缺少加固鍵 {missing}——用它組出來的複本會**比 "
                "production 弱**，在那個複本下取得的綠不承載任何語意（#638／#657／"
                "#673 同一族事故）。先重跑產生器落檔："
                "`python3 -m paulsha_cortex.trust_root unit …`。"
            )
    return tuple(props)


# ---------------------------------------------------------------------------
# 反向不變式：job 在**零額外 env** 下解到哪一份 CLI（#679）
#
# ## 這一節要修的不是程式，是驗證方法
#
# #679 的缺陷本身很小（一個 `if path_override:`），但它**存活了五輪驗證**：runbook
# 4e／5-2b、#661 與 #664 的量測、以及事故當天的每一次探針，全部長這樣：
#
#     systemd-run … --setenv=PATH=/opt/cortex/toolchain/bin:/usr/local/bin:/usr/bin:/bin
#
# 驗證環境**供應了 production 不供應的東西**，於是「job 拿不到 PATH」在結構上不可能
# 被觀察到。runbook 4e 甚至逐字預言了症狀（「系統層那份 0.42.0 一樣會 rc=0，而 job
# 跑的就變成 operator 從未判讀過的版本」），連版本號都對上了——但那一條是
# `sudo -u … env PATH=…` 跑的，所以它驗的是「toolchain 裡那份是對的版本」，
# 不是「job 實際會解到哪一份」。
#
# 這是「綠燈不承載語意」的**第五**個實例，而且是新的一類：前四次（#638 單 UID、
# #657 同型、#673 假紅兩次）是「複本比 production 弱或強」，這次是「複本比
# production **多**」。因此 #677 立下的規矩要再推一格：
#
#   **複本必須連「production 沒有設什麼」也一起複製。**
#
# `unit_replica_properties()` 天生做得到——它是從落檔的 unit 全量機械導出的，unit 有
# `Environment=PATH=` 它就帶、沒有就不帶。真正要拿掉的是探針**額外**疊上去的那一行
# `--setenv=PATH=`。本節的產生器因此有一條硬性質，由測試釘住：
# **輸出裡不得出現任何 `--setenv=`，PATH 只能來自 unit 複本本身。**
# ---------------------------------------------------------------------------

#: 探針**可執行的那些行**禁止出現的片段。`--setenv=` 一律禁止（不只 `PATH=`）：
#: 探針補任何一個 production 不供應的變數，都是同一個失效模式的下一個版本。
PATH_PROBE_FORBIDDEN_FRAGMENTS: tuple[str, ...] = ("--setenv=", "PATH=")

#: runbook 第 4e 步的共用探針名（#673 落地、#677 定案）。反向不變式**呼叫它、不重造**
#: ——加固面的定義只有一份（:func:`unit_replica_properties`），連呼叫它的那幾行 shell
#: 也不該有第二份會漂移的複本。runbook 那一節與本產生器共用這個名字。
PATH_PROBE_HELPER = "psc_run_under"


def path_probe_env_injections(lines: Sequence[str]) -> tuple[str, ...]:
    """探針裡**注入了環境變數**的可執行行（正常應為空 tuple）。

    只看非註解行是刻意的：這一節的註解必須講得出「為什麼不能加 `--setenv=PATH=`」，
    而講這句話就得寫出那個字串。判準是「探針**做**了什麼」，不是「探針**提**到什麼」。
    """

    offenders: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if any(fragment in stripped for fragment in PATH_PROBE_FORBIDDEN_FRAGMENTS):
            offenders.append(line)
    return tuple(offenders)


@dataclass(frozen=True)
class PathResolutionCase:
    """反向不變式的一列：某個 job 角色 × 某支 executor。

    「角色 × executor」是**兩層列舉**，與 :func:`job_unit_stems` 同一個理由：
    executor 決定加固剖面（#643），因此同一個角色的 `codex` 與 `claude` 跑的是**兩份
    不同的 unit 檔**——只驗其中一份等於沒驗另一份的 PATH。
    """

    principal: Principal
    account: str
    executor: str
    #: 該 (角色, executor) 實際會被起動的模板字幹（含剖面後綴）。
    unit_stem: str
    hardening_profile: str
    #: 這一列要斷言的解析結果：`<toolchain>/bin/<executor>`。
    expected_binary: str
    #: 版本的比對對象＝**同一支檔案的絕對路徑**。登記表把 toolchain 落點登記成
    #: `<toolchain>/bin/<cli>`，因此「PATH 解出來的那支」與「絕對路徑那支」印出同一
    #: 個版本字串，就是「job 跑的是登記表登記的那一份」的直接證據——不需要第二份
    #: 手抄的版本清單（那會立刻變成下一個會漂移的真相）。
    version_reference: str


def path_resolution_cases(
    scheme: UidScheme,
    layout: "PathLayout" = None,  # type: ignore[assignment]
) -> tuple[PathResolutionCase, ...]:
    """`DOWNGRADED_JOB_PRINCIPALS` × :data:`EXECUTOR_TOOLS` 的完整矩陣。

    本 scheme 沒有的角色（two-way／three-way 沒有 `GATE`）機械略去，不留空列。
    """

    layout = layout if layout is not None else DEFAULT_LAYOUT
    cases: list[PathResolutionCase] = []
    for principal in downgraded_job_principals(scheme):
        account = scheme.resolve(principal)
        if account is None:  # pragma: no cover - downgraded_job_principals 已過濾
            continue
        for tool in EXECUTOR_TOOLS:
            profile = executor_hardening_profile(tool.name)
            binary = f"{layout.toolchain_bin}/{tool.name}"
            cases.append(
                PathResolutionCase(
                    principal=principal,
                    account=account,
                    executor=tool.name,
                    unit_stem=job_unit_stem(layout, principal, profile),
                    hardening_profile=profile.profile_id,
                    expected_binary=binary,
                    version_reference=binary,
                )
            )
    return tuple(cases)


def build_path_resolution_probe(
    scheme: UidScheme,
    layout: "PathLayout" = None,  # type: ignore[assignment]
) -> list[str]:
    """反向不變式的實機探針（**只回傳字串，不執行**）。

    形態與 :func:`build_toolchain_plan` 一致：產生器出內容，runbook 貼上去跑。
    要求逐條寫在產物本身，因為讀它的人正是會忍不住「補一個 PATH 讓它過」的人。
    """

    layout = layout if layout is not None else DEFAULT_LAYOUT
    cases = path_resolution_cases(scheme, layout)
    lines: list[str] = [
        "# === #679 反向不變式：job 在**零額外 env** 下解到哪一份 CLI ===",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}）——勿手改；重跑：",
        f"#   python3 -m paulsha_cortex.trust_root path-probe {scheme.scheme_id}",
        "#",
    ]
    lines += _wrap_comment(
        "本探針**刻意不帶任何 `--setenv=`**。這是本票的核心教訓：在此之前每一條驗證"
        "都自己帶 `--setenv=PATH=…`，於是驗證環境供應了 production 不供應的東西，"
        "「job 根本沒有 PATH」在結構上不可能被觀察到。加固面複本由 "
        "`unit_replica_properties()` 從**落檔的 unit** 全量導出，因此它連"
        "「production 沒有設什麼」也一起複製——只要不再往上疊，量到的就是 job 真正"
        "會拿到的環境。"
    )
    lines += [
        "#",
        "# ⛔ 這條探針失敗時**不要**加 `--setenv=PATH=` 讓它過——那正是讓這個缺陷活了",
        "#    五輪驗證的那個動作。要補的是 unit 的 `Environment=PATH=`（重跑產生器並",
        "#    重新落檔）與 Manager EnvironmentFile 的 `PSC_*_PATH`。",
        "#",
    ]
    lines += _wrap_comment(
        "本探針量的是**第 2 層**（模板 unit 的 `Environment=PATH=`）：systemd-run "
        "起的是探針命令，不經過 shim，因此看不到 spec 那一層。第 1 層（spec 的 env）"
        "由 `job_runner.resolve_job_path()` 的 fail-closed ＋ 契約測試守住，兩層的值"
        "同源（都是 `PathLayout.job_path_value()`）。要一併驗第 1 層，走 runbook 的"
        "真實 dispatch smoke，不要在這裡手工組 spec——#645 逐字記錄過那條路怎麼把"
        "bug 繞過去。"
    )
    lines += [
        "",
        f"# 前置：先貼上 runbook 第 4e 步的**共用探針** `{PATH_PROBE_HELPER}`。",
        "# **本產生器刻意不自己定義一份**——加固面的定義只有一份"
        "（`unit_replica_properties()`），",
        "# 連呼叫它的那幾行 shell 也不該有第二份複本；兩份複本會漂移，而漂移的方向",
        "# 不由人選（#638／#657／#673／#679 是同一族事故的第一到第四次）。",
        f"declare -F {PATH_PROBE_HELPER} >/dev/null || {{",
        f"  echo \"⛔ 未定義 {PATH_PROBE_HELPER}——先貼上 runbook 第 4e 步的共用探針\" >&2",
        "  return 1 2>/dev/null || exit 1",
        "}",
        "",
    ]
    for case in cases:
        lines += [
            f"# --- {case.principal.value} × {case.executor}"
            f"（{case.account}／{case.unit_stem}@／剖面 {case.hardening_profile}）---",
            f"{PATH_PROBE_HELPER} {case.unit_stem} /bin/sh -c 'command -v {case.executor}'",
            f"#   期望逐字：{case.expected_binary}",
            f"#   ⛔ 空輸出 ⇒ job 沒有 PATH（或 toolchain 不在上面）；",
            f"#      /usr/bin/{case.executor} ⇒ **本票的原症狀**：解到系統層那一份，",
            "#      不報錯、只是產出來自一支沒人判讀過的 CLI。",
            f"{PATH_PROBE_HELPER} {case.unit_stem} /bin/sh -c '{case.executor} --version'",
            f"{case.version_reference} --version",
            "#   期望：**兩行逐字相同**（PATH 解出來的那支 == 登記表登記的那支）。",
            "",
        ]
    lines += _wrap_comment(
        "gate 角色同樣在矩陣內，而且不是湊數：gate 宣告的 "
        "`PSC_GATE_CMD_PYTEST=\"python3 -m pytest -q\"` 是相對名，同樣走 PATH 解析"
        "（#666）。gate 這三列驗的是「gate 的 PATH 確實生效且順序正確」，那條性質對 "
        "`python3` 與對 `codex` 是同一條。"
    )
    return lines


#: #708 的探針在每一格 log spool 底下用的 per-run 目錄名。固定字面量是刻意的：
#: operator 重跑時同一格會被 `spool_slot.create_slot(reset=True)` 整格重建，不會在
#: 樹裡留下一堆 `probe-<timestamp>` 需要人去掃。
JOB_LOG_PROBE_KEY = "probe-708"


def build_job_log_probe(
    scheme: UidScheme,
    layout: "PathLayout" = None,  # type: ignore[assignment]
) -> list[str]:
    """#708 反向不變式的實機探針（**只回傳字串，不執行**）。

    要證的是**兩個方向**，缺一不可：

    1. **正向**——每個降權 principal 在**零額外 env**、真實模板 unit 的加固面下
       `>>` 得進自己那一格 log，而且 Manager 讀得回來（`0620` ＋ default ACL 的
       effective 位真的是 `w`，不是被 mask 壓成 `#effective:---`）；
    2. **反向**——同一個身分對 Manager 的 dispatch log 目錄仍然寫不進去。少了這一半，
       「掛在既有通道底下、可寫面逐字不變」就只是一句宣稱：真正要排除的失敗是
       「順手把 log 目錄加進 `ReadWritePaths=` 讓它過」，而那會連 gate ledger 與
       exit sentinel 一起開放（#604）。

    加固面複本一律由 `psc_run_under` → :func:`unit_replica_properties` 從**落檔的
    unit** 全量導出（design D13）。**本產生器不自組 `--property=`、不帶
    `--setenv=`**——本 repo 兩個方向的事故各兩次（#638／#657 假綠；#673 body 與
    repro 假紅），第五次是 #679 的「複本比 production 多」。

    per-job 那一格**由 `spool_slot.prepare_job_log()` 本人建**，不是探針自己建目錄
    再建一個空檔：#645 逐字記錄過「手工組前置物剛好把 bug 繞過去」的代價，而本票
    （#708）正是那一族的下一個成員——M1 當時 operator 手工挑 log 路徑，恰好避開了
    這個缺陷，於是它活到 define 首次收斂的那一天。
    """

    layout = layout if layout is not None else DEFAULT_LAYOUT
    manager = scheme.durable_state_owner
    paths_by_asset = layout.asset_paths()
    python = f"{layout.deploy_root}/venv/bin/python3"
    lines: list[str] = [
        "# === #708 反向不變式：三個降權 principal 各自寫得出 job log 嗎 ===",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}）——勿手改；重跑：",
        f"#   python3 -m paulsha_cortex.trust_root job-log-probe {scheme.scheme_id}",
        "#",
    ]
    lines += _wrap_comment(
        "本探針**刻意不帶任何 `--setenv=`、也不自組 `--property=`**（design D13）。"
        "加固面複本由 `unit_replica_properties()` 從落檔的 unit 全量導出，因此它連"
        "「production 沒有設什麼」也一起複製——只要不再往上疊，量到的就是 job 真正"
        "會拿到的環境。"
    )
    lines += [
        "#",
        "# ⛔ 任何一條紅掉時**不要**把 log 目錄加進模板 unit 的 `ReadWritePaths=`——",
        "#    那一層住著 gate ledger 與 exit sentinel（#604），開放它等於把「證據由",
        "#    Manager 寫」這條保證賣掉。要改的是登記表那一列掛在哪條既有通道底下。",
        "#",
        f"# 前置：先貼上 runbook 第 4e 步的**共用探針** `{PATH_PROBE_HELPER}`。",
        f"declare -F {PATH_PROBE_HELPER} >/dev/null || {{",
        f"  echo \"⛔ 未定義 {PATH_PROBE_HELPER}——先貼上 runbook 第 4e 步的共用探針\" >&2",
        "  return 1 2>/dev/null || exit 1",
        "}",
        "",
    ]
    for principal in downgraded_job_principals(scheme):
        account = scheme.resolve(principal)
        if account is None:  # pragma: no cover - downgraded_job_principals 已過濾
            continue
        spool = registry.job_log_spool_for(principal)
        root = paths_by_asset[spool.asset_id]
        channel = paths_by_asset[spool.channel_asset_id]
        slot = f"{root}/{JOB_LOG_PROBE_KEY}"
        log = f"{slot}/probe.jsonl"
        stem = job_unit_stem(layout, principal, DEFAULT_HARDENING_PROFILE)
        lines += [
            f"# --- {principal.value}（{account}／{stem}@／資產 {spool.asset_id}）---",
            f"#   通道：{spool.channel_asset_id} = {channel}",
            "#   1) 那一格由**產品程式碼**建（`spool_slot.prepare_job_log`），不是",
            "#      operator 自己建目錄／建空檔——手工前置物會剛好繞過本票要驗的缺陷",
            "#      （#645 逐字記錄過同型前例；本票就是那一族的下一個成員）。",
            f"sudo -u {manager} {python} -c "
            f"'from paulsha_cortex.coordinator import spool_slot as s; "
            f's.prepare_job_log("{slot}", "{log}")\'',
            "#   2) 正向：零額外 env、真實加固面下，job 身分 append 得進去。",
            f"{PATH_PROBE_HELPER} {stem} /bin/sh -c 'printf \"JOB-LOG-WRITABLE\\n\" >> {log}'",
            "#      期望：rc=0、無輸出。`Permission denied` ⇒ ACL mask 把繼承來的 `w`",
            "#      壓成 `#effective:---`（#638 缺陷 1）；`Read-only file system` ⇒",
            "#      這一格沒被 `_minimize()` 涵蓋進模板 unit 的 ReadWritePaths。",
            "#   3) Manager 讀得回來（檔案 owner 是 Manager、mode 0620）。",
            f"sudo -u {manager} cat {log}",
            "#      期望逐字：JOB-LOG-WRITABLE",
            "#   4) **反向**：同一個身分對 Manager 的 dispatch log 目錄仍然寫不進去。",
            f"{PATH_PROBE_HELPER} {stem} /bin/sh -c "
            f"'printf x >> {layout.coordinator_root}/logs/workflow/{JOB_LOG_PROBE_KEY}'",
            "#      期望：**非零**，且訊息是 `Read-only file system`（mount 層）或",
            "#      `Permission denied`（DAC）。rc=0 ⇒ 有人把那一層開成可寫面了，",
            "#      gate ledger 與 exit sentinel 當場不再是 Manager 專屬（#604）。",
            "#   5) 收乾淨。",
            f"sudo -u {manager} rm -rf {slot}",
            "",
        ]
    lines += _wrap_comment(
        "本探針量的是 **mount ＋ ACL 兩層的實際結果**，證明不了「Manager 派得出那個 "
        "job」——那一維要走 runbook 的真實 dispatch smoke（#687 的 D13 caveat 逐字適用："
        "psc_run_under 複製的是加固面，不是派工路徑）。兩維都要有實跑證據。"
    )
    return lines


#: #710 探針拿來當 per-job 工作區的那個 job id。**固定字面量**，理由與
#: :data:`JOB_LOG_PROBE_KEY` 逐字相同：operator 重跑時同一格整個重建，不會在 pool 裡
#: 留下一堆 `probe-<timestamp>` 需要人去掃。
JOB_WORKSPACE_PROBE_JOB_ID = "probe-710"


def build_job_workspace_probe(
    scheme: UidScheme,
    layout: "PathLayout" = None,  # type: ignore[assignment]
) -> list[str]:
    """#710 反向不變式的實機探針（**只回傳字串，不執行**）。

    要證的是**三個方向**，缺一不可：

    1. **正向**——每個降權 principal 在**零額外 env**、真實模板 unit 的加固面下
       `cd` 得進自己的工作區，而且該做的事做得到（builder 寫得出檔、reviewer 讀得到、
       gate 建得出自己那一格）；
    2. **反向（per-job 隔離）**——builder 的工作區對**別的 job 帳號**仍然進不去。
       少了這一半，「只下在 per-job 那一格」就只是一句宣稱：真正要排除的失敗是
       「順手往 pool 根加一條 default ACL 讓它過」，而那會讓每個 job 帳號進得去每個
       job 的目錄（裁決 10-2 當場歸零）；
    3. **mask**——判準是 `getfacl` 的 `mask::` 與 `#effective:`，**不是**「ACL 行
       存在」。任何 `chmod` 都會重寫 mask，具名條目於是靜默失效（runbook 4e-2b）。

    加固面複本一律由 `psc_run_under` → :func:`unit_replica_properties` 從**落檔的
    unit** 全量導出（design D13）。**本產生器不自組 `--property=`、不帶 `--setenv=`**。

    ⚠️ **工作區由真實 provisioning 產生，不得手工前置**：builder 那一格由
    `coordinator/seams.ScriptWorktreeCreator` 建 clone、由
    `coordinator/job_runner.ensure_workspace_reachable()` 授權——`#645` 逐字記錄過
    「手工前置物剛好把 bug 繞過去」的代價，而本票（#710）正是那一族的下一個成員
    （M1 當時 operator 手工建了工作區、恰好避開了這個缺陷）。探針因此**呼叫產品
    程式碼**，不自己建目錄再 `setfacl`。
    """

    layout = layout if layout is not None else DEFAULT_LAYOUT
    manager = scheme.durable_state_owner
    paths_by_asset = layout.asset_paths()
    python = f"{layout.deploy_root}/venv/bin/python3"
    job_segment = f"{JOB_WORKSPACE_PROBE_JOB_ID}-<hash>"
    lines: list[str] = [
        "# === #710 反向不變式：三個降權 principal 各自進得去自己的工作區嗎 ===",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}）——勿手改；重跑：",
        f"#   python3 -m paulsha_cortex.trust_root workspace-probe {scheme.scheme_id}",
        "#",
    ]
    lines += _wrap_comment(
        "本探針**刻意不帶任何 `--setenv=`、也不自組 `--property=`**（design D13）。"
        "加固面複本由 `unit_replica_properties()` 從落檔的 unit 全量導出，因此它連"
        "「production 沒有設什麼」也一起複製。"
    )
    lines += [
        "#",
        "# ⛔ 任何一條紅掉時**不要**往 pool 根加 default ACL 讓它過——那會讓每個 job",
        "#    帳號進得去每個 job 的目錄，per-job 隔離當場歸零（裁決 10-2）。要改的是",
        "#    登記表 JOB_WORKSPACE_REACH 那一列，以及它在執行期的那一支實作。",
        "# ⛔ 也不要在 setfacl 之後補 `chmod`：那會把 ACL mask 重寫成 mode 的 group 位，",
        "#    具名條目**靜默失效**（runbook 4e-2b）。判準永遠是 getfacl 的 mask::。",
        "#",
        f"# 前置：先貼上 runbook 第 4e 步的**共用探針** `{PATH_PROBE_HELPER}`。",
        f"declare -F {PATH_PROBE_HELPER} >/dev/null || {{",
        f"  echo \"⛔ 未定義 {PATH_PROBE_HELPER}——先貼上 runbook 第 4e 步的共用探針\" >&2",
        "  return 1 2>/dev/null || exit 1",
        "}",
        "",
    ]
    for principal in downgraded_job_principals(scheme):
        account = scheme.resolve(principal)
        if account is None:  # pragma: no cover - downgraded_job_principals 已過濾
            continue
        reach = registry.job_workspace_reach_for(principal)
        stem = job_unit_stem(layout, principal, DEFAULT_HARDENING_PROFILE)
        lines += [
            f"# --- {principal.value}（{account}／{stem}@／形態 {reach.reach.value}）---",
        ]
        if reach.reach is registry.WorkspaceReach.PER_JOB_NAMED_ACL:
            pool = paths_by_asset[reach.pool_asset_ids[0]]
            workspace = f"{pool}/{job_segment}"
            lines += [
                "#   1) 工作區由**產品程式碼**建並授權，不是 operator 自己建目錄 ＋",
                "#      setfacl——手工前置物會剛好繞過本票要驗的缺陷（#645／#710）。",
                f"sudo -u {manager} {python} - <<'PSC_710_EOF'",
                "from paulsha_cortex.config import paths",
                "from paulsha_cortex.coordinator import job_runner, job_workspace, seams",
                f'job_id = "{JOB_WORKSPACE_PROBE_JOB_ID}"',
                "creator = seams.ScriptWorktreeCreator()",
                'workspace = creator.create("feature/probe-710", job_id=job_id)',
                "print(job_runner.ensure_workspace_reachable(",
                '    __import__("os").environ, role=job_runner.JOB_ROLE_BUILDER,',
                "    workspace=workspace))",
                "print(workspace)",
                "PSC_710_EOF",
                "#      期望：兩行——第一行是實際執行的 `setfacl -R -m …` 命令，",
                "#      第二行是工作區絕對路徑（下面以 $WS 代稱）。",
                "#   2) mask 判準（**不是**「ACL 行存在」）。",
                f"sudo -u {manager} getfacl -p $WS",
                f"#      期望逐字含 `user:{account}:rwx` 且 `mask::rwx`；",
                f"#      看到 `user:{account}:rwx\\t#effective:---` ⇒ 有人在 setfacl 之後",
                "#      又 chmod 了一次（runbook 4e-2b 的陷阱）。",
                f"sudo -u {manager} getfacl -p $WS/.git/HEAD",
                f"#      期望：**樹裡面**也有 `user:{account}` 且 effective 帶 r——",
                "#      只在樹根下一條 ACL 的話 job 進得去卻讀不到任何東西。",
                "#   3) 正向：零額外 env、真實加固面下，job 身分進得去且寫得出檔。",
                f"{PATH_PROBE_HELPER} {stem} /bin/sh -c 'cd $WS && "
                "printf JOB-WORKSPACE-WRITABLE > .psc-710-probe && cat .psc-710-probe'",
                "#      期望：rc=0、輸出 `JOB-WORKSPACE-WRITABLE`。",
                "#      `Permission denied` ⇒ DAC 層（ACL／mask）；",
                "#      `Read-only file system` ⇒ mount 層（模板 unit 的 ReadWritePaths）。",
            ]
            others = [
                other
                for other in downgraded_job_principals(scheme)
                if other is not principal
            ]
            for other in others:
                other_account = scheme.resolve(other)
                other_stem = job_unit_stem(layout, other, DEFAULT_HARDENING_PROFILE)
                lines += [
                    f"#   4) **反向**：{other.value}（{other_account}）不得進得去這一格。",
                    f"{PATH_PROBE_HELPER} {other_stem} /bin/sh -c 'cd $WS && ls'",
                    "#      期望：**非零**（`Permission denied`）。rc=0 ⇒ 有人把授權下在",
                    "#      pool 根上了，per-job 隔離歸零（裁決 10-2）。",
                ]
            lines += [
                "#   5) 收乾淨。",
                f"sudo -u {manager} {python} -c "
                "'from paulsha_cortex.coordinator import job_workspace as w; "
                "import sys; w.remove_clone(sys.argv[1])' $WS",
                "#      ⚠️ builder 在步驟 3 建的檔由 builder 擁有 ⇒ 這一步可能失敗",
                "#      （回收面的已知邊界，見登記表 repo-worktree 的 note）；",
                f"#      失敗時以 `sudo rm -rf $WS` 收尾，並記在 runbook 的觀察欄。",
                "",
            ]
        elif reach.reach is registry.WorkspaceReach.INHERITED_DEFAULT_ACL:
            for pool_id in reach.pool_asset_ids:
                pool = paths_by_asset[pool_id]
                lines += [
                    f"#   繼承來源：{pool_id} = {pool}",
                    f"sudo -u {manager} getfacl -p {pool}",
                    f"#      期望逐字含 `default:user:{account}:{reach.default_perms.lower()}`",
                    "#      ——Manager 在裡面建的每一格靠它繼承（POSIX：目錄帶 default ACL",
                    "#      時 umask 不生效，因此 unit 的 UMask=0077 不會壓掉 mask）。",
                    f"{PATH_PROBE_HELPER} {stem} /bin/sh -c 'cd {pool} && ls >/dev/null'",
                    "#      期望：rc=0。",
                ]
            lines += [
                "#   ⛔ 反向：本帳號對工作區**唯讀**是刻意的（交付走 spool）。",
                f"{PATH_PROBE_HELPER} {stem} /bin/sh -c "
                f"'printf x > {paths_by_asset[reach.pool_asset_ids[0]]}/.psc-710-probe'",
                "#      期望：**非零**（`Read-only file system` 或 `Permission denied`）。",
                "",
            ]
        else:  # POOL_OWNED_BY_JOB
            pool = paths_by_asset[reach.pool_asset_ids[0]]
            lines += [
                f"#   pool 根：{reach.pool_asset_ids[0]} = {pool}",
                f"sudo -u {manager} stat -c '%U:%G %a' {pool}",
                f"#      期望逐字：`{account}:{scheme.group_of(account)} 700`——本形態的",
                "#      可達性只來自 owner 位，owner 一漂走 gate 就建不出自己的副本。",
                f"{PATH_PROBE_HELPER} {stem} /bin/sh -c "
                f"'cd {pool} && : > .psc-710-probe && rm -f .psc-710-probe'",
                "#      期望：rc=0（gate 的第一個動作就是在這裡複製出一格）。",
                "",
            ]
    lines += _wrap_comment(
        "本探針量的是 **mount ＋ ACL 兩層的實際結果**，證明不了「Manager 派得出那個 "
        "job」——那一維要走 runbook 的真實 dispatch smoke（#687／#709 的 D13 caveat "
        "逐字適用：`psc_run_under` 複製的是加固面，不是派工路徑）。步驟 1 之所以呼叫 "
        "`seams.ScriptWorktreeCreator` 與 `job_runner.ensure_workspace_reachable()` "
        "而不是手工建目錄，就是為了讓被驗的那一格與派工路徑產出的那一格是**同一份**"
        "程式碼的結果。兩維都要有實跑證據。"
    )
    return lines


#: #712 探針拿來當**兩格** per-job 工作區的 job id（自己那一格 ／ 別人那一格）。
#: 固定字面量，理由同 :data:`JOB_WORKSPACE_PROBE_JOB_ID`。
GIT_TRUST_PROBE_JOB_IDS = ("probe-712-self", "probe-712-other")


def build_job_git_trust_probe(
    scheme: UidScheme,
    layout: "PathLayout" = None,  # type: ignore[assignment]
) -> list[str]:
    """#712 反向不變式的實機探針（**只回傳字串，不執行**）。

    要證的是**三個方向**，缺一不可：

    1. **缺陷還在**（零額外 env 的基線）——builder 身分在自己的工作區裡跑 `git status`
       **必須失敗**，且逐字是 `fatal: detected dubious ownership`。這一步證明：檔案
       系統層已經通了（#710 的 ACL），擋住的是 git 自己那一層；同時證明**靜態
       `.gitconfig` 真的沒有涵蓋這一格**——若哪天有人「順手加一條萬用字元讓它過」，
       這一步會**變成 rc=0**，探針當場說出真相。
    2. **正向**——經**真實派工**（`systemctl start --wait <模板實例>`，spec 由
       `job_runner.build_job_env()`／`build_job_spec()` 產出）之後，同一支 `git status`
       與 builder 真正會跑的 `git bundle create` **都成功**。
    3. **反向（放行是 per-job，不是全域）**——**同一個** job、**同一份** env，在
       **別的 job 的工作區**裡跑 `git status` **必須仍然失敗**。少了這一半，「逐 job
       放行」就只是一句宣稱：真正要排除的失敗是有人把值換成字面 `*` 或換成 pool 根
       ——那兩種寫法會讓這一步變成 rc=0。

    ## 為什麼這一支走**真實派工**，而 #710 那一支走 `psc_run_under`

    #709／#687 逐字記過那條 caveat：`psc_run_under` 複製的是**加固面**，不是派工
    路徑。#710 要驗的是 mount ＋ ACL 兩層的結果，加固面複本就夠；**本票要驗的東西
    住在 spec 的 env 裡**，而 spec 只有派工路徑才會產生。用 `--setenv=` 自己塞三個
    變數進去，量到的就只是「我塞的東西生效了」——那不是 `build_job_env()` 會不會替
    這個 job 算出它們。

    因此：步驟 1 的**基線**走 `psc_run_under`（零額外 env，證明缺陷與加固面同時
    存在），步驟 2／3 走**真實 unit ＋ 真實 spec**。**本產生器一行 `--property=` 都
    不自組、一個 `--setenv=` 都不帶**（design D13）。

    ⚠️ **兩格工作區都由真實 provisioning 產生**（`seams.ScriptWorktreeCreator` ＋
    `job_runner.ensure_workspace_reachable()`），不手工建目錄——`#645` 逐字記錄過
    手工前置物會剛好把 bug 繞過去，而 `#710`／`#712` 是那一族連續兩個成員。
    """

    layout = layout if layout is not None else DEFAULT_LAYOUT
    manager = scheme.durable_state_owner
    builder_account = scheme.resolve(Principal.BUILDER)
    python = f"{layout.deploy_root}/venv/bin/python3"
    stem = job_unit_stem(layout, Principal.BUILDER, DEFAULT_HARDENING_PROFILE)
    self_id, other_id = GIT_TRUST_PROBE_JOB_IDS
    lines: list[str] = [
        "# === #712 反向不變式：per-job clone 的 safe.directory 逐 job 放行了嗎 ===",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}）——勿手改；重跑：",
        f"#   python3 -m paulsha_cortex.trust_root git-trust-probe {scheme.scheme_id}",
        "#",
    ]
    lines += _wrap_comment(
        "git 的 dubious-ownership 是 **owner 判準，不是權限判準**：#710 的 ACL 實機生效"
        "（`getfacl` 逐字 `user:"
        f"{builder_account}"
        ":rwx`／`mask::rwx`）之後 builder job 真的跑起來了，然後死在 "
        "`fatal: detected dubious ownership` ＋ `fatal: Need a repository to create a "
        "bundle.`。因此本探針與 #710 的 `workspace-probe` **兩支都要跑**，它們量的是"
        "不同的兩層。"
    )
    lines += [
        "#",
        "# ⛔ 步驟 3 紅掉時**不要**把值改成字面 `*`、也不要改成 pool 根——前者等於對這個",
        "#    帳號整個關掉 dubious-ownership 保護（opt-out，不是授權），後者等於整個 pool",
        "#    的放行。要改的是登記表 JOB_GIT_WORKSPACE_TRUST 那一列與它的執行期實作。",
        "# ⛔ 也不要往靜態 .gitconfig 加任何一條：那份檔只放來源樹，per-job 那一格路徑",
        f"#    帶 `<job-id>`，靜態檔按 git 的逐字相等規則就裝不下（permgen 的 import 期",
        "#    斷言會擋下這兩種改法）。",
        "#",
        f"# 前置：先貼上 runbook 第 4e 步的**共用探針** `{PATH_PROBE_HELPER}`。",
        f"declare -F {PATH_PROBE_HELPER} >/dev/null || {{",
        f"  echo \"⛔ 未定義 {PATH_PROBE_HELPER}——先貼上 runbook 第 4e 步的共用探針\" >&2",
        "  return 1 2>/dev/null || exit 1",
        "}",
        "",
        "#   0) **兩格**工作區都由產品程式碼建並授權（不手工前置，#645／#709）。",
        f"sudo -u {manager} {python} - <<'PSC_712_EOF'",
        "import os",
        "from paulsha_cortex.coordinator import job_runner, seams",
        "creator = seams.ScriptWorktreeCreator()",
        f'for job_id in {list(GIT_TRUST_PROBE_JOB_IDS)!r}:',
        '    workspace = creator.create("feature/probe-712", job_id=job_id)',
        "    job_runner.ensure_workspace_reachable(",
        "        os.environ, role=job_runner.JOB_ROLE_BUILDER, workspace=workspace)",
        "    print(f'{job_id}\\t{workspace}')",
        "PSC_712_EOF",
        f"#      期望：兩行 `<job-id>\\t<絕對路徑>`；下面以 $WS_SELF（{self_id}）與",
        f"#      $WS_OTHER（{other_id}）代稱。",
        "",
        "#   1) **基線**：零額外 env、真實加固面下，git 必須**擋住**自己的工作區。",
        f"{PATH_PROBE_HELPER} {stem} /bin/sh -c 'cd $WS_SELF && git status --porcelain'",
        "#      期望：**非零**，且 stderr 逐字含 `detected dubious ownership`。",
        "#      rc=0 ⇒ 有人已經在別處放行了整棵樹（靜態 .gitconfig 的萬用字元？），",
        "#      而那正是本票否決過的形態——先查 `sudo -u "
        + f"{builder_account} git config --list --show-origin | grep safe`。",
        "",
        "#   2) 正向 ＋ 3) 反向：**同一次真實派工**（spec 由產品程式碼產生，env 因此",
        "#      是 `build_job_env()` 真的替這個 job 算出來的那一份，不是探針塞的）。",
        f"sudo -u {manager} {python} - <<'PSC_712_EOF'",
        "import os",
        "from paulsha_cortex.coordinator import job_runner, job_workspace",
        f'job_id = "{self_id}"',
        "ws_self = os.environ['WS_SELF']",
        "ws_other = os.environ['WS_OTHER']",
        "plan = job_runner.prepare_systemd_template(",
        "    os.environ, job_id=job_id, executor='codex',",
        "    role=job_runner.JOB_ROLE_BUILDER)",
        "env = job_runner.build_job_env(",
        "    manager_env=os.environ, job_id=job_id, slice_id=job_id,",
        "    repo_root=os.environ.get('PSC_REPO_ROOT', ''), workspace=ws_self,",
        "    role=job_runner.JOB_ROLE_BUILDER)",
        "print('SPEC ENV（git 那三條）:', {k: v for k, v in env.items()",
        "                                if k.startswith('GIT_CONFIG')})",
        "script = (",
        "    'set -x; '",
        "    'git status --porcelain --branch || exit 21; '",
        "    'git bundle create /tmp/psc-712.bundle --all || exit 22; '",
        "    'git -C \"$1\" status --porcelain && exit 23; '",
        "    'echo PSC-712-OK'",
        ")",
        "spec = job_runner.build_job_spec(",
        "    job_id=job_id, instance=plan.instance, unit=plan.unit,",
        "    command=['/bin/sh', '-c', script, 'psc-712', ws_other],",
        "    working_directory=ws_self,",
        "    log_path=str(job_workspace.prepare_job_log_spool(",
        "        principal_id=job_runner.JOB_ROLE_CONFIG[",
        "            job_runner.JOB_ROLE_BUILDER].log_spool_principal,",
        "        spool_key=job_id, manager_log_path=f'/tmp/psc-712-{job_id}.jsonl')),",
        "    env=env)",
        "job_runner.write_job_spec(plan.spec_path, spec, account=plan.account)",
        "print('UNIT', plan.unit)",
        "print('LOG ', spec['log_path'])",
        "PSC_712_EOF",
        "#      期望：印出 `SPEC ENV（git 那三條）`（三個鍵齊全、`GIT_CONFIG_VALUE_0`",
        "#      逐字等於 $WS_SELF）、`UNIT`、`LOG`。以下用 $PSC_712_UNIT／$PSC_712_LOG。",
        "",
        "#      起動：**真實派工路徑**（模板 unit ＋ root-owned shim 讀 spec）。",
        f"sudo -u {manager} systemctl start --wait $PSC_712_UNIT",
        "cat $PSC_712_LOG",
        "#      期望（讀 job 自己那一格 log spool，路徑由上一段印出）：",
        "#        - `git status` rc=0（**正向**：放行對自己的工作區生效）；",
        "#        - `git bundle create` rc=0（builder 真正會跑的那一支，#712 的原症狀",
        "#          逐字是 `fatal: Need a repository to create a bundle.`）；",
        "#        - 對 $WS_OTHER 的 `git status` **非零**且逐字 `detected dubious",
        "#          ownership`（**反向**：放行是 per-job，不是全域）；",
        "#        - 最後一行 `PSC-712-OK`。",
        "#      exit 21／22 ⇒ 正向那兩條之一沒過；exit 23 ⇒ **反向那條破了**",
        "#      （別的 job 的工作區也被放行了——立刻查 GIT_CONFIG_VALUE_0 是不是被改成",
        "#      `*` 或 pool 根）。",
        "",
        "#   4) 收乾淨（兩格都要）。",
        f"sudo -u {manager} {python} -c "
        "'from paulsha_cortex.coordinator import job_workspace as w; "
        "import sys; [w.remove_clone(p) for p in sys.argv[1:]]' $WS_SELF $WS_OTHER",
        "#      ⚠️ builder 在步驟 2 建的檔由 builder 擁有 ⇒ 這一步可能失敗（回收面的",
        "#      已知邊界，見登記表 repo-worktree 的 note）；失敗時以 `sudo rm -rf` 收尾。",
        "",
    ]
    lines += _wrap_comment(
        "步驟 1 走 `psc_run_under`（加固面複本、零額外 env），步驟 2／3 走**真實派工**"
        "——這是刻意的分工：#709／#687 記過 `psc_run_under` 複製的是加固面、不是派工"
        "路徑，而本票要驗的東西（spec 的 env）只有派工路徑才會產生。以 `--setenv=` "
        "自己塞那三個變數進去，量到的只會是「我塞的東西生效了」。"
    )
    return lines


def build_inner_sandbox_probe(
    scheme: UidScheme,
    layout: "PathLayout" = None,  # type: ignore[assignment]
    executor: str = "codex",
) -> list[str]:
    """#714 反向不變式的實機探針（**只回傳字串，不執行**）。

    要證的是**四個方向**，缺一不可——少了任何一個，「內層沙箱還在」就只是一句宣稱：

    1. **缺陷還在**（不帶旗標的負向對照）——executor 的**預設**內層沙箱形態在真實
       加固面下**必須仍然失敗**，且逐字是 `bwrap: Can't read
       /proc/sys/kernel/overflowuid`。這一步同時守住兩件事：外層加固面沒有被人「順手
       放寬 `ProcSubset`」（那會讓這一步變成 rc=0），以及本票選的形態切換**真的是**
       讓它通的原因。
    2. **旗標還在**——`inner_sandbox.argv` 不得換來 `Unknown feature flag`。**這是本
       探針存在的主要理由**：那個旗標名帶 `legacy`，是對 codex 某一版的觀察，不是不
       變式（PR #713 的教訓方向相反但同一族：那次把某版 git 的行為寫成不變式）。
    3. **正向**——帶上旗標之後，同一條命令在**同一份**加固面下 rc=0。
    4. **內層真的在擋**——沙箱不是「裝上了就算」：寫工作區外的檔必須 `Permission
       denied`、對外查名必須失敗。少了這一半，「旗標吃下去了但沒有沙箱」與「沙箱生效」
       在輸出上長得一模一樣，而那正是 `accepted_loss` 裡最不能靜默的那一格。

    ## 為什麼這一支走 `psc_run_under`，而不是真實派工

    要驗的東西**住在加固面與 executor argv 上**，兩者都由加固面複本忠實帶到
    （`unit_replica_properties()` 全量導出，含 `Environment=`）。#709／#687 那條
    caveat 講的是「spec 的 env 只有派工路徑才會產生」——本票不驗 spec 的 env。

    ⚠️ 但 **#714 的驗收本身仍須走真實派工**：「builder 執行得了命令且產得出非空
    bundle」那一條要的是端到端結果，不是加固面複本。本探針是**回歸守衛**，不是驗收。

    **本產生器一行 `--property=` 都不自組、一個 `--setenv=` 都不帶**（design D13）。
    """

    layout = layout if layout is not None else DEFAULT_LAYOUT
    spec = executor_inner_sandbox(executor)
    if spec is None:
        raise ValueError(
            f"executor {executor!r} 沒有登記 inner_sandbox——沒有內層沙箱就沒有這條"
            "反向不變式可驗（#714）。"
        )
    profile = executor_hardening_profile(executor)
    stem = job_unit_stem(layout, Principal.BUILDER, profile)
    binary = f"{layout.toolchain_root}/bin/{executor}"
    # 第 4 步要「外層允許、內層擋」的那一格：runbook 第 4e 步的共用探針前置已經要求
    # 建出 `<worktree pool>/probe`（`--instance probe` 的 `ReadWritePaths=%i`）。
    probe_workspace = f"{layout.worktree_root}/probe"
    flag = " ".join(spec.argv)
    groups = " ".join(spec.syscall_groups)
    lines: list[str] = [
        "# === #714 反向不變式：executor 的內層沙箱還裝得上、而且真的在擋嗎 ===",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}，executor={executor}）"
        "——勿手改；重跑：",
        f"#   python3 -m paulsha_cortex.trust_root inner-sandbox-probe {scheme.scheme_id}",
        "#",
    ]
    lines += _wrap_comment(
        f"形態：{spec.kind}（argv `{flag}`，需要 `{groups}`）。"
        f"{spec.note}"
    )
    lines += [
        "#",
        f"# 前置：先貼上 runbook 第 4e 步的**共用探針** `{PATH_PROBE_HELPER}`。",
        f"declare -F {PATH_PROBE_HELPER} >/dev/null || {{",
        f"  echo \"⛔ 未定義 {PATH_PROBE_HELPER}——先貼上 runbook 第 4e 步的共用探針\" >&2",
        "  return 1 2>/dev/null || exit 1",
        "}",
        "",
        "#   0) 加固面上真的有那組群組（D13：讀**落檔的** unit，不是讀產生器）。",
        f"sudo systemctl cat {stem}@.service | grep '^SystemCallFilter='",
        f"#      期望：值裡含 `{groups}`。沒有 ⇒ 落檔的是舊版產生器的產物，重跑第 5-2",
        "#      步落檔再回來；**不要**在這裡手動加 property 把它蓋過去。",
        "",
        "#   1) **負向對照**：不帶旗標＝executor 的預設內層沙箱形態，必須**仍然失敗**。",
        f"{PATH_PROBE_HELPER} {stem} {binary} sandbox -- /bin/pwd",
        "#      期望：非零，且 stderr 逐字含 `Can't read /proc/sys/kernel/overflowuid`。",
        "#      rc=0 ⇒ 有人放寬了 `ProcSubset`（或 executor 換了預設形態）——那會讓本票",
        "#      的整個論證失效，**停下來查清楚**，不要因為「反正也是綠的」就放過。",
        "",
        f"#   2) 旗標還在：`{flag}` 不得換來 `Unknown feature flag`。",
        f"{PATH_PROBE_HELPER} {stem} {binary} sandbox {flag} -- /bin/true",
        "#      期望：rc=0。stderr 出現 `Unknown feature flag` ⇒ **上游把旗標拿掉了**：",
        "#      內層沙箱從此不存在，而 job 會照跑。處置是回到 permgen 的 "
        "`EXECUTOR_TOOLS`",
        "#      那一列重新量一次形態，**不是**把這條探針刪掉。",
        "",
        "#   2b) **早期警報**：上游對這個旗標的 deprecation 宣告（0819 起就有）。",
        f"{PATH_PROBE_HELPER} {stem} {binary} sandbox {flag} -- /bin/true 2>&1 \\",
        "  | grep -i 'deprecat' || echo '（沒有 deprecation 訊息）'",
        "#      0819 實機逐字：`[features].use_legacy_landlock is deprecated and will be",
        "#      removed soon.`——**這是倒數，不是穩態**。它還在印，代表旗標還在；哪天它",
        "#      不見了要先確認是「上游收回宣告」還是「旗標已經被拿掉」（看第 2 步）。",
        "#      ⚠️ 這句話會以 `item.type=error` 進 `--json` 串流。它**不影響** terminal",
        "#      契約（`_extract_terminal_json()` 由尾端往回找 `agent_message`），但看到",
        "#      job log 開頭有一筆 error 時不要誤判成 job 失敗。",
        "",
        "#   3) 正向：同一份加固面下，帶旗標就通。",
        f"{PATH_PROBE_HELPER} {stem} {binary} sandbox {flag} -- /bin/pwd",
        "#      期望：rc=0，stdout 是 job 的 cwd。",
        "",
        "#   4) 內層真的在擋（**這一段不可省略**——裝上了不等於有在擋）。",
        "#",
        "#      ⚠️ **每一條都要有成對的對照組。** 拿「寫 job HOME 被擋」當證據是**假的**",
        "#      ——那一格本來就不在 `ReadWritePaths=` 內，`ProtectSystem=strict` 會先回",
        "#      `Read-only file system`，內層有沒有裝上完全看不出來。要證明內層在擋，被",
        "#      擋的那一格必須是**外層允許**的那一格。",
        "",
        f"#      4a) 對照：**沒有**內層沙箱時，外層允許寫 {probe_workspace}",
        f"{PATH_PROBE_HELPER} {stem} /bin/sh -c \\",
        f"  'cd {probe_workspace} && : > psc-714-outer && echo OUTER_ALLOWS && "
        "rm -f psc-714-outer'",
        "#          期望：`OUTER_ALLOWS`、rc=0。",
        "#      4b) **同一格**，帶內層沙箱 ⇒ 必須被擋。",
        f"{PATH_PROBE_HELPER} {stem} {binary} sandbox {flag} -- /bin/sh -c \\",
        f"  'cd {probe_workspace} && : > psc-714-inner && echo INNER_LEAK'",
        "#          期望：非零，逐字 `Permission denied`（landlock 擋寫）。",
        "#          印出 `INNER_LEAK` ⇒ 旗標吃下去了但沙箱沒生效。",
        "#      4c) 網路：帶內層沙箱 ⇒ 查不到名。",
        f"{PATH_PROBE_HELPER} {stem} {binary} sandbox {flag} -- /bin/sh -c \\",
        "  'getent hosts api.openai.com'",
        "#          期望：非零（seccomp 擋網路）。",
        "#      4d) 對照：**沒有**內層沙箱時查得到（外層的 RestrictAddressFamilies 放行",
        "#          AF_INET，所以這一條 rc=0 才代表 4c 的失敗真的來自內層）。",
        f"{PATH_PROBE_HELPER} {stem} /bin/sh -c 'getent hosts api.openai.com'",
        "#          期望：rc=0，印出 A 記錄。",
        "#      ⚠️ 4b／4c **任一**變成 rc=0（而對照組正常）⇒ 內層沒生效——那是最壞的",
        "#      狀態（看起來一切正常，實際少一層），比整個起不來還難發現。",
        "",
    ]
    lines += _wrap_comment(
        "本探針是**回歸守衛**，不是 #714 的驗收。驗收是「builder job 執行得了 shell "
        "命令**且產得出非空 bundle**」，那一條必須走真實派工（#709 的 caveat："
        f"`{PATH_PROBE_HELPER}` 複製的是加固面、不是派工路徑），工作區由真實 "
        "provisioning 產生（#645：手工前置物會把 bug 繞過去）。"
    )
    lines += _wrap_comment(
        "**明載的取捨**（`InnerSandboxSpec.accepted_loss`，本探針量不到）："
        + "；".join(spec.accepted_loss)
    )
    return lines


def evaluate_polkit(
    rule: PolkitRule,
    *,
    user: str,
    action_id: str,
    unit: str | None = None,
    verb: str | None = None,
) -> str:
    """規則決策的 Python 鏡像（polkit 無法本機執行，故以純函式測產生邏輯）。

    與 `build_polkit_rule` 產出的 JS **共用同一組常數**（subject／action／verbs／
    pattern），因此決策矩陣測到的就是規則檔的語意。回傳 `"YES"`／`"NO"`／
    `"NOT_HANDLED"`。
    """
    if user != rule.subject_account:
        return "NOT_HANDLED"
    if action_id != POLKIT_ACTION:
        return "NO"
    if not unit or not verb:
        return "NO"
    if verb not in rule.allowed_verbs:
        return "NO"
    if re.search(rule.unit_pattern, unit) is None:
        return "NO"
    return "YES"
