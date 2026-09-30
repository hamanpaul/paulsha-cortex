from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from qualification.contract import (
    CANARY_BUILDER,
    CANARY_REVIEWER,
    PROVIDERS as PROVIDER_CONTRACTS,
    TOOLCHAIN,
    canary_identity,
    render_model_identity_overlay,
)


PROVIDER_MODELS = {name: row["model_id"] for name, row in PROVIDER_CONTRACTS.items()}
PROVIDER_EFFORTS = {name: row["effort"] for name, row in PROVIDER_CONTRACTS.items()}
TOOL_VERSIONS = {name: row["version"] for name, row in TOOLCHAIN.items()}


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "qualification" / "driver.py"


def _load_driver():
    spec = importlib.util.spec_from_file_location(
        "qualification_driver_hardening", DRIVER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _result(driver, argv, *, stdout="", stderr="", returncode=0):
    return driver.CommandResult(tuple(argv), returncode, stdout, stderr)


def _provider_name(argv) -> str:
    executable = Path(argv[0]).name
    return {"agy": "agy", "copilot": "copilot", "codex": "codex"}[executable]


def _preflight(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "ready",
        "authenticated": True,
        "quota": "available",
        "fallback": False,
        "skipped": False,
    }
    payload.update(overrides)
    return payload


AGY_CONVERSATION_ID = "2a672272-32ca-4941-967e-27ca17031611"


def _agy_print_json(*, response: str = "QUALIFICATION_OK\n", status: str = "SUCCESS") -> str:
    """真實 agy 1.2.x `--print --output-format json` 的輸出形狀（不帶 model／effort）。"""

    return json.dumps(
        {
            "conversation_id": AGY_CONVERSATION_ID,
            "status": status,
            "response": response,
            "duration_seconds": 13.8,
            "num_turns": 1,
            "usage": {"input_tokens": 14001, "output_tokens": 40, "total_tokens": 14041},
        }
    ) + "\n"


def _agy_variant() -> str:
    return f"{PROVIDER_MODELS['agy']}-{PROVIDER_EFFORTS['agy']}"


def _smoke(provider: str, *, extra_model: str | None = None) -> str:
    if provider == "agy":
        output = _agy_print_json()
        if extra_model is not None:
            output += json.dumps({"fallback": True, "fallbackModel": extra_model}) + "\n"
        return output
    models = {
        "agy": PROVIDER_MODELS["agy"],
        "copilot": PROVIDER_MODELS["copilot"],
        "codex": PROVIDER_MODELS["codex"],
    }
    efforts = PROVIDER_EFFORTS
    rows = [
        {
            "provider": provider,
            "runtime_model": models[provider],
            "runtime_effort": efforts[provider],
            "type": "final",
            "role": "assistant",
            "content": "QUALIFICATION_OK",
        }
    ]
    if provider == "codex":
        rows.insert(
            0,
            {"type": "thread.started", "thread_id": "thread-provider-smoke"},
        )
    if extra_model is not None:
        rows.append(
            {
                "provider": provider,
                "runtime_model": extra_model,
                "runtime_effort": efforts[provider],
                "fallback": True,
            }
        )
    return "".join(json.dumps(row) + "\n" for row in rows)


def test_provider_smokes_use_live_preflight_and_unique_runtime_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_run(argv, **_kwargs):
        provider = _provider_name(argv)
        calls.append(("smoke", tuple(argv)))
        return _result(driver, argv, stdout=_smoke(provider))

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(
        driver, "_provider_preflight", lambda _provider, _account: _preflight()
    )
    monkeypatch.setattr(
        driver,
        "_codex_provider_thread_result",
        lambda _thread_id, **_kwargs: {
            "thread": {"id": "thread-provider-smoke"},
            "model": PROVIDER_MODELS["codex"],
            "reasoningEffort": PROVIDER_EFFORTS["codex"],
            "modelProvider": "openai",
        },
    )
    monkeypatch.setattr(
        driver,
        "_agy_persisted_model_variants",
        lambda _conversation_id, *, account: {_agy_variant()},
    )
    verdicts = driver._provider_smokes(tmp_path)
    assert [
        (row["provider"], row["runtime_model"], row["runtime_effort"])
        for row in verdicts
    ] == [
        ("agy", PROVIDER_MODELS["agy"], PROVIDER_EFFORTS["agy"]),
        ("copilot", PROVIDER_MODELS["copilot"], PROVIDER_EFFORTS["copilot"]),
        ("codex", PROVIDER_MODELS["codex"], PROVIDER_EFFORTS["codex"]),
    ]
    assert [kind for kind, _argv in calls] == [
        "smoke",
        "smoke",
        "smoke",
    ]
    evidence = json.loads((tmp_path / "provider-capabilities.json").read_text())
    assert all(
        row["preflight"]["quota"] == "available"
        for row in evidence["providers"].values()
    )
    assert all(
        row["preflight"]["fallback"] is False for row in evidence["providers"].values()
    )
    codex_argv = calls[-1][1]
    assert codex_argv[codex_argv.index("--model") + 1] == PROVIDER_MODELS["codex"]
    assert codex_argv[codex_argv.index("-c") + 1] == (
        f'model_reasoning_effort="{PROVIDER_EFFORTS["codex"]}"'
    )
    from paulsha_cortex.coordinator.launcher import build_codex_argv
    from paulsha_cortex.trust_root.registry import JobWriteContract, sandbox_mode_for

    # smoke 在模板 unit 外直接執行：取登記表 direct 欄的 builder 預設列，且與
    # canary builder 在模板 unit 內收到的 `--sandbox` 相同；legacy Landlock 不得出現。
    assert codex_argv[codex_argv.index("--sandbox") + 1] == sandbox_mode_for(
        JobWriteContract.BUILDER_WORKSPACE_WRITE
    )
    assert codex_argv[codex_argv.index("--sandbox") + 1] == sandbox_mode_for(
        JobWriteContract.BUILDER_WRITE_FORBIDDEN, trust_root_outer_unit=True
    )
    assert "--enable" not in codex_argv
    assert "use_legacy_landlock" not in codex_argv
    # 除了工作區（cwd 不是 repo → `--skip-git-repo-check`；沒有 `-o`／`-C`）之外，
    # smoke 與 production launcher 為同一模型發出的 argv 逐字相同。
    production = build_codex_argv(
        prompt="Return exactly QUALIFICATION_OK and do not use tools.",
        slice_id="provider-smoke",
        log_dir="/nonexistent",
        model=PROVIDER_MODELS["codex"],
    )
    production = production[: production.index("-o")]
    sandbox_at = production.index("--sandbox") + 2
    production[sandbox_at:sandbox_at] = ["--skip-git-repo-check"]
    assert list(codex_argv[1:]) == production[1:]
    assert codex_argv[0] == "/opt/cortex/toolchain/bin/codex"


def test_provider_smokes_reject_requested_plus_fallback_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()

    def fake_run(argv, **_kwargs):
        provider = _provider_name(argv)
        return _result(
            driver, argv, stdout=_smoke(provider, extra_model="fallback-model")
        )

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(
        driver, "_provider_preflight", lambda _provider, _account: _preflight()
    )
    monkeypatch.setattr(
        driver,
        "_codex_provider_thread_result",
        lambda _thread_id, **_kwargs: {
            "thread": {"id": "thread-provider-smoke"},
            "model": PROVIDER_MODELS["codex"],
            "reasoningEffort": PROVIDER_EFFORTS["codex"],
            "modelProvider": "openai",
        },
    )
    monkeypatch.setattr(
        driver,
        "_agy_persisted_model_variants",
        lambda _conversation_id, *, account: {_agy_variant()},
    )
    with pytest.raises(driver.QualificationFailure, match="unique exact"):
        driver._provider_smokes(tmp_path)
    assert not (tmp_path / "provider-capabilities.json").exists()


def _smokes_with_agy(
    driver,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agy_stdout: str,
    variants: set[str] | None = None,
):
    persisted_calls: list[tuple[str, str]] = []

    def fake_run(argv, **_kwargs):
        provider = _provider_name(argv)
        if provider == "agy":
            return _result(driver, argv, stdout=agy_stdout)
        return _result(driver, argv, stdout=_smoke(provider))

    def fake_variants(conversation_id, *, account):
        persisted_calls.append((conversation_id, account))
        return set(variants if variants is not None else {_agy_variant()})

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(
        driver, "_provider_preflight", lambda _provider, _account: _preflight()
    )
    monkeypatch.setattr(driver, "_agy_persisted_model_variants", fake_variants)
    monkeypatch.setattr(
        driver,
        "_codex_provider_thread_result",
        lambda _thread_id, **_kwargs: {
            "thread": {"id": "thread-provider-smoke"},
            "model": PROVIDER_MODELS["codex"],
            "reasoningEffort": PROVIDER_EFFORTS["codex"],
            "modelProvider": "openai",
        },
    )
    return persisted_calls


def test_agy_smoke_proves_model_and_effort_from_persisted_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#716：agy 1.2.x 的 JSON 輸出不帶 model／effort，改由持久化的變體 id 證明。"""

    driver = _load_driver()
    calls = _smokes_with_agy(
        driver, tmp_path, monkeypatch, agy_stdout=_agy_print_json()
    )
    verdicts = driver._provider_smokes(tmp_path)
    agy = verdicts[0]
    assert (agy["provider"], agy["runtime_model"], agy["runtime_effort"]) == (
        "agy",
        PROVIDER_MODELS["agy"],
        PROVIDER_EFFORTS["agy"],
    )
    assert calls == [(AGY_CONVERSATION_ID, PROVIDER_CONTRACTS["agy"]["account"])]
    evidence = json.loads((tmp_path / "provider-capabilities.json").read_text())
    assert evidence["providers"]["agy"]["persisted_variants"] == [_agy_variant()]
    assert evidence["providers"]["agy"]["models"] == [PROVIDER_MODELS["agy"]]
    assert evidence["providers"]["agy"]["efforts"] == [PROVIDER_EFFORTS["agy"]]


@pytest.mark.parametrize(
    ("variants", "fragment"),
    [
        ({f"{PROVIDER_MODELS['agy']}-low"}, "-low"),
        ({f"{PROVIDER_MODELS['agy']}-high", f"{PROVIDER_MODELS['agy']}-low"}, "-low"),
        (set(), "models=[]"),
    ],
)
def test_agy_smoke_rejects_persisted_variant_that_is_not_the_requested_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variants: set[str],
    fragment: str,
) -> None:
    driver = _load_driver()
    _smokes_with_agy(
        driver,
        tmp_path,
        monkeypatch,
        agy_stdout=_agy_print_json(),
        variants=variants,
    )
    with pytest.raises(driver.QualificationFailure, match="provider agy") as caught:
        driver._provider_smokes(tmp_path)
    assert fragment in str(caught.value)
    assert not (tmp_path / "provider-capabilities.json").exists()


@pytest.mark.parametrize(
    "agy_stdout",
    [
        _agy_print_json(response="QUALIFICATION_OK extra\n"),
        _agy_print_json(response="QUALIFICATION_OK\n\n"),
        _agy_print_json(status="ERROR"),
        json.dumps({"status": "SUCCESS", "response": "QUALIFICATION_OK\n"}) + "\n",
        json.dumps(
            {"conversation_id": "not-a-uuid", "status": "SUCCESS", "response": "QUALIFICATION_OK\n"}
        )
        + "\n",
    ],
)
def test_agy_smoke_requires_exact_response_and_unique_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agy_stdout: str
) -> None:
    driver = _load_driver()
    _smokes_with_agy(driver, tmp_path, monkeypatch, agy_stdout=agy_stdout)
    with pytest.raises(driver.QualificationFailure, match="provider agy"):
        driver._provider_smokes(tmp_path)


def test_agy_persisted_variants_are_read_as_the_account_from_its_own_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3
    import subprocess

    driver = _load_driver()
    home = tmp_path / "home"
    conversations = home / ".gemini" / "antigravity-cli" / "conversations"
    conversations.mkdir(parents=True)
    database = sqlite3.connect(conversations / f"{AGY_CONVERSATION_ID}.db")
    database.execute("create table executor_metadata (idx integer, data blob)")
    database.execute(
        "insert into executor_metadata values (0, ?)",
        (b"\x0a\x15" + _agy_variant().encode() + b"\x12\x05other",),
    )
    database.commit()
    database.close()
    seen: list[tuple[tuple[str, ...], str | None]] = []

    def fake_run(argv, *, user=None, env=None, timeout=120):
        seen.append((tuple(argv), user))
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            check=False,
        )
        return driver.CommandResult(
            tuple(argv), completed.returncode, completed.stdout, completed.stderr
        )

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(driver, "_account_env", lambda _account: {"HOME": str(home)})
    variants = driver._agy_persisted_model_variants(
        AGY_CONVERSATION_ID, account="cortex-reviewer-planner"
    )
    assert variants == {_agy_variant()}
    argv, user = seen[0]
    assert user == "cortex-reviewer-planner"
    assert argv[:2] == ("/usr/bin/python3", "-I")
    with pytest.raises(driver.QualificationFailure):
        driver._agy_persisted_model_variants("../escape", account="cortex-reviewer-planner")


def test_provider_preflight_uses_only_supported_pinned_argv() -> None:
    driver = _load_driver()
    adapters = driver.PROVIDER_PREFLIGHTS

    assert adapters["agy"].version == TOOL_VERSIONS["agy"]
    assert adapters["agy"].version_command[-1] == "--version"
    assert adapters["agy"].status_command[-4:] == (
        "-p",
        "/quota",
        "--output-format",
        "json",
    )
    assert adapters["copilot"].version is None
    # #716：copilot 1.0.88 的 `--no-auto-login` 會讓 headless server 不載入已儲存的
    # 登入狀態，`account.getCurrentAuth` 因此回空結果；不得再帶這個旗標。
    assert adapters["copilot"].status_command[-3:] == (
        "--headless",
        "--no-auto-update",
        "--stdio",
    )
    assert "--no-auto-login" not in adapters["copilot"].status_command
    assert adapters["copilot"].status_kind == "copilot-app-server"
    assert adapters["codex"].version == TOOL_VERSIONS["codex"]
    assert adapters["codex"].status_command[-2:] == ("app-server", "--stdio")
    assert adapters["codex"].status_kind == "codex-app-server"
    serialized = repr(adapters)
    assert "status --output-format json" not in serialized
    assert "login status --json" not in serialized
    assert "doctor" not in serialized


def test_agy_preflight_accepts_machine_readable_quota_without_prompt_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    calls: list[tuple[str, ...]] = []
    quota = {
        "status": "SUCCESS",
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {"buckets": [{"remaining_fraction": 0.5}]},
                ]
            },
        },
    }

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        if argv[-1] == "--version":
            return _result(driver, argv, stdout=f"agy version {TOOL_VERSIONS['agy']}\n")
        return _result(driver, argv, stdout=json.dumps(quota))

    monkeypatch.setattr(driver, "_run", fake_run)
    assert driver._provider_preflight("agy", "cortex-reviewer-planner") == {
        "status": "ready",
        "authenticated": True,
        "quota": "available",
        "fallback": False,
    }
    assert len(calls) == 2
    assert calls[0][-1] == "--version"
    assert calls[1][-4:] == ("-p", "/quota", "--output-format", "json")


def test_agy_preflight_rejects_exhausted_machine_readable_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()

    def fake_run(argv, **_kwargs):
        if argv[-1] == "--version":
            return _result(driver, argv, stdout=f"agy version {TOOL_VERSIONS['agy']}\n")
        return _result(
            driver,
            argv,
            stdout=json.dumps(
                {
                    "status": "SUCCESS",
                    "command": {
                        "name": "usage",
                        "data": {"groups": [{"buckets": [{"remaining_fraction": 0}]}]},
                    },
                }
            ),
        )

    monkeypatch.setattr(driver, "_run", fake_run)
    with pytest.raises(driver.QualificationFailure, match="no remaining capacity"):
        driver._provider_preflight("agy", "cortex-reviewer-planner")


def test_copilot_app_server_accepts_authenticated_quota_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        return _result(driver, argv, stdout=f"Copilot CLI {TOOL_VERSIONS['copilot']}\n")

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(
        driver,
        "_copilot_app_server_exchange",
        lambda _command, **_kwargs: (
            {
                "id": 2,
                "result": {
                    "authInfo": {"type": "user", "login": "redacted-user"}
                },
            },
            {
                "id": 3,
                "result": {
                    "quotaSnapshots": {
                        "premium_interactions": {
                            "remainingPercentage": 65,
                            "entitlementRequests": 100,
                            "usedRequests": 35,
                        },
                        "chat": {"isUnlimitedEntitlement": True},
                    }
                },
            },
        ),
    )

    assert driver._provider_preflight("copilot", "cortex-reviewer-planner") == {
        "status": "ready",
        "authenticated": True,
        "quota": "available",
        "fallback": False,
    }
    assert calls == [("/opt/cortex/toolchain/bin/copilot", "--version")]


def _copilot_preflight_with_quota(driver, monkeypatch, snapshots):
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    monkeypatch.setattr(
        driver,
        "_run",
        lambda argv, **_kwargs: _result(
            driver, argv, stdout=f"Copilot CLI {TOOL_VERSIONS['copilot']}\n"
        ),
    )
    monkeypatch.setattr(
        driver,
        "_copilot_app_server_exchange",
        lambda _command, **_kwargs: (
            {"id": 2, "result": {"authInfo": {"type": "user", "login": "redacted-user"}}},
            {"id": 3, "result": {"quotaSnapshots": snapshots}},
        ),
    )
    return driver._provider_preflight("copilot", "cortex-reviewer-planner")


def _copilot_live_snapshots(**premium_overrides):
    """copilot 1.0.88 `account.getQuota` 的真實形狀（去識別化）。"""

    unlimited = {
        "isUnlimitedEntitlement": True,
        "entitlementRequests": 0,
        "usedRequests": 0,
        "usageAllowedWithExhaustedQuota": False,
        "overage": 0,
        "overageAllowedWithExhaustedQuota": False,
        "remainingPercentage": 100,
        "hasQuota": True,
        "tokenBasedBilling": False,
        "overageEntitlement": 0,
    }
    premium = {
        "isUnlimitedEntitlement": False,
        "entitlementRequests": 1500,
        "usedRequests": 1500,
        "usageAllowedWithExhaustedQuota": True,
        "overage": 1682,
        "overageAllowedWithExhaustedQuota": True,
        "remainingPercentage": 0,
        "hasQuota": True,
        "tokenBasedBilling": False,
        "overageEntitlement": 0,
    }
    premium.update(premium_overrides)
    return {"chat": dict(unlimited), "completions": dict(unlimited), "premium_interactions": premium}


def test_copilot_preflight_accepts_exhausted_quota_when_usage_remains_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#716：額度用盡但帳號允許超額繼續使用時，provider 仍可服務。"""

    driver = _load_driver()
    assert _copilot_preflight_with_quota(driver, monkeypatch, _copilot_live_snapshots()) == {
        "status": "ready",
        "authenticated": True,
        "quota": "available",
        "fallback": False,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"usageAllowedWithExhaustedQuota": False, "overageAllowedWithExhaustedQuota": False},
        {"usageAllowedWithExhaustedQuota": None, "overageAllowedWithExhaustedQuota": None},
        {"hasQuota": False},
    ],
)
def test_copilot_preflight_rejects_exhausted_quota_without_allowed_usage(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object]
) -> None:
    driver = _load_driver()
    with pytest.raises(driver.QualificationFailure, match="no remaining quota"):
        _copilot_preflight_with_quota(
            driver, monkeypatch, _copilot_live_snapshots(**overrides)
        )


def test_copilot_app_server_without_auth_or_quota_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    monkeypatch.setattr(
        driver,
        "_run",
        lambda argv, **_kwargs: _result(
            driver, argv, stdout=f"Copilot CLI {TOOL_VERSIONS['copilot']}\n"
        ),
    )
    monkeypatch.setattr(
        driver,
        "_copilot_app_server_exchange",
        lambda _command, **_kwargs: (
            {"id": 2, "result": {}},
            {
                "id": 3,
                "error": {
                    "code": -32603,
                    "message": "Not authenticated",
                },
            },
        ),
    )
    with pytest.raises(driver.QualificationFailure, match="authenticated account"):
        driver._provider_preflight("copilot", "cortex-reviewer-planner")


def test_codex_app_server_accepts_authenticated_rate_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        return _result(driver, argv, stdout=f"codex-cli {TOOL_VERSIONS['codex']}\n")

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(
        driver,
        "_codex_app_server_exchange",
        lambda command, **_kwargs: (
            {
                "id": 2,
                "result": {
                    "account": {
                        "type": "chatgpt",
                        "email": "redacted@example.invalid",
                        "planType": "pro",
                    },
                    "requiresOpenaiAuth": False,
                },
            },
            {
                "id": 3,
                "result": {
                    "rateLimits": {
                        "primary": {
                            "usedPercent": 25,
                            "windowDurationMins": 300,
                            "resetsAt": 1_900_000_000,
                        },
                        "secondary": None,
                        "rateLimitReachedType": None,
                        "spendControlReached": None,
                    }
                },
            },
        ),
    )

    assert driver._provider_preflight("codex", "cortex-builder") == {
        "status": "ready",
        "authenticated": True,
        "quota": "available",
        "fallback": False,
    }
    assert calls == [("/opt/cortex/toolchain/bin/codex", "--version")]


def _codex_preflight_with_rate_limits(driver, monkeypatch, result):
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    monkeypatch.setattr(
        driver,
        "_run",
        lambda argv, **_kwargs: _result(
            driver, argv, stdout=f"codex-cli {TOOL_VERSIONS['codex']}\n"
        ),
    )
    monkeypatch.setattr(
        driver,
        "_codex_app_server_exchange",
        lambda _command, **_kwargs: (
            {"id": 2, "result": {"account": {"type": "chatgpt", "planType": "prolite"}}},
            {"id": 3, "result": result},
        ),
    )
    return driver._provider_preflight("codex", "cortex-builder")


def _codex_live_rate_limits(*, ordinary_usage_allowed, has_credits=False, used=11):
    """codex 0.157 `account/rateLimits/read` 的真實形狀（去識別化）。"""

    return {
        "ordinaryUsageAllowed": ordinary_usage_allowed,
        "rateLimits": {
            "limitName": None,
            "normalModelSlug": None,
            "primary": {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": 1_900_000_000},
            "secondary": None,
            "credits": {"hasCredits": has_credits, "unlimited": False, "balance": "0"},
            "spendControlReached": False,
            "planType": "prolite",
            "rateLimitReachedType": None,
        },
        "rateLimitResetCredits": {"availableCount": 0, "credits": []},
        "rateLimitUpsell": None,
    }


def test_codex_preflight_ignores_purchased_credits_when_plan_usage_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#716：`credits` 是加購點數；方案內額度可用（ordinaryUsageAllowed）時不看它。"""

    driver = _load_driver()
    assert _codex_preflight_with_rate_limits(
        driver, monkeypatch, _codex_live_rate_limits(ordinary_usage_allowed=True)
    ) == {"status": "ready", "authenticated": True, "quota": "available", "fallback": False}


@pytest.mark.parametrize("ordinary", [False, None])
def test_codex_preflight_requires_credits_when_plan_usage_is_not_allowed(
    monkeypatch: pytest.MonkeyPatch, ordinary: object
) -> None:
    driver = _load_driver()
    with pytest.raises(driver.QualificationFailure, match="no remaining credits"):
        _codex_preflight_with_rate_limits(
            driver, monkeypatch, _codex_live_rate_limits(ordinary_usage_allowed=ordinary)
        )
    assert _codex_preflight_with_rate_limits(
        driver,
        monkeypatch,
        _codex_live_rate_limits(ordinary_usage_allowed=ordinary, has_credits=True),
    )["quota"] == "available"


def test_codex_preflight_still_rejects_an_exhausted_plan_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    with pytest.raises(driver.QualificationFailure, match="no remaining rate-limit capacity"):
        _codex_preflight_with_rate_limits(
            driver, monkeypatch, _codex_live_rate_limits(ordinary_usage_allowed=True, used=100)
        )


def test_codex_app_server_without_live_account_or_rate_limits_fails_closed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        return _result(driver, argv, stdout=f"codex-cli {TOOL_VERSIONS['codex']}\n")

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(
        driver,
        "_codex_app_server_exchange",
        lambda _command, **_kwargs: (
            {"id": 2, "result": {"account": None, "requiresOpenaiAuth": True}},
            {
                "id": 3,
                "error": {
                    "code": -32600,
                    "message": "codex account authentication required",
                },
            },
        ),
    )
    with pytest.raises(driver.QualificationFailure, match="authenticated account"):
        driver._provider_preflight("codex", "cortex-builder")
    assert calls == [("/opt/cortex/toolchain/bin/codex", "--version")]


_FAKE_CODEX_APP_SERVER = r"""
import json, sys, time
answer_rate_limits = sys.argv[1] == "answer"
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        print(json.dumps({"id": message["id"], "result": {"userAgent": "fake"}}), flush=True)
    elif method == "account/read":
        print(json.dumps({"id": message["id"], "result": {"account": {"type": "chatgpt"}}}), flush=True)
    elif method == "account/rateLimits/read" and answer_rate_limits:
        print(json.dumps({"id": message["id"], "result": {"rateLimits": {}}}), flush=True)
"""


def test_codex_app_server_timeout_names_the_pending_request() -> None:
    """#716：canary 只看到 `status probe timed out`，無從判斷卡在哪個請求。"""

    driver = _load_driver()
    with pytest.raises(
        driver.QualificationFailure, match="timed out waiting for account/rateLimits/read"
    ):
        driver._codex_app_server_exchange(
            (sys.executable, "-c", _FAKE_CODEX_APP_SERVER, "silent"),
            user="",
            env={},
            timeout=2,
        )
    account, rate_limits = driver._codex_app_server_exchange(
        (sys.executable, "-c", _FAKE_CODEX_APP_SERVER, "answer"),
        user="",
        env={},
        timeout=10,
    )
    assert account["result"]["account"]["type"] == "chatgpt"
    assert rate_limits["result"] == {"rateLimits": {}}


def _codex_ready_responses():
    return (
        {"id": 2, "result": {"account": {"type": "chatgpt", "planType": "prolite"}}},
        {
            "id": 3,
            "result": {
                "ordinaryUsageAllowed": True,
                "rateLimits": {
                    "primary": {"usedPercent": 11, "windowDurationMins": 10080, "resetsAt": 1_900_000_000},
                    "secondary": None,
                    "rateLimitReachedType": None,
                    "spendControlReached": False,
                },
            },
        },
    )


def test_codex_preflight_retries_a_timed_out_status_probe_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    monkeypatch.setattr(
        driver,
        "_run",
        lambda argv, **_kwargs: _result(
            driver, argv, stdout=f"codex-cli {TOOL_VERSIONS['codex']}\n"
        ),
    )
    attempts: list[int] = []

    def flaky(_command, **kwargs):
        attempts.append(kwargs["timeout"])
        if len(attempts) == 1:
            raise driver.QualificationFailure(
                "Codex app-server status probe timed out waiting for account/rateLimits/read"
            )
        return _codex_ready_responses()

    monkeypatch.setattr(driver, "_codex_app_server_exchange", flaky)
    assert driver._provider_preflight("codex", "cortex-builder")["status"] == "ready"
    assert attempts == [60, 60]


def test_codex_preflight_recovers_on_the_third_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#716：canary 兩度在 account/read 連續逾時兩次，重跑即過——第三次 process 要能接住。"""
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    monkeypatch.setattr(
        driver,
        "_run",
        lambda argv, **_kwargs: _result(
            driver, argv, stdout=f"codex-cli {TOOL_VERSIONS['codex']}\n"
        ),
    )
    attempts: list[int] = []

    def twice_stuck(_command, **kwargs):
        attempts.append(kwargs["timeout"])
        if len(attempts) < 3:
            raise driver.QualificationFailure(
                "Codex app-server status probe timed out waiting for account/read"
            )
        return _codex_ready_responses()

    monkeypatch.setattr(driver, "_codex_app_server_exchange", twice_stuck)
    assert driver._provider_preflight("codex", "cortex-builder")["status"] == "ready"
    assert attempts == [60, 60, 60]


def test_codex_preflight_fails_after_the_last_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    monkeypatch.setattr(
        driver,
        "_run",
        lambda argv, **_kwargs: _result(
            driver, argv, stdout=f"codex-cli {TOOL_VERSIONS['codex']}\n"
        ),
    )
    attempts: list[int] = []

    def always_timeout(_command, **kwargs):
        attempts.append(kwargs["timeout"])
        raise driver.QualificationFailure(
            "Codex app-server status probe timed out waiting for account/read"
        )

    monkeypatch.setattr(driver, "_codex_app_server_exchange", always_timeout)
    with pytest.raises(driver.QualificationFailure, match="timed out waiting for account/read"):
        driver._provider_preflight("codex", "cortex-builder")
    assert len(attempts) == 3


def test_codex_preflight_does_not_retry_a_definitive_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    monkeypatch.setattr(
        driver,
        "_run",
        lambda argv, **_kwargs: _result(
            driver, argv, stdout=f"codex-cli {TOOL_VERSIONS['codex']}\n"
        ),
    )
    attempts: list[int] = []

    def closed(_command, **kwargs):
        attempts.append(kwargs["timeout"])
        raise driver.QualificationFailure("Codex app-server closed before status response")

    monkeypatch.setattr(driver, "_codex_app_server_exchange", closed)
    with pytest.raises(driver.QualificationFailure, match="closed before status response"):
        driver._provider_preflight("codex", "cortex-builder")
    assert len(attempts) == 1


def test_codex_agent_loop_uses_provider_persisted_thread_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    monkeypatch.setattr(
        driver,
        "_codex_provider_thread_result",
        lambda _thread_id, **_kwargs: {
            "thread": {"id": "thread-build-job"},
            "model": PROVIDER_MODELS["codex"],
            "reasoningEffort": PROVIDER_EFFORTS["codex"],
            "modelProvider": "openai",
            "persistedModelProvider": "openai",
            "persistedCwd": "/var/lib/cortex/worktree/build-job",
        },
    )

    identity = driver._codex_thread_runtime_identity(
        "thread-build-job",
        expected_worktree="/var/lib/cortex/worktree/build-job",
        codex_home=Path("/var/lib/cortex/runtime/codex-home/builder/build-job"),
    )

    assert identity == {
        "runtime_model": PROVIDER_MODELS["codex"],
        "runtime_effort": PROVIDER_EFFORTS["codex"],
        "model_provider": "openai",
        "thread_sha256": hashlib.sha256(b"thread-build-job").hexdigest(),
    }


def test_codex_provider_thread_combines_read_and_resume_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {"HOME": "/builder"})
    observed_env: dict[str, str] = {}

    def fake_exchange(_command, **kwargs):
        observed_env.update(kwargs["env"])
        return (
            {
                "id": 2,
                "result": {
                    "thread": {
                        "id": "thread-build-job",
                        "cwd": "/reclaimed/build-job",
                        "modelProvider": "openai",
                    }
                },
            },
            {
                "id": 3,
                "result": {
                    "thread": {"id": "thread-build-job"},
                    "model": PROVIDER_MODELS["codex"],
                    "reasoningEffort": PROVIDER_EFFORTS["codex"],
                    "modelProvider": "openai",
                    "cwd": "/builder",
                },
            },
        )

    monkeypatch.setattr(
        driver,
        "_codex_thread_resume_exchange",
        fake_exchange,
    )

    result = driver._codex_provider_thread_result(
        "thread-build-job", codex_home=Path("/runtime/codex-home/build-job")
    )

    assert result["persistedCwd"] == "/reclaimed/build-job"
    assert result["persistedModelProvider"] == "openai"
    assert result["model"] == PROVIDER_MODELS["codex"]
    assert observed_env["HOME"] == "/builder"
    assert observed_env["CODEX_HOME"] == "/runtime/codex-home/build-job"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "different-model"),
        ("reasoningEffort", "normal"),
        ("modelProvider", "fallback"),
        ("persistedModelProvider", "fallback"),
        ("persistedCwd", "/tmp/forged"),
    ],
)
def test_codex_agent_loop_rejects_provider_thread_identity_drift(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    result = {
        "thread": {"id": "thread-build-job"},
        "model": PROVIDER_MODELS["codex"],
        "reasoningEffort": PROVIDER_EFFORTS["codex"],
        "modelProvider": "openai",
        "persistedModelProvider": "openai",
        "persistedCwd": "/var/lib/cortex/worktree/build-job",
    }
    result[field] = value
    monkeypatch.setattr(
        driver,
        "_codex_provider_thread_result",
        lambda _thread_id, **_kwargs: result,
    )

    with pytest.raises(driver.QualificationFailure, match="provider thread identity"):
        driver._codex_thread_runtime_identity(
            "thread-build-job",
            expected_worktree="/var/lib/cortex/worktree/build-job",
            codex_home=Path("/var/lib/cortex/runtime/codex-home/builder/build-job"),
        )


def test_generated_canary_identity_overlay_authorizes_independent_dispatch(
    tmp_path: Path,
) -> None:
    driver = _load_driver()
    config_root = tmp_path / "config"
    config_root.mkdir()
    (config_root / "model-identities.yaml").write_text(
        render_model_identity_overlay(), encoding="utf-8"
    )

    from paulsha_cortex.coordinator.model_identities import load_model_identities
    from paulsha_cortex.coordinator.model_resolution import PACKAGED_FALLBACK_DENY

    roster = load_model_identities(config_root)
    assert roster.resolution_context.policy.packaged_fallback == PACKAGED_FALLBACK_DENY
    builder = roster.require(*canary_identity(CANARY_BUILDER))
    reviewer = roster.require(*canary_identity(CANARY_REVIEWER))
    assert (builder.executor, builder.model_id) == (
        driver.DEPLOYMENT_CANARY_BUILDER_EXECUTOR,
        driver.DEPLOYMENT_CANARY_BUILDER_MODEL,
    )
    assert "build" in builder.capabilities
    assert {"planning", "review"} <= set(reviewer.capabilities)
    assert builder.independence_domain == "openai"
    assert reviewer.independence_domain == "google"
    # AGY 的 identity id 以 `<model>-<effort>` 表達 effort，由 provider 契約導出。
    assert reviewer.model_id == (
        f"{PROVIDER_MODELS['agy']}-{PROVIDER_EFFORTS['agy']}"
    )
    driver._validate_canary_dispatch_model_identities(
        {"PSC_PROJECT_CONFIG_ROOT": str(config_root)}
    )


@pytest.mark.parametrize(
    "mutation", ["missing-overlay", "same-domain-reviewer", "builder-without-build"]
)
def test_canary_identity_preflight_rejects_unusable_roster(
    tmp_path: Path, mutation: str
) -> None:
    driver = _load_driver()
    config_root = tmp_path / "config"
    config_root.mkdir()
    overlay = render_model_identity_overlay()
    if mutation == "same-domain-reviewer":
        overlay = overlay.replace('independence_domain: "google"', 'independence_domain: "openai"')
        overlay = overlay.replace('    live_probe: "agy-plan-sandbox"\n', "")
        overlay = overlay.replace('["planning", "review"]', '["review"]')
    elif mutation == "builder-without-build":
        overlay = overlay.replace('capabilities: ["build"]', 'capabilities: ["review"]')
    if mutation != "missing-overlay":
        (config_root / "model-identities.yaml").write_text(overlay, encoding="utf-8")

    with pytest.raises(driver.QualificationFailure, match="model identity roster"):
        driver._validate_canary_dispatch_model_identities(
            {"PSC_PROJECT_CONFIG_ROOT": str(config_root)}
        )


def test_canary_builder_argv_matches_the_production_template_launcher(
    tmp_path: Path,
) -> None:
    """closeout 期待的 builder argv 必須與 #716 後 launcher 在模板 unit 內發出的逐字相同。"""

    driver = _load_driver()
    from paulsha_cortex.coordinator.launcher import build_codex_argv

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    last = tmp_path / "job.last.json"
    production = build_codex_argv(
        prompt="PROMPT",
        slice_id="job",
        log_dir=str(tmp_path),
        worktree=str(worktree),
        model=driver.DEPLOYMENT_CANARY_BUILDER_MODEL,
        write_forbidden=True,
        trust_root_outer_unit=True,
        last_message_path=str(last),
    )
    expected = [
        "codex",
        "exec",
        "--ignore-user-config",
        "PROMPT",
        "--json",
        *driver._codex_canary_builder_sandbox_argv(),
        "--model",
        driver.DEPLOYMENT_CANARY_BUILDER_MODEL,
        "-c",
        f'model_reasoning_effort="{PROVIDER_EFFORTS["codex"]}"',
        "-o",
        str(last),
        "-C",
        str(worktree.resolve()),
    ]
    assert production == expected
    assert "--enable" not in production


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        (
            [{"type": "final", "role": "assistant", "content": "QUALIFICATION_OK"}],
            True,
        ),
        (
            [
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "QUALIFICATION_OK"},
                }
            ],
            True,
        ),
        (
            [
                {
                    "type": "assistant.message",
                    "data": {"content": "QUALIFICATION_OK"},
                }
            ],
            True,
        ),
        (
            [
                {
                    "type": "user.message",
                    "data": {"content": "Return exactly QUALIFICATION_OK"},
                }
            ],
            False,
        ),
        (
            [
                {
                    "type": "assistant.message",
                    "data": {"content": "I refuse. QUALIFICATION_OK"},
                }
            ],
            False,
        ),
        (
            [
                {
                    "type": "assistant.message",
                    "data": {"content": "QUALIFICATION_OK extra"},
                }
            ],
            False,
        ),
        (
            [
                {
                    "type": "assistant.message",
                    "data": {"content": "QUALIFICATION_OK"},
                },
                {
                    "type": "assistant.message",
                    "data": {"content": "QUALIFICATION_OK"},
                },
            ],
            False,
        ),
        ([{"type": "user", "content": "Return QUALIFICATION_OK"}], False),
        (
            [
                {
                    "type": "final",
                    "role": "assistant",
                    "content": "I refuse. QUALIFICATION_OK",
                }
            ],
            False,
        ),
        (
            [{"type": "final", "role": "assistant", "content": "QUALIFICATION_OK\n"}],
            False,
        ),
    ],
)
def test_final_assistant_response_must_be_exact(
    records: list[object], expected: bool
) -> None:
    driver = _load_driver()
    assert driver._has_exact_final_assistant_response(records) is expected


def test_manager_github_probe_uses_only_installed_manager_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    home = tmp_path / "manager-home"
    home.mkdir()
    gitconfig = home / ".gitconfig"
    # 與 permgen.build_account_gitconfig() 為 durable owner 產生的形狀相同（#716）。
    gitconfig.write_text(_INSTALLED_MANAGER_CREDENTIAL_SECTION, encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.setattr(
        driver.pwd,
        "getpwnam",
        lambda _account: SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid()),
    )
    monkeypatch.setattr(
        driver, "_require_installed_manager_gitconfig", lambda path: None
    )
    calls: list[tuple[tuple[str, ...], str | None, dict[str, str]]] = []
    refs = "a" * 40 + "\trefs/heads/main\n"

    def fake_run(argv, *, user=None, env=None, timeout=120):
        command = tuple(argv)
        calls.append((command, user, dict(env or {})))
        if "config" in command:
            return _result(driver, command, stdout=_installed_helper_rows(gitconfig))
        if command[:2] == ("/usr/bin/python3", "-c"):
            return _result(driver, command, stdout="credential-ok\n")
        if "ls-remote" in command:
            return _result(driver, command, stdout=refs)
        return _result(driver, command)

    monkeypatch.setattr(driver, "_run", fake_run)
    driver._manager_github_probe("owner/repo", "b" * 40, evidence, source_repo=repo)

    assert calls
    assert all(user == "cortex-manager" for _argv, user, _env in calls)
    assert all(env["HOME"] == str(home) for _argv, _user, env in calls)
    push = next(argv for argv, _user, _env in calls if "push" in argv)
    assert "-c" not in push
    assert not any("credential.helper" in value for value in push)
    assert any(argv[:2] == ("/usr/bin/python3", "-c") for argv, _user, _env in calls)
    payload = json.loads((evidence / "manager-github-auth.json").read_text())
    assert payload["remote_refs_unchanged"] is True
    assert payload["before_sha256"] == payload["after_sha256"]


_INSTALLED_MANAGER_CREDENTIAL_SECTION = (
    '[credential "https://github.com"]\n'
    "\thelper =\n"
    "\thelper = !/usr/bin/gh auth git-credential\n"
)
_HELPER_KEY = "credential.https://github.com.helper"


def _installed_helper_rows(gitconfig) -> str:
    """`git config --show-origin --show-scope --get-regexp` 對 installed gitconfig 的真實輸出。"""

    origin = f"file:{gitconfig}"
    return (
        f"global\t{origin}\t{_HELPER_KEY} \n"
        f"global\t{origin}\t{_HELPER_KEY} !/usr/bin/gh auth git-credential\n"
    )


@pytest.mark.parametrize(
    "config_output",
    [
        # repo 本地再加掛一個 helper（排在 reset 之後，會真的生效）
        _installed_helper_rows("/installed/.gitconfig")
        + "local\tfile:/repo/.git/config\tcredential.helper store\n",
        # 來源不是 installed gitconfig
        _installed_helper_rows("/wrong/.gitconfig"),
        # 舊的不限 URL 形狀（installer 從不產生）
        "global\tfile:/installed/.gitconfig\tcredential.helper !/usr/bin/gh auth git-credential\n",
        # 缺少清空繼承 helper 的 reset 列
        f"global\tfile:/installed/.gitconfig\t{_HELPER_KEY} !/usr/bin/gh auth git-credential\n",
        # helper 不是 gh
        f"global\tfile:/installed/.gitconfig\t{_HELPER_KEY} \n"
        f"global\tfile:/installed/.gitconfig\t{_HELPER_KEY} store\n",
        # 找不到任何 helper
        "",
    ],
)
def test_manager_github_probe_rejects_ambiguous_or_uninstalled_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_output: str
) -> None:
    driver = _load_driver()
    home = Path("/installed")
    monkeypatch.setattr(
        driver.pwd,
        "getpwnam",
        lambda _account: SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid()),
    )
    monkeypatch.setattr(
        driver, "_require_installed_manager_gitconfig", lambda path: None
    )

    def fake_run(argv, **_kwargs):
        if "config" in argv:
            return _result(driver, argv, stdout=config_output)
        return _result(driver, argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    with pytest.raises(driver.QualificationFailure, match="credential helper"):
        driver._manager_github_probe(
            "owner/repo", "b" * 40, tmp_path, source_repo=tmp_path
        )


def test_manager_helper_inventory_parses_real_git_output_for_the_installed_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#716：canary 以 `credential.helper` 查不到 installer 寫的 URL-scoped helper。"""

    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    driver = _load_driver()
    home = tmp_path / "manager-home"
    home.mkdir()
    gitconfig = home / ".gitconfig"
    gitconfig.write_text(_INSTALLED_MANAGER_CREDENTIAL_SECTION, encoding="utf-8")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(
        driver.pwd,
        "getpwnam",
        lambda _account: SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid()),
    )
    monkeypatch.setattr(
        driver, "_require_installed_manager_gitconfig", lambda path: None
    )
    refs = "a" * 40 + "\trefs/heads/main\n"

    def fake_run(argv, *, user=None, env=None, timeout=120):
        command = tuple(argv)
        if "config" in command:
            completed = subprocess.run(
                ["git", *command[1:]],
                capture_output=True,
                text=True,
                env={"HOME": str(home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1"},
                check=False,
            )
            return driver.CommandResult(
                command, completed.returncode, completed.stdout, completed.stderr
            )
        if command[:2] == ("/usr/bin/python3", "-c"):
            return _result(driver, command, stdout="credential-ok\n")
        if "ls-remote" in command:
            return _result(driver, command, stdout=refs)
        return _result(driver, command)

    monkeypatch.setattr(driver, "_run", fake_run)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    driver._manager_github_probe("owner/repo", "b" * 40, evidence, source_repo=repo)
    (repo / ".git" / "config").write_text(
        (repo / ".git" / "config").read_text(encoding="utf-8")
        + "[credential]\n\thelper = store\n",
        encoding="utf-8",
    )
    with pytest.raises(driver.QualificationFailure, match="credential helper"):
        driver._manager_github_probe("owner/repo", "b" * 40, evidence, source_repo=repo)


def test_manager_probe_lists_only_refs_so_real_ls_remote_output_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#716：真實 `git ls-remote` 第一行是 `HEAD`，canary 以 malformed remote refs 失敗。"""

    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    driver = _load_driver()
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "--allow-empty", "-m", "seed"],
        check=True,
    )
    subprocess.run(["git", "-C", str(work), "push", "-q", str(origin), "main"], check=True)
    home = tmp_path / "manager-home"
    home.mkdir()
    gitconfig = home / ".gitconfig"
    gitconfig.write_text(_INSTALLED_MANAGER_CREDENTIAL_SECTION, encoding="utf-8")
    monkeypatch.setattr(
        driver.pwd,
        "getpwnam",
        lambda _account: SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid()),
    )
    monkeypatch.setattr(driver, "_require_installed_manager_gitconfig", lambda path: None)
    seen: list[tuple[str, ...]] = []

    def fake_run(argv, *, user=None, env=None, timeout=120):
        command = tuple(argv)
        seen.append(command)
        if "config" in command:
            return _result(driver, command, stdout=_installed_helper_rows(gitconfig))
        if command[:2] == ("/usr/bin/python3", "-c"):
            return _result(driver, command, stdout="credential-ok\n")
        if "ls-remote" in command:
            real = [c for c in command[1:] if c != f"https://github.com/owner/repo.git"]
            completed = subprocess.run(
                ["git", *real, str(origin)], capture_output=True, text=True, check=False
            )
            return driver.CommandResult(
                command, completed.returncode, completed.stdout, completed.stderr
            )
        return _result(driver, command)

    monkeypatch.setattr(driver, "_run", fake_run)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    driver._manager_github_probe("owner/repo", "b" * 40, evidence, source_repo=work)
    ls_remote = [c for c in seen if "ls-remote" in c]
    assert len(ls_remote) == 2 and all("--refs" in c for c in ls_remote)
    payload = json.loads((evidence / "manager-github-auth.json").read_text())
    assert payload["remote_refs_unchanged"] is True


def test_manager_github_probe_does_not_emit_credential_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    home = Path("/installed")
    monkeypatch.setattr(
        driver.pwd,
        "getpwnam",
        lambda _account: SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid()),
    )
    monkeypatch.setattr(
        driver, "_require_installed_manager_gitconfig", lambda path: None
    )

    def fake_run(argv, **_kwargs):
        if "config" in argv:
            return _result(
                driver, argv, stdout=_installed_helper_rows("/installed/.gitconfig")
            )
        if tuple(argv[:2]) == ("/usr/bin/python3", "-c"):
            return _result(driver, argv, stdout="username=manager\npassword=SECRET\n")
        return _result(driver, argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    with pytest.raises(driver.QualificationFailure, match="credential probe"):
        driver._manager_github_probe(
            "owner/repo", "b" * 40, tmp_path, source_repo=tmp_path
        )
    assert not (tmp_path / "manager-github-auth.json").exists()


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_json(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _canonical(value)
    path.write_bytes(content)
    path.chmod(0o600)
    return hashlib.sha256(content).hexdigest()


def _dispatch_fixture(tmp_path: Path, driver):
    coordinator = tmp_path / "coordinator"
    repo = tmp_path / "repo"
    repo.mkdir()
    candidate = "a" * 40
    probe_candidate = "9" * 40
    run_id = "run-qualification"
    work_id = "qualification-work"
    repository = "owner/repo"
    issue = 42
    phases = ("claim", "define", "plan", "build", "verify", "review", "ship")
    personas = {
        "claim": "manager", "define": "planner", "plan": "planner", "build": "builder",
        "verify": "reviewer", "review": "reviewer", "ship": "manager",
    }
    steps = [
        {
            "phase": phase,
            "card": "worktree-isolation" if phase == "build" else f"{phase}-card",
            "persona": personas[phase],
            "gate_result": "passed",
        }
        for phase in phases
    ]
    jobs = []
    artifacts: list[Path] = []
    workflow_evidence: dict[str, tuple[Path, str]] = {}
    probe_codex_home: Path | None = None
    for phase in ("plan", "build", "verify", "review", "ship"):
        job_id = f"{phase}-job"
        worktree = tmp_path / "reclaimed" / job_id
        card = "worktree-isolation" if phase == "build" else f"{phase}-card"
        job = {
            "job_id": job_id,
            "status": "exited",
            "exit_code": 0,
            "workflow_run_id": run_id,
            "workflow_claim_key": "claim:v1:" + "1" * 64,
            "workflow_repo": repository,
            "workflow_card": card,
            "workflow_phase": phase,
            "workflow_repo_root": str(worktree) if phase == "build" else str(repo),
            "workflow_inputs": [],
            "workflow_outputs": [],
            "workflow_output_baseline": [],
            "source_revision": "2" * 64,
            "subject_head": (
                probe_candidate if phase == "build" else candidate
            ) if phase != "plan" else None,
            "worktree": str(worktree),
            "template_instance": job_id,
        }
        if phase == "build":
            probe_codex_home = (
                tmp_path / "runtime" / "codex-home" / "builder" / job_id
            )
            probe_codex_home.mkdir(parents=True)
            log = (
                coordinator
                / "commit-spool"
                / "build-logs"
                / job_id
                / "job.jsonl"
            )
            log.parent.mkdir(parents=True)
            log.write_text(
                json.dumps(
                    {
                        "type": "thread.started",
                        "thread_id": "thread-build-job",
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": (
                                "/usr/bin/bash -lc '/usr/bin/git rev-parse HEAD'"
                            ),
                            "aggregated_output": f"{probe_candidate}\n",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            log.chmod(0o620)
            job.update(
                {
                    "persona": "builder",
                    "executor": "codex",
                    "model_id": PROVIDER_MODELS["codex"],
                    "runtime_principal": "builder",
                    "runtime_mode": "systemd-template",
                    "runtime_surface": "builder-codex-home",
                    "credential_publish": True,
                    "log_path": str(log),
                }
            )
            artifacts.append(log)
            spec_path = coordinator / "job-specs" / "builder" / f"{job_id}.json"
            prompt = driver._expected_worktree_isolation_prompt(
                job,
                {
                    "run_id": run_id,
                    "work_id": work_id,
                    "repo": repository,
                    "openspec_refs": [],
                },
            )
            last_message = log.with_name("job.last.json")
            # Manager 在 Trust Root 模板 unit 內實際發出的 argv（#716 之後）。
            from paulsha_cortex.coordinator.launcher import build_codex_argv

            codex_argv = build_codex_argv(
                prompt=prompt,
                slice_id=job_id,
                log_dir=str(log.parent),
                worktree=str(worktree),
                model=PROVIDER_MODELS["codex"],
                write_forbidden=True,
                trust_root_outer_unit=True,
                last_message_path=str(last_message),
            )
            bundle_part = coordinator / "commit-spool" / job_id / "commits.bundle.part"
            bundle_final = coordinator / "commit-spool" / job_id / "commits.bundle"
            script = "; ".join(
                (
                    driver.shlex.join(codex_argv),
                    "__psc_rc=$?",
                    (
                        f"git -C {driver.shlex.quote(str(worktree))} bundle create "
                        f"{driver.shlex.quote(str(bundle_part))} "
                        f'"$(git -C {driver.shlex.quote(str(worktree))} symbolic-ref HEAD)" '
                        "^refs/cortex/base "
                        f"&& chmod 0644 {driver.shlex.quote(str(bundle_part))} "
                        f"&& mv -f {driver.shlex.quote(str(bundle_part))} "
                        f"{driver.shlex.quote(str(bundle_final))}"
                    ),
                    (
                        f"{{ [ -f {driver.shlex.quote(str(last_message))} ] && chmod 0644 "
                        f"{driver.shlex.quote(str(last_message))}; }} 2>/dev/null || :"
                    ),
                    'exit "$__psc_rc"',
                )
            )
            _write_json(
                spec_path,
                {
                    "spec_version": 1,
                    "instance": job_id,
                    "job_id": job_id,
                    "unit": f"cortex-job-ro-jit@{job_id}.service",
                    "command": [
                        "bash",
                        "-c",
                        script,
                    ],
                    "working_directory": str(worktree),
                    "log_path": str(log),
                    "env": {
                        "CODEX_HOME": str(probe_codex_home),
                        "PATH": driver.DEPLOYMENT_CANARY_BUILDER_PATH,
                        "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": "safe.directory",
                        "GIT_CONFIG_VALUE_0": str(worktree),
                    },
                },
            )
            spec_path.chmod(0o640)
            artifacts.append(spec_path)
        envelope = {
            "schema_version": 1,
            "kind": phase,
            "job": {
                "job_id": job_id,
                "run_id": run_id,
                "claim_key": job["workflow_claim_key"],
                "repo": repository,
                "source_revision": job["source_revision"],
                "card_id": job["workflow_card"],
                "phase": phase,
                "inputs": [],
                "outputs": [],
                "output_baseline": [],
            },
            "payload": {
                "schema_version": 1,
                "kind": (
                    "workflow-review-result" if phase == "review" else "workflow-card"
                ),
                "status": "passed",
                "run_id": run_id,
                "card_id": job["workflow_card"],
                "candidate": (
                    probe_candidate if phase == "build" else candidate
                ),
                "outputs": [],
                **(
                    {
                        "state": "passed",
                        "builder_job_id": "build-job",
                        "reviewer_job_id": job_id,
                    }
                    if phase == "review"
                    else {}
                ),
            },
            "artifacts": [],
        }
        evidence_path = coordinator / "evidence" / "workflow" / f"{job_id}.json"
        evidence_hash = _write_json(evidence_path, envelope)
        workflow_evidence[phase] = (evidence_path, evidence_hash)
        artifacts.append(evidence_path)
        job["workflow_evidence"] = {
            "kind": phase,
            "path": evidence_path.relative_to(coordinator).as_posix(),
            "hash": evidence_hash,
        }
        if phase != "ship":
            control = coordinator / "control" / f"{job_id}.log"
            job["control_log_path"] = str(control)
            ledger = control.with_name(f"{control.stem}.gates.json")
            _write_json(
                ledger,
                {
                    "schema_version": 1,
                    "kind": "workflow-gate-ledger",
                    "slice_id": job_id,
                    # #1096：closeout 現在逐項驗證部署層宣告的 gate（pytest）存在且
                    # 為 terminal passed，正向 fixture 因此不能再用空 `gates: []`。
                    "gates": [
                        {"name": "pytest", "status": "passed", "exit_code": 0}
                    ],
                },
            )
            artifacts.append(ledger)
        jobs.append(job)

    bundle = coordinator / "commit-spool" / "build-job" / "commits.bundle"
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b"real git bundle placeholder")
    bundle.parent.chmod(0o500)
    artifacts.append(bundle)

    completion = {
        "schema_version": 1,
        "slice_id": work_id,
        "candidate": candidate,
        "work_authority": {
            "repo": repository,
            "work_id": work_id,
            "run_id": run_id,
            "mapped_issues": [issue],
            "merge_commit": "b" * 40,
        },
    }
    completion_path = coordinator / "evidence" / "completion" / "completion.json"
    _write_json(completion_path, completion)
    completion_hash = hashlib.sha256(
        json.dumps(completion, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    artifacts.append(completion_path)
    brainstorm_path = (
        coordinator / "evidence" / "planning" / f"brainstorm-{run_id}.json"
    )
    brainstorm_hash = _write_json(
        brainstorm_path,
        {
            "schema_version": 1,
            "kind": "brainstorm-peer",
            "run_id": run_id,
            "work_id": work_id,
        },
    )
    artifacts.append(brainstorm_path)
    review_path, review_hash = workflow_evidence["review"]
    copilot_path = coordinator / "evidence" / "delivery-adapter" / "copilot.json"
    copilot_hash = _write_json(
        copilot_path,
        {
            "schema_version": 1,
            "kind": "copilot",
            "run_id": run_id,
            "work_id": work_id,
            "candidate": candidate,
            "status": "passed",
        },
    )
    artifacts.append(copilot_path)
    gate_refs = [
        {
            "kind": "brainstorm",
            "ref": str(brainstorm_path),
            "sha256": brainstorm_hash,
        },
        {
            "kind": "foreign-review",
            "ref": str(review_path),
            "sha256": review_hash,
        },
        {
            "kind": "copilot",
            "ref": str(copilot_path),
            "sha256": copilot_hash,
        },
    ]
    workflow = {
        "run_id": run_id,
        "work_id": work_id,
        "repo": repository,
        "workspace_root": str(repo),
        "current_phase": "ship",
        "steps": steps,
        "issue_refs": [f"{repository}#{issue}"],
        "evidence_refs": [
            str((coordinator / "evidence" / "workflow" / f"{phase}-job.json"))
            for phase in ("plan", "build", "verify", "review", "ship")
        ],
        "gate_refs": gate_refs,
        "candidate_head": candidate,
        "verified_head": candidate,
        "facets": [],
        "gate_status": "passed",
        "status": "done",
        "completion_record_path": str(completion_path),
        "completion_record_hash": completion_hash,
        "completion_record_revision": candidate,
        "completion_source_revisions": {"openspec:qualification": "rev"},
        "pr_candidate": candidate,
        "merge_revision": "b" * 40,
        "model_chain_override": {
            "builder": {
                "executor": "codex",
                "model_id": PROVIDER_MODELS["codex"],
            }
        },
        "resolved_model_chain": {
            "builder": {
                "executor": "codex",
                "model_id": PROVIDER_MODELS["codex"],
                "independence_domain": "openai",
                "source": "run-override",
                "envelope_source": "default",
            }
        },
    }
    registry = coordinator / "jobs.json"
    _write_json(
        registry,
        {
            "schema_version": 2,
            "seq": 9,
            "jobs": jobs,
            "slices": [],
            "workflows": [workflow],
            "legacy_records": {},
            "reclaim_resets": [],
        },
    )
    artifacts.append(registry)
    driver._codex_thread_runtime_identity = lambda thread_id, **_kwargs: {
        "runtime_model": PROVIDER_MODELS["codex"],
        "runtime_effort": PROVIDER_EFFORTS["codex"],
        "model_provider": "openai",
        "thread_sha256": hashlib.sha256(thread_id.encode()).hexdigest(),
    }
    return {
        "coordinator": coordinator,
        "repo": repo,
        "candidate": candidate,
        "probe_candidate": probe_candidate,
        "run_id": run_id,
        "work_id": work_id,
        "repository": repository,
        "issue": issue,
        "registry": registry,
        "artifacts": artifacts,
        "codex_home": probe_codex_home,
    }


def _dispatch_fixture_fake_run(driver, fixture):
    """#1096：多個 closeout 綁定測試共用同一套 `_run` 假身分，只認得
    `_dispatch_fixture` 會真的呼叫到的四種 git 子命令。"""

    def fake_run(argv, **_kwargs):
        if "cat-file" in argv or ("bundle" in argv and "verify" in argv):
            return _result(driver, argv)
        if "worktree" in argv:
            return _result(driver, argv, stdout=f"worktree {fixture['repo']}\n")
        if "bundle" in argv and "list-heads" in argv:
            return _result(
                driver, argv, stdout=f"{fixture['probe_candidate']} refs/heads/work\n"
            )
        raise AssertionError(argv)

    return fake_run


def _validate_fixture_closeout(driver, fixture):
    return driver._validate_dispatch_closeout(
        repository=fixture["repository"],
        work_id=fixture["work_id"],
        issue=fixture["issue"],
        terminal={
            "status": "done",
            "run_id": fixture["run_id"],
            "work_id": fixture["work_id"],
        },
        coordinator_root=fixture["coordinator"],
    )


def _drop_fixture_jobs(fixture, *phases: str) -> None:
    registry = json.loads(fixture["registry"].read_text(encoding="utf-8"))
    registry["jobs"] = [
        job for job in registry["jobs"] if job.get("workflow_phase") not in phases
    ]
    _write_json(fixture["registry"], registry)


def test_dispatch_closeout_requires_jobs_only_for_builder_and_reviewer_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#716：plan（writing-plans-light）與 ship（archive／policy-commit）由 Manager
    執行、define 的 planner 走 planning runtime，都不產生 registry job。真實 canary
    因此永遠湊不齊 plan／ship job——只有 builder／reviewer 步驟的 phase 必須有 job。"""
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    _drop_fixture_jobs(fixture, "plan", "ship")
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_run", _dispatch_fixture_fake_run(driver, fixture))

    _markers, _rows, workflow, _probe = _validate_fixture_closeout(driver, fixture)
    assert workflow["run_id"] == fixture["run_id"]


def test_dispatch_closeout_names_missing_job_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    _drop_fixture_jobs(fixture, "review")
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_run", _dispatch_fixture_fake_run(driver, fixture))

    with pytest.raises(driver.QualificationFailure, match="phase chain is incomplete: missing=review"):
        _validate_fixture_closeout(driver, fixture)


def test_dispatch_closeout_rejects_forged_marker_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    forged = tmp_path / "forged.txt"
    forged.write_text(
        "qualification-work candidate bundle verdict ledger evidence completion",
        encoding="utf-8",
    )
    coordinator = tmp_path / "coordinator"
    coordinator.mkdir()
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    with pytest.raises(driver.QualificationFailure, match="registry"):
        driver._validate_dispatch_closeout(
            repository="owner/repo",
            work_id="qualification-work",
            issue=42,
            terminal={"status": "done"},
            coordinator_root=coordinator,
        )


def test_dispatch_closeout_binds_structured_authority_hashes_and_reclaim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    registry = json.loads(fixture["registry"].read_text(encoding="utf-8"))
    gate_refs = {row["kind"]: row for row in registry["workflows"][0]["gate_refs"]}
    assert Path(gate_refs["brainstorm"]["ref"]).is_absolute()
    assert Path(gate_refs["brainstorm"]["ref"]).parent.name == "planning"
    assert gate_refs["foreign-review"]["ref"] == str(
        fixture["coordinator"] / "evidence" / "workflow" / "review-job.json"
    )
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())

    def fake_run(argv, **_kwargs):
        if "cat-file" in argv:
            return _result(driver, argv)
        if "worktree" in argv:
            return _result(driver, argv, stdout=f"worktree {fixture['repo']}\n")
        if "bundle" in argv and "verify" in argv:
            return _result(driver, argv)
        if "bundle" in argv and "list-heads" in argv:
            return _result(
                driver, argv, stdout=f"{fixture['probe_candidate']} refs/heads/work\n"
            )
        raise AssertionError(argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    markers, rows, workflow, agent_loop_probe = driver._validate_dispatch_closeout(
        repository=fixture["repository"],
        work_id=fixture["work_id"],
        issue=fixture["issue"],
        terminal={
            "status": "done",
            "run_id": fixture["run_id"],
            "work_id": fixture["work_id"],
        },
        coordinator_root=fixture["coordinator"],
    )
    assert set(markers) == {
        "agent-loop-command",
        "candidate",
        "bundle",
        "verdict",
        "ledger",
        "evidence",
        "completion",
    }
    assert workflow["run_id"] == fixture["run_id"]
    assert agent_loop_probe == {
        "schema_version": 1,
        "executor": "codex",
        "model_id": PROVIDER_MODELS["codex"],
        "card_id": "worktree-isolation",
        "builder_job_ids": ["build-job"],
        "successful_command_count": 1,
        "all_outputs_nonempty": True,
        "command_sha256": agent_loop_probe["command_sha256"],
        "output_sha256": agent_loop_probe["output_sha256"],
        "log_sha256": agent_loop_probe["log_sha256"],
        "thread_sha256": agent_loop_probe["thread_sha256"],
        "runtime_model": PROVIDER_MODELS["codex"],
        "runtime_effort": PROVIDER_EFFORTS["codex"],
        "model_provider": "openai",
        "probe_candidate_sha": fixture["probe_candidate"],
    }
    assert all(
        len(agent_loop_probe[field]) == 64
        for field in (
            "command_sha256",
            "output_sha256",
            "log_sha256",
            "thread_sha256",
        )
    )
    assert rows
    for row in rows:
        path = fixture["coordinator"].parent / row["path"]
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]


def test_dispatch_closeout_records_only_the_validated_registry_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    validated_sha = hashlib.sha256(fixture["registry"].read_bytes()).hexdigest()
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    mutated = False

    def fake_run(argv, **_kwargs):
        nonlocal mutated
        if "cat-file" in argv or ("bundle" in argv and "verify" in argv):
            return _result(driver, argv)
        if "worktree" in argv:
            payload = json.loads(fixture["registry"].read_text())
            payload["workflows"][0]["status"] = "ongoing"
            _write_json(fixture["registry"], payload)
            mutated = True
            return _result(driver, argv, stdout=f"worktree {fixture['repo']}\n")
        if "bundle" in argv and "list-heads" in argv:
            return _result(
                driver,
                argv,
                stdout=f"{fixture['probe_candidate']} refs/heads/work\n",
            )
        raise AssertionError(argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    _markers, rows, _workflow, _probe = driver._validate_dispatch_closeout(
        repository=fixture["repository"],
        work_id=fixture["work_id"],
        issue=fixture["issue"],
        terminal={
            "status": "done",
            "run_id": fixture["run_id"],
            "work_id": fixture["work_id"],
        },
        coordinator_root=fixture["coordinator"],
    )

    registry_row = next(
        row for row in rows if row["path"].endswith("coordinator/jobs.json")
    )
    current_sha = hashlib.sha256(fixture["registry"].read_bytes()).hexdigest()
    assert mutated is True
    assert current_sha != validated_sha
    assert registry_row["sha256"] == validated_sha
    assert registry_row["sha256"] != current_sha


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GIT_DIR", "/attacker/decoy.git"),
        ("PATH", "/attacker/bin:/usr/bin:/bin"),
    ],
)
def test_dispatch_closeout_rejects_an_environment_that_redirects_the_head_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
) -> None:
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    spec_path = (
        fixture["coordinator"] / "job-specs" / "builder" / "build-job.json"
    )
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["env"][key] = value
    _write_json(spec_path, spec)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())

    def fake_run(argv, **_kwargs):
        if "cat-file" in argv or ("bundle" in argv and "verify" in argv):
            return _result(driver, argv)
        if "worktree" in argv:
            return _result(driver, argv, stdout=f"worktree {fixture['repo']}\n")
        if "bundle" in argv and "list-heads" in argv:
            return _result(
                driver,
                argv,
                stdout=f"{fixture['probe_candidate']} refs/heads/work\n",
            )
        raise AssertionError(argv)

    monkeypatch.setattr(driver, "_run", fake_run)

    with pytest.raises(driver.QualificationFailure, match="environment|PATH|runtime"):
        driver._validate_dispatch_closeout(
            repository=fixture["repository"],
            work_id=fixture["work_id"],
            issue=fixture["issue"],
            terminal={
                "status": "done",
                "run_id": fixture["run_id"],
                "work_id": fixture["work_id"],
            },
            coordinator_root=fixture["coordinator"],
        )


def test_dispatch_closeout_rejects_a_bundle_changed_during_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    bundle = (
        fixture["coordinator"]
        / "commit-spool"
        / "build-job"
        / "commits.bundle"
    )
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())

    def fake_run(argv, **_kwargs):
        if "cat-file" in argv or ("bundle" in argv and "verify" in argv):
            return _result(driver, argv)
        if "worktree" in argv:
            return _result(driver, argv, stdout=f"worktree {fixture['repo']}\n")
        if "bundle" in argv and "list-heads" in argv:
            bundle.write_bytes(b"mutated after list-heads")
            return _result(
                driver,
                argv,
                stdout=f"{fixture['probe_candidate']} refs/heads/work\n",
            )
        raise AssertionError(argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    with pytest.raises(driver.QualificationFailure, match="changed while"):
        driver._validate_dispatch_closeout(
            repository=fixture["repository"],
            work_id=fixture["work_id"],
            issue=fixture["issue"],
            terminal={
                "status": "done",
                "run_id": fixture["run_id"],
                "work_id": fixture["work_id"],
            },
            coordinator_root=fixture["coordinator"],
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-override",
        "resolved-executor",
        "job-model",
        "runtime-mode",
        "log-path",
        "no-command",
        "wrong-card",
        "spec-command",
        "spec-short-model",
        "spec-short-sandbox",
        "spec-approve-for-me",
        "spec-second-codex",
        "spec-forged-suffix",
        "spec-prescribed-prompt",
        "spec-missing-codex-home",
        "spec-wrong-codex-home",
        "echo-only",
        "wrong-head-output",
    ],
)
def test_dispatch_closeout_requires_exact_codex_agent_loop_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    payload = json.loads(fixture["registry"].read_text())
    workflow = payload["workflows"][0]
    build_job = next(
        row for row in payload["jobs"] if row["workflow_phase"] == "build"
    )
    if mutation == "missing-override":
        workflow["model_chain_override"] = None
    elif mutation == "resolved-executor":
        workflow["resolved_model_chain"]["builder"]["executor"] = "copilot"
    elif mutation == "job-model":
        build_job["model_id"] = "different-model"
    elif mutation == "runtime-mode":
        build_job["runtime_mode"] = "direct"
    elif mutation == "log-path":
        build_job["log_path"] = str(tmp_path / "forged.jsonl")
    elif mutation == "wrong-card":
        build_job["workflow_card"] = "tdd-red"
        locator = build_job["workflow_evidence"]
        evidence_path = fixture["coordinator"] / locator["path"]
        envelope = json.loads(evidence_path.read_text())
        envelope["job"]["card_id"] = "tdd-red"
        envelope["payload"]["card_id"] = "tdd-red"
        locator["hash"] = _write_json(evidence_path, envelope)
    elif mutation.startswith("spec-"):
        spec_path = (
            fixture["coordinator"]
            / "job-specs"
            / "builder"
            / "build-job.json"
        )
        spec = json.loads(spec_path.read_text())
        if mutation == "spec-command":
            spec["command"][2] = spec["command"][2].replace(
                "codex exec", "copilot -p", 1
            )
        elif mutation == "spec-short-model":
            spec["command"][2] = spec["command"][2].replace(
                f"--model {PROVIDER_MODELS['codex']}",
                f"--model {PROVIDER_MODELS['codex']} -m {PROVIDER_MODELS['copilot']}",
                1,
            )
        elif mutation == "spec-short-sandbox":
            spec["command"][2] = spec["command"][2].replace(
                "--sandbox danger-full-access",
                "--sandbox danger-full-access -s read-only",
                1,
            )
        elif mutation == "spec-approve-for-me":
            spec["command"][2] = spec["command"][2].replace(
                "--json", "--json --approve-for-me", 1
            )
        elif mutation == "spec-second-codex":
            spec["command"][2] += "; codex exec forged --json"
        elif mutation == "spec-missing-codex-home":
            spec["env"].pop("CODEX_HOME")
        elif mutation == "spec-wrong-codex-home":
            spec["env"]["CODEX_HOME"] = str(tmp_path / "forged-codex-home")
        elif mutation == "spec-prescribed-prompt":
            spec["command"][2] = spec["command"][2].replace(
                "this prompt prescribes no repository command or command text",
                "run /usr/bin/git rev-parse HEAD as the prescribed repository command",
                1,
            )
        else:
            spec["command"][2] = spec["command"][2].replace(
                '; exit "$__psc_rc"',
                '; printf \'%s\\n\' \'{"type":"item.completed","item":{"type":"command_execution","command":"git rev-parse HEAD","aggregated_output":"forged","exit_code":0,"status":"completed"}}\'; exit 0',
                1,
            )
        _write_json(spec_path, spec)
        spec_path.chmod(0o640)
    elif mutation in {"echo-only", "wrong-head-output"}:
        Path(build_job["log_path"]).write_text(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": (
                            "printf harmless"
                            if mutation == "echo-only"
                            else "/usr/bin/git rev-parse HEAD"
                        ),
                        "aggregated_output": (
                            "harmless\n"
                            if mutation == "echo-only"
                            else f"{'f' * 40}\n"
                        ),
                        "exit_code": 0,
                        "status": "completed",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        Path(build_job["log_path"]).write_text(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": (
                            "fake command_execution exit_code=0 "
                            "aggregated_output=not-real"
                        ),
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
    _write_json(fixture["registry"], payload)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())

    def fake_run(argv, **_kwargs):
        if "cat-file" in argv or ("bundle" in argv and "verify" in argv):
            return _result(driver, argv)
        if "worktree" in argv:
            return _result(driver, argv, stdout=f"worktree {fixture['repo']}\n")
        if "bundle" in argv and "list-heads" in argv:
            return _result(
                driver, argv, stdout=f"{fixture['probe_candidate']} refs/heads/work\n"
            )
        raise AssertionError(argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    with pytest.raises(driver.QualificationFailure, match="Codex agent-loop"):
        driver._validate_dispatch_closeout(
            repository=fixture["repository"],
            work_id=fixture["work_id"],
            issue=fixture["issue"],
            terminal={
                "status": "done",
                "run_id": fixture["run_id"],
                "work_id": fixture["work_id"],
            },
            coordinator_root=fixture["coordinator"],
        )


@pytest.mark.parametrize(
    "item",
    [
        {
            "type": "command_execution",
            "command": "git rev-parse HEAD",
            "aggregated_output": "head\n",
            "exit_code": False,
            "status": "completed",
        },
        {
            "type": "command_execution",
            "command": "git rev-parse HEAD",
            "aggregated_output": "head\n",
            "exit_code": 1,
            "status": "failed",
        },
        {
            "type": "command_execution",
            "command": "git rev-parse HEAD",
            "aggregated_output": "",
            "exit_code": 0,
            "status": "completed",
        },
        {
            "type": "agent_message",
            "text": "command_execution completed exit_code=0 output=head",
        },
    ],
)
def test_codex_agent_loop_parser_rejects_nonproof_events(item: dict) -> None:
    driver = _load_driver()
    content = (json.dumps({"type": "item.completed", "item": item}) + "\n").encode()

    with pytest.raises(driver.QualificationFailure, match="Codex agent-loop"):
        driver._codex_agent_loop_observation(
            (("build-job", content),),
            expected_head="a" * 40,
            expected_worktree="/var/lib/cortex/worktree/build-job",
        )


@pytest.mark.parametrize(
    "command",
    [
        "./git rev-parse HEAD",
        "/usr/bin/git rev-parse HEAD | printf %s " + "a" * 40,
        "/usr/bin/git rev-parse HEAD || printf %s " + "a" * 40,
        "/bin/bash -lc '/usr/bin/git rev-parse HEAD | printf %s "
        + "a" * 40
        + "'",
        "/bin/bash -lc '/usr/bin/git rev-parse HEAD || printf %s "
        + "a" * 40
        + "'",
    ],
)
def test_codex_agent_loop_parser_rejects_forged_head_command_shapes(
    command: str,
) -> None:
    driver = _load_driver()
    content = (
        json.dumps(
            {"type": "thread.started", "thread_id": "thread-build-job"}
        )
        + "\n"
        + json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": command,
                    "aggregated_output": "a" * 40 + "\n",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        + "\n"
    ).encode()

    with pytest.raises(driver.QualificationFailure, match="Codex agent-loop"):
        driver._codex_agent_loop_observation(
            (("build-job", content),),
            expected_head="a" * 40,
            expected_worktree="/var/lib/cortex/worktree/build-job",
        )


def _work_show_envelope(
    work_id: str,
    *,
    state: str,
    run_id: str = "run-qualification",
    run_status: str = "running",
    facets: tuple[str, ...] = (),
) -> dict[str, object]:
    """`cortex work show --json` 的真實形狀：envelope 外層＋`item`。

    envelope 其他區段（providers、fleet_health）也帶 `status`／`state` 欄位，
    值可能是 closed／done；terminal 判定不得被它們誤導（#716 canary run
    36519844244 在 build 階段約 2 分鐘就被誤判結案）。
    """

    return {
        "schema": "cortex/work-item/v1",
        "degraded": False,
        "providers": [
            {
                "name": "github",
                "status": "ok",
                "sources": [{"source_id": "github_issue:other#9", "status": "closed"}],
            }
        ],
        "fleet_health": {"jobs": [{"job_id": "other-job", "state": "done"}]},
        "item": {
            "work_id": work_id,
            "repo": "owner/repo",
            "state": state,
            "facets": list(facets),
            "workflow_run_id": run_id if run_status not in {"done", "failed", "superseded"} else None,
            "sources": [
                {"kind": "github_issue", "ref": "owner/repo#42", "status": "open"},
                {"kind": "workflow_run", "ref": run_id, "status": run_status},
            ],
        },
    }


def _full_dispatch_fixture(
    driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shows: list[dict[str, object]]
) -> tuple[list[tuple[str, ...]], list[object]]:
    config_root = tmp_path / "config"
    config_root.mkdir()
    (config_root / "model-identities.yaml").write_text(
        render_model_identity_overlay(), encoding="utf-8"
    )
    calls: list[tuple[str, ...]] = []
    closeout_terminals: list[object] = []
    remaining = list(shows)

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        if "show" in argv:
            record = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return _result(driver, argv, stdout=json.dumps(record) + "\n")
        return _result(driver, argv)

    def fake_closeout(**kwargs):
        closeout_terminals.append(kwargs["terminal"])
        return (
            ["agent-loop-command"],
            [{"path": "coordinator/jobs.json", "sha256": "d" * 64}],
            {"run_id": "run-qualification", "candidate_head": "f" * 40},
            {"schema_version": 1},
        )

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(driver.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        driver,
        "_installed_runtime_env",
        lambda: {
            "PSC_COORDINATOR_ROOT": str(tmp_path / "coordinator"),
            "PSC_PROJECT_CONFIG_ROOT": str(config_root),
        },
    )
    monkeypatch.setattr(driver, "_validate_dispatch_closeout", fake_closeout)
    return calls, closeout_terminals


def test_full_dispatch_ignores_done_values_outside_the_work_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """envelope 其他區段的 closed／done 不算結案；只看本 work item（#716）。"""

    driver = _load_driver()
    ongoing = _work_show_envelope("qualification-work", state="on-going")
    finished = _work_show_envelope(
        "qualification-work", state="on-going", run_status="done"
    )
    calls, closeouts = _full_dispatch_fixture(
        driver, tmp_path, monkeypatch, [ongoing, ongoing, finished]
    )

    driver._full_dispatch(
        repository="owner/repo",
        work_id="qualification-work",
        issue=42,
        release_candidate_sha="a" * 40,
        timeout=60,
        evidence_dir=tmp_path / "evidence",
    )

    assert sum(1 for argv in calls if "show" in argv) == 3
    assert closeouts == [finished["item"]]


def test_full_dispatch_times_out_while_the_bound_run_is_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    ongoing = _work_show_envelope("qualification-work", state="on-going")
    _calls, closeouts = _full_dispatch_fixture(driver, tmp_path, monkeypatch, [ongoing])
    clock = iter(float(tick) for tick in range(0, 1000, 5))
    monkeypatch.setattr(driver.time, "monotonic", lambda: next(clock))

    with pytest.raises(
        driver.QualificationFailure,
        match="before timeout: state=on-going runs=run-qualification:running facets=none",
    ):
        driver._full_dispatch(
            repository="owner/repo",
            work_id="qualification-work",
            issue=42,
            release_candidate_sha="a" * 40,
            timeout=30,
            evidence_dir=tmp_path / "evidence",
        )
    assert closeouts == []


def test_full_dispatch_timeout_names_the_stuck_step_jobs_and_daemon_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#716 canary run 36538644074：workflow 在 build 停了兩小時、沒有 needs_human，
    逾時訊息只看得到 item 狀態。逾時時另從 Manager registry 列出各步驟與該 run
    的 job 狀態，並從 `inspect status` 帶出 daemon tick 健康度（只輸出列舉 token）。"""

    driver = _load_driver()
    ongoing = _work_show_envelope("qualification-work", state="on-going")
    calls, closeouts = _full_dispatch_fixture(driver, tmp_path, monkeypatch, [ongoing])
    coordinator = tmp_path / "coordinator"
    coordinator.mkdir()
    (coordinator / "jobs.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "jobs": [
                    {
                        "job_id": "job-1",
                        "workflow_run_id": "run-qualification",
                        "workflow_phase": "build",
                        "workflow_card": "worktree-isolation",
                        "status": "exited",
                        "exit_code": 0,
                        "executor": "codex",
                    },
                    {
                        "job_id": "job-2",
                        "workflow_run_id": "run-qualification",
                        "workflow_phase": "build",
                        "workflow_card": "tdd-red",
                        "status": "running",
                        "exit_code": None,
                        "executor": "codex",
                    },
                    {"job_id": "other", "workflow_run_id": "run-other", "status": "running"},
                ],
                "workflows": [
                    {
                        "run_id": "run-qualification",
                        "work_id": "qualification-work",
                        "repo": "owner/repo",
                        "current_phase": "build",
                        "status": "ongoing",
                        "gate_status": "running",
                        "facets": [],
                        "steps": [
                            {"phase": "build", "card": "worktree-isolation", "gate_result": "passed"},
                            {"phase": "build", "card": "tdd-red", "gate_result": "pending"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    status = {
        "daemon": {
            "consecutive_tick_failures": 3,
            "tick_circuit_open": True,
            "last_tick_error": "Traceback: secret detail that must not leak",
            "last_tick_at": "2026-09-29T09:50:00Z",
        },
        "in_flight": [{"job_id": "job-2"}],
        "workflow_waits": [
            {"run_id": "run-qualification", "work_id": "qualification-work", "phase": "build", "reason": "provider-rate-limited"},
            {"run_id": "run-other", "work_id": "other-work", "phase": "verify", "reason": "not-dispatchable"},
        ],
    }
    real_run = driver._run

    def run_with_status(argv, **kwargs):
        if "inspect" in argv:
            calls.append(tuple(argv))
            return _result(driver, argv, stdout=json.dumps(status) + "\n")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(driver, "_run", run_with_status)
    clock = iter(float(tick) for tick in range(0, 1000, 5))
    monkeypatch.setattr(driver.time, "monotonic", lambda: next(clock))

    with pytest.raises(driver.QualificationFailure) as failure:
        driver._full_dispatch(
            repository="owner/repo",
            work_id="qualification-work",
            issue=42,
            release_candidate_sha="a" * 40,
            timeout=30,
            evidence_dir=tmp_path / "evidence",
        )

    message = str(failure.value)
    assert "current_phase=build status=ongoing gate_status=running" in message
    assert "steps=build:worktree-isolation=passed;build:tdd-red=pending" in message
    assert "jobs=build:worktree-isolation:exited:0:codex;build:tdd-red:running:none:codex" in message
    assert "run-other" not in message
    assert "consecutive_tick_failures=3 tick_circuit_open=true last_tick_error=present" in message
    assert "in_flight=1" in message
    assert "workflow_wait=build:provider-rate-limited" in message
    assert "not-dispatchable" not in message
    assert "last_tick_at=2026-09-29T09:50:00Z" in message
    assert "secret detail" not in message
    assert closeouts == []


def test_full_dispatch_ignores_a_record_for_another_work_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    foreign = _work_show_envelope("other-work", state="done", run_status="done")
    _calls, closeouts = _full_dispatch_fixture(driver, tmp_path, monkeypatch, [foreign])
    clock = iter(float(tick) for tick in range(0, 1000, 5))
    monkeypatch.setattr(driver.time, "monotonic", lambda: next(clock))

    with pytest.raises(driver.QualificationFailure, match="before timeout: item=unobserved"):
        driver._full_dispatch(
            repository="owner/repo",
            work_id="qualification-work",
            issue=42,
            release_candidate_sha="a" * 40,
            timeout=30,
            evidence_dir=tmp_path / "evidence",
        )
    assert closeouts == []


@pytest.mark.parametrize("run_status", ["failed", "superseded"])
def test_full_dispatch_fails_when_the_bound_run_ends_without_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_status: str
) -> None:
    driver = _load_driver()
    ended = _work_show_envelope(
        "qualification-work", state="todo", run_status=run_status
    )
    _calls, closeouts = _full_dispatch_fixture(driver, tmp_path, monkeypatch, [ended])

    with pytest.raises(driver.QualificationFailure, match="failed/needs_human"):
        driver._full_dispatch(
            repository="owner/repo",
            work_id="qualification-work",
            issue=42,
            release_candidate_sha="a" * 40,
            timeout=60,
            evidence_dir=tmp_path / "evidence",
        )
    assert closeouts == []


def test_full_dispatch_waits_while_any_bound_run_is_still_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """舊 run 已 done、新 run 仍進行中（狀態值不在已知清單內也算進行中）時不得結案。"""

    driver = _load_driver()
    mixed = _work_show_envelope("qualification-work", state="on-going", run_id="run-old", run_status="done")
    mixed["item"]["sources"].append(
        {"kind": "workflow_run", "ref": "run-qualification", "status": "blocked-on-review"}
    )
    _calls, closeouts = _full_dispatch_fixture(driver, tmp_path, monkeypatch, [mixed])
    clock = iter(float(tick) for tick in range(0, 1000, 5))
    monkeypatch.setattr(driver.time, "monotonic", lambda: next(clock))

    with pytest.raises(driver.QualificationFailure, match="before timeout"):
        driver._full_dispatch(
            repository="owner/repo",
            work_id="qualification-work",
            issue=42,
            release_candidate_sha="a" * 40,
            timeout=30,
            evidence_dir=tmp_path / "evidence",
        )
    assert closeouts == []


def test_full_dispatch_rejects_a_done_run_that_is_not_the_closed_out_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    finished = _work_show_envelope(
        "qualification-work", state="on-going", run_id="run-stale", run_status="done"
    )
    _full_dispatch_fixture(driver, tmp_path, monkeypatch, [finished])

    with pytest.raises(driver.QualificationFailure, match="not bound"):
        driver._full_dispatch(
            repository="owner/repo",
            work_id="qualification-work",
            issue=42,
            release_candidate_sha="a" * 40,
            timeout=60,
            evidence_dir=tmp_path / "evidence",
        )


def test_full_dispatch_pins_codex_builder_and_persists_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    calls: list[tuple[str, ...]] = []
    terminal = _work_show_envelope(
        "qualification-work", state="on-going", run_id="run-qualification", run_status="done"
    )

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        if "show" in argv:
            return _result(driver, argv, stdout=json.dumps(terminal) + "\n")
        return _result(driver, argv)

    observation = {
        "schema_version": 1,
        "executor": "codex",
        "model_id": PROVIDER_MODELS["codex"],
        "card_id": "worktree-isolation",
        "builder_job_ids": ["build-job"],
        "successful_command_count": 1,
        "all_outputs_nonempty": True,
        "command_sha256": "a" * 64,
        "output_sha256": "b" * 64,
        "log_sha256": "c" * 64,
        "thread_sha256": "d" * 64,
        "runtime_model": PROVIDER_MODELS["codex"],
        "runtime_effort": PROVIDER_EFFORTS["codex"],
        "model_provider": "openai",
        "probe_candidate_sha": "9" * 40,
    }
    config_root = tmp_path / "config"
    config_root.mkdir()
    (config_root / "model-identities.yaml").write_text(
        render_model_identity_overlay(), encoding="utf-8"
    )
    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(
        driver,
        "_installed_runtime_env",
        lambda: {
            "PSC_COORDINATOR_ROOT": str(tmp_path / "coordinator"),
            "PSC_PROJECT_CONFIG_ROOT": str(config_root),
        },
    )
    monkeypatch.setattr(
        driver,
        "_validate_dispatch_closeout",
        lambda **_kwargs: (
            ["agent-loop-command"],
            [{"path": "coordinator/jobs.json", "sha256": "d" * 64}],
            {
                "run_id": "run-qualification",
                "candidate_head": "f" * 40,
            },
            observation,
        ),
    )

    driver._full_dispatch(
        repository="owner/repo",
        work_id="qualification-work",
        issue=42,
        release_candidate_sha="a" * 40,
        timeout=1,
        evidence_dir=tmp_path / "evidence",
    )

    assert calls[0] == (
        "/opt/cortex/venv/bin/cortex",
        "run",
        "work",
        "intake",
        "qualification-work",
        "--repo",
        "owner/repo",
        "--issue",
        "42",
        "--combo",
        "feature-oneshot",
        "--builder-executor",
        "codex",
        "--builder-model",
        PROVIDER_MODELS["codex"],
        "--wait",
        "--timeout",
        "60",
        "--json",
    )
    evidence = json.loads(
        (tmp_path / "evidence" / "dispatch-closeout.json").read_text()
    )
    assert evidence["agent_loop_probe"] == observation
    assert evidence["release_candidate_sha"] == "a" * 40
    assert evidence["workflow_candidate_sha"] == "f" * 40
    assert evidence["terminal"] == {
        "state": "done",
        "work_id": "qualification-work",
        "run_id": "run-qualification",
    }


def test_full_dispatch_rejects_an_overlong_work_id_before_intake(
    tmp_path: Path,
) -> None:
    driver = _load_driver()

    with pytest.raises(driver.QualificationFailure, match="work identity"):
        driver._full_dispatch(
            repository="owner/repo",
            work_id="a" * 129,
            issue=42,
            release_candidate_sha="a" * 40,
            timeout=1,
            evidence_dir=tmp_path / "evidence",
        )


@pytest.mark.parametrize("mutation", ["wrong-hash", "wrong-work", "live-worktree"])
def test_dispatch_closeout_fails_closed_on_binding_or_reclaim_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    payload = json.loads(fixture["registry"].read_text())
    if mutation == "wrong-hash":
        payload["jobs"][0]["workflow_evidence"]["hash"] = "0" * 64
    elif mutation == "wrong-work":
        payload["workflows"][0]["work_id"] = "another-work"
    else:
        Path(payload["jobs"][1]["worktree"]).mkdir(parents=True)
    _write_json(fixture["registry"], payload)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())

    def fake_run(argv, **_kwargs):
        if "cat-file" in argv or ("bundle" in argv and "verify" in argv):
            return _result(driver, argv)
        if "worktree" in argv:
            return _result(driver, argv, stdout=f"worktree {fixture['repo']}\n")
        if "bundle" in argv and "list-heads" in argv:
            return _result(
                driver, argv, stdout=f"{fixture['probe_candidate']} refs/heads/work\n"
            )
        raise AssertionError(argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    with pytest.raises(driver.QualificationFailure):
        driver._validate_dispatch_closeout(
            repository=fixture["repository"],
            work_id=fixture["work_id"],
            issue=fixture["issue"],
            terminal={
                "status": "done",
                "run_id": fixture["run_id"],
                "work_id": fixture["work_id"],
            },
            coordinator_root=fixture["coordinator"],
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "hash",
        "symlink",
        "symlink-ancestor",
        "absolute-escape",
        "lexical-escape",
        "duplicate",
    ],
)
def test_dispatch_closeout_resolves_every_delivery_gate_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    payload = json.loads(fixture["registry"].read_text())
    refs = payload["workflows"][0]["gate_refs"]
    first = Path(refs[0]["ref"])
    if mutation == "missing":
        first.unlink()
    elif mutation == "hash":
        refs[0]["sha256"] = "0" * 64
    elif mutation == "symlink":
        target = first.with_name("target.json")
        first.rename(target)
        first.symlink_to(target)
    elif mutation == "symlink-ancestor":
        target = first.parent.with_name("planning-target")
        first.parent.rename(target)
        first.parent.symlink_to(target, target_is_directory=True)
    elif mutation == "absolute-escape":
        outside = tmp_path / "outside-gate.json"
        refs[0]["sha256"] = _write_json(outside, {"status": "passed"})
        refs[0]["ref"] = str(outside)
    elif mutation == "lexical-escape":
        outside = fixture["coordinator"] / "outside-gate.json"
        refs[0]["sha256"] = _write_json(outside, {"status": "passed"})
        refs[0]["ref"] = str(
            fixture["coordinator"] / "evidence" / ".." / outside.name
        )
    else:
        refs[1]["ref"] = refs[0]["ref"]
        refs[1]["sha256"] = refs[0]["sha256"]
    _write_json(fixture["registry"], payload)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())

    def fake_run(argv, **_kwargs):
        if "cat-file" in argv or ("bundle" in argv and "verify" in argv):
            return _result(driver, argv)
        if "worktree" in argv:
            return _result(driver, argv, stdout=f"worktree {fixture['repo']}\n")
        if "bundle" in argv and "list-heads" in argv:
            return _result(
                driver, argv, stdout=f"{fixture['probe_candidate']} refs/heads/work\n"
            )
        raise AssertionError(argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    with pytest.raises(driver.QualificationFailure, match="delivery gate"):
        driver._validate_dispatch_closeout(
            repository=fixture["repository"],
            work_id=fixture["work_id"],
            issue=fixture["issue"],
            terminal={
                "status": "done",
                "run_id": fixture["run_id"],
                "work_id": fixture["work_id"],
            },
            coordinator_root=fixture["coordinator"],
        )


# ---------------------------------------------------------------------------
# #1096：canary closeout 綁定 gate ledger 與 delivery gate 到本次派工
# ---------------------------------------------------------------------------


def test_dispatch_closeout_rejects_a_missing_expected_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """部署層宣告的 gate（pytest）完全沒出現在 ledger 時必須擋下——過去只驗
    ledger 外層形狀，連空的 `gates: []` 都會被放行，讓「gate 被跳過」的回歸
    通過 canary。"""

    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    ledger_path = fixture["coordinator"] / "control" / "build-job.gates.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["gates"] = []
    _write_json(ledger_path, ledger)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_run", _dispatch_fixture_fake_run(driver, fixture))
    with pytest.raises(
        driver.QualificationFailure, match="missing an expected passed gate"
    ):
        _validate_fixture_closeout(driver, fixture)


def test_dispatch_closeout_rejects_a_non_passed_expected_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """部署層宣告的 gate 若存在但沒有 passed（例如 pytest 真的跑了但 failed），
    closeout 必須擋下，不能只因為 ledger 外層形狀合法就放行。"""

    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    # #716：Manager 只替 build phase 產生權威 ledger（GATE_LEDGER_REQUIRED_PHASES）。
    ledger_path = fixture["coordinator"] / "control" / "build-job.gates.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["gates"] = [{"name": "pytest", "status": "failed", "exit_code": 1}]
    _write_json(ledger_path, ledger)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_run", _dispatch_fixture_fake_run(driver, fixture))
    with pytest.raises(
        driver.QualificationFailure, match="missing an expected passed gate"
    ):
        _validate_fixture_closeout(driver, fixture)


def test_dispatch_closeout_rejects_a_malformed_gate_ledger_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ledger 的 `gates` 項目必須是帶 `name`／`status` 的物件；過去任何列表內容都
    會通過形狀檢查，缺 `status` 的殘缺列不該被靜默接受成「沒有這個 gate」。"""

    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    ledger_path = fixture["coordinator"] / "control" / "build-job.gates.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["gates"] = [{"name": "pytest"}]
    _write_json(ledger_path, ledger)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_run", _dispatch_fixture_fake_run(driver, fixture))
    with pytest.raises(
        driver.QualificationFailure, match="gate ledger entry is malformed"
    ):
        _validate_fixture_closeout(driver, fixture)


def test_dispatch_closeout_rejects_a_gate_ledger_slice_id_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ledger 的 `slice_id` 過去只驗型別是字串，換成別的 job 的 slice_id 也會通過；
    這裡要求逐字等於它自己 job 的 `job_id`，把 ledger 綁回本次派工的那一個 job。"""

    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    ledger_path = fixture["coordinator"] / "control" / "build-job.gates.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["slice_id"] = "verify-job"
    _write_json(ledger_path, ledger)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_run", _dispatch_fixture_fake_run(driver, fixture))
    with pytest.raises(
        driver.QualificationFailure, match="gate ledger schema is invalid"
    ):
        _validate_fixture_closeout(driver, fixture)


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "another-run"),
        ("work_id", "another-work"),
        ("candidate", "c" * 40),
    ],
)
def test_dispatch_closeout_rejects_delivery_gate_evidence_from_another_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    """copilot delivery gate evidence 若自報的 run_id／work_id／candidate 與本次
    派工不符，closeout 必須拒絕——過去只以 kind／path／hash 採信，他 run 或舊
    candidate 遺留、hash 對得上的合法檔案一樣能滿足 closeout。"""

    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    payload = json.loads(fixture["registry"].read_text())
    refs = payload["workflows"][0]["gate_refs"]
    copilot_ref = next(row for row in refs if row["kind"] == "copilot")
    tampered = json.loads(Path(copilot_ref["ref"]).read_text())
    tampered[field] = value
    copilot_ref["sha256"] = _write_json(Path(copilot_ref["ref"]), tampered)
    _write_json(fixture["registry"], payload)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_run", _dispatch_fixture_fake_run(driver, fixture))
    with pytest.raises(
        driver.QualificationFailure, match="not bound to this dispatch"
    ):
        _validate_fixture_closeout(driver, fixture)


def test_dispatch_closeout_rejects_a_foreign_review_evidence_substitute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`foreign-review` gate_ref 必須逐字指向本 run 已獨立驗過（run_id／repo／
    candidate／reviewer_job_id 皆核對過）的 review job workflow evidence。把它
    換成另一份形狀合法、hash 也對得上自己內容的檔案——模擬他 run／舊 candidate
    遺留的 foreign-review 證據——必須被拒，不能只因為湊得出合法的 path＋hash
    就滿足 closeout。"""

    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    payload = json.loads(fixture["registry"].read_text())
    refs = payload["workflows"][0]["gate_refs"]
    foreign_ref = next(row for row in refs if row["kind"] == "foreign-review")
    original = json.loads(Path(foreign_ref["ref"]).read_text())
    substitute = copy.deepcopy(original)
    substitute["job"]["run_id"] = "another-run"
    substitute_path = (
        fixture["coordinator"] / "evidence" / "workflow" / "foreign-substitute.json"
    )
    substitute_hash = _write_json(substitute_path, substitute)
    foreign_ref["ref"] = str(substitute_path)
    foreign_ref["sha256"] = substitute_hash
    _write_json(fixture["registry"], payload)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_run", _dispatch_fixture_fake_run(driver, fixture))
    with pytest.raises(
        driver.QualificationFailure, match="foreign-review evidence is not bound"
    ):
        _validate_fixture_closeout(driver, fixture)


def test_dispatch_closeout_accepts_the_positive_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1096 的所有新綁定檢查疊加後，既有正向 canary fixture（未經任何 mutation）
    仍必須通過——新檢查不得讓合法的 closeout 誤判為失敗。"""

    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_run", _dispatch_fixture_fake_run(driver, fixture))
    markers, artifact_rows, workflow, agent_loop_probe = _validate_fixture_closeout(
        driver, fixture
    )
    assert workflow["run_id"] == fixture["run_id"]
    assert artifact_rows


# ---------------------------------------------------------------------------
# #716：probe repo 的 intake 前準備（clone／登記／預檢／等 authority）
# ---------------------------------------------------------------------------


def _probe_receipt(tmp_path: Path) -> tuple[dict, Path, Path]:
    state = tmp_path / "state"
    source_root = state / "repos"
    config_root = state / "config" / "paulsha"
    source_root.mkdir(parents=True)
    config_root.mkdir(parents=True)
    receipt = {
        "plan": {
            "roots": {"state": str(state)},
            "source_repositories": ["paulsha-cortex"],
            "assets": [
                {
                    "asset_id": "repo-source-tree",
                    "path": str(source_root),
                    "is_directory": True,
                }
            ],
        }
    }
    return receipt, source_root, config_root


def _patch_probe_registration(
    driver, monkeypatch: pytest.MonkeyPatch, config_root: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(
        driver,
        "_installed_runtime_env",
        lambda: {"PSC_PROJECT_CONFIG_ROOT": str(config_root)},
    )
    monkeypatch.setattr(driver, "_account_runtime_env", lambda _account: {})
    monkeypatch.setattr(
        driver, "_account_env", lambda _account: {"HOME": str(tmp_path / "home")}
    )
    monkeypatch.setattr(driver, "_require_installed_manager_gitconfig", lambda _p: None)
    monkeypatch.setattr(driver.os, "chown", lambda *_args: None)


def test_probe_registration_clones_as_manager_and_writes_exact_project_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from paulsha_cortex.monitor.config import load_config
    from qualification.contract import CANARY_GIT_IDENTITY

    driver = _load_driver()
    receipt, source_root, config_root = _probe_receipt(tmp_path)
    _patch_probe_registration(driver, monkeypatch, config_root, tmp_path)
    checkout = source_root / "probe-repo"
    remote = "https://github.com/owner/probe-repo.git"
    calls: list[tuple[tuple[str, ...], str | None]] = []

    def fake_run(argv, *, user=None, **_kwargs):
        calls.append((tuple(argv), user))
        if "clone" in argv:
            checkout.mkdir()
            return _result(driver, argv)
        if "get-url" in argv:
            return _result(driver, argv, stdout=remote + "\n")
        if "symbolic-ref" in argv:
            return _result(driver, argv, stdout="main\n")
        if argv[0] == "/opt/cortex/venv/bin/python":
            assert (config_root / "project-cortex.yaml").is_file()
            payload = {"workspaces": [str(checkout)], "resolved": str(checkout)}
            return _result(driver, argv, stdout=json.dumps(payload) + "\n")
        return _result(driver, argv)

    monkeypatch.setattr(driver, "_run", fake_run)

    assert driver._register_probe_checkout(
        receipt=receipt, repository="owner/probe-repo"
    ) == checkout

    assert calls[0] == (
        ("/usr/bin/git", "clone", "--quiet", "--", remote, str(checkout)),
        "cortex-manager",
    )
    identity = {
        argv[-2]: argv[-1] for argv, _user in calls if "config" in argv and "--local" in argv
    }
    assert identity == {
        "user.name": CANARY_GIT_IDENTITY["name"],
        "user.email": CANARY_GIT_IDENTITY["email"],
    }
    assert all(user == "cortex-manager" for argv, user in calls if argv[0] != "systemctl")
    assert (("systemctl", "restart", "cortex-monitor.service"), None) in calls
    project_config = config_root / "project-cortex.yaml"
    assert oct(project_config.stat().st_mode & 0o777) == "0o644"
    config = load_config(config_path=project_config)
    assert [(row.name, row.path, row.exact_project) for row in config.workspaces] == [
        ("probe-repo", checkout, True)
    ]


def test_probe_registration_refuses_existing_config_or_installed_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    receipt, _source_root, config_root = _probe_receipt(tmp_path)
    _patch_probe_registration(driver, monkeypatch, config_root, tmp_path)

    def unexpected_run(argv, **_kwargs):
        raise AssertionError(f"must fail before running {argv}")

    monkeypatch.setattr(driver, "_run", unexpected_run)
    with pytest.raises(driver.QualificationFailure, match="checkout name"):
        driver._register_probe_checkout(
            receipt=receipt, repository="owner/paulsha-cortex"
        )
    (config_root / "project-cortex.yaml").write_text("workspaces: []\n", encoding="utf-8")
    with pytest.raises(driver.QualificationFailure, match="already exists"):
        driver._register_probe_checkout(receipt=receipt, repository="owner/probe-repo")


def test_probe_registration_rolls_back_config_when_manager_resolves_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    receipt, source_root, config_root = _probe_receipt(tmp_path)
    _patch_probe_registration(driver, monkeypatch, config_root, tmp_path)
    checkout = source_root / "probe-repo"

    def fake_run(argv, **_kwargs):
        if "clone" in argv:
            checkout.mkdir()
        if "get-url" in argv:
            return _result(driver, argv, stdout="https://github.com/owner/probe-repo.git\n")
        if "symbolic-ref" in argv:
            return _result(driver, argv, stdout="main\n")
        if argv[0] == "/opt/cortex/venv/bin/python":
            payload = {"workspaces": [str(checkout)], "resolved": "/elsewhere"}
            return _result(driver, argv, stdout=json.dumps(payload))
        if argv[0] == "systemctl":
            raise AssertionError("Monitor must not restart on a failed registration")
        return _result(driver, argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    with pytest.raises(driver.QualificationFailure, match="does not resolve"):
        driver._register_probe_checkout(receipt=receipt, repository="owner/probe-repo")
    assert not (config_root / "project-cortex.yaml").exists()


def _probe_github(driver, **overrides):
    labels = driver._production_pr_labels("owner/probe", "probe-work", 7)
    responses = {
        "repos/owner/probe": (
            0,
            {
                "archived": False,
                "default_branch": "main",
                "has_issues": True,
                "allow_merge_commit": True,
                "permissions": {"push": True},
            },
        ),
        "repos/owner/probe/issues/7": (0, {"state": "open"}),
        "repos/owner/probe/rules/branches/main": (0, []),
        "repos/owner/probe/branches/main/protection/required_pull_request_reviews": (
            1,
            None,
        ),
        **{f"repos/owner/probe/labels/{label}": (0, {"name": label}) for label in labels},
    }
    responses.update(overrides)
    return lambda path, **_kwargs: responses[path]


def test_probe_repository_prerequisites_accept_a_mergeable_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    assert driver._production_pr_labels("owner/probe", "probe-work", 7)
    monkeypatch.setattr(driver, "_manager_gh_api", _probe_github(driver))
    driver._probe_repository_prerequisites("owner/probe", "probe-work", 7)


def test_probe_repository_prerequisites_report_every_blocking_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    label = driver._production_pr_labels("owner/probe", "probe-work", 7)[0]
    fake = _probe_github(
        driver,
        **{
            "repos/owner/probe": (
                0,
                {
                    "archived": False,
                    "default_branch": "main",
                    "has_issues": True,
                    "allow_merge_commit": False,
                    "permissions": {"push": True},
                },
            ),
            f"repos/owner/probe/labels/{label}": (1, None),
            "repos/owner/probe/rules/branches/main": (
                0,
                [
                    {
                        "type": "pull_request",
                        "parameters": {"required_approving_review_count": 1},
                    }
                ],
            ),
        },
    )
    monkeypatch.setattr(driver, "_manager_gh_api", fake)
    with pytest.raises(driver.QualificationFailure) as failure:
        driver._probe_repository_prerequisites("owner/probe", "probe-work", 7)
    message = str(failure.value)
    assert "merge commits are not allowed" in message
    assert f"PR label {label!r} does not exist" in message
    assert "ruleset requires approving reviews" in message


def test_probe_runtime_prerequisites_check_gate_pytest_policy_check_and_ctags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qualification.contract import PROBE_GATE_PYTEST_VERSION, WHEELS

    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_env", lambda _account: {})
    calls: list[tuple[tuple[str, ...], str | None]] = []
    versions = {
        "/opt/cortex/venv/bin/python3": WHEELS["policy_check"]["version"],
        "/usr/bin/python3": WHEELS["policy_check"]["version"],
    }

    def fake_run(argv, *, user=None, **_kwargs):
        calls.append((tuple(argv), user))
        if argv[:3] == ("python3", "-m", "pytest"):
            return _result(driver, argv, stdout=f"pytest {PROBE_GATE_PYTEST_VERSION}\n")
        if argv[0] in versions:
            return _result(driver, argv, stdout=versions[argv[0]] + "\n")
        if argv[0] == "ctags":
            return _result(driver, argv, stdout="Universal Ctags 5.9.0\n")
        raise AssertionError(argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    driver._probe_runtime_prerequisites()
    assert (("python3", "-m", "pytest", "--version"), "cortex-gate") in calls
    assert {user for argv, user in calls if argv[0] != "python3"} == {"cortex-manager"}

    versions["/usr/bin/python3"] = "1.0.0"
    with pytest.raises(driver.QualificationFailure, match="/usr/bin/python3"):
        driver._probe_runtime_prerequisites()


def test_probe_policy_version_must_match_the_installed_engine(tmp_path: Path) -> None:
    from qualification.contract import WHEELS

    driver = _load_driver()
    policy = tmp_path / ".project-policy.yml"
    policy.write_text(
        f"policy_version: {WHEELS['policy_check']['version']}\n", encoding="utf-8"
    )
    driver._probe_policy_version(tmp_path)
    policy.write_text("policy_version: 0.0.1\n", encoding="utf-8")
    with pytest.raises(driver.QualificationFailure, match="policy_version"):
        driver._probe_policy_version(tmp_path)


def test_probe_authority_wait_requires_the_linked_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    monkeypatch.setattr(driver, "_account_runtime_env", lambda _account: {})
    monkeypatch.setattr(driver.time, "sleep", lambda _seconds: None)
    states = [
        {"ok": False, "reason": "confirmed work authority missing or ambiguous"},
        {"ok": True, "mapped_issues": [], "mapped_openspec": ["probe"]},
        {"ok": True, "mapped_issues": [7], "mapped_openspec": ["probe"]},
    ]
    calls: list[tuple[tuple[str, ...], str | None]] = []

    def fake_run(argv, *, user=None, **_kwargs):
        calls.append((tuple(argv), user))
        return _result(driver, argv, stdout=json.dumps(states.pop(0)) + "\n")

    monkeypatch.setattr(driver, "_run", fake_run)
    driver._wait_for_probe_authority(
        repository="owner/probe", work_id="probe-work", issue=7, timeout=60
    )
    assert len(calls) == 3
    assert all(argv[-2:] == ("owner/probe", "probe-work") for argv, _ in calls)
    assert {user for _argv, user in calls} == {"cortex-manager"}

    monkeypatch.setattr(
        driver,
        "_run",
        lambda argv, **_kwargs: _result(
            driver, argv, stdout=json.dumps({"ok": True, "mapped_issues": [3]})
        ),
    )
    with pytest.raises(driver.QualificationFailure, match="issue #7 is not linked"):
        driver._wait_for_probe_authority(
            repository="owner/probe", work_id="probe-work", issue=7, timeout=0
        )


def test_prepare_probe_dispatch_runs_prechecks_before_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver()
    order: list[str] = []
    monkeypatch.setattr(
        driver, "_probe_repository_prerequisites", lambda *_a: order.append("remote")
    )
    monkeypatch.setattr(
        driver, "_probe_runtime_prerequisites", lambda: order.append("runtime")
    )

    def register(**_kwargs):
        order.append("register")
        return Path("/checkout")

    monkeypatch.setattr(driver, "_register_probe_checkout", register)
    monkeypatch.setattr(driver, "_probe_policy_version", lambda _c: order.append("policy"))
    monkeypatch.setattr(
        driver, "_wait_for_probe_authority", lambda **_k: order.append("authority")
    )
    driver._prepare_probe_dispatch(
        receipt={}, repository="owner/probe", work_id="probe-work", issue=7
    )
    assert order == ["remote", "runtime", "register", "policy", "authority"]


def test_full_dispatch_reports_the_structured_needs_human_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    config_root = tmp_path / "config"
    config_root.mkdir()
    (config_root / "model-identities.yaml").write_text(
        render_model_identity_overlay(), encoding="utf-8"
    )
    terminal = {
        "item": {"work_id": "probe-work", "state": "on-going", "facets": ["needs_human"]},
        "blocking_reason": {
            "reason": "copilot-current-head-review-missing",
            "detail": "Copilot review was not submitted",
        },
    }

    def fake_run(argv, **_kwargs):
        if "show" in argv:
            return _result(driver, argv, stdout=json.dumps(terminal) + "\n")
        return _result(driver, argv)

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(
        driver,
        "_installed_runtime_env",
        lambda: {"PSC_PROJECT_CONFIG_ROOT": str(config_root)},
    )
    with pytest.raises(
        driver.QualificationFailure, match="copilot-current-head-review-missing"
    ):
        driver._full_dispatch(
            repository="owner/probe",
            work_id="probe-work",
            issue=7,
            release_candidate_sha="a" * 40,
            timeout=30,
            evidence_dir=tmp_path / "evidence",
        )


def test_manager_helper_expectation_matches_the_permgen_generated_gitconfig() -> None:
    """driver 對 installed helper 的預期必須與 permgen 產生的設定同源（#716）。"""

    from paulsha_cortex.trust_root import permgen

    driver = _load_driver()
    assert driver.GITHUB_HTTPS_CREDENTIAL_URL == permgen.GITHUB_HTTPS_CREDENTIAL_URL
    assert driver.MANAGER_GH_CREDENTIAL_HELPER == permgen.durable_owner_git_credential_helper()


def test_dispatch_closeout_failure_names_the_unmet_conditions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#716：canary 只看到 `phase chain or candidate binding is invalid`，看不出停在哪。"""

    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    payload = json.loads(fixture["registry"].read_text(encoding="utf-8"))
    workflow = payload["workflows"][0]
    workflow["status"] = "ongoing"
    workflow["current_phase"] = "build"
    workflow["gate_status"] = "running"
    workflow["facets"] = ["needs_human"]
    workflow["needs_human_reason"] = {"reason": "job-failed", "detail": "x" * 50}
    workflow["steps"] = [
        row for row in workflow["steps"] if row.get("phase") in {"claim", "define", "plan", "build"}
    ]
    workflow["steps"][-1]["gate_result"] = "failed"
    _write_json(fixture["registry"], payload)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    with pytest.raises(driver.QualificationFailure) as caught:
        _validate_fixture_closeout(driver, fixture)
    message = str(caught.value)
    assert "phase chain or candidate binding is invalid" in message
    for fragment in (
        "current_phase=build",
        "status=ongoing",
        "gate_status=running",
        "needs_human=job-failed",
        "missing_phases=verify,review,ship",
        "failed_steps=build:",
    ):
        assert fragment in message
    assert "x" * 50 not in message


def test_dispatch_failure_prints_bounded_scrubbed_job_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#716：派工失敗時把 job unit journal 與 gate.log 尾端印出，遮蔽 credential 形狀。

    journal 以 unit glob 查，不先列 unit：結束的模板 instance 已從 unit 清單卸載
    （run 36634548058 的 list-units 是空的），journal 仍有紀錄。
    """
    driver = _load_driver()
    root = tmp_path / "coordinator"
    log_dir = root / "gate-ledger-spool" / "gate-logs" / "wf-demo"
    log_dir.mkdir(parents=True)
    (log_dir / "gate.log").write_text(
        "".join(f"gate line {index}\n" for index in range(100))
        + "token ghp_" + "a" * 30 + "\n",
        encoding="utf-8",
    )
    queried: list[tuple[str, ...]] = []

    def fake_run(argv, **_kwargs):
        queried.append(tuple(argv))
        assert argv[0] == "/usr/bin/journalctl"
        pattern = argv[argv.index("-u") + 1]
        body = "".join(f"{pattern} journal {index}\n" for index in range(100))
        return _result(
            driver, argv, stdout=body + "Authorization: Bearer " + "b" * 40 + "\n"
        )

    monkeypatch.setattr(driver, "_run", fake_run)
    driver._report_job_unit_diagnostics({"PSC_COORDINATOR_ROOT": str(root)})
    err = capsys.readouterr().err

    assert err.startswith("dispatch job diagnostics:\n")
    assert [argv[argv.index("-u") + 1] for argv in queried] == list(
        driver._JOB_DIAGNOSTIC_UNIT_GLOBS
    )
    assert "cortex-gate-job@*" in driver._JOB_DIAGNOSTIC_UNIT_GLOBS
    # 每個 glob 只取尾端若干行。
    assert "cortex-gate-job@* journal 99" in err
    assert "cortex-gate-job@* journal 0\n" not in err
    assert "--- gate log wf-demo" in err
    assert "gate line 99" in err and "gate line 10\n" not in err
    assert "ghp_" + "a" * 30 not in err
    assert "b" * 40 not in err
    assert "<redacted>" in err
    assert len(err) <= driver._JOB_DIAGNOSTIC_CHARS + 64
    # 每段各自截尾：前面的 journal 再長也擠不掉最後的 gate.log。
    for pattern in driver._JOB_DIAGNOSTIC_UNIT_GLOBS:
        section = err.split(f"--- journal {pattern}\n", 1)[1].split("\n--- ", 1)[0]
        assert len(section) <= driver._JOB_DIAGNOSTIC_SECTION_CHARS


def test_dispatch_failure_prints_recent_workflow_job_log_tails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#716：unit exit 0 卻沒交付 terminal JSON 時，原因只在 job 自己的 log 裡。"""
    import os as _os

    driver = _load_driver()
    root = tmp_path / "coordinator"
    logs = root / "logs" / "workflow"
    logs.mkdir(parents=True)
    for index in range(5):
        log = logs / f"wf-job-{index}.jsonl"
        log.write_text(
            "".join(f"job{index} line {n}\n" for n in range(100))
            + ("sk-" + "c" * 30 + "\n" if index == 4 else ""),
            encoding="utf-8",
        )
        _os.utime(log, (1_000_000 + index, 1_000_000 + index))
    monkeypatch.setattr(
        driver, "_run", lambda argv, **_kwargs: _result(driver, argv, stdout="")
    )

    driver._report_job_unit_diagnostics({"PSC_COORDINATOR_ROOT": str(root)})
    err = capsys.readouterr().err

    for index in (2, 3, 4):
        assert f"--- job log workflow/wf-job-{index}.jsonl" in err
        assert f"job{index} line 99" in err
    assert "wf-job-1.jsonl" not in err
    assert "job4 line 0\n" not in err
    assert "sk-" + "c" * 30 not in err


def test_dispatch_failure_prints_template_job_log_spools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#716：模板 unit 的 job log 在 principal 的 log spool，不在 logs/workflow。"""
    import os as _os

    driver = _load_driver()
    root = tmp_path / "coordinator"
    reviewer = root / "review-verdicts" / "planning-logs" / "wf-verification-4"
    builder = root / "commit-spool" / "build-logs" / "wf-build-3"
    for index, directory in enumerate((builder, reviewer)):
        directory.mkdir(parents=True)
        log = directory / "job.jsonl"
        log.write_text(f"{directory.name} tail\n", encoding="utf-8")
        _os.utime(log, (2_000_000 + index, 2_000_000 + index))
    monkeypatch.setattr(
        driver, "_run", lambda argv, **_kwargs: _result(driver, argv, stdout="")
    )

    driver._report_job_unit_diagnostics({"PSC_COORDINATOR_ROOT": str(root)})
    err = capsys.readouterr().err

    assert "--- job log wf-verification-4/job.jsonl" in err
    assert "wf-verification-4 tail" in err
    assert "--- job log wf-build-3/job.jsonl" in err


def test_job_diagnostics_never_mask_the_dispatch_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = _load_driver()

    def broken(_env):
        raise RuntimeError("boom")

    monkeypatch.setattr(driver, "_job_unit_diagnostics", broken)
    driver._report_job_unit_diagnostics({})
    assert "job diagnostics unavailable: RuntimeError" in capsys.readouterr().err


def _copilot_findings_stop(work_id: str, *, candidate: str = "c" * 40) -> dict[str, object]:
    envelope = _work_show_envelope(work_id, state="on-going", facets=("needs_human",))
    envelope["blocking_reason"] = {
        "reason": "delivery-needs-human",
        "detail": "ship validator 判定交付需要人工介入：copilot-findings",
        "context": {"delivery_reason": "copilot-findings", "candidate": candidate},
    }
    return envelope


def _retry_build_calls(calls):
    return [argv for argv in calls if "retry-build" in argv]


def test_full_dispatch_hands_copilot_findings_to_the_builder_then_closes_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#716：Copilot 即使建議核可，只要有一條 optional finding，ship 就停住；canary
    改走 operator 的正式出口（retry-build）交給 builder 修，而不是直接判失敗。"""

    driver = _load_driver()
    ongoing = _work_show_envelope("qualification-work", state="on-going")
    finished = _work_show_envelope("qualification-work", state="on-going", run_status="done")
    calls, closeouts = _full_dispatch_fixture(
        driver, tmp_path, monkeypatch,
        [ongoing, _copilot_findings_stop("qualification-work"), ongoing, finished],
    )

    driver._full_dispatch(
        repository="owner/repo", work_id="qualification-work", issue=42,
        release_candidate_sha="a" * 40, timeout=600, evidence_dir=tmp_path / "evidence",
    )

    retries = _retry_build_calls(calls)
    assert len(retries) == 1
    argv = retries[0]
    assert argv[argv.index("--expected-candidate") + 1] == "c" * 40
    assert argv[argv.index("--repo") + 1] == "owner/repo"
    assert closeouts == [finished["item"]]


def test_full_dispatch_gives_up_after_the_copilot_fix_round_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    stop = _copilot_findings_stop("qualification-work")
    calls, _closeouts = _full_dispatch_fixture(driver, tmp_path, monkeypatch, [stop])

    with pytest.raises(driver.QualificationFailure, match="copilot-findings"):
        driver._full_dispatch(
            repository="owner/repo", work_id="qualification-work", issue=42,
            release_candidate_sha="a" * 40, timeout=600, evidence_dir=tmp_path / "evidence",
        )
    assert len(_retry_build_calls(calls)) == driver.DEPLOYMENT_CANARY_COPILOT_FIX_ROUNDS


def test_full_dispatch_does_not_retry_other_needs_human_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    stop = _copilot_findings_stop("qualification-work")
    stop["blocking_reason"]["context"]["delivery_reason"] = "candidate-behind-main"
    calls, _closeouts = _full_dispatch_fixture(driver, tmp_path, monkeypatch, [stop])

    with pytest.raises(driver.QualificationFailure, match="failed/needs_human terminal"):
        driver._full_dispatch(
            repository="owner/repo", work_id="qualification-work", issue=42,
            release_candidate_sha="a" * 40, timeout=600, evidence_dir=tmp_path / "evidence",
        )
    assert _retry_build_calls(calls) == []


def test_driver_gate_ledger_phases_mirror_the_manager_contract() -> None:
    """#716：driver 只對 Manager 會產生權威 ledger 的 phase 要求 ledger。"""

    from paulsha_cortex.coordinator import manager

    driver = _load_driver()
    assert driver.GATE_LEDGER_PHASES == manager.GATE_LEDGER_REQUIRED_PHASES


def test_dispatch_closeout_does_not_require_ledgers_for_reviewer_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#716：模板模式下 verify／review job 沒有 gate ledger；closeout 不得因此失敗。"""
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    registry = json.loads(fixture["registry"].read_text(encoding="utf-8"))
    for job in registry["jobs"]:
        if job.get("workflow_phase") in {"verify", "review"}:
            control = Path(job.get("control_log_path") or job["log_path"])
            control.with_name(f"{control.stem}.gates.json").unlink(missing_ok=True)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_run", _dispatch_fixture_fake_run(driver, fixture))

    _markers, _rows, workflow, _probe = _validate_fixture_closeout(driver, fixture)
    assert workflow["run_id"] == fixture["run_id"]


def _prepend_build_job(fixture, *, job_id: str, card: str, gate_status: str) -> None:
    """在既有 build job 之前插入一個同身分的 build job（例如 tdd-red），帶自己的
    evidence 與 Manager ledger——對應真實 canary 的 worktree-isolation → tdd-red →
    subagent-build 順序。"""

    coordinator = fixture["coordinator"]
    registry = json.loads(fixture["registry"].read_text(encoding="utf-8"))
    final = next(job for job in registry["jobs"] if job["workflow_phase"] == "build")
    envelope = json.loads(
        (coordinator / final["workflow_evidence"]["path"]).read_text(encoding="utf-8")
    )
    envelope["job"] = {**envelope["job"], "job_id": job_id, "card_id": card}
    evidence_rel = f"evidence/workflow/{job_id}.json"
    evidence_hash = _write_json(coordinator / evidence_rel, envelope)
    control = coordinator / "control" / f"{job_id}.log"
    control.write_text("", encoding="utf-8")
    _write_json(
        control.with_name(f"{job_id}.gates.json"),
        {
            "gates": [{"exit_code": 1, "name": "pytest", "status": gate_status}],
            "kind": "workflow-gate-ledger",
            "schema_version": 1,
            "slice_id": job_id,
        },
    )
    early = {
        **final,
        "job_id": job_id,
        "workflow_card": card,
        "template_instance": job_id,
        "control_log_path": str(control),
        "workflow_evidence": {"hash": evidence_hash, "kind": "build", "path": evidence_rel},
    }
    index = registry["jobs"].index(final)
    registry["jobs"].insert(index, early)
    _write_json(fixture["registry"], registry)


def test_dispatch_closeout_accepts_a_red_ledger_before_the_final_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#716：tdd-red 的 pytest 依設計必須 failed；只有最後一個 build job（產出交付
    candidate 的那一個）必須讓部署宣告的 gate 全部 passed。"""
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    _prepend_build_job(fixture, job_id="tdd-job", card="tdd-red", gate_status="failed")
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_run", _dispatch_fixture_fake_run(driver, fixture))

    _markers, _rows, workflow, _probe = _validate_fixture_closeout(driver, fixture)
    assert workflow["run_id"] == fixture["run_id"]


def _reviewer_job_on_subject(
    fixture, *, source_job_id: str, job_id: str | None, subject: str
) -> None:
    """讓 reviewer job 綁到指定 subject：`job_id` 給值時複製一份插在原 job 之前
    （archive 前那一輪），否則就地改寫原 job 的 subject 與 evidence。"""

    coordinator = fixture["coordinator"]
    registry = json.loads(fixture["registry"].read_text(encoding="utf-8"))
    source = next(job for job in registry["jobs"] if job["job_id"] == source_job_id)
    envelope = json.loads(
        (coordinator / source["workflow_evidence"]["path"]).read_text(encoding="utf-8")
    )
    target_id = job_id or source_job_id
    envelope["job"] = {**envelope["job"], "job_id": target_id}
    envelope["payload"] = {**envelope["payload"], "candidate": subject}
    if "reviewer_job_id" in envelope["payload"]:
        envelope["payload"]["reviewer_job_id"] = target_id
    evidence_rel = f"evidence/workflow/{target_id}.json"
    evidence_hash = _write_json(coordinator / evidence_rel, envelope)
    updated = {
        **source,
        "job_id": target_id,
        "subject_head": subject,
        "workflow_evidence": {**source["workflow_evidence"], "hash": evidence_hash, "path": evidence_rel},
    }
    index = registry["jobs"].index(source)
    if job_id is None:
        registry["jobs"][index] = updated
    else:
        registry["jobs"].insert(index, updated)
    _write_json(fixture["registry"], registry)


def test_dispatch_closeout_accepts_pre_archive_reviews_of_an_earlier_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#716：ship 的 archive commit 換掉 workflow candidate；archive 前的 verify／
    review 驗的是舊 candidate，各自綁自己的 subject 即可。"""
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    for phase in ("verify", "review"):
        _reviewer_job_on_subject(
            fixture, source_job_id=f"{phase}-job", job_id=f"{phase}-pre-archive",
            subject="e" * 40,
        )
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_run", _dispatch_fixture_fake_run(driver, fixture))

    _markers, _rows, workflow, _probe = _validate_fixture_closeout(driver, fixture)
    assert workflow["run_id"] == fixture["run_id"]


def test_dispatch_closeout_requires_the_final_candidate_to_be_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    _reviewer_job_on_subject(
        fixture, source_job_id="verify-job", job_id=None, subject="e" * 40,
    )
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_run", _dispatch_fixture_fake_run(driver, fixture))

    with pytest.raises(
        driver.QualificationFailure,
        match="final workflow candidate was not verified and reviewed: observed=review",
    ):
        _validate_fixture_closeout(driver, fixture)


def test_dispatch_closeout_requires_the_read_only_template_for_the_probe_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#716：worktree-isolation 是 BUILDER_WRITE_FORBIDDEN，必須跑在 `cortex-job-ro`
    模板；可寫模板代表派工選錯了工作區契約。"""
    driver = _load_driver()
    fixture = _dispatch_fixture(tmp_path, driver)
    spec_path = fixture["coordinator"] / "job-specs" / "builder" / "build-job.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["unit"] = "cortex-job-jit@build-job.service"
    _write_json(spec_path, spec)
    monkeypatch.setattr(driver, "_manager_uid", lambda: os.getuid())
    monkeypatch.setattr(driver, "_run", _dispatch_fixture_fake_run(driver, fixture))

    with pytest.raises(
        driver.QualificationFailure, match="job spec authority mismatch: unit"
    ):
        _validate_fixture_closeout(driver, fixture)
