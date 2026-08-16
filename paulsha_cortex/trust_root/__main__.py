"""`python -m paulsha_cortex.trust_root` —— on-demand trust-root 診斷。

Phase 1 不改 `cortex` CLI（避免動到 R-16 help 對齊面）；operator／CI 以本模組
入口取得 R1 登記表摘要與 R3 自檢診斷。全部唯讀。

用法：
    python -m paulsha_cortex.trust_root selfcheck   # R3 自檢診斷（JSON）
    python -m paulsha_cortex.trust_root registry    # R1 登記表摘要（JSON）
    python -m paulsha_cortex.trust_root equation     # R1 雙向等式結果
    python -m paulsha_cortex.trust_root permissions [two-way|three-way] [--commands] [--paths]
                                                    # Phase 2a 權限計畫（JSON 或命令序列）
    python -m paulsha_cortex.trust_root unit [two-way|three-way]
                                        [--manager|--job|--job-properties]
                                                    # Phase 2b systemd unit 內容
                                                    # （--job-properties＝方案 A 的
                                                    #   systemd-run --property= 清單）
    python -m paulsha_cortex.trust_root polkit [two-way|three-way] [--transient|--template]
                                                    # Phase 2b 降權 polkit 規則內容
                                                    # （--transient＝方案 A，預設；
                                                    #   --template＝方案 B）
    python -m paulsha_cortex.trust_root scaffold [two-way|three-way]
                                                    # Phase 2b 骨架目錄的 install -d 命令

`permissions`／`unit`／`polkit`／`scaffold` 只**產生**計畫與內容字串，**絕不執行**
任何 root 操作、不寫任何系統路徑——命令供 operator 在 Phase 2b runbook 中手動
sudo 執行。`--paths` 讓 `--commands` 以 `permgen.DEFAULT_LAYOUT` 的真實絕對路徑
輸出（0816 裁決：/var/lib/cortex ＋ /opt/cortex），不再帶 placeholder。
"""
from __future__ import annotations

import json
import sys
from typing import Sequence

from . import permgen, registry, selfcheck


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
        rest = args[1:]
        scheme_id = "two-way"
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
        plan = permgen.generate_plan(permgen.SCHEMES[scheme_id])
        if want_commands:
            path_of = permgen.asset_paths() if want_paths else None
            for line in permgen.plan_to_commands(plan, path_of=path_of):
                print(line)
        else:
            print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if command == "unit":
        rest = args[1:]
        scheme_id = "two-way"
        which = "manager"
        for token in rest:
            if token == "--manager":
                which = "manager"
            elif token == "--job":
                which = "job"
            elif token == "--job-properties":
                which = "job-properties"
            elif token in permgen.SCHEMES:
                scheme_id = token
            else:
                print(f"unknown unit arg: {token}", file=sys.stderr)
                return 2
        scheme = permgen.SCHEMES[scheme_id]
        if which == "job-properties":
            print("# 方案 A（systemd-run transient unit）的 --property= 建議清單。")
            print("# 與方案 B 的模板 unit 同源（同一加固表 ＋ 同一份登記表導出的 RWP）。")
            print("# 注意：%i 是 systemd 模板 specifier；A 方案（transient）請由呼叫端")
            print("#       代入該 job 的實際 worktree 路徑。")
            for prop in permgen.transient_unit_properties(scheme):
                print(prop)
            return 0
        unit = (
            permgen.build_manager_unit(scheme)
            if which == "manager"
            else permgen.build_job_unit(scheme)
        )
        print(unit.content, end="")
        return 0
    if command == "polkit":
        rest = args[1:]
        scheme_id = "two-way"
        plan = permgen.PolkitPlan.TRANSIENT
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
        scheme_id = "two-way"
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
        return 0

    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
