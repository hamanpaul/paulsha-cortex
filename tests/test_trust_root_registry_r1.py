"""R1（trust-root Phase 1）：登記表雙向等式與分類完整性。

驗收：登記表等式測試綠——每個 durable path 函式都被分類（無遺漏）、每個 resolver
都對應到實際函式（無矛盾），且新增未登記的 path 函式即 FAIL 並指名。
"""
from __future__ import annotations

import dataclasses
from pathlib import Path  # noqa: F401 — 供假函式的 return annotation 字串比對

import pytest

from paulsha_cortex.config import paths
from paulsha_cortex.trust_root import registry
from paulsha_cortex.trust_root.registry import (
    ASSET_REGISTRY,
    AssetTier,
    HEADLESS_PERSONAS,
    KNOWN_DUPLICATE_DERIVATIONS,
    Principal,
    TrustTree,
    check_registry_equation,
    discover_path_functions,
)


def test_equation_holds_on_current_tree() -> None:
    """spec §R1 雙向等式：現況登記表與 path 契約完全對齊。"""
    result = check_registry_equation()
    assert result.ok, result.failure_summary()
    assert result.unregistered_functions == ()
    assert result.dangling_resolvers == ()
    assert result.stale_acknowledgements == ()


def test_every_path_function_is_accounted_for() -> None:
    """每個回傳 Path 的公開函式都被登記或明示豁免（無遺漏）。"""
    discovered = set(discover_path_functions())
    accounted = registry.registered_path_resolvers() | set(
        registry.ACKNOWLEDGED_NON_ASSET_PATHS
    )
    assert discovered <= accounted, discovered - accounted


def test_scenario_new_unregistered_path_function_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario：新增一個回傳 durable 路徑的函式但未登記 → 等式測試 FAIL 並指名。"""

    def brand_new_durable_root() -> "Path":  # noqa: F821
        return Path("/tmp/brand-new")

    # 讓反射認得它屬 config.paths 且回傳 Path。
    brand_new_durable_root.__module__ = "paulsha_cortex.config.paths"
    brand_new_durable_root.__annotations__ = {"return": "Path"}
    monkeypatch.setattr(paths, "brand_new_durable_root", brand_new_durable_root, raising=False)

    result = check_registry_equation()
    assert not result.ok
    assert "paulsha_cortex.config.paths:brand_new_durable_root" in result.unregistered_functions
    assert "brand_new_durable_root" in result.failure_summary()


def test_dangling_resolver_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolver 指向不存在的函式（打錯字／函式被刪）→ FAIL。"""
    bad = dataclasses.replace(
        ASSET_REGISTRY[0], path_resolver="paulsha_cortex.config.paths:nope"
    )
    patched = (bad,) + tuple(ASSET_REGISTRY[1:])
    monkeypatch.setattr(registry, "ASSET_REGISTRY", patched)
    result = check_registry_equation()
    assert not result.ok
    assert "paulsha_cortex.config.paths:nope" in result.dangling_resolvers


def test_asset_ids_unique() -> None:
    ids = [a.asset_id for a in ASSET_REGISTRY]
    assert len(ids) == len(set(ids)), "asset_id 必須唯一（無矛盾）"


def test_every_asset_fully_classified() -> None:
    """每個資產都有 tier ∈ enum、tree ∈ enum、至少一個 writer/reader、ingress_kind。"""
    for a in ASSET_REGISTRY:
        assert isinstance(a.tier, AssetTier)
        assert isinstance(a.tree, TrustTree)
        assert a.writers, a.asset_id
        assert a.readers, a.asset_id
        assert a.ingress_kind is not None, a.asset_id


def test_all_three_headless_personas_covered() -> None:
    """spec §R1：盤點必須涵蓋 builder／reviewer／planner 三者，不能只封 builder。"""
    assert registry.personas_covered() == HEADLESS_PERSONAS


def test_review_verdict_is_shortest_attack_path() -> None:
    """§3 最短攻擊路徑：review-verdict 為 job-visible 且 reviewer/同UID 可寫。"""
    verdict = registry.asset_by_id("review-verdict")
    assert verdict.tree is TrustTree.JOB_VISIBLE
    assert Principal.ANY_SAME_UID in verdict.writers
    assert verdict.tier is AssetTier.TIER_0


def test_headless_writable_manager_owned_flags_the_drift() -> None:
    """Manager-owned 但 headless 可寫的資產清單非空——這正是 Phase 2 要收斂的核心。"""
    flagged = {a.asset_id for a in registry.headless_writable_manager_owned()}
    # 背景段點名的幾項必在其中。
    for expected in {
        "runtime-agents-tree",
        "coordinator-root-tree",
        "jobs-registry",
        "delivery-journal",
        "runtime-bootstrap-env",
        "model-identity-overlay",
    }:
        assert expected in flagged, expected


def test_known_duplicate_derivations_are_registered_single_source() -> None:
    """spec §R1 Scenario「重複路徑推導」：已知重複點固化為單一 canonical asset。"""
    for asset_id, sites in KNOWN_DUPLICATE_DERIVATIONS.items():
        asset = registry.asset_by_id(asset_id)  # 單一 asset_id → 單一真相
        assert len(sites) >= 2, "重複推導清單至少兩處"
        # 該資產自身的 derived_in 也必須記錄這些推導點（收斂到登記表）。
        for site in sites:
            assert any(site.split(":")[0] in d for d in asset.derived_in), (asset_id, site)


def test_mutation_ingress_inventory_complete() -> None:
    """spec §C：八類 mutation ingress 全數登記，且現況全部未認證、headless 可達。"""
    assert len(registry.MUTATION_INGRESS) == 8
    for ingress in registry.MUTATION_INGRESS:
        assert ingress.authenticated is False
        assert ingress.headless_reachable is True


def test_tier0_assets_present() -> None:
    """Tier-0 資產至少涵蓋 spec §A 點名的 authority-bearing 清單核心。"""
    tier0_ids = {a.asset_id for a in registry.assets_by_tier(AssetTier.TIER_0)}
    for expected in {
        "jobs-registry",
        "review-verdict",
        "maintainer-attestation",
        "gate-ledger",
        "delivery-journal",
        "control-request-queue",
        "runtime-bootstrap-env",
        "model-identity-overlay",
    }:
        assert expected in tier0_ids, expected
