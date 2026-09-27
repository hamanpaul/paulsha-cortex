"""#836 collector 與 #839 admission 共用同一份 `cortex/quota-pools/v1` 設定檔。

live 驗收發現：collector 文件允許在同一份設定加上選填 `collector_targets`，但
admission 的 `parse_quota_pools_config` 以封閉鍵集合拒絕它
（`quota-pools-config-unknown-key`），shadow 因此整條退回「沒接上」、不寫
decision receipt。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import quota_admission, quota_collectors

_PROFILE = "epk:v1:resolved:" + "a" * 64
_POOL_REF = {
    "authority_id": "operator-budget-authority",
    "account_id": "codex-main",
    "pool_id": "pool-codex",
    "revision": "1",
}


def _payload(**extra: object) -> dict:
    payload = {
        "schema": "cortex/quota-pools/v1",
        "config_revision": "shared-v1",
        "descriptors": [
            {
                "schema_version": 1,
                **_POOL_REF,
                "authority_ref": "operator:pool-map/v1",
                "provenance_refs": ["operator:pool-map/v1"],
                "units": [
                    {
                        "unit_id": "codex-percent", "version": "1", "quantity_kind": "amount",
                        "semantics_ref": "provider:openai-codex-app-server/rate-limit-percent/v2",
                    }
                ],
                "windows": [
                    {
                        "window_id": "primary", "kind": "rolling",
                        "unit_ref": {"unit_id": "codex-percent", "version": "1"},
                        "duration_ms": 604_800_000,
                    }
                ],
            }
        ],
        "unit_catalog": [],
        "bindings": [
            {
                "schema_version": 1, "binding_id": "codex-builder", "revision": "1",
                "subject": {
                    "kind": "profile",
                    "profile_ref": {"state": "known", "value": {"schema_version": 1, "key": _PROFILE}},
                },
                "constraints": [{"state": "known", "value": {"pool_ref": _POOL_REF, "window_id": "primary"}}],
                "coverage": {"state": "complete", "gaps": []},
            }
        ],
    }
    payload.update(extra)
    return payload


def test_admission_and_collector_accept_the_same_file_with_collector_targets(tmp_path: Path) -> None:
    targets = {
        "codex": [
            {
                "resource_key": "codex:codex:primary", "window_id": "primary",
                "profile_key": _PROFILE, "pool_ref": _POOL_REF,
            }
        ]
    }
    path = tmp_path / "quota-pools.json"
    path.write_text(json.dumps(_payload(collector_targets=targets)), encoding="utf-8")

    admission = quota_admission.load_quota_pools_config(path)
    assert admission is not None
    assert admission.config_revision == "shared-v1"
    assert len(admission.bindings) == 1

    collector = quota_collectors.load_collector_config(path)
    assert collector.executors() == ("codex",)


def test_admission_still_rejects_truly_unknown_keys(tmp_path: Path) -> None:
    with pytest.raises(quota_admission.QuotaPoolsConfigError, match="unknown-key"):
        quota_admission.parse_quota_pools_config(_payload(surprise=1))
