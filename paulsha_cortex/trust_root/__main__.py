"""`python -m paulsha_cortex.trust_root` —— on-demand trust-root 診斷。

Phase 1 不改 `cortex` CLI（避免動到 R-16 help 對齊面）；operator／CI 以本模組
入口取得 R1 登記表摘要與 R3 自檢診斷。全部唯讀。

用法：
    python -m paulsha_cortex.trust_root selfcheck   # R3 自檢診斷（JSON）
    python -m paulsha_cortex.trust_root registry    # R1 登記表摘要（JSON）
    python -m paulsha_cortex.trust_root equation     # R1 雙向等式結果
    python -m paulsha_cortex.trust_root permissions [two-way|three-way] [--commands]
                                                    # Phase 2a 權限計畫（JSON 或命令序列）

`permissions` 只**產生**權限計畫／命令字串，**絕不執行**任何 root 操作——命令供
operator 在 Phase 2b runbook 中手動 sudo 執行。
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
        for token in rest:
            if token == "--commands":
                want_commands = True
            elif token in permgen.SCHEMES:
                scheme_id = token
            else:
                print(f"unknown permissions arg: {token}", file=sys.stderr)
                return 2
        plan = permgen.generate_plan(permgen.SCHEMES[scheme_id])
        if want_commands:
            for line in permgen.plan_to_commands(plan):
                print(line)
        else:
            print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
