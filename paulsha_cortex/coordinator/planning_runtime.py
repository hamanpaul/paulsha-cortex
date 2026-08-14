from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable, Mapping

from .launcher import build_agy_argv
from .model_identities import (
    AGY_MODEL_ID,
    CapabilityProbe,
    IdentityRegistry,
    ModelIdentity,
    load_model_identities,
    probe_agy_capability,
)
from .planning import required_heading_hint


@dataclass(frozen=True)
class ProductionPlanningRuntime:
    identity_registry: IdentityRegistry
    probes: Mapping[tuple[str, str], CapabilityProbe]
    primary_questioner: Callable[[Mapping[str, object]], object]
    secondary_planner: Callable[[Mapping[str, object], ModelIdentity], object]
    primary_integrator: Callable[[Mapping[str, object], Mapping[str, object]], object]


def _planning_argv(identity: ModelIdentity, prompt: str, temp_dir: str, worktree: Path) -> list[str]:
    if identity.executor == "agy":
        return build_agy_argv(
            prompt=prompt,
            slice_id="cortex-planning-runtime",
            log_dir=temp_dir,
            worktree=str(worktree),
            allow_unsafe=False,
            model=identity.model_id,
        )
    if identity.executor == "codex":
        return [
            "codex", "exec", prompt, "--json", "--sandbox", "read-only",
            "--model", identity.model_id, "-o", str(Path(temp_dir) / "last.json"),
            "-C", str(worktree), "--skip-git-repo-check",
        ]
    if identity.executor == "claude":
        # issue #404：刻意不帶 `--permission-mode plan`——plan 模式的系統
        # 提示要求模型「必須產出計畫或呼叫 ExitPlanMode」，與這裡「必須
        # 回傳純 JSON」的確定性回聲任務直接衝突（issue 404 實測矩陣：兩者
        # 同時存在時，模型會以「須先給一份計畫」為由拒絕直接回 JSON）。
        # 安全層改由其他機制共同承擔：`--tools ""` 讓模型完全沒有工具可
        # 呼叫；`_invoke_json` 的一次性 disposable sandbox 讓任何輸出頂多
        # 落在拋棄式複本；operator 樹在呼叫前後各做一次 `_tree_snapshot`
        # 比對，任何 operator 內容變化一律 fail-closed 並回滾；`_invoke_json`
        # 另外對 claude 身分注入 hermetic `CLAUDE_CONFIG_DIR`，同時隔離
        # operator 帳號下的 user MCP servers／plugins／hooks／使用者層
        # CLAUDE.md，避免這些注入項讓模型敘事跑題或繞過純 JSON 契約。
        return [
            "claude", "-p", prompt, "--output-format", "json",
            "--tools", "", "--model", identity.model_id,
            "--add-dir", str(worktree),
        ]
    raise ValueError(f"unsupported read-only planning executor: {identity.executor}")


def _tree_snapshot(root: Path) -> str:
    """Hash the complete tree shape, content, links, and stable metadata.

    The planner runs in a disposable copy, but the operator checkout is also
    hashed before and after launch.  This catches direct writes through an
    absolute path even when the planner exits non-zero.
    """

    digest = hashlib.sha256()

    def add_metadata(path: Path) -> os.stat_result:
        metadata = path.lstat()
        digest.update(f"{metadata.st_mode}:{metadata.st_uid}:{metadata.st_gid}".encode())
        digest.update(b"\0")
        try:
            names = sorted(os.listxattr(path, follow_symlinks=False))
        except (AttributeError, OSError):
            names = []
        for name in names:
            digest.update(name.encode("utf-8", errors="surrogateescape"))
            digest.update(b"=")
            try:
                digest.update(os.getxattr(path, name, follow_symlinks=False))
            except OSError:
                digest.update(b"<unreadable>")
            digest.update(b"\0")
        return metadata

    def visit(path: Path, relative: Path) -> None:
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        metadata = add_metadata(path)
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(b"link\0")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif stat.S_ISDIR(metadata.st_mode):
            digest.update(b"dir\0")
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                if child.name == ".git":
                    continue
                # issue #397：本機部署常見拓撲是 daemon 與 planning launcher
                # 共用同一棵 operator 工作樹（daemon 以 repo 為
                # WorkingDirectory 常駐），daemon 對既有模組的 lazy import
                # 會隨時在快照窗口內重編 `__pycache__/*.pyc`。這是可由原始碼
                # 100% 重生的 bytecode 快取、不是 operator 內容，計入雜湊會把
                # 這種正常 churn 誤判成「planner 汙染 operator worktree」而
                # fail-closed 到整段 raise + rollback。跳過的盲點取捨：CPython
                # 的 .pyc 是 timestamp/hash-based 驗證（PEP 552），植入的孤兒
                # .pyc 若與對應 .py 的 mtime／hash 不符會被直接忽略重新編譯，
                # 不會被 import 採用，因此跳過雜湊不會讓惡意 bytecode 有機可乘
                # ——真正的程式碼污染仍必須經過 .py／其他原始檔變更，那些不受
                # 此例外規則影響，fail-closed 行為維持不變。
                if child.name == "__pycache__" or child.name.endswith(".pyc"):
                    continue
                # issue #399：`.gitignore:8` 的 `/runtime/` 是 manager daemon
                # 以 repo 為 WorkingDirectory 常駐時的狀態殘留（例如
                # `runtime/handoff/wf-*.json` 每個 periodic tick 都會被整份
                # 重寫，內容含時間戳必變，issue #373 的迴圈使其每 ~55 秒
                # 必然發生一次）。它不受版控，verification gate 是讀 git
                # diff 來判斷候選檔案，gitignored 的內容本就不會進候選
                # 清單，跳過它的雜湊盲點可控——真正的程式碼污染必須經過
                # git 追蹤的檔案，不受此例外規則影響，fail-closed 行為維持
                # 不變。只跳過快照 root 直下的 `runtime/`（用 relative path
                # 判斷，而非只比對 dir name），避免誤跳深層同名目錄（例如
                # `tests/fixtures/runtime/`）。
                if relative == Path(".") and child.name == "runtime":
                    continue
                visit(child, relative / child.name)
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"file\0")
            digest.update(path.read_bytes())
        else:
            digest.update(b"special\0")
            digest.update(str(metadata.st_rdev).encode())
        digest.update(b"\0")

    visit(root, Path("."))
    return digest.hexdigest()


def _copy_planning_sandbox(worktree: Path, destination: Path) -> None:
    # issue #397：sandbox 是拋棄式複本，bytecode 可由原始碼重生、不必複製；
    # 排除它同時避免 copytree 過程中 daemon 正在改寫／汰換 __pycache__ 內容
    # 造成 race read（複製到一半 .pyc 消失或被截斷）引發與 planner 汙染無關
    # 的例外。
    pycache_ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc")

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = set(pycache_ignore(directory, names))
        # issue #399：與 `_tree_snapshot` 同語意排除 worktree root 直下的
        # `/runtime/`（daemon 狀態殘留，見該函式內註解）；sandbox 不需要
        # 這份內容，同時避免複製途中 daemon 正在改寫 handoff 檔案造成
        # race read。`shutil.ignore_patterns` 是按名稱全樹匹配，若直接
        # 加入 "runtime" pattern 會連深層同名目錄（例如
        # `pkg/runtime/`）一併誤殺，因此改用自訂 callable，只在走訪到
        # worktree 根目錄時才把 "runtime" 加進忽略清單。
        if Path(directory) == worktree and "runtime" in names:
            ignored.add("runtime")
        return ignored

    shutil.copytree(
        worktree,
        destination,
        symlinks=True,
        ignore=ignore,
    )


def _make_tree_traversable(root: Path) -> None:
    """Restore enough owner access to inspect and replace a hostile tree.

    The launcher can chmod directories through an absolute path.  Never follow
    symlinks while recovering access; the immutable baseline restores the
    original metadata after the polluted entries have been removed.
    """

    if root.is_symlink():
        raise RuntimeError("planning recovery root cannot be a symlink")
    os.chmod(root, 0o700, follow_symlinks=False)

    def visit(directory: Path) -> None:
        os.chmod(directory, 0o700, follow_symlinks=False)
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name == ".git" or entry.is_symlink():
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    visit(path)

    visit(root)


def _restore_operator_tree(worktree: Path, baseline: Path) -> None:
    _make_tree_traversable(worktree)
    for child in worktree.iterdir():
        if child.name == ".git":
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child)
    for source in baseline.iterdir():
        target = worktree / source.name
        if source.is_symlink():
            target.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
        elif source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)
    shutil.copystat(baseline, worktree, follow_symlinks=False)
    if _tree_snapshot(worktree) != _tree_snapshot(baseline):
        raise RuntimeError("planning operator restore verification failed")


_FENCED_JSON = re.compile(
    r"```(?:json)?\s*\n(?P<body>\{.*\})\s*\n```",
    flags=re.DOTALL | re.IGNORECASE,
)

# issue #401 巢狀欄位名——CLI launcher（claude/codex/agy）的成功 envelope
# 用來裝「模型實際輸出」的欄位名不一而足，沿用既有偵測順序。
_ENVELOPE_KEYS = ("result", "content", "message", "text")

# issue #401：questioner／integrator／secondary planner 的 prompt 過去只用
# 「Return only ... JSON」這類軟性措辭，模型（實測 sonnet 對 questioner
# prompt）偶爾仍回散文推理夾雜 JSON，甚至純散文。附加這段明確的輸出契約，
# 降低模型不遵守純 JSON 格式的機率；即使模型仍不遵守，`_extract_json` 也
# 已改為 fail-closed（見上）而非把 CLI envelope 誤當輸出本體。
_JSON_OUTPUT_CONTRACT = (
    "Output contract: reply with exactly one JSON object and nothing else — "
    "no prose, no explanation, no code fences. Your reply MUST start with '{'."
)


def _find_json_object(text: str, *, allow_partial: bool = False) -> object | None:
    """從字串中盡量抽出一個 JSON 物件；找不到回傳 ``None``（不拋例外）。

    - 先去除整段以 ```/```json 包裹的 code fence（沿用既有 regex，只接受
      「整串」剛好是單一 fenced code block 的情形）。
    - 再嘗試對整串做 `json.loads`。
    - `allow_partial=True` 時才進一步做**平衡大括號掃描**：找到第一個
      `{`，逐字元計數 `{`/`}` 深度（用簡單狀態機忽略字串字面值內、含跳脫
      序列 `\"`/`\\` 的大括號），抓出第一個平衡區塊後再嘗試 `json.loads`。
      這個平衡掃描刻意只在 `allow_partial=True` 時啟用——只給「從散文中
      抽取內嵌 JSON」的呼叫端使用（見 `_extract_json` 對 envelope 巢狀
      欄位的處理）。頂層候選字串（CLI 原始 stdout／`--output-file` 內容）
      必須維持既有的嚴格「整串才算」語意，否則像
      `"Commentary.\\n```json\\n{...}\\n```\\n"` 這種帶前言的輸出會被
      誤判為合法 JSON，弱化既有防呆
      （見 `test_planning_json_parser_accepts_only_whole_fenced_object`）。
    """
    text = text.strip()
    fenced = _FENCED_JSON.fullmatch(text)
    if fenced is not None:
        text = fenced.group("body")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    if not allow_partial:
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                block = text[start : index + 1]
                try:
                    return json.loads(block)
                except (json.JSONDecodeError, TypeError):
                    return None
    return None


def _extract_json(stdout: str, output_path: Path) -> object:
    candidates = [stdout.strip()]
    if output_path.is_file():
        candidates.insert(0, output_path.read_text(encoding="utf-8").strip())
    for candidate in candidates:
        value = _find_json_object(candidate)
        if not isinstance(value, dict):
            continue
        if any(key in value for key in _ENVELOPE_KEYS):
            # issue #401：這是 CLI launcher 的成功 envelope（例如 claude CLI
            # 的 `api_error_status` 等 20+ 鍵），不是模型輸出本體。模型有時
            # 不遵守「只回 JSON」的指示、在 envelope 的巢狀欄位裡回散文推理
            # （內容可能正確、格式不從）。依序嘗試每個巢狀欄位：先整串
            # `json.loads`，失敗再從散文中抽取內嵌 JSON 物件；任何一個成功
            # 就回傳抽出的物件。
            fallback_snippet: str | None = None
            for key in _ENVELOPE_KEYS:
                nested = value.get(key)
                if not isinstance(nested, str):
                    continue
                extracted = _find_json_object(nested, allow_partial=True)
                if extracted is not None:
                    return extracted
                if fallback_snippet is None:
                    fallback_snippet = nested[:160]
            # 全部抽取失敗：絕不能 fall through 把整個 envelope dict 當成
            # 輸出本體回傳（修復前的行為）——那會讓下游驗證（例如
            # `validate_question_pack`）報出 `unexpected key: api_error_status`
            # 這種完全誤導的診斷。改為明確 raise，訊息帶散文片段方便除錯。
            detail = fallback_snippet if fallback_snippet is not None else "<no string field>"
            raise ValueError(f"planning launcher result is not JSON: {detail}")
        return value
    # 2026-08-14 實測：agy 服務暫時性 503 時**印錯誤文字但 exit 0**
    # （`Error: Eligibility check failed: UNAVAILABLE (code 503)`），launcher
    # 因此走到這裡。修法前這行不帶任何 stdout 內容，錯誤文字隨 temp_dir 一起
    # 被丟棄——operator 只看得到「no JSON object」，診斷得靠手動重現。帶上
    # 截斷片段後：(1) 503 當場可見；(2) 上游 `_is_planning_transient_service_failure`
    # 能據此把分類從 `content` 改判 `environment`，recover-planning 才有路。
    snippet = next(
        (candidate[:160] for candidate in candidates if candidate),
        "<empty output>",
    )
    raise ValueError(f"planning launcher returned no JSON object: {snippet}")


def _seed_hermetic_claude_env(temp_dir: str) -> dict[str, str] | None:
    """issue #404：拿掉 `--permission-mode plan` 後，claude 呼叫若不做任何
    額外隔離，會直接繼承 operator `~/.claude`（superpowers plugin、記憶
    hooks、user 層 CLAUDE.md、user MCP servers 全部注入），讓 planning 呼叫
    的模型輸出摻雜與本次規劃無關的敘事。改為在本次呼叫專用的 tempdir 下
    建一個一次性 hermetic config 目錄，只播種登入所需的 credentials，藉此
    同時隔離上述注入項，但不影響登入態。

    查無登入憑證（`~/.claude/.credentials.json` 不存在）時不代為猜測——
    回傳 ``None``，維持不設 `CLAUDE_CONFIG_DIR`，讓 claude CLI 依原生行為
    自行回報 not logged in。`--bare` 與空的 `CLAUDE_CONFIG_DIR` 都會直接
    弄丟登入態（issue 404 實測矩陣已驗證不可用），因此缺檔時不得改用空
    目錄頂替，只能整組跳過。
    """

    source_credentials = Path.home() / ".claude" / ".credentials.json"
    if not source_credentials.is_file():
        return None
    config_dir = Path(temp_dir) / "claude-config"
    config_dir.mkdir()
    config_dir.chmod(0o700)
    destination_credentials = config_dir / ".credentials.json"
    shutil.copy2(source_credentials, destination_credentials)
    destination_credentials.chmod(0o600)
    return {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}


def _invoke_json(
    identity: ModelIdentity,
    prompt: str,
    *,
    worktree: Path,
    runner: Callable[..., object],
    timeout_seconds: int,
) -> object:
    operator_before = _tree_snapshot(worktree)
    with tempfile.TemporaryDirectory(prefix="cortex-planning-") as temp_dir:
        baseline = Path(temp_dir) / "baseline"
        sandbox = Path(temp_dir) / "checkout"
        _copy_planning_sandbox(worktree, baseline)
        shutil.copytree(baseline, sandbox, symlinks=True)
        sandbox_before = _tree_snapshot(sandbox)
        output_path = Path(temp_dir) / "last.json"
        argv = _planning_argv(identity, prompt, temp_dir, sandbox)
        run_kwargs: dict[str, object] = {}
        if identity.executor == "claude":
            # 僅 claude 路徑帶 env 覆寫；其他 executor（codex/agy）維持不帶，
            # 避免行為外溢。
            env = _seed_hermetic_claude_env(temp_dir)
            if env is not None:
                run_kwargs["env"] = env
        failure: BaseException | None = None
        result: object | None = None
        try:
            raw = runner(
                argv,
                cwd=str(sandbox),
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                **run_kwargs,
            )
            returncode = getattr(raw, "returncode", None)
            stdout = getattr(raw, "stdout", None)
            if returncode != 0 or not isinstance(stdout, str):
                raise ValueError(
                    f"planning launcher failed: {identity.executor}/{identity.model_id}"
                )
            result = _extract_json(stdout, output_path)
        except BaseException as exc:
            failure = exc
        finally:
            try:
                sandbox_dirty = _tree_snapshot(sandbox) != sandbox_before
            except BaseException:
                sandbox_dirty = True
                try:
                    _make_tree_traversable(sandbox)
                except BaseException:
                    pass
            if sandbox_dirty:
                failure = ValueError("planning launcher modified disposable read-only sandbox")

            operator_dirty = False
            try:
                operator_dirty = _tree_snapshot(worktree) != operator_before
            except BaseException:
                operator_dirty = True
            if operator_dirty:
                try:
                    _restore_operator_tree(worktree, baseline)
                except BaseException as exc:
                    failure = RuntimeError("planning operator restore failed")
                    failure.__cause__ = exc
                else:
                    failure = ValueError(
                        "planning launcher modified operator worktree; changes rolled back"
                    )
        if failure is not None:
            raise failure
        return result


def _probe_identity(
    identity: ModelIdentity,
    *,
    worktree: Path,
    runner: Callable[..., object],
    timeout_seconds: int,
) -> CapabilityProbe:
    expected = {
        "capability": "cortex-planning-json",
        "executor": identity.executor,
        "model": identity.model_id,
    }
    prompt = "Return only this JSON object and do not call tools: " + json.dumps(
        expected, ensure_ascii=False, separators=(",", ":")
    )
    try:
        value = _invoke_json(
            identity,
            prompt,
            worktree=worktree,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        return CapabilityProbe(
            False,
            identity.executor,
            identity.model_id,
            identity.independence_domain,
            "safe-probe-failed",
            type(exc).__name__,
        )
    if value != expected:
        return CapabilityProbe(
            False,
            identity.executor,
            identity.model_id,
            identity.independence_domain,
            "identity-mismatch",
        )
    return CapabilityProbe.ready_for(
        identity.executor, identity.model_id, identity.independence_domain
    )


def _planning_source_material(
    pack: Mapping[str, object], *, root: Path, max_bytes: int = 262_144
) -> dict[str, str]:
    questions = pack.get("questions")
    if not isinstance(questions, list):
        raise ValueError("planning question pack has no questions")
    refs = sorted(
        {
            ref
            for question in questions
            if isinstance(question, dict)
            for ref in question.get("source_refs", [])
            if isinstance(ref, str)
        }
    )
    material: dict[str, str] = {}
    total = 0
    for ref in refs:
        pure = PurePosixPath(ref)
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != ref:
            raise ValueError("planning source ref is not canonical repo-relative")
        current = root
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("planning source ref traverses symlink")
        try:
            target = current.resolve(strict=True)
            target.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError("planning source ref is unavailable") from exc
        if not target.is_file():
            raise ValueError("planning source ref is not a file")
        try:
            body = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("planning source ref is unreadable") from exc
        total += len(body.encode("utf-8"))
        if total > max_bytes:
            raise ValueError("planning source material exceeds bounded context")
        material[ref] = body
    return material


def _planning_destinations(pack: Mapping[str, object]) -> dict[str, str]:
    questions = pack.get("questions")
    if not isinstance(questions, list):
        return {}
    slugs = {
        parts[2]
        for question in questions if isinstance(question, dict)
        for ref in question.get("source_refs", [])
        if isinstance(ref, str)
        and (parts := PurePosixPath(ref).parts)[:2] == ("openspec", "changes")
        and len(parts) >= 4
    }
    if not slugs:
        # #408：small-fix 等無 openspec-propose 卡的 combo，work item 錨點是
        # workstream todo（docs/superpowers/workstreams/<slug>/todo.md）——
        # 沒有這個 fallback 時 destinations 恆為空，integrator 被要求
        # 「Use the supplied destination paths」卻拿到空 dict，只能自行發明
        # 路徑、必被 _publish_planning_artifacts 的 governed-roots 驗證拒收。
        # openspec 錨點優先；兩者皆無或歧義（多 slug）維持空 dict 的
        # 既有 fail-closed 行為。
        slugs = {
            parts[3]
            for question in questions if isinstance(question, dict)
            for ref in question.get("source_refs", [])
            if isinstance(ref, str)
            and (parts := PurePosixPath(ref).parts)[:3]
            == ("docs", "superpowers", "workstreams")
            and len(parts) >= 5
            and parts[4] == "todo.md"
        }
    if len(slugs) != 1:
        return {}
    slug = next(iter(slugs))
    return {
        "spec": f"docs/superpowers/specs/{slug}-spec.md",
        "design": f"docs/superpowers/specs/{slug}-design.md",
        "plan": f"docs/superpowers/plans/{slug}.md",
    }


def build_production_planning_runtime(
    *,
    primary: tuple[str, str],
    worktree: str | Path,
    runner: Callable[..., object] = subprocess.run,
    timeout_seconds: int = 120,
) -> ProductionPlanningRuntime:
    """Build the daemon's real, safe, heterogeneous planning adapters."""

    root = Path(worktree).resolve()
    registry = load_model_identities()
    probes: dict[tuple[str, str], CapabilityProbe] = {}
    for identity in registry.identities:
        if "planning" not in identity.capabilities:
            continue
        if identity.executor == "agy" and identity.model_id == AGY_MODEL_ID:
            probes[(identity.executor, identity.model_id)] = probe_agy_capability(
                runner=runner, timeout_seconds=min(timeout_seconds, 45)
            )
        else:
            probes[(identity.executor, identity.model_id)] = _probe_identity(
                identity,
                worktree=root,
                runner=runner,
                timeout_seconds=timeout_seconds,
            )

    primary_identity = registry.get(*primary)

    def invoke_primary(prompt: str) -> object:
        if primary_identity is None:
            raise ValueError("primary planning identity is not configured")
        return _invoke_json(
            primary_identity,
            prompt,
            worktree=root,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )

    def questioner(report: Mapping[str, object]) -> object:
        return invoke_primary(
            "Return only the exact question-pack JSON required to resolve this completeness report. "
            + _JSON_OUTPUT_CONTRACT
            + " Input: "
            + json.dumps(report, ensure_ascii=False, sort_keys=True)
        )

    def secondary(pack: Mapping[str, object], identity: ModelIdentity) -> object:
        source_material = _planning_source_material(pack, root=root)
        return _invoke_json(
            identity,
            "Do not call tools, run commands, make decisions, or edit files. Use only the supplied "
            "source material. Return exactly one JSON object with keys schema_version=1, "
            "question_pack_id, and evidence. Evidence must contain every question exactly once; "
            "each row has only question_id, non-empty claims string list, and non-empty source_refs "
            "string list naming supplied sources. " + _JSON_OUTPUT_CONTRACT + " Input: "
            + json.dumps(
                {"question_pack": pack, "source_material": source_material},
                ensure_ascii=False,
                sort_keys=True,
            ),
            worktree=root,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )

    def integrator(pack: Mapping[str, object], evidence: Mapping[str, object]) -> object:
        # #406：prompt 必須把 validate_primary_integration 的結構約束逐條講給模型，
        # 只列欄位名（不給語意）時模型會把不確定的 artifact_refs 留空 → 必然驗證失敗。
        # #516：同一教訓的第二輪，這次補的是 question_pack_id 與 secondary_evidence_hash
        # 兩個 echo-back 欄位——兩個值都已在輸入裡（question_pack.pack_id、
        # secondary_evidence.evidence_hash），模型只需原樣複製；但輸入欄位名
        # （evidence_hash）與輸出欄位名（secondary_evidence_hash）不同，後者字面上
        # 像是要模型自己算 hash，只列欄位名時會反覆撞 evidence hash mismatch。
        # #520：第三輪，這次是必要標題。舊句「required headings: Requirements for
        # spec, ...」字面上可讀成「標題就是 `Requirements for spec`」，模型照抄後必然
        # 撞 required-section-missing。標題要求現改由 `planning.required_heading_hint()`
        # 依驗收判準（`_ACCEPTED_HEADINGS` / `_REQUIRED_HEADINGS`）機械產生——prompt 端
        # 不再持有第二份真實來源，判準改動會自動同步到 prompt。
        return invoke_primary(
            "Do not call tools or edit files. Integrate only the supplied evidence. Return exactly one "
            "JSON object with schema_version=1, question_pack_id, secondary_evidence_hash, resolutions, "
            "and artifacts. question_pack_id must be copied verbatim from the input question_pack.pack_id "
            "value. secondary_evidence_hash must be copied verbatim from the input "
            "secondary_evidence.evidence_hash field; do not compute, derive, or invent a hash. "
            "Each resolution has only question_id, decision, artifact_kind, artifact_refs. "
            "Resolve every question exactly once. artifact_kind must equal the question kind without its "
            "'missing-' prefix. artifact_refs must be a NON-EMPTY list of the destination path(s) this "
            "resolution's artifact(s) are written to — the same strings used as artifacts[].path. "
            "The set of all artifacts[].path values must equal the union of all artifact_refs. "
            "Each artifact has only kind, path, content; content must be complete UTF-8 Markdown with "
            "frontmatter status: accepted and the matching work_item. "
            + required_heading_hint()
            + " Use the supplied destination paths. "
            + _JSON_OUTPUT_CONTRACT + " Input: "
            + json.dumps(
                {
                    "question_pack": pack,
                    "secondary_evidence": evidence,
                    "destinations": _planning_destinations(pack),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    return ProductionPlanningRuntime(registry, probes, questioner, secondary, integrator)
