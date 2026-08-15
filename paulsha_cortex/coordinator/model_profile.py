"""#452 A/D：patchmud 一次性評測巷道（`cortex model profile` 的核心邏輯）。

選配、不在熱路徑：偵測不到 ``patchmud`` 可執行檔即印明確 skip 訊息並回 0。
只透過 CLI 邊界互動（``patchmud run``／``patchmud report``），本模組 MUST NOT
``import patchmud``。評測結果經 :func:`envelope_mapping.map_report_to_envelope`
換算後只產 diff 預覽；**經明確 ``--apply`` 才寫 registry**（#454 R3 人工複核閘），
且空 ``accepts_bands``（below-green-floor）絕不落 registry。

寫入目標是 packaged registry 檔（repo 內 ``data/model-identities.yaml``，operator
看 git diff 後自行 commit）——不可寫 custom overlay：``load_model_identities``
對 custom 覆蓋 packaged 同鍵身分會 raise shadow error。

一次性語意（#452 D）：評測指紋 ``(executor, model_id, persona, deck_id,
deck content_sha256, patchmud version)`` 存 ``profile_provenance.fingerprint``；
指紋未變→``already-profiled`` skip；``--force`` 重評。熱路徑（claim／dispatch／
resume／tick 的派工判定）永不同步觸發本模組。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .._yaml import safe_load
from .envelope_mapping import EnvelopeMappingError, map_report_to_envelope
from .model_identities import (
    ENVELOPE_FIELDS,
    IdentityRegistry,
    _load_model_identity_file,
    _packaged_registry_path,
    project_envelope,
)

PROFILE_SCHEMA = "cortex-model-profile/v1"

#: cortex 身分 → patchmud `--model` 參數值（短別名或完整 spec 皆可，patchmud
#: 端會 normalize）。patchmud 支援 anthropic HTTP／claude CLI
#: fallback（別名 sonnet/haiku/opus/fable）與 codex／agy OAuth headless CLI
#: adapter（paulsha-patchmud#14）。約束（cortex#466 A-2）：CLI adapter 的
#: reasoning effort 硬編 `high`，只有 high 檔位身分可對應；agy 用完整 spec
#: 而非 patchmud 短別名（別名表會演進）。copilot／cg 無 patchmud adapter，
#: 一律 per-identity skip（adapter-unavailable）維持 default，禁止假造可跑；
#: codex 身分待 #456 R4 登錄後再補格。
PATCHMUD_MODEL_ALIASES: Mapping[tuple[str, str], str] = {
    ("claude", "sonnet"): "sonnet",
    ("agy", "gemini-3.1-pro-high"): "agy:gemini-3.1-pro",
}

#: deck 可量測的 persona 維度：pilot-v1 只量 builder（#456 R7 分期註記；
#: planner／reviewer 題庫待 paulsha-patchmud#13）。未知 deck 保守視為僅 builder。
DECK_MEASURED_PERSONAS: Mapping[str, tuple[str, ...]] = {
    "pilot-v1": ("builder",),
}
_DEFAULT_MEASURED_PERSONAS = ("builder",)

#: persona → 所需 capability（與 manager._MODEL_CHAIN_CAPABILITY_BY_PERSONA 對齊）。
_PERSONA_CAPABILITY = {
    "planner": "planning",
    "builder": "build",
    "reviewer": "review",
}

DEFAULT_DECK_ID = "pilot-v1"
DEFAULT_LOADOUT = "P0T0R0"

_RATE_LIMIT_RE = re.compile(r"\b429\b|rate.?limit", re.IGNORECASE)
#: encounter provenance 的 pin 行（patchmud 機器寫入：頂層 64-hex，容許引號）。
_CONTENT_SHA_LINE_RE = re.compile(r"^content_sha256:\s*['\"]?([0-9a-f]{64})['\"]?\s*$")
_MAX_ENCOUNTER_ATTEMPTS = 3
_PLAIN_SCALAR_RE = re.compile(r"[A-Za-z][A-Za-z0-9._+:/@#-]*")

ProcessRunner = Callable[..., object]


def default_patchmud_root() -> Path:
    """本機約定路徑（issue #452 §A）：$HOME/prj_pri/paulsha-patchmud。"""

    return Path(os.environ.get("PSC_PATCHMUD_ROOT", str(Path.home() / "prj_pri" / "paulsha-patchmud")))


@dataclass(frozen=True)
class ProfileOptions:
    apply: bool = False
    force: bool = False
    deck_id: str = DEFAULT_DECK_ID
    loadout: str = DEFAULT_LOADOUT
    patchmud_bin: str | None = None
    patchmud_root: Path | None = None
    registry_file: Path | None = None
    identity_filter: tuple[str, ...] = field(default_factory=tuple)


def deck_content_sha256(deck_dir: Path) -> str:
    """deck 指紋＝聚合各 encounter `provenance.yaml` 的 `content_sha256`。

    與 patchmud 的 encounter pin 同語意（card + repo/** + hidden/**，排除快取
    與 reference_timings）。逐檔 rglob hash 會把 `patchmud validate-deck` 對
    reference_timings 的例行覆寫誤判成 deck 內容變更、誤觸全量重評
    （cortex#466 A-3）；provenance 缺漏 fail-closed。
    """

    digest = hashlib.sha256()
    for encounter in _encounter_dirs(deck_dir):
        provenance_path = encounter / "provenance.yaml"
        try:
            text = provenance_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(
                f"encounter provenance 不可讀：{provenance_path}（{type(exc).__name__}: {exc}）"
            ) from exc
        # 行掃描取 pin，不整份餵 subset YAML parser：provenance 含自由文字
        # 多行欄位（如 variant_notes 折行），超出零依賴 parser 的子集，實跑
        # pilot-v1 已踩到 fail-closed 誤觸。pin 行由 patchmud 機器寫入、
        # 格式固定（頂層 `content_sha256: <64 hex>`），恰一行才收。
        matches = [
            match.group(1)
            for line in text.splitlines()
            if (match := _CONTENT_SHA_LINE_RE.match(line))
        ]
        if len(matches) != 1:
            raise ValueError(
                f"encounter provenance 應恰含一行 content_sha256 pin"
                f"（實得 {len(matches)} 行）：{provenance_path}"
            )
        digest.update(encounter.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(matches[0].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _encounter_dirs(deck_dir: Path) -> list[Path]:
    return sorted(p for p in deck_dir.iterdir() if p.is_dir())


def _report_group_key(report: object) -> tuple[str, str]:
    """profile report 的聚合鍵 (model, loadout)：從 report 本身取，不猜測。

    patchmud PR #15 起 run.yaml 記 `normalize_model_spec()` 展開後的完整
    model spec——不是 CLI 別名，且 anthropic↔claude CLI fallback 隨執行當下
    憑證狀態浮動，別名查表必落空（cortex#466 A-1）。profile 的 runs_root 為
    單一身分專用，report 內必恰一組聚合鍵；多於一組＝runs_root 被污染，
    fail-closed。
    """

    if not isinstance(report, Mapping):
        raise ValueError("report 必須是 mapping")
    leaderboards = report.get("leaderboards")
    board = leaderboards.get("clear_rate") if isinstance(leaderboards, Mapping) else None
    rows = board.get("rows") if isinstance(board, Mapping) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("report 缺 leaderboards.clear_rate.rows")
    keys: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError(f"clear_rate row 必須是 mapping：{row!r}")
        model = row.get("model")
        loadout = row.get("loadout")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"clear_rate row 缺非空 model：{row!r}")
        if not isinstance(loadout, str) or not loadout.strip():
            raise ValueError(f"clear_rate row 缺非空 loadout：{row!r}")
        keys.add((model.strip(), loadout.strip()))
    if len(keys) != 1:
        raise ValueError(
            "profile report 應恰含一組 (model, loadout)，實得："
            + "; ".join(f"{model}/{loadout}" for model, loadout in sorted(keys))
        )
    return next(iter(keys))


def _emit_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if _PLAIN_SCALAR_RE.fullmatch(text) and text not in {"true", "false", "null"}:
        return text
    return json.dumps(text, ensure_ascii=False)


def _emit_mapping(lines: list[str], payload: Mapping[str, object], indent: int) -> None:
    pad = " " * indent
    for key, value in payload.items():
        if isinstance(value, Mapping):
            lines.append(f"{pad}{key}:")
            _emit_mapping(lines, value, indent + 2)
        elif isinstance(value, (list, tuple)):
            inner = ", ".join(_emit_scalar(item) for item in value)
            lines.append(f"{pad}{key}: [{inner}]")
        else:
            lines.append(f"{pad}{key}: {_emit_scalar(value)}")


_REGISTRY_HEADER = """\
# #452 B／#456 R3：候選身分 roster（capabilities 為候選宣告，benchmark 前）。
# 封套欄位「沒寫＝預設、有寫＝實測」（#453 R4：registry 永不寫入預設值）；
# 實測值由 `cortex model profile` 產 diff、經人工複核 --apply 後落地。
# 列序即候選優先序：agy 維持首位，保住既有 planner 熱路徑選擇不變。
"""


def render_registry_file(rows: Sequence[Mapping[str, object]], *, schema_version: int = 3) -> str:
    lines: list[str] = [_REGISTRY_HEADER.rstrip("\n"), f"schema_version: {schema_version}", "identities:"]
    key_order = (
        "executor",
        "model_id",
        "independence_domain",
        "capabilities",
        "live_probe",
        *ENVELOPE_FIELDS,
        "profile_provenance",
    )
    for row in rows:
        first = True
        for key in key_order:
            if key not in row or row[key] is None:
                continue
            value = row[key]
            prefix = "  - " if first else "    "
            first = False
            if isinstance(value, Mapping):
                lines.append(f"{prefix}{key}:")
                _emit_mapping(lines, value, 6)
            elif isinstance(value, (list, tuple)):
                inner = ", ".join(_emit_scalar(item) for item in value)
                lines.append(f"{prefix}{key}: [{inner}]")
            else:
                lines.append(f"{prefix}{key}: {_emit_scalar(value)}")
    return "\n".join(lines) + "\n"


def write_registry_file(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    """先寫暫存檔並以 loader round-trip 驗證，通過才原子取代（fail-closed）。"""

    text = render_registry_file(rows)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        _load_model_identity_file(tmp)
    except ValueError:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def _fingerprint(
    *, executor: str, model_id: str, persona: str, deck_id: str,
    deck_sha: str, patchmud_version: str,
) -> dict[str, str]:
    return {
        "executor": executor,
        "model_id": model_id,
        "persona": persona,
        "deck_id": deck_id,
        "deck_content_sha256": deck_sha,
        "patchmud_version": patchmud_version,
    }


def _existing_fingerprint(identity, persona: str) -> Mapping[str, object] | None:
    provenance = getattr(identity, "profile_provenance", None)
    if not isinstance(provenance, Mapping):
        return None
    fingerprint = provenance.get("fingerprint")
    if not isinstance(fingerprint, Mapping):
        return None
    if str(fingerprint.get("persona", "")).strip() != persona:
        return None
    return fingerprint


def _run_process(runner: ProcessRunner, argv: list[str]) -> tuple[int, str]:
    try:
        raw = runner(argv, shell=False, capture_output=True, text=True)
    except Exception as exc:  # noqa: BLE001 - 選配巷道 fail-soft，不拖垮呼叫端
        return 1, f"{type(exc).__name__}: {exc}"
    returncode = getattr(raw, "returncode", None)
    stdout = getattr(raw, "stdout", "") or ""
    stderr = getattr(raw, "stderr", "") or ""
    if not isinstance(returncode, int):
        return 1, "malformed process result"
    return returncode, f"{stdout}\n{stderr}"


def _run_encounter_with_backoff(
    runner: ProcessRunner,
    argv: list[str],
    *,
    sleep: Callable[[float], None],
) -> tuple[bool, str]:
    """單一 encounter 執行；429/rate-limit 訊號指數退避重試（#455 §4.2）。"""

    output = ""
    for attempt in range(_MAX_ENCOUNTER_ATTEMPTS):
        code, output = _run_process(runner, argv)
        if code == 0:
            return True, output
        if not _RATE_LIMIT_RE.search(output):
            return False, output
        if attempt + 1 < _MAX_ENCOUNTER_ATTEMPTS:
            sleep(float(2 ** (attempt + 1)))
    return False, output


def _profile_cells(registry: IdentityRegistry, measured_personas: tuple[str, ...]):
    """列出 (identity, persona) 目標格：capability 對應 persona × deck 可量測維度。"""

    for identity in registry.identities:
        for persona in ("planner", "builder", "reviewer"):
            if _PERSONA_CAPABILITY[persona] not in identity.capabilities:
                continue
            yield identity, persona, persona in measured_personas


def run_model_profile(
    options: ProfileOptions,
    *,
    runner: ProcessRunner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """執行 profile 巷道，回傳結構化結果（porcelain 層負責呈現）。

    回傳 dict：``schema``／``skip_reason``（patchmud 不在場時）／``patchmud``
    （bin、version、deck 指紋）／``cells``（逐格 status+reason+diff）／
    ``applied``／``registry_file``。
    """

    result: dict[str, object] = {
        "schema": PROFILE_SCHEMA,
        "skip_reason": None,
        "patchmud": None,
        "cells": [],
        "applied": False,
    }
    registry_file = options.registry_file or _packaged_registry_path()
    result["registry_file"] = str(registry_file)

    patchmud_bin = options.patchmud_bin or shutil.which("patchmud")
    if patchmud_bin is None:
        result["skip_reason"] = "patchmud-not-found"
        return result
    patchmud_root = options.patchmud_root or default_patchmud_root()
    version_file = Path(patchmud_root) / "VERSION"
    try:
        patchmud_version = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        patchmud_version = ""
    if not patchmud_version:
        result["skip_reason"] = "patchmud-version-unresolvable"
        return result
    deck_dir = Path(patchmud_root) / "decks" / options.deck_id
    if not deck_dir.is_dir():
        raise ValueError(f"deck 目錄不存在：{deck_dir}")
    encounters = _encounter_dirs(deck_dir)
    if not encounters:
        raise ValueError(f"deck 目錄無任何 encounter：{deck_dir}")
    deck_sha = deck_content_sha256(deck_dir)
    measured_personas = DECK_MEASURED_PERSONAS.get(options.deck_id, _DEFAULT_MEASURED_PERSONAS)
    deck_info = {
        "deck_id": options.deck_id,
        "content_sha256": deck_sha,
        "encounter_count": len(encounters),
        "measured_personas": list(measured_personas),
    }
    result["patchmud"] = {
        "bin": str(patchmud_bin),
        "version": patchmud_version,
        "root": str(patchmud_root),
        "deck": dict(deck_info),
    }

    registry = _load_model_identity_file(Path(registry_file))
    # #452 對抗審查修正：--identity 打錯字時不得靜默產出零 cells 讓操作者誤信
    # 「全部已評測完」——查無對應身分即明確報錯（porcelain 層以 exit 2 呈現）；
    # `/` 與 `:` 兩種拼法都接受（與 build_capability_lookup 的解析一致）。
    identity_filter: tuple[str, ...] = ()
    if options.identity_filter:
        known_labels = {
            f"{identity.executor}/{identity.model_id}" for identity in registry.identities
        }
        normalized: list[str] = []
        unknown: list[str] = []
        for raw in options.identity_filter:
            text = str(raw).strip()
            label = text if "/" in text else text.replace(":", "/", 1)
            if label not in known_labels:
                unknown.append(text)
            normalized.append(label)
        if unknown:
            raise ValueError(
                "--identity 查無對應身分："
                + ", ".join(unknown)
                + "（registry 內可用："
                + ", ".join(sorted(known_labels))
                + "；接受 executor/model_id 或 executor:model_id 拼法）"
            )
        identity_filter = tuple(normalized)
    rows = [identity.to_dict() for identity in registry.identities]
    row_index = {
        (str(row["executor"]), str(row["model_id"])): position
        for position, row in enumerate(rows)
    }
    current_text = render_registry_file(rows)
    cells: list[dict[str, object]] = []
    applied_any = False
    profiled_at = now() if now is not None else datetime.now(timezone.utc)
    timestamp = profiled_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    run_stamp = profiled_at.strftime("%Y%m%dT%H%M%SZ")

    for identity, persona, persona_measurable in _profile_cells(registry, measured_personas):
        label = f"{identity.executor}/{identity.model_id}"
        if identity_filter and label not in identity_filter:
            continue
        cell: dict[str, object] = {
            "executor": identity.executor,
            "model_id": identity.model_id,
            "persona": persona,
        }
        cells.append(cell)
        if not persona_measurable:
            # deck 量不到這個 persona 維度（pilot-v1 只量 builder）：誠實維持
            # default，不假跑（#454 R1 persona-dimension-unmeasured）。
            cell["status"] = "skipped"
            cell["reason"] = "persona-dimension-unmeasured"
            continue
        existing = _existing_fingerprint(identity, persona)
        fingerprint = _fingerprint(
            executor=identity.executor,
            model_id=identity.model_id,
            persona=persona,
            deck_id=options.deck_id,
            deck_sha=deck_sha,
            patchmud_version=patchmud_version,
        )
        if existing is not None and dict(existing) == fingerprint and not options.force:
            cell["status"] = "already-profiled"
            cell["reason"] = "fingerprint-unchanged"
            continue
        alias = PATCHMUD_MODEL_ALIASES.get((identity.executor, identity.model_id))
        if alias is None:
            # 誠實約束：本身分沒有可驅動的 patchmud model spec 對應。
            cell["status"] = "skipped"
            cell["reason"] = "adapter-unavailable"
            cell["detail"] = (
                "PATCHMUD_MODEL_ALIASES 無此身分對應（copilot／cg 無 patchmud "
                "adapter；CLI adapter effort 硬編 high，非 high 檔位不可對應）；"
                "本身分維持 default 封套"
            )
            continue

        # cortex#466 A-4：run 封存落耐久位置（比照 #455 實測慣例：patchmud
        # repo runs/，不進版控），registry 的 provenance 才能回溯到 events／
        # ledger／replay 證據；mkdtemp 用完即棄會讓封套值失去出處。
        base_runs_root = (
            Path(patchmud_root)
            / "runs"
            / f"profile-{identity.executor}-{identity.model_id}-{run_stamp}"
        )
        runs_root = base_runs_root
        suffix = 1
        while runs_root.exists():
            suffix += 1
            runs_root = base_runs_root.with_name(f"{base_runs_root.name}-{suffix}")
        runs_root.mkdir(parents=True)
        report_root = runs_root / "report"
        cell["runs_root"] = str(runs_root)
        failures: list[str] = []
        for encounter_dir in encounters:
            ok, output = _run_encounter_with_backoff(
                runner,
                [
                    str(patchmud_bin),
                    "run",
                    str(encounter_dir),
                    "--model",
                    alias,
                    "--loadout",
                    options.loadout,
                    "--runs-root",
                    str(runs_root),
                    "--run-id",
                    f"profile-{identity.executor}-{identity.model_id}-{encounter_dir.name}",
                ],
                sleep=sleep,
            )
            if not ok:
                failures.append(f"{encounter_dir.name}: {output.strip()[:200]}")
        if failures:
            # 先掛上逐關失敗明細：report 若因 run 全滅而失敗，操作者才看得到
            # 根因（實跑 e2e 驗證發現的可觀測性缺口——429 全滅時只剩 report
            # glob 錯誤，看不出是限流）。
            cell["encounter_failures"] = failures
        report_code, report_output = _run_process(
            runner,
            [
                str(patchmud_bin),
                "report",
                "--runs",
                str(runs_root / "*"),
                "--out",
                str(report_root),
            ],
        )
        if report_code != 0:
            cell["status"] = "failed"
            cell["reason"] = "report-failed"
            cell["detail"] = report_output.strip()[:400]
            continue
        # 機器契約優先（paulsha-patchmud#26）：report.json 零歧義；無檔時退回
        # subset YAML parser（相容舊版 patchmud——PyYAML 的長 scalar 折行
        # 形狀仍可能超出子集，實跑驗證已踩過，升級 patchmud 即免疫）。
        json_path = report_root / "report.json"
        yaml_path = report_root / "report.yaml"
        try:
            if json_path.is_file():
                report = json.loads(json_path.read_text(encoding="utf-8"))
            else:
                report = safe_load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            # json.JSONDecodeError 與 YAMLError 皆為 ValueError 子類。
            cell["status"] = "failed"
            cell["reason"] = "report-unreadable"
            cell["detail"] = f"{type(exc).__name__}: {exc}"
            continue
        try:
            # cortex#466 A-1：聚合鍵從 report 本身取（run.yaml 記 normalize
            # 後的完整 spec，別名查表必落空且隨憑證狀態浮動）。
            report_model, report_loadout = _report_group_key(report)
        except ValueError as exc:
            cell["status"] = "failed"
            cell["reason"] = "report-group-ambiguous"
            cell["detail"] = str(exc)
            continue
        try:
            mapping = map_report_to_envelope(
                report,
                executor=identity.executor,
                model_id=identity.model_id,
                persona=persona,
                deck=deck_info,
                patchmud_version=patchmud_version,
                report_model=report_model,
                report_loadout=report_loadout,
            )
        except EnvelopeMappingError as exc:
            cell["status"] = "failed"
            cell["reason"] = "mapping-rejected"
            cell["detail"] = str(exc)
            continue
        provenance = mapping["provenance"]
        # A-4：觀測記錄帶 run 封存出處（observation 為自由欄，loader 不設鍵白名單）。
        observation = {**dict(provenance["observation"]), "runs_root": str(runs_root)}
        cell["observation"] = observation
        cell["mapping_reasons"] = dict(provenance["reasons"])
        if not provenance["registry_writable"]:
            # 空的實測 accepts_bands（below-green-floor）或全 default 結果不得
            # 落 registry（#454 R3；#209 R2「非空」契約）——人工裁決除名或維持。
            cell["status"] = "not-writable"
            cell["reason"] = str(provenance["reasons"].get("accepts_bands"))
            continue

        proposed_rows = [dict(row) for row in rows]
        target = proposed_rows[row_index[(identity.executor, identity.model_id)]]
        for field_name in ENVELOPE_FIELDS:
            if provenance["source"].get(field_name) == "measured":
                value = mapping["envelope"][field_name]
                target[field_name] = list(value) if isinstance(value, (list, tuple)) else value
            else:
                target.pop(field_name, None)
        target["profile_provenance"] = {
            "fingerprint": dict(provenance["fingerprint"]),
            "source": dict(provenance["source"]),
            "reasons": dict(provenance["reasons"]),
            "observation": dict(observation),
            "profiled_at": timestamp,
        }
        proposed_text = render_registry_file(proposed_rows)
        diff = "".join(
            difflib.unified_diff(
                current_text.splitlines(keepends=True),
                proposed_text.splitlines(keepends=True),
                fromfile=str(registry_file),
                tofile=f"{registry_file}（proposed）",
            )
        )
        cell["diff"] = diff
        cell["envelope"] = {
            key: (list(value) if isinstance(value, (list, tuple)) else value)
            for key, value in mapping["envelope"].items()
        }
        if options.apply:
            write_registry_file(Path(registry_file), proposed_rows)
            rows = proposed_rows
            current_text = proposed_text
            applied_any = True
            cell["status"] = "applied"
        else:
            cell["status"] = "proposed"
        cell["reason"] = str(provenance["reasons"].get("accepts_bands"))

    result["cells"] = cells
    result["applied"] = applied_any
    return result


def envelope_display_rows(registry: IdentityRegistry) -> list[dict[str, object]]:
    """`cortex inspect models` 的顯示資料：每身分 × persona 的封套投影＋來源。

    #534：加上 ``resolution_layer``（operator-overlay／evaluated-roster／
    packaged-fallback／parked），讓 operator 一眼看出每顆模型憑什麼進熱路徑。
    """

    from . import model_resolution

    context = registry.resolution_context
    display: list[dict[str, object]] = []
    for identity in registry.identities:
        for persona in ("planner", "builder", "reviewer"):
            if _PERSONA_CAPABILITY[persona] not in identity.capabilities:
                continue
            resolution_layer = model_resolution.identity_layer(
                identity,
                role=model_resolution.role_for_persona(persona),
                eval_roster=context.eval_roster,
            ) or "parked"
            projection = project_envelope(identity, persona)
            provenance_summary: dict[str, object] | None = None
            if projection.provenance is not None:
                fingerprint = projection.provenance.get("fingerprint")
                provenance_summary = {
                    "fingerprint": dict(fingerprint) if isinstance(fingerprint, Mapping) else None,
                    "profiled_at": projection.provenance.get("profiled_at"),
                }
            display.append(
                {
                    "executor": identity.executor,
                    "model_id": identity.model_id,
                    "independence_domain": identity.independence_domain,
                    "persona": persona,
                    "resolution_layer": resolution_layer,
                    "envelope": {
                        key: (list(value) if isinstance(value, (list, tuple)) else value)
                        for key, value in projection.envelope.items()
                    },
                    "source": dict(projection.source),
                    "provenance": provenance_summary,
                }
            )
    return display
