"""issue #684（#672 票 C）：probe 結果快取——指紋、TTL、fail-closed。

驗收（issue 原文三條）：

1. 連續兩次建構 runtime，第二次的 executor 呼叫次數為 **0**；
2. 改動**任一**指紋輸入必重探；
3. 快取損毀一律視為 **miss**，**不得沿用 `ready`**（fail-closed）。

第三條是本票最重要的不變式：一份壞掉的快取檔絕對不能被解讀成「探過了、是
ready」。因此本檔對「壞」窮舉了七種形態（JSON 壞、payload 不是物件、schema 不符、
items 不是物件、row 不是物件、row 身分欄位被改、row 同時帶 ready 與失敗診斷），
每一種都必須以**重探**收場。
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import job_runner, planning_probe_cache, planning_runtime
from paulsha_cortex.coordinator.model_identities import (
    AGY_MODEL_ID,
    CapabilityProbe,
    IdentityRegistry,
    ModelIdentity,
)
from paulsha_cortex.trust_root import permgen, registry as trust_registry


CACHE_ASSET = "planning-probe-cache"


def _completed(stdout: str = "", returncode: int = 0):
    return type("Completed", (), {"stdout": stdout, "stderr": "", "returncode": returncode})()


def _identity(executor: str = "codex", model_id: str = "primary") -> ModelIdentity:
    return ModelIdentity(
        executor=executor,
        model_id=model_id,
        independence_domain="openai" if executor == "codex" else "google",
        capabilities=("planning",),
    )


def _registry(*identities: ModelIdentity) -> IdentityRegistry:
    rows: list[dict[str, object]] = []
    for identity in identities or (_identity(),):
        row: dict[str, object] = {
            "executor": identity.executor,
            "model_id": identity.model_id,
            "independence_domain": identity.independence_domain,
            "capabilities": list(identity.capabilities),
        }
        if identity.executor == "agy":
            row["live_probe"] = "agy-plan-sandbox"
        rows.append(row)
    return IdentityRegistry.from_rows(rows)


def _echo_runner(calls: list[list[str]]):
    """把 probe prompt 裡的 JSON 原樣回吐——probe 因此一律 ready。"""

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if list(argv) == ["agy", "models"]:
            return _completed(f"{AGY_MODEL_ID}\n")
        prompt = argv[argv.index("--print") + 1] if "--print" in argv else argv[2]
        for marker in (
            "Return only this compact JSON object and perform no tool calls: ",
            "Return only this JSON object and do not call tools: ",
        ):
            if marker in prompt:
                return _completed(prompt.split(marker, 1)[1] + "\n")
        return _completed(json.dumps({"schema_version": 1, "question_pack_id": "qp"}))

    return runner


@pytest.fixture
def hermetic_fingerprint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把三個會被 stat 的指紋來源全部釘進 tmp_path，讓測試可以逐一改動它們。

    - executor 可執行檔：`PATH` 指向 `bin/`（裡面有一支假 `codex`）；
    - 憑證：`HOME` 指向 `home/`（`.codex/auth.json`）；
    - 模板 unit：`job_runner.DEFAULT_TEMPLATE_UNIT_DIR` 指向 `units/`。
    """

    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake = binaries / "codex"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")

    units = tmp_path / "units"
    units.mkdir()
    (units / "cortex-reviewer-job-jit@.service").write_text("[Unit]\n", encoding="utf-8")

    monkeypatch.setenv("PATH", str(binaries))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(job_runner, "DEFAULT_TEMPLATE_UNIT_DIR", str(units))
    return tmp_path


def _fingerprint(identity: ModelIdentity | None = None, *, roster: str = "roster-v1"):
    return planning_probe_cache.compute_fingerprint(identity or _identity(), roster=roster)


# ---------------------------------------------------------------------------
# 驗收 1：連續兩次建構 runtime，第二次零 executor 呼叫
# ---------------------------------------------------------------------------


def test_second_runtime_build_makes_zero_executor_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identities = _registry(_identity(), _identity("agy", AGY_MODEL_ID))
    monkeypatch.setattr(planning_runtime, "load_model_identities", lambda: identities)
    cache = tmp_path / "planning-probe-cache.json"
    calls: list[list[str]] = []

    first = planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"),
        worktree=tmp_path,
        runner=_echo_runner(calls),
        probe_cache_path=cache,
    )
    assert first.probes[("codex", "primary")].ready is True
    assert first.probes[("agy", AGY_MODEL_ID)].ready is True
    assert calls, "第一次建構必須真的探測"
    first_round = len(calls)

    second = planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"),
        worktree=tmp_path,
        runner=_echo_runner(calls),
        probe_cache_path=cache,
    )
    # 驗收第一條：第二次的 executor 呼叫次數為 0。
    assert len(calls) == first_round
    # 而且結果逐欄相同——快取不是「跳過探測」，是「重放上一次的結論」。
    assert second.probes == first.probes


def test_cache_defaults_to_the_manager_owned_coordinator_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """未指定路徑時落在 `<coordinator_root>/planning-probe-cache.json`。"""

    monkeypatch.setenv("PSC_AGENTS_ROOT", str(tmp_path / "agents"))
    monkeypatch.setattr(planning_runtime, "load_model_identities", lambda: _registry())
    planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"), worktree=tmp_path, runner=_echo_runner([])
    )
    expected = tmp_path / "agents" / "coordinator" / planning_probe_cache.CACHE_FILENAME
    assert expected.is_file()
    # Manager-owned 0600：一份新建的快取不該在 permgen 下一次跑之前是可被別人讀的。
    assert stat.S_IMODE(expected.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# 驗收 2：改動任一指紋輸入必重探
# ---------------------------------------------------------------------------


def test_cache_key_includes_job_runner_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hermetic_fingerprint: Path
) -> None:
    """`PSC_JOB_RUNNER` 由 direct 改 systemd-template ⇒ 快取必失效。

    **這條是本票最重要的不變式**：兩種模式的執行環境（PATH／HOME／憑證／seccomp／
    MDWE）完全不同，一個在 direct 下成立的 ready 對 job 模式毫無保證。它同時是
    plan 要求「票 F 的切換必須一次到位」的機械保證。
    """

    monkeypatch.delenv("PSC_JOB_RUNNER", raising=False)
    direct = _fingerprint()
    assert direct.job_runner_mode == job_runner.RUNNER_DIRECT

    monkeypatch.setenv("PSC_JOB_RUNNER", job_runner.RUNNER_SYSTEMD_TEMPLATE)
    downgraded = _fingerprint()
    assert downgraded.job_runner_mode == job_runner.RUNNER_SYSTEMD_TEMPLATE
    assert downgraded.digest != direct.digest

    cache = planning_probe_cache.ProbeCache.open(tmp_path / "cache.json")
    cache.put(_identity(), CapabilityProbe.ready_for("codex", "primary", "openai"), fingerprint=direct)
    assert cache.get(_identity(), fingerprint=direct) is not None
    assert cache.get(_identity(), fingerprint=downgraded) is None


def test_cache_invalidated_by_executor_binary_fingerprint(
    tmp_path: Path, hermetic_fingerprint: Path
) -> None:
    before = _fingerprint()
    assert before.resolved_binary == str(hermetic_fingerprint / "bin" / "codex")

    cache = planning_probe_cache.ProbeCache.open(tmp_path / "cache.json")
    cache.put(_identity(), CapabilityProbe.ready_for("codex", "primary", "openai"), fingerprint=before)
    assert cache.get(_identity(), fingerprint=before) is not None

    binary = hermetic_fingerprint / "bin" / "codex"
    os.utime(binary, ns=(1, 1))
    after = _fingerprint()
    assert after.digest != before.digest
    assert cache.get(_identity(), fingerprint=after) is None


def test_cache_invalidated_by_template_unit_fingerprint(
    tmp_path: Path, hermetic_fingerprint: Path
) -> None:
    """模板 unit 檔改變 ⇒ 失效（＝任何讓 operator 重跑產生器的改動都自動全重探）。

    這一條是 design D-C 的核心：`PROFILE_LOCKED_KEYS`（#677）說明**剖面名相同不代表
    unit 內容相同**。只認剖面名會沿用一個對新 unit 不成立的 ready。
    """

    unit = hermetic_fingerprint / "units" / "cortex-reviewer-job-jit@.service"
    before = _fingerprint()
    assert before.unit == "cortex-reviewer-job-jit@.service"
    # 剖面名這一格**沒有**變（jit 還是 jit）——變的是 unit 檔本身。
    unit.write_text("[Unit]\n[Service]\nReadWritePaths=/new\n", encoding="utf-8")
    after = _fingerprint()
    assert after.hardening_profile == before.hardening_profile == job_runner.HARDENING_PROFILE_JIT
    assert after.template_unit != before.template_unit
    assert after.digest != before.digest

    cache = planning_probe_cache.ProbeCache.open(tmp_path / "cache.json")
    cache.put(_identity(), CapabilityProbe.ready_for("codex", "primary", "openai"), fingerprint=before)
    assert cache.get(_identity(), fingerprint=after) is None


def test_cache_invalidated_by_credential_fingerprint(
    tmp_path: Path, hermetic_fingerprint: Path
) -> None:
    credential = hermetic_fingerprint / "home" / ".codex" / "auth.json"
    before = _fingerprint()
    credential.write_text('{"token": "refreshed"}', encoding="utf-8")
    after = _fingerprint()
    assert after.executor_credential != before.executor_credential
    assert after.digest != before.digest

    cache = planning_probe_cache.ProbeCache.open(tmp_path / "cache.json")
    cache.put(_identity(), CapabilityProbe.ready_for("codex", "primary", "openai"), fingerprint=before)
    assert cache.get(_identity(), fingerprint=after) is None


def test_credential_fingerprint_never_reads_the_file_contents(
    hermetic_fingerprint: Path,
) -> None:
    """憑證只取 `st_size/st_mtime_ns`——token 不得出現在指紋的任何中間狀態。"""

    credential = hermetic_fingerprint / "home" / ".codex" / "auth.json"
    credential.write_text('{"token":"sk-secret-value"}', encoding="utf-8")
    fingerprint = _fingerprint()
    assert "sk-secret-value" not in json.dumps(fingerprint.as_dict())
    assert "size=" in fingerprint.executor_credential
    assert "mtime_ns=" in fingerprint.executor_credential


def test_credential_appearing_from_absent_invalidates(
    tmp_path: Path, hermetic_fingerprint: Path
) -> None:
    """`<absent>` → 有檔案也是一次改動。憑證剛部署好的那一輪必須重探。"""

    credential = hermetic_fingerprint / "home" / ".codex" / "auth.json"
    credential.unlink()
    absent = _fingerprint()
    assert absent.executor_credential.endswith(planning_probe_cache.FINGERPRINT_ABSENT)
    credential.write_text("{}", encoding="utf-8")
    assert _fingerprint().digest != absent.digest


def test_cache_invalidated_by_roster_digest(tmp_path: Path, hermetic_fingerprint: Path) -> None:
    """overlay 改動 ⇒ 失效。摘要取的是**解析結果**，不是 overlay 檔的 mtime。"""

    one = planning_probe_cache.roster_digest(_registry(_identity()))
    two = planning_probe_cache.roster_digest(
        _registry(_identity(), _identity("agy", AGY_MODEL_ID))
    )
    assert one != two
    # 同樣內容重算一次必須相同（canonical，不吃順序／空白）。
    assert one == planning_probe_cache.roster_digest(_registry(_identity()))

    before = _fingerprint(roster=one)
    after = _fingerprint(roster=two)
    assert before.digest != after.digest
    cache = planning_probe_cache.ProbeCache.open(tmp_path / "cache.json")
    cache.put(_identity(), CapabilityProbe.ready_for("codex", "primary", "openai"), fingerprint=before)
    assert cache.get(_identity(), fingerprint=after) is None


def test_every_digest_field_changes_the_digest(hermetic_fingerprint: Path) -> None:
    """六個 digest 欄位**逐欄**釘住：少算任何一格都會讓某類改動靜默沿用舊 ready。

    用機械方式列舉而不是六條手寫測試——新增一個指紋分量時這條自動涵蓋它。
    """

    import dataclasses

    base = _fingerprint()
    assert set(planning_probe_cache.FINGERPRINT_DIGEST_FIELDS) <= {
        f.name for f in dataclasses.fields(base)
    }
    for name in planning_probe_cache.FINGERPRINT_DIGEST_FIELDS:
        mutated = dataclasses.replace(base, **{name: getattr(base, name) + "-changed"})
        assert mutated.digest != base.digest, name
    # 兩個診斷欄位刻意**不**進 digest（它們的內容已包含在對應的 digest 欄位裡）。
    for name in ("resolved_binary", "unit"):
        mutated = dataclasses.replace(base, **{name: "irrelevant"})
        assert mutated.digest == base.digest, name


# ---------------------------------------------------------------------------
# 驗收 3：快取損毀一律視為 miss，不得沿用 ready
# ---------------------------------------------------------------------------


CORRUPTIONS: tuple[tuple[str, str], ...] = (
    ("not-json", "{ this is not json"),
    ("payload-not-object", "[]"),
    ("schema-mismatch", json.dumps({"schema": "cortex-planning-probe-cache/v0", "items": {}})),
    ("items-not-object", json.dumps({"schema": planning_probe_cache.CACHE_SCHEMA, "items": []})),
    ("empty-file", ""),
)


@pytest.mark.parametrize("label,payload", CORRUPTIONS, ids=[c[0] for c in CORRUPTIONS])
def test_corrupt_cache_is_miss_not_ready(
    tmp_path: Path, hermetic_fingerprint: Path, label: str, payload: str
) -> None:
    """壞檔 ⇒ 重探，**絕不**沿用舊 ready。

    「舊 ready」在這裡是刻意先寫進去的：檔案裡確實有一列 ready 的內容，只是外層
    壞掉。fail-open 的實作會「盡量救回能救的」——那正是本條要擋的。
    """

    path = tmp_path / "cache.json"
    path.write_text(payload, encoding="utf-8")
    cache = planning_probe_cache.ProbeCache.open(path)
    assert cache.unreadable is True
    assert cache.get(_identity(), fingerprint=_fingerprint()) is None
    assert cache.entries() == []


def test_corrupt_cache_never_serves_a_ready_row_hidden_inside(
    tmp_path: Path, hermetic_fingerprint: Path
) -> None:
    """壞掉的**外層**裡包著一列格式完全正確的 ready，仍然是 miss。"""

    fingerprint = _fingerprint()
    good = tmp_path / "good.json"
    cache = planning_probe_cache.ProbeCache.open(good)
    cache.put(_identity(), CapabilityProbe.ready_for("codex", "primary", "openai"), fingerprint=fingerprint)
    cache.flush()
    intact = json.loads(good.read_text(encoding="utf-8"))
    assert intact["items"]["codex::primary"]["ready"] is True

    broken = tmp_path / "broken.json"
    # schema 換成別的版本；items 裡那一列逐字不動。
    broken.write_text(
        json.dumps({"schema": "cortex-planning-probe-cache/v9", "items": intact["items"]}),
        encoding="utf-8",
    )
    assert planning_probe_cache.ProbeCache.open(broken).get(
        _identity(), fingerprint=fingerprint
    ) is None


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda row: row.update({"ready": "yes"}), id="ready-not-bool"),
        pytest.param(lambda row: row.update({"executor": "claude"}), id="executor-rewritten"),
        pytest.param(lambda row: row.update({"model_id": "other"}), id="model-rewritten"),
        pytest.param(lambda row: row.update({"independence_domain": "zhipu"}), id="domain-rewritten"),
        pytest.param(lambda row: row.update({"probed_at_epoch": "soon"}), id="timestamp-not-number"),
        pytest.param(lambda row: row.pop("probed_at_epoch"), id="timestamp-missing"),
        pytest.param(lambda row: row.update({"reason": "smoke-failed"}), id="ready-with-failure"),
        pytest.param(lambda row: row.update({"diagnostic": 7}), id="diagnostic-not-string"),
        pytest.param(lambda row: row.update({"fingerprint": "0" * 64}), id="fingerprint-rewritten"),
    ],
)
def test_malformed_ready_row_is_a_miss(
    tmp_path: Path, hermetic_fingerprint: Path, mutate
) -> None:
    """row 層級的窮舉：任何一格不對，那一列的 ready 就不得被端出來。"""

    fingerprint = _fingerprint()
    path = tmp_path / "cache.json"
    cache = planning_probe_cache.ProbeCache.open(path)
    cache.put(_identity(), CapabilityProbe.ready_for("codex", "primary", "openai"), fingerprint=fingerprint)
    cache.flush()

    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload["items"]["codex::primary"])
    path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = planning_probe_cache.ProbeCache.open(path)
    # 外層沒壞，所以不是 `unreadable`——但那一列仍然不得被採信。
    assert reopened.unreadable is False
    assert reopened.get(_identity(), fingerprint=fingerprint) is None


def test_corrupt_cache_logs_a_reason_distinct_from_probe_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """壞檔要落 `planning-probe-cache-unreadable`——與「probe 失敗」分開。

    少了這一條就會出現「快取檔壞了，症狀卻報成 provider 不可用」，而那正是 #670
    的排查方向被整個帶偏的形態。
    """

    path = tmp_path / "cache.json"
    path.write_text("{ broken", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger=planning_probe_cache.logger.name):
        planning_probe_cache.ProbeCache.open(path)
    messages = [record.getMessage() for record in caplog.records]
    assert any(planning_probe_cache.CACHE_UNREADABLE_REASON in message for message in messages)
    # 三個既有的 probe 失敗 reason 都不得出現——它們是不同的一件事。
    assert not any(
        marker in message
        for message in messages
        for marker in ("safe-probe-failed", "smoke-failed", "models-probe-failed")
    )


def test_corrupt_cache_is_repaired_in_place_on_next_flush(
    tmp_path: Path, hermetic_fingerprint: Path
) -> None:
    """壞檔不是死結：下一輪重探之後就地寫回一份合法的快取。"""

    path = tmp_path / "cache.json"
    path.write_text("{ broken", encoding="utf-8")
    cache = planning_probe_cache.ProbeCache.open(path)
    fingerprint = _fingerprint()
    cache.put(_identity(), CapabilityProbe.ready_for("codex", "primary", "openai"), fingerprint=fingerprint)
    assert cache.flush() is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == planning_probe_cache.CACHE_SCHEMA
    assert planning_probe_cache.ProbeCache.open(path).get(
        _identity(), fingerprint=fingerprint
    ) is not None


def test_expired_ready_never_served_when_reprobe_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ready 過期且重探失敗 ⇒ not ready。**不得**因為「上次是 ready」而沿用。"""

    identities = _registry(_identity())
    monkeypatch.setattr(planning_runtime, "load_model_identities", lambda: identities)
    cache = tmp_path / "cache.json"
    good = planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"),
        worktree=tmp_path,
        runner=_echo_runner([]),
        probe_cache_path=cache,
    )
    assert good.probes[("codex", "primary")].ready is True

    # TTL 設 1 秒並把時鐘往前推——ready 過期。
    monkeypatch.setenv(planning_probe_cache.READY_TTL_ENV, "1")
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["items"]["codex::primary"]["probed_at_epoch"] -= 3600
    cache.write_text(json.dumps(payload), encoding="utf-8")

    def exploding(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    degraded = planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"),
        worktree=tmp_path,
        runner=exploding,
        probe_cache_path=cache,
    )
    probe = degraded.probes[("codex", "primary")]
    assert probe.ready is False
    assert probe.reason == "safe-probe-failed"
    assert probe.diagnostic == "FileNotFoundError"


def test_clock_going_backwards_is_a_miss(tmp_path: Path, hermetic_fingerprint: Path) -> None:
    """`probed_at` 落在未來（NTP 校正、VM 還原）⇒ miss。

    不擋這一條的話，一次時鐘跳躍就能讓一列 ready 被無限期沿用——TTL 對它失效。
    """

    fingerprint = _fingerprint()
    path = tmp_path / "cache.json"
    cache = planning_probe_cache.ProbeCache.open(path, clock=lambda: 10_000.0)
    cache.put(_identity(), CapabilityProbe.ready_for("codex", "primary", "openai"), fingerprint=fingerprint)
    cache.flush()

    rewound = planning_probe_cache.ProbeCache.open(path, clock=lambda: 1_000.0)
    assert rewound.get(_identity(), fingerprint=fingerprint) is None


def test_ttl_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ready／not-ready 兩段 TTL 分開，且非法值一律當 0（＝永遠 miss），不落回預設。"""

    cache = planning_probe_cache.ProbeCache.open(tmp_path / "cache.json")
    assert cache.ttl_seconds(ready=True) == planning_probe_cache.DEFAULT_READY_TTL_SECONDS
    assert cache.ttl_seconds(ready=False) == planning_probe_cache.DEFAULT_NOT_READY_TTL_SECONDS
    # 失敗要快速重試、成功不需要頻繁重確認——兩段不同是刻意的。
    assert cache.ttl_seconds(ready=False) < cache.ttl_seconds(ready=True)

    monkeypatch.setenv(planning_probe_cache.READY_TTL_ENV, "60")
    monkeypatch.setenv(planning_probe_cache.NOT_READY_TTL_ENV, "3O0")  # 打錯字（字母 O）
    tuned = planning_probe_cache.ProbeCache.open(tmp_path / "cache.json")
    assert tuned.ttl_seconds(ready=True) == 60
    assert tuned.ttl_seconds(ready=False) == 0


def test_zero_ttl_disables_the_cache(tmp_path: Path, hermetic_fingerprint: Path) -> None:
    fingerprint = _fingerprint()
    path = tmp_path / "cache.json"
    cache = planning_probe_cache.ProbeCache.open(path)
    cache.put(_identity(), CapabilityProbe.ready_for("codex", "primary", "openai"), fingerprint=fingerprint)
    cache.flush()
    disabled = planning_probe_cache.ProbeCache.open(
        path, env={planning_probe_cache.READY_TTL_ENV: "0"}
    )
    assert disabled.get(_identity(), fingerprint=fingerprint) is None


# ---------------------------------------------------------------------------
# 快取內容：失敗側的診斷（design D5）
# ---------------------------------------------------------------------------


def test_cache_records_failure_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """失敗側存 reason／diagnostic／族／unit／hardening_profile／resolved_binary。

    `reason`／`diagnostic` **逐字沿用** `CapabilityProbe`——#674 的
    `stdout_excerpt()`／`strip_code_fence()` 是那件事的唯一真相，快取層不得再造一份
    節錄邏輯（design D5 明文）。
    """

    identities = _registry(_identity())
    monkeypatch.setattr(planning_runtime, "load_model_identities", lambda: identities)
    cache = tmp_path / "cache.json"

    def malformed(argv, **kwargs):
        return _completed("not json at all", returncode=0)

    runtime = planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"),
        worktree=tmp_path,
        runner=malformed,
        probe_cache_path=cache,
    )
    probe = runtime.probes[("codex", "primary")]
    assert probe.ready is False

    row = json.loads(cache.read_text(encoding="utf-8"))["items"]["codex::primary"]
    assert row["ready"] is False
    assert row["reason"] == probe.reason
    assert row["diagnostic"] == probe.diagnostic
    assert row["family"] == "planning-output-malformed"
    # D8 要的三格（票 E 之後的拒因表消費者）。
    for key in ("unit", "hardening_profile", "resolved_binary"):
        assert key in row
    # 票 E 才有來源的三格先立在 schema 上，目前恆為 None——這是票的邊界，不是缺口。
    assert row["returncode"] is None
    assert row["stdout_prefix"] is None
    assert row["binary_version"] is None
    # 指紋分量明表：operator 直接看得出「是哪一格變了」。
    assert set(row["fingerprint_inputs"]) == set(
        planning_probe_cache.FINGERPRINT_DIGEST_FIELDS
    )


def test_ledger_keeps_first_observed_and_counts_observations(
    tmp_path: Path, hermetic_fingerprint: Path
) -> None:
    """沿用 `not_claimable` 的 ledger 形狀：operator 看得出「這個 provider 掛多久了」。"""

    fingerprint = _fingerprint()
    failed = CapabilityProbe(False, "codex", "primary", "openai", "smoke-failed", "exit-code:1")
    cache = planning_probe_cache.ProbeCache.open(tmp_path / "cache.json")
    first = cache.put(_identity(), failed, fingerprint=fingerprint, now="2026-08-18T00:00:00+00:00")
    second = cache.put(_identity(), failed, fingerprint=fingerprint, now="2026-08-18T00:10:00+00:00")
    assert second["first_observed_at"] == first["first_observed_at"]
    assert second["last_observed_at"] == "2026-08-18T00:10:00+00:00"
    assert second["observations"] == 2

    # 結果變了就是**新的**一件事，`first_observed_at` 重新起算——否則「掛多久了」
    # 會把兩段不同原因的失敗接成一段，那個數字就不再是真話。
    recovered = cache.put(
        _identity(),
        CapabilityProbe.ready_for("codex", "primary", "openai"),
        fingerprint=fingerprint,
        now="2026-08-18T00:20:00+00:00",
    )
    assert recovered["first_observed_at"] == "2026-08-18T00:20:00+00:00"
    assert recovered["observations"] == 1


def test_identity_leaving_the_roster_is_pruned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """roster 移掉一個 identity ⇒ 它的 row 自動清除（`not_claimable.clear()` 同型）。"""

    cache = tmp_path / "cache.json"
    monkeypatch.setattr(
        planning_runtime,
        "load_model_identities",
        lambda: _registry(_identity(), _identity("agy", AGY_MODEL_ID)),
    )
    planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"),
        worktree=tmp_path,
        runner=_echo_runner([]),
        probe_cache_path=cache,
    )
    assert set(json.loads(cache.read_text(encoding="utf-8"))["items"]) == {
        "codex::primary",
        f"agy::{AGY_MODEL_ID}",
    }

    monkeypatch.setattr(planning_runtime, "load_model_identities", lambda: _registry(_identity()))
    planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"),
        worktree=tmp_path,
        runner=_echo_runner([]),
        probe_cache_path=cache,
    )
    assert set(json.loads(cache.read_text(encoding="utf-8"))["items"]) == {"codex::primary"}


# ---------------------------------------------------------------------------
# 快取是輔助設施：它壞掉／寫不進去都不得拖垮 planning
# ---------------------------------------------------------------------------


def test_fingerprint_never_raises_when_a_component_is_unresolvable(
    monkeypatch: pytest.MonkeyPatch, hermetic_fingerprint: Path
) -> None:
    """job 模式 ＋ PATH 未宣告（#679 的 fail-closed）⇒ 那一格落 `<unresolved:…>`。

    而且它是一個**會變**的值：PATH 一旦補上，指紋就變、快取自動失效重探。
    """

    monkeypatch.setenv("PSC_JOB_RUNNER", job_runner.RUNNER_SYSTEMD_TEMPLATE)
    monkeypatch.delenv("PSC_REVIEWER_PATH", raising=False)
    undeclared = _fingerprint()
    assert undeclared.executor_binary.startswith(
        planning_probe_cache.FINGERPRINT_UNRESOLVED_PREFIX
    )
    # 只帶例外**型別名**，不帶訊息——訊息會夾帶路徑與 env。
    assert undeclared.executor_binary == "<unresolved:JobRunnerError>"

    monkeypatch.setenv("PSC_REVIEWER_PATH", str(hermetic_fingerprint / "bin"))
    declared = _fingerprint()
    assert not declared.executor_binary.startswith(
        planning_probe_cache.FINGERPRINT_UNRESOLVED_PREFIX
    )
    assert declared.digest != undeclared.digest


def test_unknown_executor_fails_closed_into_the_fingerprint(hermetic_fingerprint: Path) -> None:
    """未登記的 executor（`cg`）沒有剖面 ⇒ 兩格都是 `<unresolved:…>`，不是猜一份。"""

    fingerprint = planning_probe_cache.compute_fingerprint(
        _identity("cg", "glm-5.3"), roster="roster-v1"
    )
    assert fingerprint.hardening_profile == "<unresolved:JobRunnerError>"
    assert fingerprint.template_unit == "<unresolved:JobRunnerError>"
    assert fingerprint.unit is None


def test_unwritable_cache_does_not_break_planning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """寫不進去 ⇒ 那一輪只是沒有快取，planning 照常完成。"""

    identities = _registry(_identity())
    monkeypatch.setattr(planning_runtime, "load_model_identities", lambda: identities)
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file, not a directory", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger=planning_probe_cache.logger.name):
        runtime = planning_runtime.build_production_planning_runtime(
            primary=("codex", "primary"),
            worktree=tmp_path,
            runner=_echo_runner([]),
            probe_cache_path=blocked / "cache.json",
        )
    assert runtime.probes[("codex", "primary")].ready is True
    assert any(
        planning_probe_cache.CACHE_UNWRITABLE_REASON in record.getMessage()
        for record in caplog.records
    )


def test_concurrent_writer_rows_survive_our_flush(
    tmp_path: Path, hermetic_fingerprint: Path
) -> None:
    """另一個行程在我們探測期間寫進來的 row 不會被我們整份蓋掉。

    這**不是**鎖：兩個同時 miss 的 tick 仍會各自探一次（代價＝多探一次）。這條釘
    住的是「並行的代價不得升級成掉資料」——落盤前重讀磁碟並以它為底疊上本次異動。
    """

    path = tmp_path / "cache.json"
    ours = planning_probe_cache.ProbeCache.open(path)
    ours.put(_identity(), CapabilityProbe.ready_for("codex", "primary", "openai"), fingerprint=_fingerprint())

    # 「另一個行程」在我們 flush 之前寫了別的 identity。
    theirs = planning_probe_cache.ProbeCache.open(path)
    agy = _identity("agy", AGY_MODEL_ID)
    theirs.put(agy, CapabilityProbe.ready_for("agy", AGY_MODEL_ID, "google"), fingerprint=_fingerprint(agy))
    theirs.flush()

    ours.flush(keep=[("codex", "primary"), ("agy", AGY_MODEL_ID)])
    items = json.loads(path.read_text(encoding="utf-8"))["items"]
    assert set(items) == {"codex::primary", f"agy::{AGY_MODEL_ID}"}


# ---------------------------------------------------------------------------
# 登記表：Manager-owned，且不在任何 job 模板 unit 的 RWP 裡
# ---------------------------------------------------------------------------


def test_cache_asset_is_registered_manager_owned() -> None:
    asset = trust_registry.asset_by_id(CACHE_ASSET)
    assert asset.tree is trust_registry.TrustTree.MANAGER_OWNED
    assert asset.writers == (trust_registry.Principal.MANAGER,)
    assert asset.readers == (trust_registry.Principal.MANAGER,)
    path = permgen.DEFAULT_LAYOUT.asset_paths()[CACHE_ASSET]
    assert path.endswith("/" + planning_probe_cache.CACHE_FILENAME)
    assert path.startswith(permgen.DEFAULT_LAYOUT.coordinator_root + "/")


def test_cache_asset_not_in_any_job_unit_rwp() -> None:
    """快取不出現在任一 job 模板 unit 的 `ReadWritePaths` 產出中。

    job 一旦寫得動快取，「這個 provider 是 ready 的」就變成模型可以自證的東西——
    而 `select_secondary_planner()` 的整個異質性論證正建立在那個判定不是模型說了算上。
    """

    path = permgen.DEFAULT_LAYOUT.asset_paths()[CACHE_ASSET]
    for scheme in permgen.SCHEMES.values():
        plan = permgen.generate_plan(scheme)
        for principal in trust_registry.UNTRUSTED_EXECUTION_PRINCIPALS:
            account = scheme.resolve(principal)
            if account is None:
                continue
            targets = permgen.required_write_targets(
                plan, permgen.DEFAULT_LAYOUT, account, principals=frozenset({principal})
            )
            assert CACHE_ASSET not in targets, (scheme.scheme_id, principal)
            assert path not in set(targets.values())

    for principal in trust_registry.DOWNGRADED_JOB_PRINCIPALS:
        for profile in permgen.HARDENING_PROFILES:
            unit = permgen.build_job_unit(
                permgen.FOUR_WAY_SCHEME, principal=principal, profile=profile
            )
            assert path not in unit.content
            assert path not in unit.read_write_paths
