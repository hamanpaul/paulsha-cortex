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
from typing import Mapping, Sequence

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


# ---------------------------------------------------------------------------
# 四個模型 executor 的實體形態（#640 實機盤點）
#
# 形態不同 ⇒ 搬進部署樹的方式不能一概而論，因此固化成一張表：runbook 的安裝步驟與
# 測試都由它導出，新增／換掉一個 executor 只改這裡一列。
# ---------------------------------------------------------------------------

class ExecutorShape(Enum):
    """executor 在檔案系統上的實體形態。"""

    NODE_SCRIPT = "node-script"    # `#!/usr/bin/env node` ＋ JS 本體（需 node runtime）
    NATIVE_ELF = "native-elf"      # 自帶原生執行檔（不依賴任何 runtime）
    SHELL_SCRIPT = "shell-script"  # shell script（可能再叫別的程式，安裝時要查一次）


@dataclass(frozen=True)
class ExecutorTool:
    """一個模型 CLI 的搬移契約。"""

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

#: 走**系統層**的通用 runtime（裁決 (a) 的另一半）。node 換版本幾乎不影響模型輸出，
#: 因此不進部署樹；但它仍是**部署決定**——某個 CLI 哪天提高下限（目前 codex 宣告
#: `node >=16`，apt 候選 20.x 可用）時要一併升，否則它會變成下一個無聲漂移點。
TOOLCHAIN_SYSTEM_RUNTIMES: tuple[str, ...] = ("node",)

#: job `PATH` 的系統層尾段（toolchain 之後）。`node`（codex 的 runtime）、`git`、
#: wrapper 內的 `python3` 都在這裡。刻意不含任何 `sbin`。
JOB_PATH_SYSTEM_TAIL: tuple[str, ...] = ("/usr/local/bin", "/usr/bin", "/bin")


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

    DEPLOYMENT = "deployment"        # enforcement plane：owner＝deploy/root
    MANAGER_STATE = "manager-state"  # Manager-owned durable state：owner＝durable_state_owner
    JOB = "job"                      # job-visible：owner＝對應 job 帳號（或 runtime 逐案 chown）


@dataclass(frozen=True)
class AclEntry:
    """單條 POSIX ACL（供跨帳號的精確授權；Manager-owned 上只會出現唯讀條目）。"""

    account: str
    perms: str          # "rX"（讀，dir 自動含 traverse）／"rwx"／"wx"
    default: bool = False  # 是否為 default ACL（dir 內新建物件繼承）

    @property
    def writable(self) -> bool:
        return "w" in self.perms

    def render(self, path: str) -> str:
        flag = "-d -m" if self.default else "-m"
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
            "writer_accounts": sorted(self.writer_accounts),
            "reader_accounts": sorted(self.reader_accounts),
            "acls": [
                {"account": a.account, "perms": a.perms, "default": a.default}
                for a in self.acls
            ],
            "runtime_managed": self.runtime_managed,
            "legacy_writers": list(self.legacy_writers),
            "rationale": self.rationale,
            "open_points": list(self.open_points),
        }

    def commands(self, path: str) -> list[str]:
        """產生本資產的 chown／chmod／setfacl 命令字串（**只回傳字串，不執行**）。

        `path` 由呼叫端提供（runbook 以 shell 變數帶入真實路徑）；未提供具體路徑時
        以清楚標記的 placeholder 呈現。
        """
        cmds = [
            f"chown {self.owner}:{self.group} {path}",
            f"chmod {self.mode_str} {path}",
        ]
        for acl in self.acls:
            cmds.append(acl.render(path))
            # dir 需同時設 access 與 default ACL，讓新建物件繼承。
            if self.is_directory and not acl.default:
                cmds.append(AclEntry(acl.account, acl.perms, default=True).render(path))
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
})
_FILE_ASSET_IDS = frozenset({
    "control-daemon-lock",
    "control-status",
    # #640：憑證是**單一檔**（`<HOME>/<relpath>`）。這條明列不是裝飾——file/dir 的
    # 判定直接決定 permgen 會不會把一整個目錄 chown 給 job 帳號，而該目錄必須維持
    # root-owned 才有「能改內容、不能增刪換」那條性質。
    "builder-executor-credential",
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

    - enforcement plane（`DEPLOYMENT_WRITE`，或 writer 含 INSTALLER 的 bootstrap env）
      → DEPLOYMENT（owner＝root/deploy）。
    - control file queue（`CONTROL_FILE_QUEUE`）：登記表現況標為 job-visible（任何同
      UID 可建檔），但 spec §R4 明定其提交改走 Manager-owned authenticated socket、
      queue 目錄不再世界可寫——故目標 owner 收斂為 MANAGER_STATE（附 open point）。
    - Manager-owned 樹的其餘資產 → MANAGER_STATE。
    - 其餘 job-visible 樹 → JOB。
    """
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

        if asset.ingress_kind is IngressKind.INTERPROCESS:
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
    group_bits = _mask_write((mode >> 3) & 0o7)
    other_bits = _mask_write(mode & 0o7)
    mode = (mode & 0o700) | (group_bits << 3) | other_bits

    return PermissionEntry(
        asset_id=asset.asset_id,
        tier=asset.tier.name,
        tree=asset.tree.value,
        owner_class=owner_class,
        owner=owner,
        group=scheme.group_of(owner),
        mode=mode,
        is_directory=is_dir,
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
        lines.append(f"# [{e.tier}] {e.asset_id} ({e.owner_class.value}) — {e.rationale}")
        if e.runtime_managed:
            lines.append("#   注意：per-child owner 由降權啟動器逐案 chown（本節僅容器層）。")
        for op in e.open_points:
            lines.append(f"#   後續依賴：{op}")
        if per_job:
            lines.append("#   per-job：由降權啟動器在 spawn 時套用，setup 階段不執行。")
        cmds = list(e.commands(path))
        if e.is_directory:
            # 目錄一定先建起來，後續 chown／chmod／setfacl 必然有對象。
            cmds.insert(0, f"install -d {path}")
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

    骨架清單是由**多條獨立規則**疊出來的（`~/.codex` 來自 hooks、憑證父目錄來自
    #640），預設 layout 下兩者指向同一層。去重讓 `install -d` 不會重複輸出，也讓
    「無多餘」那類等式測試不必為一個純顯示層的重複而放寬。
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
    #: builder 的帳號名。只給 `asset_paths()` 用（`codex-hooks`／`builder-gitconfig` 掛在
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
        """該帳號的 `~/.codex`。root-owned——job 不得替換自己的 hooks。"""
        return f"{self.home_of(account)}/.codex"

    def executor_credential_of(self, account: str) -> str:
        """該帳號的 executor 憑證檔（#640 裁決 (b)）。**檔案由該帳號擁有**（0600）。

        與 `codex_hooks_dir_of()` 刻意落在**同一層**目錄：那一層是 root-owned 的骨架
        目錄，因此 job 能就地改寫自己這份憑證的內容（refresh），卻建不了新檔、刪不掉、
        也換不掉同目錄下的 `hooks.json`——「增／刪／換」需要的是**目錄**的寫入權。
        """
        return f"{self.home_of(account)}/{self.executor_credential_relpath}"

    def executor_credential_dir_of(self, account: str) -> str:
        """憑證檔的父目錄。**必須 root-owned**——裁決 (b) 的性質全部落在這一層。"""
        return _parent_dir(self.executor_credential_of(account))

    def gitconfig_of(self, account: str) -> str:
        """該帳號的 `~/.gitconfig`。root-owned——job 不得替換自己的 git 設定（#623）。

        git 只在 `$HOME/.gitconfig`（global scope，屬 git 的 *protected configuration*）
        認 `safe.directory`，因此位置由帳號的 HOME 決定，與 `~/.codex` 同一個模式。
        """
        return f"{self.home_of(account)}/.gitconfig"

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
        return {
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
            # #640：四個模型 executor 的部署樹落點 ＋ 兩個 job 帳號各自的憑證。
            "executor-toolchain": self.toolchain_root,
            "builder-executor-credential": self.executor_credential_of(
                self.builder_account
            ),
            "repo-worktree": job,
            "dispatch-worktree-pool": wt,
            "jobs-registry": f"{c}/jobs.json",
            "review-verdict": f"{job}/.psc-review-verdict.json",
            # Phase 2a 受控通道（PR #599）：<coordinator>/review-verdicts/<reviewer_job_id>/
            "review-verdict-spool": self.review_verdict_spool_root,
            # #623／#634 成果回收的 bundle spool：<coordinator>/commit-spool/<job-id>/
            "commit-spool": self.commit_spool_root,
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
            "workflow-report-journal": f"{c}/workflow-report-transactions",
            "digest-outbox": f"{c}/digest/outbox",
            "engineering-outcome-outbox": f"{c}/engineering-outcomes",
            "model-identity-overlay": f"{self.project_config_root}/model-identities.yaml",
            "combo-card-override": f"{a}/config/combos",
            "handoff-manifest": f"{job}/.psc-handoff.json",
            "runtime-bootstrap-env": self.env_file,
            "codex-hooks": f"{self.builder_home}/.codex/hooks.json",
            "work-items-yaml": f"{job}/.cortex/work-items.yaml",
        }

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
            if account in job_accounts:
                account_dirs.append(
                    (self.codex_hooks_dir_of(account), root, g(root), 0o755)
                )
                # #640 裁決 (b)：憑證**檔**由 job 帳號擁有，放它的**目錄**維持
                # root-owned——job 因此改得了自己那份憑證的內容，卻建不了新檔、
                # 刪不掉、也換不掉同目錄下的 root-owned `hooks.json`。預設 relpath
                # 下這一層就是上面那個 `~/.codex`，去重後不會重複出現；由
                # `executor_credential_dir_of()` **導出**而非再寫死一次，換 relpath
                # 時這條保護才會跟著走。
                account_dirs.append(
                    (self.executor_credential_dir_of(account), root, g(root), 0o755)
                )
            account_dirs.append((self.cache_of(account), account, g(account), 0o700))
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
                f"job 帳號 {account} 的 HOME 快取（git/gh/uv）；HOME 與 ~/.codex 皆 root-owned 不可替換。",
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
#: `ReadWritePaths` 只掛在**檔案本身**，父目錄（同時放著 root-owned 的 `codex-hooks`）
#: 連 mount 層都不開放可寫——「檔案 job-owned、目錄 root-owned」這條性質因此在
#: **檔案系統**與 **systemd mount** 兩層同時成立，而不是只靠其中一層。
#:
#: 代價（裁決刻意接受）：以「暫存檔 ＋ rename 原子替換」形式 refresh 的 CLI 會失敗，
#: 因為那需要在同目錄建檔。診斷方式見 Phase 2b runbook 第 4e 步。
IN_PLACE_CONTENT_WRITE_ASSETS: frozenset[str] = frozenset({
    "builder-executor-credential",
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
    for entry in plan.entries:
        if account not in plan.all_writable_accounts(entry):
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
    ("SystemCallFilter", "@system-service",
     "seccomp 白名單：只留一般服務所需 syscall。"),
    ("SystemCallErrorNumber", "EPERM",
     "被過濾的 syscall 回 EPERM 而非 SIGSYS——失敗可觀測，不是無聲當掉。"),
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

    def to_dict(self) -> dict[str, object]:
        return {
            "unit_name": self.unit_name,
            "install_path": self.install_path,
            "account": self.account,
            "exec_start": self.exec_start,
            "environment_file": self.environment_file,
            "read_write_paths": list(self.read_write_paths),
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
        lines.append(f"# {why}")
        if effective != value:
            lines += _wrap_comment(
                f"※ 剖面覆寫（profile={profile.profile_id}）：嚴格剖面為 "
                f"{key}={value}，本剖面改為 {key}={effective}。**這是本檔與 "
                f"strict 剖面唯一的差異**；理由與接受的代價見檔頭「加固剖面」段。"
            )
        lines.append(f"{key}={effective}")
    return lines


def _rwp_lines(owners: Mapping[str, tuple[str, ...]]) -> list[str]:
    lines = [
        "# --- ReadWritePaths：由 R1 登記表機械導出（permgen），勿手擴 ---",
        "# 每條後面列出它涵蓋的登記表資產；新增 durable state 時改登記表、重跑產生器。",
    ]
    for path, covered in owners.items():
        lines.append(f"#   涵蓋：{', '.join(covered) if covered else '（無）'}")
        lines.append(f"ReadWritePaths={path}")
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
        "# --- clone 來源（登記表 repo-source-tree，#623）：對 job **唯讀** ---",
        f"#   {job_layout.repo_source_root}/<slug> → `git clone --no-hardlinks` 到",
        f"#   {job_layout.worktree_root}/%i（整個 clone 由本 job 帳號擁有，已在下方 RWP 內）。",
        "# 來源樹的 owner 是 Manager（0817 裁決），job 帳號只拿到唯讀 ACL：讀得到、",
        "# 寫不進去，共用 object store 那條「builder 能寫 Manager 的樹」的路因此在 git",
        "# 這一層就不存在；下方 ReadWritePaths **不含**來源樹。",
        f"# 跨擁有者 clone 由 {job_layout.gitconfig_of(account)} 的 safe.directory 放行",
        "# （root-owned、本帳號唯讀；登記表資產，內容同樣由 permgen 產生）。",
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
        "# **PATH 刻意不寫在這份 unit 上**：模板 unit 的 ExecStart 是 root-owned shim，",
        "# 而 shim 以 `execvpe(argv[0], argv, spec['env'])` **整份換掉**環境——job 解析",
        "# 命令用的 PATH 來自 **spec 的 env**，不是本 unit 的 Environment=。在這裡寫一行",
        "# Environment=PATH= 只會產生一個看起來承載作用、實際被 shim 丟掉的設定。",
        "# 真正的來源是 Manager 端 root-owned EnvironmentFile 裡的（job 改不了）：",
        f"#   {JOB_PATH_ENV_BY_PRINCIPAL[principal]}={job_layout.job_path_value()}",
        "# toolchain 排最前面是必要的：系統層可能另有一份同名但舊很多的 CLI（實機盤點",
        "# 到兩份 codex 差 100 個以上小版本），排後面會被它蓋掉，而症狀是「跑得起來但",
        "# 版本不是你以為的那個」。",
        "# --- executor 憑證（登記表 *-executor-credential，#640 裁決 (b)）---",
        f"#   {job_layout.executor_credential_of(account)}：**檔案**由本 job 帳號擁有",
        "# （0600，token 過期可自行 refresh），**放它的目錄維持 root-owned**——因此",
        "# 本帳號建不了新檔、刪不掉、也換不掉同目錄下的 root-owned hooks.json。",
        "# 下方 ReadWritePaths 只掛**那一個檔**，不是它的父目錄（一般規則會折算成父",
        "# 目錄，這一族是明示的例外，見 permgen.IN_PLACE_CONTENT_WRITE_ASSETS）。",
        "# 憑證尚未落位時本 unit 會**起不來**（systemd 對不存在的 ReadWritePaths 目標",
        "# 報錯）——那是刻意的 fail-closed：沒有登入態的 job 本來就做不了事，在 exec",
        "# 前失敗比走到呼叫模型那一步才 rc=127 好查得多。",
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
    body += _rwp_lines(owners)
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
#: 與 `codex-hooks`（同樣 root-owned、同樣落在帳號 HOME 下）逐位元相同。
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
    都是 root-owned，帳號自己放不了這個檔。這正是登記表既有的 `codex-hooks`
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


# ---------------------------------------------------------------------------
# executor toolchain 的落位計畫（#640）
# ---------------------------------------------------------------------------

def build_toolchain_plan(
    scheme: UidScheme = DEFAULT_SCHEME,
    layout: PathLayout = DEFAULT_LAYOUT,
) -> list[str]:
    """產生 executor toolchain 的落位步驟（**只回傳字串，絕不執行**）。

    分工：**權限**由登記表經 `plan_to_commands()` 產出（`executor-toolchain` 那一節），
    本函式產的是**內容落位**——哪一支 CLI 用哪種方式搬進 `<deploy_root>/toolchain`，
    比照 `build_job_shim()`／`build_account_gitconfig()` 的定位。

    來源路徑刻意留成 shell 變數：那是 operator 機器上的位置（nvm 樹／`~/.local/bin`），
    產生器猜不到也不該猜。但**來源的判準**是固定的，寫進輸出裡：一律取 operator
    **實際在用的那一份**（`command -v` 解出來的），不是另外裝一份系統的。
    """
    tail = ", ".join(JOB_PATH_SYSTEM_TAIL)
    lines = [
        f"# {layout.toolchain_root} —— executor toolchain（登記表資產 executor-toolchain）",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}）——勿手改；重跑：",
        f"#   python3 -m paulsha_cortex.trust_root toolchain {scheme.scheme_id}",
        "#",
        "# ===== 裁決 (a)（#640）=====",
        "# node 走**系統層**（通用 runtime，換版本幾乎不影響產出）；四個模型 CLI 落進",
        "# 部署樹，因為「job 跑的是哪個版本的模型 CLI」**會**影響產出——那必須是一個",
        "# 可稽核的部署決定，而不是跟著 operator 自己的環境漂移。",
        "#",
        "# ===== 來源一律取 operator 實際在用的那一份 =====",
        "# **不要** `npm install -g` 另裝一份系統的。實機盤點：同一台機器上系統層的",
        "# codex 是 0.42.0、operator 實際在用的是 0.147.0（差 100 個以上小版本）——",
        "# 照「系統層有什麼就用什麼」，job 會跑一份 operator 從未判讀過的版本，而",
        "# 症狀是「跑得起來但結果對不上」，不是 `command not found`。",
        "#",
        "# ===== 系統層 runtime（apt，不進部署樹）=====",
    ]
    for runtime in TOOLCHAIN_SYSTEM_RUNTIMES:
        lines.append(
            f"#   {runtime}：版本本身仍是**部署決定**——某個 CLI 哪天提高下限時要一併升，"
            "否則它會變成下一個無聲漂移點。"
        )
    lines += [
        f"#   目前只有 `codex` 需要它；`claude`／`agy` 自帶原生執行檔、`copilot` 是 "
        "shell script。",
        "",
        "# --- 目錄骨架（權限與登記表那一節逐位元相同）---",
        f"install -d -o {scheme.deploy_account} -g {scheme.group_of(scheme.deploy_account)}"
        f" -m 0755 {layout.toolchain_root}",
        f"install -d -o {scheme.deploy_account} -g {scheme.group_of(scheme.deploy_account)}"
        f" -m 0755 {layout.toolchain_bin}",
        f"install -d -o {scheme.deploy_account} -g {scheme.group_of(scheme.deploy_account)}"
        f" -m 0755 {layout.toolchain_lib}",
    ]
    for tool in EXECUTOR_TOOLS:
        profile = executor_hardening_profile(tool.name)
        lines += [
            "",
            f"# --- {tool.name}（{tool.shape.value}"
            + ("；**需要系統層 node**" if tool.needs_node else "")
            + f"）---",
            f"#   加固剖面：{profile.profile_id} ⇒ "
            f"{job_unit_stem(layout, Principal.BUILDER, profile)}@<id>.service（#643）",
            f"#   {tool.note}",
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
            ]
        else:
            lines += [
                f'#     cp -a "$SRC" {layout.toolchain_bin}/{tool.name}',
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
