"""Secret-safe deployment diagnostics for the unified lifecycle runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import quote
from unittest.mock import patch

from .github_rate_limit import is_rate_limit_signal
from .monitor.socket_path import socket_path_fits, socket_path_limit_detail

DOCTOR_SCHEMA = "cortex-doctor/v1"
AUTO_LABEL = "cortex:auto-on-going"
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
INSTANCE_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
Runner = Callable[..., object]
AgyProbe = Callable[[], tuple[bool, str]]


@dataclass(frozen=True)
class ProbeResult:
    name: str
    status: str
    detail: str
    required: bool

    def __post_init__(self) -> None:
        if self.status not in {"pass", "warn", "fail"}:
            raise ValueError(f"invalid doctor probe status: {self.status}")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "required": self.required,
        }


@dataclass(frozen=True)
class DoctorReport:
    probes: tuple[ProbeResult, ...]

    @property
    def ok(self) -> bool:
        return not any(probe.required and probe.status == "fail" for probe in self.probes)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": DOCTOR_SCHEMA,
            "ok": self.ok,
            "probes": [probe.to_dict() for probe in self.probes],
        }


def _process(
    runner: Runner,
    argv: list[str],
) -> tuple[int, str]:
    try:
        raw = runner(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except Exception:
        return 1, ""
    returncode = getattr(raw, "returncode", None)
    stdout = getattr(raw, "stdout", "")
    if not isinstance(returncode, int):
        return 1, ""
    return returncode, stdout if isinstance(stdout, str) else ""


def _gh_auth_probe(runner: Runner) -> ProbeResult:
    """#370: ``gh auth status`` exits non-zero on rate limit *and* on a
    genuinely invalid credential -- exit code alone can't tell them apart,
    and misreporting a rate limit as "authentication failed" sends
    operators chasing a token that isn't actually broken (see #370's
    runtime evidence: a secondary rate limit was misdiagnosed this way).
    Classifies stderr/stdout the same way as the Monitor GitHub provider
    (`monitor/providers.py`) and canonical authority classification
    (`coordinator/claim.py`) so all three agree. Never echoes the raw
    command output into the probe detail -- only a fixed, secret-free
    string (see ``test_doctor_does_not_echo_credentials_from_failed_command``).
    """
    try:
        raw = runner(["gh", "auth", "status"], shell=False, capture_output=True, text=True, timeout=45)
    except Exception:
        return ProbeResult("gh-auth", "fail", "authentication failed", True)
    returncode = getattr(raw, "returncode", None)
    if returncode == 0:
        return ProbeResult("gh-auth", "pass", "authenticated", True)
    stderr = getattr(raw, "stderr", "")
    stdout = getattr(raw, "stdout", "")
    message = "\n".join(value for value in (stderr, stdout) if isinstance(value, str))
    if is_rate_limit_signal(message):
        return ProbeResult(
            "gh-auth",
            "warn",
            "GitHub rate limit exceeded -- wait for the window to reset before treating this as a credential failure",
            False,
        )
    return ProbeResult("gh-auth", "fail", "authentication failed", True)


def _valid_repo(value: str | None) -> bool:
    if value is None or REPO_RE.fullmatch(value) is None:
        return False
    owner, name = value.split("/", 1)
    return owner not in {".", ".."} and name not in {".", ".."}


def _preflight_probe(env: Mapping[str, str]) -> ProbeResult:
    try:
        _load_runtime_preflight_command(env)
    except (ImportError, OSError, ValueError) as exc:
        return ProbeResult("preflight", "fail", _preflight_failure_detail(exc), True)
    return ProbeResult("preflight", "pass", "runtime validator accepted typed executable", True)


def _preflight_failure_detail(exc: BaseException) -> str:
    message = str(exc).lower()
    if "psc_preflight_cmd is required" in message:
        return (
            "PSC_PREFLIGHT_CMD is required (category: required); "
            "set it to a typed argv, for example: `python3 -m project_preflight`."
        )
    if "psc_preflight_cmd is malformed" in message:
        return (
            "PSC_PREFLIGHT_CMD is malformed (category: malformed); use typed argv format "
            "and avoid shell metacharacters."
        )
    if "psc_preflight_cmd shell wrapper is not allowed" in message:
        return (
            "PSC_PREFLIGHT_CMD uses a shell wrapper (category: shell-wrapper-not-allowed); "
            "replace it with typed argv "
            "for example: `python3 -m project_preflight`."
        )
    if message.startswith("psc_preflight_cmd executable unavailable: "):
        return (
            "PSC_PREFLIGHT_CMD executable unavailable (category: executable-unavailable); "
            "install the configured "
            "preflight executable and verify the command path."
        )
    return (
        "Preflight command validation failed. Set `PSC_PREFLIGHT_CMD` as typed "
        "argv and keep it consistent with your delivery preflight command."
    )


def _load_runtime_preflight_command(env: Mapping[str, str]) -> tuple[str, ...]:
    """Use the delivery runtime's single command validator; missing PR C fails closed."""
    from .coordinator.preflight import load_preflight_command

    return load_preflight_command(env=env)


def _deck_required_gate_names() -> frozenset[str]:
    """Gate names the packaged deck's acceptance criteria will demand at harvest.

    Same judge as the harvest path
    (``terminal_contract.expected_gate_names_for_test_policy``), fed by every
    card's ``execution.test_policy`` in the packaged ``cards.yaml`` -- the union
    over cards is the superset any combo can require, so a deployment covering it
    covers every combo it might select.
    """
    from .coordinator.terminal_contract import expected_gate_names_for_test_policy
    from .deck.schema import DEFAULT_CARDS_PATH, load_cards

    required: set[str] = set()
    for card in load_cards(DEFAULT_CARDS_PATH).values():
        required |= expected_gate_names_for_test_policy(card.test_policy)
    return frozenset(required)


def _gate_declaration_probe(env: Mapping[str, str]) -> ProbeResult:
    """#540: prove the gate declarations exist *before* a builder card is dispatched.

    A manager whose environment declares no ``PSC_GATE_CMD_*`` still writes a
    ``gates: []`` ledger when the job ends, so every build card carrying a
    ``test_policy`` dies at harvest with
    ``gate-ledger-missing-expected-gate`` -- correct fail-closed behaviour, but
    the operator only learns about it after a builder has already produced a
    valid Candidate (observed on run ``workflow-084f75e2178cf7547476``: a
    conforming RED commit could not be accepted because
    ``PSC_GATE_CMD_PYTEST`` was missing from one instance's manager env, and the
    only trace was a line in ``manager.log``). This probe turns that into an
    up-front, actionable diagnosis.
    """
    from .coordinator.gate_ledger import GATE_ENV_PREFIX, GateSpecError, declared_gate_names

    try:
        required = _deck_required_gate_names()
    except Exception:
        # Deck data unavailable/invalid is a different probe's business; never
        # let it masquerade as a gate declaration failure.
        return ProbeResult(
            "gate-declarations",
            "warn",
            "packaged deck cards could not be read; gate declaration coverage not checked",
            False,
        )
    try:
        declared = frozenset(declared_gate_names(env))
    except GateSpecError:
        return ProbeResult(
            "gate-declarations",
            "fail",
            f"{GATE_ENV_PREFIX}* declaration is invalid (typed argv required, shell wrappers "
            "rejected); the gate ledger degrades to a single failed entry and every build card "
            "fails closed",
            True,
        )
    missing = sorted(required - declared)
    if missing:
        example = f"{GATE_ENV_PREFIX}{missing[0].upper()}"
        return ProbeResult(
            "gate-declarations",
            "fail",
            f"{GATE_ENV_PREFIX}* does not cover the gate(s) the deck's acceptance criteria "
            f"require: {missing} (declared: {sorted(declared)}); builder cards will be rejected "
            f"with gate-ledger-missing-expected-gate. Declare for example "
            f"`{example}=python3 -m pytest -q` in the manager EnvironmentFile and restart the "
            "service (the gate name is the lowercased suffix of the variable name)",
            True,
        )
    if not declared:
        return ProbeResult(
            "gate-declarations",
            "warn",
            f"no {GATE_ENV_PREFIX}* declared; the gate ledger will always be empty, so passed "
            "cards carry no independent evidence",
            False,
        )
    return ProbeResult(
        "gate-declarations",
        "pass",
        f"{GATE_ENV_PREFIX}* declares {sorted(declared)} and covers the deck-required gate(s) "
        f"{sorted(required)}",
        True,
    )


def _identity_probe(env: Mapping[str, str], agents_root: Path) -> ProbeResult:
    config_root = Path(
        env.get("PSC_PROJECT_CONFIG_ROOT", str(agents_root / "config" / "paulsha"))
    ).expanduser()
    try:
        schema_version = _load_runtime_model_identities(config_root)
    except Exception as exc:
        # Catch identity validation failures broadly to avoid leaking runtime payloads
        # while preserving fail-closed behavior for unknown future errors.
        return ProbeResult("model-identities", "fail", _identity_failure_detail(exc), True)
    return ProbeResult(
        "model-identities",
        "pass",
        f"runtime-validated schema v{schema_version} with canonical agy identity",
        True,
    )


def _model_resolution_probe(env: Mapping[str, str], agents_root: Path) -> ProbeResult:
    """#534／#509：診斷「overlay 宣告」與「生效解析」不一致。

    #509 的假 PASS 有兩個成因：doctor 只驗 registry 載得起來，不驗**解析結果**；
    而且沒有把自己讀的 config root 說出來，operator 無從發現 doctor 與 daemon
    讀的根本是兩份檔案。本 probe 兩者都補：走與 tick 同一個合併載入器，跑與
    manager 同一個 `model_resolution.rank_candidates`，並在 detail 裡明示
    config root。
    """

    config_root = Path(
        env.get("PSC_PROJECT_CONFIG_ROOT", str(agents_root / "config" / "paulsha"))
    ).expanduser()
    try:
        from .coordinator import model_resolution
        from .coordinator.model_identities import load_model_identities

        registry = load_model_identities(config_root)
    except Exception:
        # registry 本身載不起來由 model-identities probe 負責報告，這裡不重複噪音。
        return ProbeResult(
            "model-resolution",
            "fail",
            f"identity registry unavailable at {config_root}; see model-identities probe",
            True,
        )
    context = registry.resolution_context
    failures: list[str] = []
    warnings: list[str] = []
    for note in context.notes:
        if note.severity == "fail":
            failures.append(f"{note.code}: {note.detail}")
        elif note.severity == "warn":
            warnings.append(f"{note.code}: {note.detail}")
    summary: list[str] = []
    for persona, role in sorted(model_resolution.ROLE_BY_PERSONA.items()):
        candidates = [
            identity for identity in registry.identities if role in identity.capabilities
        ]
        ranked = model_resolution.rank_candidates(
            candidates, role=role, context=context
        )
        if not ranked.ordered:
            failures.append(
                f"{persona}: 無可解析身分（{ranked.exclusion_detail() or 'no candidate declared'}）"
            )
            continue
        top = ranked.ordered[0]
        layer = ranked.layer_of(top)
        summary.append(f"{persona}={top.executor}/{top.model_id}[{layer}]")
        overlay_declared = [
            identity
            for identity in candidates
            if model_resolution.identity_origin(identity)
            == model_resolution.IDENTITY_ORIGIN_OVERLAY
        ]
        if layer != model_resolution.RESOLUTION_LAYER_OVERLAY and overlay_declared:
            # 不變式守衛：overlay 宣告了這個角色，生效解析就必須落在第 1 層。
            # #534 之前正是這條不成立（packaged roster 的內建列序壓過人工指定），
            # 而 doctor 只驗 registry 載得起來、驗不到解析結果，於是回報 PASS。
            failures.append(
                f"{persona}: overlay 宣告 "
                + ", ".join(f"{i.executor}/{i.model_id}" for i in overlay_declared)
                + f"，生效解析卻是 {top.executor}/{top.model_id}[{layer}]"
            )
        elif layer == model_resolution.RESOLUTION_LAYER_PACKAGED:
            warnings.append(
                f"{persona}: 解析落在 packaged 候選 {top.executor}/{top.model_id}"
                "（未經 patchmud eval／人工複核）"
            )
    for entry in context.eval_roster.entries:
        if registry.get(entry.executor, entry.model_id) is None:
            warnings.append(
                f"model-eval-roster 列出的 {entry.executor}/{entry.model_id} 不在 registry 內"
                "（清單過期或身分已下架）"
            )
    detail_tail = f"（config root: {config_root}）"
    if failures:
        return ProbeResult(
            "model-resolution", "fail", "; ".join(failures) + detail_tail, True
        )
    if warnings:
        return ProbeResult(
            "model-resolution", "warn", "; ".join(warnings) + detail_tail, False
        )
    return ProbeResult(
        "model-resolution",
        "pass",
        "resolution chain consistent: " + ", ".join(summary) + detail_tail,
        True,
    )


def _identity_failure_detail(exc: BaseException) -> str:
    message = str(exc).lower()
    if "model-identities missing" in message:
        return (
            "model-identities is missing (category: registry-missing); "
            "create PSC_PROJECT_CONFIG_ROOT/model-identities.yaml first."
        )
    if "unreadable" in message:
        return (
            "model-identities is unreadable (category: registry-unreadable); "
            "ensure the file exists and can be read "
            "by the doctor runtime."
        )
    if (
        "model-identities schema_version" in message
        or "schema_version must be" in message
        or "model-identities invalid root" in message
        or "schema/contract invalid" in message
        or "schema invalid" in message
        or "contract invalid" in message
    ):
        return (
            "model-identities schema or contract is invalid (category: registry-invalid); "
            "fix identity schema and "
            "capability settings."
        )
    if "canonical agy planning identity missing" in message:
        return (
            "canonical agy planning identity missing (category: registry-invalid); "
            "define planning identity "
            "with `executor: agy` and planning capability."
        )
    return (
        "model-identities validation failed. Ensure a valid identities file exists, "
        "passes schema/contracts, and contains the canonical planning identity."
    )


def _load_runtime_model_identities(config_root: Path) -> int:
    """Validate the exact registry consumed by planner/reviewer selection."""
    from .coordinator.model_identities import (
        AGY_DOMAIN,
        AGY_LIVE_PROBE,
        AGY_MODEL_ID,
        load_model_identities,
    )

    registry = load_model_identities(config_root)
    identity = registry.get("agy", AGY_MODEL_ID)
    if (
        identity is None
        or identity.independence_domain != AGY_DOMAIN
        or "planning" not in identity.capabilities
        or identity.live_probe != AGY_LIVE_PROBE
    ):
        raise ValueError("canonical agy planning identity missing")
    return int(registry.schema_version)


#: review sandbox 要跑起來所需的外部程式（#661 起是常數而非行內字面值）。
#:
#: 提出來的理由不是可讀性，而是**它必須可被登記表比對**：#661 的實機症狀正是這張
#: 清單裡的 `srt` 從未進過 `permgen` 的 toolchain 名冊，於是四分部署把四個 executor
#: 都搬進部署樹之後，doctor 仍紅在 `review-sandbox`。測試以這個常數對照登記表，
#: 讓「probe 要求的程式」與「登記表涵蓋的程式」不能再各走各的。
REVIEW_SANDBOX_EXECUTABLES: tuple[str, ...] = ("claude", "bwrap", "socat", "srt", "python3")


def _review_sandbox_probe(
    env: Mapping[str, str],
    agents_root: Path,
    *,
    runner: Runner = subprocess.run,
    live: bool = False,
) -> ProbeResult:
    """Validate the executable Claude sandbox surface when it can review.

    #452 B／#456 R6：packaged roster 登錄的 claude review 是**候選宣告**，登錄
    不隱含本機可用。只有 host-local overlay 明示宣告 claude review 時，本 probe
    才以 fail gate 把關（operator 宣稱本機可跑，缺件是真錯誤——與 #452 前行為
    逐項相同）；候選宣告僅來自 packaged 時，同樣的檢查照跑但降級為 warn／非
    required，不得因 roster 落地讓原本健康的部署 doctor 轉紅。
    """

    config_root = Path(
        env.get("PSC_PROJECT_CONFIG_ROOT", str(agents_root / "config" / "paulsha"))
    ).expanduser()
    try:
        from .coordinator.model_identities import load_model_identities

        registry = load_model_identities(config_root)
    except (ImportError, OSError, ValueError):
        return ProbeResult(
            "review-sandbox", "fail", "review identity registry unavailable", True
        )
    declared = any(
        identity.executor == "claude" and "review" in identity.capabilities
        for identity in registry.identities
    )
    if not declared:
        return ProbeResult(
            "review-sandbox", "warn", "no Claude review identity configured", False
        )
    result = _review_sandbox_checks(env, runner=runner, live=live)
    if result.status == "fail" and not _custom_overlay_declares_claude_review(config_root):
        return ProbeResult(
            "review-sandbox",
            "warn",
            f"{result.detail} (packaged claude review identity is a candidate "
            "declaration; registry listing does not imply local availability, #456 R6)",
            False,
        )
    return result


def _review_sandbox_checks(
    env: Mapping[str, str],
    *,
    runner: Runner,
    live: bool,
) -> ProbeResult:
    search_path = env.get("PATH")
    executables = {
        name: shutil.which(name, path=search_path)
        for name in REVIEW_SANDBOX_EXECUTABLES
    }
    missing = [name for name, path in executables.items() if path is None]
    if missing:
        return ProbeResult(
            "review-sandbox",
            "fail",
            f"missing required executable(s): {','.join(missing)}",
            True,
        )
    claude = str(executables["claude"])
    bwrap = str(executables["bwrap"])
    socat = str(executables["socat"])
    srt = str(executables["srt"])
    python = str(executables["python3"])
    claude_code, claude_version = _process(runner, [claude, "--version"])
    version_match = re.search(r"\b([0-9]+)\.([0-9]+)\.([0-9]+)\b", claude_version)
    if (
        claude_code != 0
        or version_match is None
        or tuple(int(part) for part in version_match.groups()) < (2, 1, 187)
    ):
        return ProbeResult(
            "review-sandbox",
            "fail",
            "Claude Code 2.1.187 or newer is required",
            True,
        )
    help_code, help_text = _process(runner, [claude, "--help"])
    required_flags = {
        "--disable-slash-commands",
        "--json-schema",
        "--permission-mode",
        "--safe-mode",
        "--setting-sources",
        "--settings",
        "--tools",
    }
    if help_code != 0 or any(flag not in help_text for flag in required_flags):
        return ProbeResult(
            "review-sandbox", "fail", "Claude review sandbox CLI surface unavailable", True
        )
    dependency_commands = ([bwrap, "--version"], [socat, "-V"], [srt, "--version"])
    if any(_process(runner, list(argv))[0] != 0 for argv in dependency_commands):
        return ProbeResult(
            "review-sandbox", "fail", "Claude sandbox dependency execution failed", True
        )
    if live:
        smoke_code, _ = _process(
            runner,
            [
                bwrap,
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--unshare-net",
                "--die-with-parent",
                "/bin/true",
            ],
        )
        if smoke_code != 0:
            return ProbeResult(
                "review-sandbox", "fail", "native read-only sandbox smoke failed", True
            )
        try:
            from .coordinator.launcher import _claude_review_settings

            with tempfile.TemporaryDirectory(prefix="cortex-review-sandbox-") as root:
                candidate = Path(root) / "candidate"
                candidate.mkdir()
                policy = json.loads(_claude_review_settings(root))["sandbox"]["filesystem"]
                settings = Path(root) / "srt-settings.json"
                settings.write_text(
                    json.dumps(
                        {
                            "filesystem": {**policy, "allowWrite": []},
                            "network": {"allowedDomains": [], "deniedDomains": []},
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                unix_socket_code, _ = _process(
                    runner,
                    [
                        srt,
                        "--settings",
                        str(settings),
                        "--",
                        python,
                        "-c",
                        (
                            "import errno,socket,sys;"
                            "\ntry: socket.socket(socket.AF_UNIX)"
                            "\nexcept PermissionError as exc: sys.exit(0 if exc.errno == errno.EPERM else 2)"
                            "\nelse: sys.exit(1)"
                        ),
                    ],
                )
        except (KeyError, OSError, TypeError, ValueError):
            unix_socket_code = 2
        if unix_socket_code != 0:
            return ProbeResult(
                "review-sandbox", "fail", "configured reviewer sandbox smoke failed", True
            )
    return ProbeResult(
        "review-sandbox", "pass", "Claude native Bash sandbox runtime ready", True
    )


def _custom_overlay_declares_claude_review(config_root: Path) -> bool:
    """host-local overlay（非 packaged 候選 roster）是否明示宣告 claude review。"""

    custom = config_root / "model-identities.yaml"
    if not custom.is_file():
        return False
    try:
        from .coordinator.model_identities import _load_model_identity_file

        overlay = _load_model_identity_file(custom)
    except (ImportError, ValueError):
        return False
    return any(
        identity.executor == "claude" and "review" in identity.capabilities
        for identity in overlay.identities
    )


def _default_agy_probe() -> tuple[bool, str]:
    try:
        from .coordinator.model_identities import (
            AGY_DOMAIN,
            AGY_MODEL_ID,
            probe_agy_capability,
        )
    except ImportError:
        return False, "unavailable"
    result = probe_agy_capability()
    matches = (
        result.executor == "agy"
        and result.model_id == AGY_MODEL_ID
        and result.independence_domain == AGY_DOMAIN
    )
    ready = bool(result.ready) and matches
    return ready, "ready" if ready else "unavailable"


def _unit_environment_files(path: Path, *, home: Path) -> tuple[tuple[Path, bool], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ValueError("unit unreadable") from exc
    files: list[tuple[Path, bool]] = []
    for raw in lines:
        line = raw.strip()
        if not line.startswith("EnvironmentFile="):
            continue
        value = line.split("=", 1)[1]
        optional = value.startswith("-")
        if optional:
            value = value[1:]
        value = value.replace("%h", str(home))
        candidate = Path(value).expanduser()
        if not value or "%" in value or not candidate.is_absolute():
            raise ValueError("EnvironmentFile path invalid")
        files.append((candidate, optional))
    if not files:
        raise ValueError("EnvironmentFile missing")
    return tuple(files)


def _authoritative_bootstrap_env(
    env_files: tuple[tuple[Path, bool], ...],
    *,
    home: Path,
    instance: str,
) -> Path:
    legacy = home / ".agents" / "core" / "runtime" / f"{instance}-manager.env"
    declared = {path for path, _optional in env_files}
    if legacy in declared:
        return legacy
    required = [path for path, optional in env_files if not optional]
    if len(required) != 1:
        raise ValueError("managed bootstrap EnvironmentFile missing")
    return required[0]


def _parse_environment_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError("EnvironmentFile unreadable") from exc
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        key, separator, value = line.partition("=")
        if not separator or ENV_KEY_RE.fullmatch(key) is None or key in values:
            raise ValueError("EnvironmentFile entry invalid or duplicate")
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("EnvironmentFile quote invalid")
            value = value[1:-1]
        values[key] = value
    return values


def _runtime_defaults(
    environment: Mapping[str, str],
    *,
    home: Path,
    instance: str,
) -> dict[str, str]:
    effective = dict(environment)
    effective["HOME"] = str(home)
    agents_root = Path(effective.get("PSC_AGENTS_ROOT", str(home / ".agents"))).expanduser()
    effective.setdefault("PSC_AGENTS_ROOT", str(agents_root))
    effective.setdefault("PSC_RUN_ROOT", str(agents_root / "run" / instance))
    effective.setdefault("PSC_MONITOR_STATE_ROOT", str(agents_root / "monitor"))
    effective.setdefault("PSC_PROJECT_CONFIG_ROOT", str(agents_root / "config" / "paulsha"))
    return effective


def _load_bootstrap_environment(
    *,
    home: Path,
    instance: str,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    if INSTANCE_RE.fullmatch(instance) is None or not home.is_absolute():
        raise ValueError("service instance/home invalid")
    unit_root = home / ".config" / "systemd" / "user"
    manager_unit = unit_root / f"{instance}-manager.service"
    monitor_unit = unit_root / f"{instance}-monitor.service"
    if manager_unit.is_symlink() or monitor_unit.is_symlink():
        raise ValueError("managed units must not be symlinks")
    manager_files = _unit_environment_files(
        manager_unit,
        home=home,
    )
    monitor_files = _unit_environment_files(
        monitor_unit,
        home=home,
    )
    if manager_files != monitor_files:
        raise ValueError("manager/monitor EnvironmentFile order differs")
    bootstrap_env = _authoritative_bootstrap_env(manager_files, home=home, instance=instance)
    effective = dict(base_env)
    loaded_files: set[Path] = set()
    for env_path, optional in manager_files:
        if not env_path.exists():
            if optional:
                continue
            raise ValueError("required EnvironmentFile missing")
        if env_path.is_symlink() or not env_path.is_file():
            raise ValueError("EnvironmentFile must be a regular non-symlink file")
        effective.update(_parse_environment_file(env_path))
        loaded_files.add(env_path)
    if bootstrap_env not in loaded_files:
        raise ValueError("managed bootstrap EnvironmentFile missing")
    effective = _runtime_defaults(effective, home=home, instance=instance)
    roots = ("PSC_AGENTS_ROOT", "PSC_RUN_ROOT", "PSC_MONITOR_STATE_ROOT", "PSC_PROJECT_CONFIG_ROOT")
    if any(not Path(effective[name]).expanduser().is_absolute() for name in roots):
        raise ValueError("effective runtime root is not absolute")
    return effective


def _service_environment_probe(
    *,
    home: Path,
    instance: str,
    live: bool,
    base_env: Mapping[str, str],
) -> tuple[ProbeResult, dict[str, str]]:
    if INSTANCE_RE.fullmatch(instance) is None:
        return (
            ProbeResult("service-paths", "fail", "instance name is invalid", True),
            _runtime_defaults(base_env, home=home, instance="cortex"),
        )
    timer = home / ".config" / "systemd" / "user" / f"{instance}-manager.timer"
    try:
        effective = _load_bootstrap_environment(
            home=home,
            instance=instance,
            base_env=base_env,
        )
        if timer.is_symlink() or not timer.is_file():
            raise FileNotFoundError("manager timer missing")
    except FileNotFoundError:
        return (
            ProbeResult(
                "service-paths",
                "fail" if live else "warn",
                "managed service bootstrap path(s) missing",
                live,
            ),
            _runtime_defaults(base_env, home=home, instance=instance),
        )
    except (OSError, ValueError):
        return (
            ProbeResult("service-paths", "fail", "managed bootstrap environment is invalid", True),
            _runtime_defaults(base_env, home=home, instance=instance),
        )
    return (
        ProbeResult("service-paths", "pass", "effective service environment is valid", live),
        effective,
    )


def _repo_identity_probe(effective: Mapping[str, str]) -> ProbeResult:
    """#366：比對 env 內 PSC_REPO_IDENTITY 身分戳記與 PSC_REPO_ROOT 目前實際解析出的
    身分是否一致，讓 PSC_REPO_ROOT 被靜默改寫的漂移能在潛伏期內被偵測到。"""
    from .deploy.installer import _resolve_repo_identity

    repo_root_raw = effective.get("PSC_REPO_ROOT", "").strip()
    stamp = effective.get("PSC_REPO_IDENTITY", "").strip()
    if not repo_root_raw:
        return ProbeResult(
            "repo-identity", "warn", "PSC_REPO_ROOT not set; identity drift not checked", False
        )
    if not stamp:
        return ProbeResult(
            "repo-identity",
            "warn",
            "PSC_REPO_IDENTITY stamp missing (env predates #366 identity guard); "
            "rerun `cortex install service` to backfill",
            False,
        )
    try:
        actual = _resolve_repo_identity(Path(repo_root_raw))
    except Exception:
        return ProbeResult(
            "repo-identity", "warn", "unable to resolve current repo identity for PSC_REPO_ROOT", False
        )
    if actual != stamp:
        return ProbeResult(
            "repo-identity",
            "fail",
            f"PSC_REPO_ROOT identity drift: recorded PSC_REPO_IDENTITY={stamp!r} but "
            f"PSC_REPO_ROOT={repo_root_raw!r} currently resolves to {actual!r}",
            True,
        )
    return ProbeResult("repo-identity", "pass", "PSC_REPO_ROOT matches recorded PSC_REPO_IDENTITY stamp", False)


# #371／#375：managed_env 的 `preserve_existing` 曾把 PSC_PROJECT_CONFIG_ROOT
# 鎖成一個早期殘留的錯誤值，`cortex install service` 重裝也修不好；
# PSC_CONTROL_ROOT 是 #375 新收進 managed_env 的鍵，同一種 bug class 可能重演。
# installer 端已修好（兩者都改成每次 install 一律以目前 PSC_AGENTS_ROOT／
# instance 重新推導），但已經部署、尚未重跑 install service 的既有安裝仍可能
# 卡著舊值。這個 probe 是獨立於 install 之外的潛伏期偵測：不需要重裝就能看見
# 目前 effective 值是否已經跟目前的推導值分岔。
_MANAGED_PATH_DRIFT_TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("PSC_PROJECT_CONFIG_ROOT", ("config", "paulsha")),
    ("PSC_CONTROL_ROOT", ("control",)),
)


def _managed_path_drift_probe(
    effective: Mapping[str, str], *, agents_root: Path, instance: str
) -> ProbeResult:
    drifted: list[str] = []
    missing: list[str] = []
    for key, relative in _MANAGED_PATH_DRIFT_TARGETS:
        # PSC_CONTROL_ROOT 是 instance-scoped（`control/<instance>`），其餘目前
        # 仍是 agents_root 底下的固定子目錄——與 installer.py 的 managed_env
        # 推導公式保持一致。
        derived = (
            agents_root.joinpath(*relative, instance)
            if key == "PSC_CONTROL_ROOT"
            else agents_root.joinpath(*relative)
        )
        raw = effective.get(key, "").strip()
        if not raw:
            # 鍵完全缺席＝installer 這個版本以前從未寫過它（例如 #375 之前的舊
            # PSC_CONTROL_ROOT），屬預期過渡態，不視為主動 drift。
            missing.append(key)
            continue
        actual = Path(raw).expanduser()
        if actual != derived:
            drifted.append(f"{key}: effective={actual} derived={derived}")
    if drifted:
        return ProbeResult(
            "managed-path-drift",
            "fail",
            "managed path drift detected (rerun `cortex install service` to repair): "
            + "; ".join(drifted),
            True,
        )
    if missing:
        return ProbeResult(
            "managed-path-drift",
            "warn",
            "managed path(s) not yet written by installer (legacy install predates this "
            "key becoming instance-scoped managed state; rerun `cortex install service` "
            "to adopt): " + ", ".join(missing),
            False,
        )
    return ProbeResult(
        "managed-path-drift",
        "pass",
        "managed paths match current PSC_AGENTS_ROOT-derived values",
        False,
    )


def _shared_project_config_root_probe(
    effective: Mapping[str, str], *, home: Path, instance: str
) -> ProbeResult:
    """Detect shared project config roots from local bootstrap env files only."""
    raw_root = effective.get("PSC_PROJECT_CONFIG_ROOT", "").strip()
    if not raw_root:
        return ProbeResult(
            "shared-project-config-root",
            "pass",
            "PSC_PROJECT_CONFIG_ROOT is not set; shared-root check not applicable",
            False,
        )
    project_root = Path(raw_root).expanduser().resolve()
    runtime_dir = home / ".agents" / "core" / "runtime"
    owners: dict[str, Path] = {instance: project_root}
    if runtime_dir.is_dir():
        for env_file in sorted(runtime_dir.glob("*-manager.env")):
            name = env_file.name[: -len("-manager.env")]
            if INSTANCE_RE.fullmatch(name) is None:
                continue
            try:
                values = _parse_environment_file(env_file)
            except (OSError, ValueError):
                continue
            candidate = values.get("PSC_PROJECT_CONFIG_ROOT", "").strip()
            if not candidate:
                agents = values.get("PSC_AGENTS_ROOT", "").strip()
                candidate = str(Path(agents).expanduser() / "config" / "paulsha") if agents else ""
            if candidate:
                owners[name] = Path(candidate).expanduser().resolve()
    shared_instances = sorted(
        name for name, candidate in owners.items() if candidate == project_root
    )
    if len(shared_instances) < 2:
        return ProbeResult(
            "shared-project-config-root",
            "pass",
            "project config root is not shared by multiple local instances",
            False,
        )

    repo_paths: tuple[str, ...] = ()
    config_path = project_root / "project-cortex.yaml"
    try:
        from .monitor.config import load_config
        from .monitor.scanner import scan_workspaces

        states = scan_workspaces(load_config(config_path=config_path))
        repo_paths = tuple(sorted({str(Path(state.path).resolve()) for state in states}))
    except (OSError, ValueError):
        pass
    impact = (
        f"repeated scan repos={len(repo_paths)} ({', '.join(repo_paths)})"
        if repo_paths
        else "repeated scan repos=unknown"
    )
    return ProbeResult(
        "shared-project-config-root",
        "warn",
        f"project config root {project_root} is shared by instances "
        f"{', '.join(shared_instances)}; {impact}",
        False,
    )


def _service_paths_probe(*, home: Path, instance: str, live: bool) -> ProbeResult:
    result, _effective = _service_environment_probe(
        home=home,
        instance=instance,
        live=live,
        base_env=os.environ,
    )
    return result


def _root_is_creatable(path: Path) -> bool:
    if not path.is_absolute() or path.is_symlink():
        return False
    if path.exists():
        return path.is_dir() and os.access(path, os.W_OK | os.X_OK)
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)


def _load_runtime_monitor_socket_path(env: Mapping[str, str]) -> Path:
    """Resolve custom/default socket through the production Monitor config loader."""
    from .monitor.config import load_config

    with patch.dict(os.environ, dict(env), clear=True):
        return Path(load_config().socket_path).expanduser()


def _request_runtime_monitor(socket_path: Path, payload: Mapping[str, object]) -> dict:
    """Use the production work API client; missing PR A fails closed."""
    from .monitor.work_api import MonitorSocketClient

    return MonitorSocketClient(socket_path=socket_path, timeout=2.0).request(payload)


def _monitor_path_probes(
    *,
    state_root: Path,
    socket_path: Path,
    live: bool,
) -> tuple[ProbeResult, ProbeResult]:
    state_root = Path(state_root).expanduser()
    if not state_root.is_absolute():
        state = ProbeResult("monitor-state", "fail", "monitor state root must be absolute", True)
    elif not _root_is_creatable(state_root):
        state = ProbeResult("monitor-state", "fail", "monitor state root is not writable/creatable", True)
    else:
        state = ProbeResult("monitor-state", "pass", "durable state root is writable/creatable", True)

    socket_path = Path(socket_path).expanduser()
    run_root = socket_path.parent
    if not socket_path.is_absolute():
        monitor_socket = ProbeResult("monitor-socket", "fail", "monitor socket root must be absolute", True)
    elif not socket_path_fits(socket_path):
        # #608：`sun_path` 只有 108 bytes。超限時 bind/connect 會失敗成一句
        # 沒有數字的 `OSError`，在 live probe 下會被記成「socket 沒在聽」——
        # 與「monitor 根本沒跑」無法區分。在 live probe 之前先量，讓 doctor
        # 直接說出這是路徑長度的環境限制，附實際 byte 數。
        monitor_socket = ProbeResult(
            "monitor-socket",
            "fail",
            socket_path_limit_detail(socket_path, role="monitor socket"),
            True,
        )
    elif not _root_is_creatable(run_root):
        monitor_socket = ProbeResult("monitor-socket", "fail", "monitor socket root is not writable/creatable", True)
    elif not live:
        monitor_socket = ProbeResult("monitor-socket", "warn", "socket connectivity not probed", False)
    else:
        try:
            response = _request_runtime_monitor(
                socket_path,
                {"kind": "list_work_items", "states": [], "include_done": False, "explain": False},
            )
        except OSError:
            monitor_socket = ProbeResult("monitor-socket", "fail", "monitor socket is not listening", True)
        except (ImportError, RuntimeError, ValueError):
            monitor_socket = ProbeResult("monitor-socket", "fail", "monitor work API probe failed", True)
        else:
            data = response.get("data") if isinstance(response, dict) else None
            if (
                not isinstance(response, dict)
                or response.get("ok") is not True
                or not isinstance(data, dict)
                or data.get("schema") != "cortex-work/v1"
                or not isinstance(data.get("items"), list)
            ):
                monitor_socket = ProbeResult("monitor-socket", "fail", "monitor work API protocol invalid", True)
            else:
                monitor_socket = ProbeResult("monitor-socket", "pass", "cortex-work/v1 read API ready", True)
    return state, monitor_socket


def _parse_included_github_response(raw: str) -> tuple[dict[str, object] | None, frozenset[str]]:
    normalized = raw.replace("\r\n", "\n")
    header_text = ""
    body = normalized
    if normalized.startswith("HTTP/") and "\n\n" in normalized:
        header_text, body = normalized.split("\n\n", 1)
    scopes: set[str] = set()
    for line in header_text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "x-oauth-scopes":
            scopes.update(item.strip().lower() for item in value.split(",") if item.strip())
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, frozenset(scopes)
    return (payload if isinstance(payload, dict) else None), frozenset(scopes)


def _github_write_capabilities_proven(
    payload: Mapping[str, object],
    scopes: frozenset[str],
) -> bool:
    permissions = payload.get("permissions")
    collaborator_write = isinstance(permissions, dict) and (
        permissions.get("push") is True or permissions.get("admin") is True
    )
    token_permissions = payload.get("token_permissions")
    fine_grained = isinstance(token_permissions, dict) and all(
        token_permissions.get(name) in {"write", "admin"}
        for name in ("contents", "issues", "pull_requests")
    )
    classic_scope = "repo" in scopes or (
        payload.get("private") is False and "public_repo" in scopes
    )
    return bool(collaborator_write and (fine_grained or classic_scope))


def run_doctor(
    *,
    probe_live: bool,
    repo: str | None = None,
    instance: str = "cortex",
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    runner: Runner = subprocess.run,
    agy_probe: AgyProbe | None = None,
) -> DoctorReport:
    environment = dict(os.environ if env is None else env)
    home_path = Path(home) if home is not None else Path(environment.get("HOME", str(Path.home())))
    service_probe, effective = _service_environment_probe(
        home=home_path,
        instance=instance,
        live=probe_live,
        base_env=environment,
    )
    agents_root = Path(effective["PSC_AGENTS_ROOT"]).expanduser()
    state_root = Path(effective["PSC_MONITOR_STATE_ROOT"]).expanduser()
    try:
        socket_path = _load_runtime_monitor_socket_path(effective)
    except (ImportError, OSError, ValueError):
        state_probe, _ignored_socket = _monitor_path_probes(
            state_root=state_root,
            socket_path=Path(effective["PSC_RUN_ROOT"]) / "project-monitor.sock",
            live=False,
        )
        socket_probe = ProbeResult(
            "monitor-socket",
            "fail",
            "production Monitor config did not resolve a socket path",
            True,
        )
    else:
        state_probe, socket_probe = _monitor_path_probes(
            state_root=state_root,
            socket_path=socket_path,
            live=probe_live,
        )
    probes: list[ProbeResult] = [
        _preflight_probe(effective),
        _gate_declaration_probe(effective),
        _identity_probe(effective, agents_root),
        _model_resolution_probe(effective, agents_root),
        _review_sandbox_probe(
            effective,
            agents_root,
            runner=runner,
            live=probe_live,
        ),
        service_probe,
        _repo_identity_probe(effective),
        _managed_path_drift_probe(effective, agents_root=agents_root, instance=instance),
        _shared_project_config_root_probe(effective, home=home_path, instance=instance),
        state_probe,
        socket_probe,
    ]
    if not probe_live:
        probes.extend(
            (
                ProbeResult("gh-auth", "warn", "live probe skipped", False),
                ProbeResult("gh-permissions", "warn", "live probe skipped", False),
                ProbeResult("auto-label", "warn", "live probe skipped", False),
                ProbeResult("agy", "warn", "live probe skipped", False),
            )
        )
        return DoctorReport(tuple(probes))
    if not _valid_repo(repo):
        probes.extend(
            (
                ProbeResult("gh-auth", "fail", "--repo owner/name is required", True),
                ProbeResult("gh-permissions", "fail", "repository unavailable", True),
                ProbeResult("auto-label", "fail", "repository label unavailable", True),
            )
        )
    else:
        probes.append(_gh_auth_probe(runner))
        repo_code, repo_stdout = _process(
            runner,
            ["gh", "api", "--include", f"repos/{repo}"],
        )
        permission = False
        if repo_code == 0:
            payload, scopes = _parse_included_github_response(repo_stdout)
            permission = payload is not None and _github_write_capabilities_proven(payload, scopes)
        probes.append(
            ProbeResult(
                "gh-permissions",
                "pass" if permission else "fail",
                (
                    "contents/issues/pull-requests write capabilities proven"
                    if permission
                    else "required write capabilities not proven"
                ),
                True,
            )
        )
        label_code, _ = _process(
            runner,
            ["gh", "api", f"repos/{repo}/labels/{quote(AUTO_LABEL, safe='')}"],
        )
        probes.append(
            ProbeResult("auto-label", "pass" if label_code == 0 else "fail", "auto label exists" if label_code == 0 else "auto label missing", True)
        )
    ready, _diagnostic = (agy_probe or _default_agy_probe)()
    probes.append(
        ProbeResult("agy", "pass" if ready else "fail", "safe plan/sandbox capability ready" if ready else "safe agy capability unavailable", True)
    )
    return DoctorReport(tuple(probes))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cortex doctor",
        description="檢查 unified lifecycle 的本機設定；--probe-live 會執行 gh、agy 與 Monitor socket probes。",
    )
    parser.add_argument(
        "--probe-live",
        action="store_true",
        help="執行 gh auth/permission/label、agy safe smoke 與 Monitor socket 連線",
    )
    parser.add_argument("--repo", help="GitHub owner/name；live probe 必填")
    parser.add_argument("--instance", default="cortex", help="systemd instance 前綴")
    parser.add_argument("--json", action="store_true", help="輸出 cortex-doctor/v1 JSON")
    args = parser.parse_args(argv)
    report = run_doctor(
        probe_live=args.probe_live,
        repo=args.repo,
        instance=args.instance,
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        for probe in report.probes:
            print(f"{probe.status.upper():4} {probe.name}: {probe.detail}")
    return 0 if report.ok else 1
