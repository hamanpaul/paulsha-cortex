"""`python -m paulsha_cortex.trust_root` —— on-demand trust-root 診斷。

Phase 1 不改 `cortex` CLI（避免動到 R-16 help 對齊面）；operator／CI 以本模組
入口取得 R1 登記表摘要與 R3 自檢診斷。全部唯讀。

用法：
    python -m paulsha_cortex.trust_root selfcheck   # R3 自檢診斷（JSON）
    python -m paulsha_cortex.trust_root registry    # R1 登記表摘要（JSON）
    python -m paulsha_cortex.trust_root equation     # R1 雙向等式結果
    python -m paulsha_cortex.trust_root permissions [four-way|three-way|two-way]
                                        [--commands] [--paths]
                                        [--operator-account <帳號名|none>]
                                        [--external-reader-account <帳號名|none>]
                                                    # Phase 2a 權限計畫（JSON 或命令序列）
    python -m paulsha_cortex.trust_root unit [four-way|three-way|two-way]
                                        [--manager|--monitor|--egress-proxy
                                         |--job|--review-job|--gate-job
                                         |--job-properties]
                                        [--profile strict|jit]
                                                    # Phase 2b systemd unit 內容
                                                    # （--monitor＝monitor 的 system-level
                                                    #   unit：與 manager 同帳號、同加固段，
                                                    #   但 ReadWritePaths 只由 monitor
                                                    #   persona 的登記表面導出，嚴格更窄；
                                                    #   --egress-proxy＝#716 的出口 proxy
                                                    #   服務 unit（User=cortex-egress，
                                                    #   非任何 job 帳號、無 root、零
                                                    #   ReadWritePaths）；
                                                    #   --job＝builder 的模板 unit；
                                                    #   --review-job＝#615 M2 的 reviewer
                                                    #   ＋planner 模板 unit（同帳號，
                                                    #   User=cortex-reviewer-planner，
                                                    #   unit 名 cortex-reviewer-job@.service）；
                                                    #   --gate-job＝#629 的 gate 執行身分
                                                    #   模板 unit（User=cortex-gate，
                                                    #   unit 名 cortex-gate-job@.service；
                                                    #   只有 four-way 有這個帳號）；
                                                    #   --job-properties＝方案 A 的
                                                    #   systemd-run --property= 清單；
                                                    #   --profile＝#643 的 per-executor
                                                    #   加固剖面，只對 --job／--review-job／
                                                    #   --gate-job／--job-properties 有意義。
                                                    #   預設
                                                    #   strict＝完整加固表；jit 只放寬
                                                    #   MemoryDenyWriteExecute，給 node 型
                                                    #   executor（codex／copilot）用，
                                                    #   unit 名尾綴 -jit）
    python -m paulsha_cortex.trust_root unit-replica <unit 檔路徑|->
                                        [--instance <job id>]
                                        [--allow-drift]
                                                    # #673：把一份**已落檔**的 unit 讀成
                                                    # systemd-run 的 --property= 完整清單，
                                                    # 供 runbook／測試在**真實加固面**下
                                                    # 跑探針。`-` 讀 stdin（可接
                                                    # `systemctl cat`）。
                                                    # 契約是「全帶，不選」：[Service] 段
                                                    # 除執行面指令（ExecStart= 之類）外
                                                    # 全部帶出——手抄子集是 #638／#657／
                                                    # #673 同一族事故的成因。
                                                    # 落檔的 unit 少了任一加固鍵即
                                                    # **拒絕產出**（--allow-drift 可關，
                                                    # 但那等於放棄本命令的用途）
    python -m paulsha_cortex.trust_root path-probe [four-way|three-way|two-way]
                                                    # #679：反向不變式的實機探針——
                                                    # 每個 job 角色 × 每支 executor，
                                                    # 以**零額外 env** 起 job，斷言
                                                    # 解到的是 <toolchain>/bin/<cli>
                                                    # 且版本 == 登記表登記的那一份。
                                                    # 產出**刻意不含任何 --setenv=**：
                                                    # 驗證環境供應 production 不供應的
                                                    # 東西，正是讓 #679 活過五輪驗證的
                                                    # 那個動作。
    python -m paulsha_cortex.trust_root job-log-probe [four-way|three-way|two-way]
                                                    # #708：反向不變式的實機探針——
                                                    # 每個降權 principal 以**零額外
                                                    # env**、真實模板 unit 的加固面
                                                    # 起一段命令，正向斷言「寫得出自己
                                                    # 那一格 log 且 Manager 讀得回來」、
                                                    # 反向斷言「Manager 的 dispatch log
                                                    # 目錄仍然寫不進去」（那一層住著
                                                    # gate ledger 與 exit sentinel）。
                                                    # 同樣**不含任何 --setenv=**（D13）。
    python -m paulsha_cortex.trust_root inner-sandbox-probe [four-way|three-way|two-way] [codex]
                                                    # #714：反向不變式的實機探針——
                                                    # executor **自帶的內層沙箱**在
                                                    # 真實加固面下裝不裝得上、以及它
                                                    # 到底有沒有在擋。四個方向：不帶
                                                    # 旗標必須仍失敗（外層沒被偷偷
                                                    # 放寬）、旗標必須還存在（不得回
                                                    # `Unknown feature flag`）、帶了
                                                    # 就通、且寫工作區外／對外連線
                                                    # 必須被擋。同樣**不含任何
                                                    # --setenv=**（D13）。
    python -m paulsha_cortex.trust_root workspace-probe [four-way|three-way|two-way]
    python -m paulsha_cortex.trust_root git-trust-probe [four-way|three-way|two-way]
                                                    # #710：反向不變式的實機探針——
                                                    # 每個降權 principal 以**零額外
                                                    # env**、真實模板 unit 的加固面
                                                    # 起一段命令，正向斷言「cd 得進
                                                    # 自己的工作區」、反向斷言「別的
                                                    # job 帳號進不去 builder 那一格」
                                                    # （per-job 隔離），並以 getfacl
                                                    # 的 mask::／#effective: 判準驗
                                                    # ACL 沒有被 chmod 壓掉。工作區
                                                    # 由**真實 provisioning** 產生，
                                                    # 不手工前置（#645）。
    python -m paulsha_cortex.trust_root shim [four-way|three-way|two-way]
                                                    # Phase 2b 方案 B 的降權 shim 內容
                                                    # （模板 unit 的固定 ExecStart=）
    python -m paulsha_cortex.trust_root gitconfig [four-way|three-way|two-way]
                                        [--builder|--reviewer-planner|--manager]
                                        --source-repo <slug> [--source-repo <slug>…]
                                                    # #623：帳號 HOME 下 root-owned 的
                                                    # .gitconfig 內容（來源樹的
                                                    # safe.directory）。三份同構：兩個
                                                    # job 帳號 ＋ Manager（Manager 也要
                                                    # 對來源樹跑 git，同樣會撞 dubious
                                                    # ownership）
    python -m paulsha_cortex.trust_root toolchain [four-way|three-way|two-way]
                                                    # #640：executor toolchain 的落位
                                                    # 步驟（四個模型 CLI 進
                                                    # <deploy_root>/toolchain、node 走
                                                    # 系統層、job 的 PSC_BUILDER_PATH）
    python -m paulsha_cortex.trust_root polkit [four-way|three-way|two-way]
                                        [--template|--transient]
                                                    # Phase 2b 降權 polkit 規則內容
                                                    # （--template＝方案 B，**預設**；
                                                    #   --transient＝方案 A，對照用）
                                                    # 內容涵蓋**該方案實際落檔**的全部
                                                    # 降權 job 角色具名模板（#629 起，
                                                    # four-way＝builder ＋ reviewer/planner
                                                    # ＋ gate，各兩個加固剖面＝六個字幹；
                                                    # three-way／two-way 沒有 gate 帳號，
                                                    # 因此不含 gate 字幹），仍是**單一**
                                                    # addRule、單一 return YES
    python -m paulsha_cortex.trust_root scaffold [four-way|three-way|two-way]
                                                    # Phase 2b 骨架目錄的 install -d 命令
    python -m paulsha_cortex.trust_root egress-allowlist
                                                    # #716：出口白名單（唯一來源＝
                                                    # permgen.EXECUTOR_TOOLS 的 api_hosts）。
                                                    # proxy 執行期讀的是同一支函式。

UID 方案未指定時一律用 **`four-way`**（#629 的定案：`cortex-manager`／
`cortex-reviewer-planner`／`cortex-builder`／**`cortex-gate`**）。`three-way`
（0816 第三輪裁決 A）與 `two-way` 保留為向後相容選項，需**顯式**打出——打錯字不會
靜默退回較寬鬆的方案。那兩個方案對 `GATE` 明示「本部署沒有這個角色」，因此不產生
gate 的 unit／ACL／polkit 字幹，降權模式下 build 卡照 `require_ledger` fail closed
（＝#629 之前的現況）。

`permissions`／`unit`／`shim`／`gitconfig`／`toolchain`／`polkit`／`scaffold`／`path-probe`／`job-log-probe`／`workspace-probe`／`git-trust-probe`／`inner-sandbox-probe`
只**產生**計畫與內容字串，
**絕不執行**任何 root 操作、不寫任何系統路徑——命令供 operator 在 Phase 2b runbook
中手動 sudo 執行。`--paths` 讓 `--commands` 以 `permgen.DEFAULT_LAYOUT` 的真實絕對
路徑輸出（0816 裁決：/var/lib/cortex ＋ /opt/cortex），不再帶 placeholder。

## 部署決定型 principal 的對應（#626）

`operator` 與 `external`（outbox 下游 reader）是**抽象角色**，不是帳號名——對應到誰是
部署決定。兩條注入管道，**CLI 旗標優先於 env**：

    --operator-account <帳號名>          PSC_OPERATOR_ACCOUNT=<帳號名>
    --external-reader-account <帳號名>   PSC_EXTERNAL_READER_ACCOUNT=<帳號名>

值為 `none` 表示「本部署沒有這個角色的實體」，該 principal 的授權整組略去。
兩者皆未給時 `--commands` **fail-closed**：印出可操作的錯誤訊息到 stderr、stdout 一行
都不輸出、回傳碼 2。因為 runbook 第 2b 步以 `sudo sh -e` 執行整份 script，一行
`setfacl -m u:<不存在的帳號>:rX` 就會中止它並留下半套用的權限樹。

## 來源 repo 的宣告（#623，同樣是部署決定）

`gitconfig` 需要**逐字**的 `safe.directory` 路徑（git 不吃目錄萬用字元），而
「本 instance 治理哪些 repo」是部署決定。兩條注入管道，**CLI 旗標優先於 env**：

    --source-repo <slug>（可重複）      PSC_SOURCE_REPO_SLUGS=<slug>[,<slug>…]

未給時 fail-closed（stdout 一行都不輸出、回傳碼 2）：空的 `[safe]` 段裝得起來、
服務也起得來，然後**每個 job 在第一次 `git clone` 才失敗**——症狀離原因很遠。

env 只在本 CLI 這一層讀取——`permgen` 維持純函式（不讀 env、不碰 IO）。
"""
from __future__ import annotations

import shlex

import json
import os
import sys
from pathlib import Path
from typing import Sequence

from . import permgen, registry, selfcheck
from .registry import Principal


#: `--<principal>-account none` 的字面值——明示本部署沒有這個角色的實體。
ABSENT_TOKEN = "none"

#: 來源 repo slug 的 CLI 旗標與 env 變數（#623；旗標可重複，優先於 env）。
SOURCE_REPO_FLAG = "--source-repo"
SOURCE_REPO_ENV = "PSC_SOURCE_REPO_SLUGS"


def _source_repo_slugs(
    rest: list[str],
    env: "dict[str, str] | None" = None,
) -> "tuple[list[str], list[str], str | None]":
    """抽出 `--source-repo <slug>`（可重複）；未給旗標時退回 env。

    回傳 `(剩餘 token, slug 清單, 錯誤訊息)`。env 值以逗號／空白分隔。
    """
    environ = os.environ if env is None else env
    remaining: list[str] = []
    slugs: list[str] = []
    pending = False
    for token in rest:
        if pending:
            slugs.append(token)
            pending = False
            continue
        flag, sep, inline = token.partition("=")
        if flag != SOURCE_REPO_FLAG:
            remaining.append(token)
            continue
        if sep:
            if not inline:
                return remaining, slugs, f"{SOURCE_REPO_FLAG} 需要一個 slug"
            slugs.append(inline)
        else:
            pending = True
    if pending:
        return remaining, slugs, f"{SOURCE_REPO_FLAG} 需要一個 slug"
    if not slugs:
        raw = (environ.get(SOURCE_REPO_ENV) or "").replace(",", " ")
        slugs = [part for part in raw.split() if part]
    return remaining, slugs, None


def _account_overrides(
    rest: list[str],
    env: "dict[str, str] | None" = None,
) -> "tuple[list[str], dict[Principal, str], str | None]":
    """抽出部署決定型 principal 的對應（CLI 旗標優先於 env）。

    回傳 `(剩餘 token, principal→帳號, 錯誤訊息)`。旗標與 env 變數名都由
    `permgen.PRINCIPAL_ACCOUNT_OPTIONS` 導出——新增一個 principal 不必改本函式。
    """
    environ = os.environ if env is None else env
    overrides: dict[Principal, str] = {}
    for opt in permgen.PRINCIPAL_ACCOUNT_OPTIONS:
        value = environ.get(opt.env_var)
        if value:
            overrides[opt.principal] = value

    by_flag = {opt.cli_flag: opt for opt in permgen.PRINCIPAL_ACCOUNT_OPTIONS}
    remaining: list[str] = []
    pending = None
    for token in rest:
        if pending is not None:
            overrides[pending.principal] = token
            pending = None
            continue
        flag, sep, inline = token.partition("=")
        opt = by_flag.get(flag)
        if opt is None:
            remaining.append(token)
            continue
        if sep:
            if not inline:
                return remaining, overrides, f"{opt.cli_flag} 需要一個帳號名（或 `none`）"
            overrides[opt.principal] = inline
        else:
            pending = opt
    if pending is not None:
        return remaining, overrides, f"{pending.cli_flag} 需要一個帳號名（或 `none`）"
    return remaining, {
        p: (permgen.ABSENT_ACCOUNT if v == ABSENT_TOKEN else v)
        for p, v in overrides.items()
    }, None


def _registry_summary() -> dict[str, object]:
    return {
        "asset_count": len(registry.ASSET_REGISTRY),
        "tier0": len(registry.assets_by_tier(registry.AssetTier.TIER_0)),
        "tier1": len(registry.assets_by_tier(registry.AssetTier.TIER_1)),
        "manager_owned": len(registry.manager_owned_assets()),
        "job_visible": len(registry.job_visible_assets()),
        "headless_writable_manager_owned": [
            a.asset_id for a in registry.headless_writable_manager_owned()
        ],
        "personas_covered": sorted(p.value for p in registry.personas_covered()),
        "mutation_ingress": [i.ingress_id for i in registry.MUTATION_INGRESS],
        "assets": [
            {
                "asset_id": a.asset_id,
                "tier": a.tier.name,
                "tree": a.tree.value,
                "path_resolver": a.path_resolver,
                "writers": [w.value for w in a.writers],
                "ingress_kind": a.ingress_kind.value,
            }
            for a in registry.ASSET_REGISTRY
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "selfcheck"

    if command == "selfcheck":
        report = selfcheck.run_self_check()
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        for line in report.warning_lines():
            print(line, file=sys.stderr)
        return 0
    if command == "registry":
        print(json.dumps(_registry_summary(), ensure_ascii=False, indent=2))
        return 0
    if command == "equation":
        result = registry.check_registry_equation()
        print(json.dumps(
            {
                "ok": result.ok,
                "unregistered_functions": list(result.unregistered_functions),
                "dangling_resolvers": list(result.dangling_resolvers),
                "stale_acknowledgements": list(result.stale_acknowledgements),
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0 if result.ok else 1
    if command == "permissions":
        rest, overrides, err = _account_overrides(args[1:])
        if err is not None:
            print(err, file=sys.stderr)
            return 2
        scheme_id = permgen.DEFAULT_SCHEME_ID
        want_commands = False
        want_paths = False
        for token in rest:
            if token == "--commands":
                want_commands = True
            elif token == "--paths":
                want_paths = True
            elif token in permgen.SCHEMES:
                scheme_id = token
            else:
                print(f"unknown permissions arg: {token}", file=sys.stderr)
                return 2
        try:
            scheme = permgen.SCHEMES[scheme_id].with_principal_accounts(overrides)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        plan = permgen.generate_plan(scheme)
        if want_commands:
            path_of = permgen.asset_paths() if want_paths else None
            try:
                # fail-closed：未解析的 principal 一律在**輸出前**攔下——stdout 保持
                # 空的，被重導成 script 的檔案因此是空檔，而不是一份跑到一半會中止
                # 的半套 script（#626）。
                lines = permgen.plan_to_commands(plan, path_of=path_of)
            except (permgen.UnresolvedPrincipalError,
                    permgen.UnknownAccountInOutputError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            for line in lines:
                print(line)
        else:
            # JSON 是診斷模式：不 fail-closed，但未對應的 principal 必須看得見——
            # 既在 payload 裡（`unresolved_principals`），也在 stderr 提醒一次。
            print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
            unresolved = plan.unresolved_principals
            if unresolved:
                print(
                    permgen.unresolved_principal_message(unresolved, scheme_id),
                    file=sys.stderr,
                )
        return 0
    if command == "unit":
        rest = args[1:]
        scheme_id = permgen.DEFAULT_SCHEME_ID
        which = "manager"
        profile_id = permgen.DEFAULT_HARDENING_PROFILE.profile_id
        expect_profile = False
        for token in rest:
            if expect_profile:
                if token not in permgen.HARDENING_PROFILES_BY_ID:
                    print(
                        f"unknown hardening profile: {token}"
                        f"（可用：{sorted(permgen.HARDENING_PROFILES_BY_ID)}）",
                        file=sys.stderr,
                    )
                    return 2
                profile_id = token
                expect_profile = False
            elif token == "--manager":
                which = "manager"
            elif token == "--monitor":
                which = "monitor"
            elif token == "--egress-proxy":
                which = "egress-proxy"
            elif token == "--job":
                which = "job"
            elif token == "--review-job":
                which = "review-job"
            elif token == "--gate-job":
                which = "gate-job"
            elif token == "--job-properties":
                which = "job-properties"
            elif token == "--profile":
                expect_profile = True
            elif token.startswith("--profile="):
                candidate = token.split("=", 1)[1]
                if candidate not in permgen.HARDENING_PROFILES_BY_ID:
                    print(
                        f"unknown hardening profile: {candidate}"
                        f"（可用：{sorted(permgen.HARDENING_PROFILES_BY_ID)}）",
                        file=sys.stderr,
                    )
                    return 2
                profile_id = candidate
            elif token in permgen.SCHEMES:
                scheme_id = token
            else:
                print(f"unknown unit arg: {token}", file=sys.stderr)
                return 2
        if expect_profile:
            print("--profile 需要一個剖面 id", file=sys.stderr)
            return 2
        scheme = permgen.SCHEMES[scheme_id]
        profile = permgen.HARDENING_PROFILES_BY_ID[profile_id]
        if which == "job-properties":
            print("# 方案 A（systemd-run transient unit）的 --property= 建議清單。")
            print("# 與方案 B 的模板 unit 同源（同一加固表 ＋ 同一份登記表導出的 RWP）。")
            print(f"# 加固剖面：{profile_id}（--profile 可切換；預設為最嚴格的那一份）。")
            print("# 注意：%i 是 systemd 模板 specifier；A 方案（transient）請由呼叫端")
            print("#       代入該 job 的實際 worktree 路徑。")
            for prop in permgen.transient_unit_properties(scheme, profile=profile):
                print(prop)
            return 0
        if which == "job":
            print(permgen.build_job_unit(scheme, profile=profile).content, end="")
            return 0
        if which in ("review-job", "gate-job"):
            # #615 M2：reviewer＋planner 的模板（同一份，兩者同帳號）。
            # #629：gate 執行身分的模板（第四個帳號，不代表任何 persona）。
            principal = (
                Principal.REVIEWER if which == "review-job" else Principal.GATE
            )
            if scheme.resolve(principal) is None:
                print(
                    f"scheme={scheme.scheme_id} 沒有 `{principal.value}` 帳號，"
                    f"因此沒有這份模板 unit（#629：改用 --scheme four-way）",
                    file=sys.stderr,
                )
                return 2
            print(
                permgen.build_job_unit(
                    scheme, principal=principal, profile=profile
                ).content,
                end="",
            )
            return 0
        if profile_id != permgen.DEFAULT_HARDENING_PROFILE.profile_id:
            # 剖面只對 job 模板 unit 有意義；靜默忽略會產出一份與旗標不符的內容。
            print(
                "--profile 只適用於 --job／--review-job／--gate-job／--job-properties"
                f"（收到 {which}）",
                file=sys.stderr,
            )
            return 2
        builders = {
            "manager": permgen.build_manager_unit,
            "monitor": permgen.build_monitor_unit,
            "egress-proxy": permgen.build_egress_proxy_unit,
        }
        print(builders[which](scheme).content, end="")
        return 0
    if command == "egress-allowlist":
        # 出口白名單的唯一來源（permgen.egress_allowlist()）。runbook 的驗證步驟
        # 與 proxy 執行期讀的是同一支函式，因此這裡印出來的就是實際放行的那一份。
        print("# 出口白名單（#716）——由 permgen.EXECUTOR_TOOLS 的 api_hosts 機械導出。")
        print(f"# proxy: {permgen.EGRESS_PROXY.url}"
              f"（unit {permgen.EGRESS_PROXY.unit_name}, User={permgen.EGRESS_PROXY.account}）")
        print(f"# job unit: IPAddressDeny=any + IPAddressAllow="
              f"{permgen.EGRESS_PROXY.ip_address_allow}")
        for entry in permgen.egress_allowlist():
            mark = "" if entry.measured else "   # ⚠ 未實機量測"
            print(f"{entry.host}{mark}")
        return 0
    if command == "unit-replica":
        rest = args[1:]
        source: str | None = None
        instance = "probe"
        require_hardening = True
        expect_instance = False
        for token in rest:
            if expect_instance:
                instance = token
                expect_instance = False
            elif token == "--instance":
                expect_instance = True
            elif token.startswith("--instance="):
                instance = token.split("=", 1)[1]
            elif token == "--allow-drift":
                require_hardening = False
            elif not token.startswith("--") and source is None:
                source = token
            else:
                print(f"unknown unit-replica arg: {token}", file=sys.stderr)
                return 2
        if expect_instance:
            print("--instance 需要一個 job id", file=sys.stderr)
            return 2
        if source is None:
            print(
                "unit-replica 需要一個 unit 檔路徑（`-` 讀 stdin）",
                file=sys.stderr,
            )
            return 2
        try:
            text = (
                sys.stdin.read()
                if source == "-"
                else Path(source).read_text(encoding="utf-8")
            )
        except OSError as exc:
            print(f"讀不到 unit: {exc}", file=sys.stderr)
            return 2
        try:
            props = permgen.unit_replica_properties(
                text, instance=instance, require_hardening=require_hardening
            )
        except permgen.UnitReplicaDriftError as exc:
            # fail-closed：stdout 保持空的，被 `$(...)` 展開時不會產生半套清單。
            print(str(exc), file=sys.stderr)
            return 2
        for prop in props:
            print(prop)
        return 0
    if command == "path-probe":
        rest = args[1:]
        scheme_id = permgen.DEFAULT_SCHEME_ID
        for token in rest:
            if token in permgen.SCHEMES:
                scheme_id = token
            else:
                print(f"unknown path-probe arg: {token}", file=sys.stderr)
                return 2
        for line in permgen.build_path_resolution_probe(permgen.SCHEMES[scheme_id]):
            print(line)
        return 0
    if command == "job-log-probe":
        rest = args[1:]
        scheme_id = permgen.DEFAULT_SCHEME_ID
        for token in rest:
            if token in permgen.SCHEMES:
                scheme_id = token
            else:
                print(f"unknown job-log-probe arg: {token}", file=sys.stderr)
                return 2
        for line in permgen.build_job_log_probe(permgen.SCHEMES[scheme_id]):
            print(line)
        return 0
    if command == "workspace-probe":
        rest = args[1:]
        scheme_id = permgen.DEFAULT_SCHEME_ID
        for token in rest:
            if token in permgen.SCHEMES:
                scheme_id = token
            else:
                print(f"unknown workspace-probe arg: {token}", file=sys.stderr)
                return 2
        for line in permgen.build_job_workspace_probe(permgen.SCHEMES[scheme_id]):
            print(line)
        return 0
    if command == "git-trust-probe":
        rest = args[1:]
        scheme_id = permgen.DEFAULT_SCHEME_ID
        for token in rest:
            if token in permgen.SCHEMES:
                scheme_id = token
            else:
                print(f"unknown git-trust-probe arg: {token}", file=sys.stderr)
                return 2
        for line in permgen.build_job_git_trust_probe(permgen.SCHEMES[scheme_id]):
            print(line)
        return 0
    if command == "inner-sandbox-probe":
        rest = args[1:]
        scheme_id = permgen.DEFAULT_SCHEME_ID
        executor = "codex"
        for token in rest:
            if token in permgen.SCHEMES:
                scheme_id = token
            elif any(tool.name == token for tool in permgen.EXECUTOR_TOOLS):
                executor = token
            else:
                print(f"unknown inner-sandbox-probe arg: {token}", file=sys.stderr)
                return 2
        try:
            lines = permgen.build_inner_sandbox_probe(
                permgen.SCHEMES[scheme_id], executor=executor
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        for line in lines:
            print(line)
        return 0
    if command == "shim":
        rest = args[1:]
        scheme_id = permgen.DEFAULT_SCHEME_ID
        for token in rest:
            if token in permgen.SCHEMES:
                scheme_id = token
            else:
                print(f"unknown shim arg: {token}", file=sys.stderr)
                return 2
        shim = permgen.build_job_shim(permgen.SCHEMES[scheme_id])
        print(shim.content, end="")
        return 0
    if command == "gitconfig":
        rest, slugs, err = _source_repo_slugs(args[1:])
        if err is not None:
            print(err, file=sys.stderr)
            return 2
        scheme_id = permgen.DEFAULT_SCHEME_ID
        principal = Principal.BUILDER
        # 旗標→persona 由 permgen 那張表導出：新增一份 .gitconfig 不必改本函式。
        by_flag = {
            flag: p for p, flag in permgen.ACCOUNT_GITCONFIG_FLAGS.items()
        }
        for token in rest:
            if token in by_flag:
                principal = by_flag[token]
            elif token in permgen.SCHEMES:
                scheme_id = token
            else:
                print(f"unknown gitconfig arg: {token}", file=sys.stderr)
                return 2
        try:
            layout = permgen.DEFAULT_LAYOUT.with_source_repo_slugs(slugs)
            # fail-closed 與 `--commands` 同一個理由：一份「裝得起來但每一次 git 操作
            # 都會失敗」的 .gitconfig，比產生器拒絕產出危險得多（#623）。
            blob = permgen.build_account_gitconfig(
                permgen.SCHEMES[scheme_id], layout, principal
            )
        except (permgen.UnresolvedSourceRepoError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(blob.content, end="")
        return 0
    if command == "toolchain":
        rest = args[1:]
        scheme_id = permgen.DEFAULT_SCHEME_ID
        for token in rest:
            if token in permgen.SCHEMES:
                scheme_id = token
            else:
                print(f"unknown toolchain arg: {token}", file=sys.stderr)
                return 2
        for line in permgen.build_toolchain_plan(permgen.SCHEMES[scheme_id]):
            print(line)
        return 0
    if command == "polkit":
        rest = args[1:]
        scheme_id = permgen.DEFAULT_SCHEME_ID
        # 0816 第三輪裁決：方案 B（root-owned 模板 unit）定案，故為預設。
        plan = permgen.PolkitPlan.TEMPLATE
        for token in rest:
            if token == "--transient":
                plan = permgen.PolkitPlan.TRANSIENT
            elif token == "--template":
                plan = permgen.PolkitPlan.TEMPLATE
            elif token in permgen.SCHEMES:
                scheme_id = token
            else:
                print(f"unknown polkit arg: {token}", file=sys.stderr)
                return 2
        rule = permgen.build_polkit_rule(permgen.SCHEMES[scheme_id], plan=plan)
        print(rule.content, end="")
        return 0
    if command == "scaffold":
        rest = args[1:]
        scheme_id = permgen.DEFAULT_SCHEME_ID
        for token in rest:
            if token in permgen.SCHEMES:
                scheme_id = token
            else:
                print(f"unknown scaffold arg: {token}", file=sys.stderr)
                return 2
        scheme = permgen.SCHEMES[scheme_id]
        print("# trust-root Phase 2b 骨架目錄（非登記表資產的父層）——只產生字串。")
        print(f"# scheme={scheme_id}；operator review 後手動 sudo 執行。")
        for path, owner, group, mode in permgen.DEFAULT_LAYOUT.scaffold_directories(scheme):
            print(f"install -d -o {owner} -g {group} -m {format(mode, '04o')} {path}")
        for path, owner, group, mode, is_directory in permgen.DEFAULT_LAYOUT.codex_control_scaffold(scheme):
            if is_directory:
                print(f"install -d -o {owner} -g {group} -m {format(mode, '04o')} {path}")
            else:
                # First install creates a syntactically valid deployment asset;
                # reruns preserve the operator's desired policy byte-for-byte.
                content = permgen.DEFAULT_LAYOUT.codex_control_initial_content(path).rstrip("\n")
                qpath = shlex.quote(path)
                print(
                    f"if [ ! -e {qpath} ]; then printf '%s\\n' {shlex.quote(content)} | "
                    f"install -D -o {owner} -g {group} -m {format(mode, '04o')} "
                    f"/dev/stdin {qpath}; fi"
                )
                print(f"chown {owner}:{group} {qpath}")
                print(f"chmod {format(mode, '04o')} {qpath}")
        for line in permgen.DEFAULT_LAYOUT.codex_authority_seed_commands(scheme):
            print(line)
        return 0

    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
