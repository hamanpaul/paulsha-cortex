"""#452 A：`cortex model profile` porcelain 入口（呈現層；核心邏輯在
coordinator.model_profile）。patchmud 不在場時印明確 skip 訊息並回 0。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from paulsha_cortex.coordinator.model_profile import (
    DEFAULT_DECK_ID,
    ProfileOptions,
    run_model_profile,
)

from . import COMMANDS, PorcelainCommand, register

MODEL_PROFILE_SCHEMA = "cortex-porcelain/model-profile/v1"


def register_commands() -> None:
    if "model" in COMMANDS:
        return
    register(
        PorcelainCommand(
            name="model",
            help="模型 profile 側寫與 execution qualification 候選／核可生命週期",
            run=main,
        )
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex model")
    sub = parser.add_subparsers(dest="command", required=True)

    profile = sub.add_parser(
        "profile",
        help="對 registry 內 source=default 的身分跑 patchmud deck，產封套 diff 預覽",
    )
    profile.add_argument("--apply", action="store_true", help="經人工複核後套用實測封套；不核可 execution qualification")
    profile.add_argument("--force", action="store_true", help="忽略評測指紋，強制重評")
    profile.add_argument("--deck", default=DEFAULT_DECK_ID, help="deck id（預設 pilot-v1）")
    profile.add_argument("--patchmud-bin", default=None, help="patchmud 可執行檔路徑（預設 PATH 查找）")
    profile.add_argument("--patchmud-root", default=None, help="patchmud repo 根（預設 $HOME/prj_pri/paulsha-patchmud 或 PSC_PATCHMUD_ROOT）")
    profile.add_argument("--registry-file", default=None, help="registry 寫入目標（預設 packaged data/model-identities.yaml）")
    profile.add_argument(
        "--identity",
        action="append",
        default=[],
        help="只處理指定身分（executor/model_id 或 executor:model_id，可重複；查無對應身分即報錯）",
    )
    profile.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/model-profile/v1 JSON")

    qualification = sub.add_parser(
        "qualification",
        help="匯入版本化 report、建立 human-review receipt、查詢／撤銷資格",
    )
    qualification_sub = qualification.add_subparsers(dest="qualification_command", required=True)

    import_report = qualification_sub.add_parser("import", help="驗證 PatchMUD report v2 並建立待複核 candidate")
    import_report.add_argument("--report", required=True, help="PatchMUD report v2 JSON 檔")
    import_report.add_argument("--source-revision", required=True, help="producer repo revision（小寫 git SHA）")
    import_report.add_argument("--profile-key", required=True, help="report 中 exact epk:v1:resolved profile key")
    import_report.add_argument("--executor", required=True, help="Cortex executor id")
    import_report.add_argument("--model-id", required=True, help="Cortex model id")
    import_report.add_argument("--role", required=True, choices=("planning", "build", "review"), help="execution role capability")
    import_report.add_argument("--profile-binding", default=None, help="選用的 execution-profile binding JSON 檔；缺少時 qualification 保持 unknown")
    import_report.add_argument("--profile-source-revision", default=None, help="profile producer revision；預設沿用 report revision")
    import_report.add_argument("--test-only", action="store_true", help="明確標記測試 candidate；不能在 live policy 派工")
    import_report.add_argument("--expected-revision", required=True, type=int, help="CAS index revision")
    import_report.add_argument("--idempotency-key", required=True, help="本次匯入的穩定冪等鍵")

    approve = qualification_sub.add_parser(
        "approve", help="經 operator 確認核發受治理 receipt，並發布 exact candidate"
    )
    approve.add_argument("candidate_id")
    approve.add_argument("--actor", required=True, help="真人 operator／reviewer 識別")
    approve.add_argument("--reason", required=True, help="核可理由，會寫入不可變 receipt")
    approve.add_argument("--policy-revision", required=True, help="核可政策版本")
    approve.add_argument("--reviewed-at", required=True, help="含時區的 ISO-8601 核可時間")
    approve.add_argument("--expires-at", required=True, help="含時區的 ISO-8601 receipt 到期時間")
    approve.add_argument("--expected-revision", required=True, type=int, help="CAS index revision")
    approve.add_argument("--idempotency-key", required=True, help="本次核可的穩定冪等鍵")
    approve.add_argument("--yes", action="store_true", help="明示確認；非互動呼叫必須提供")

    review = qualification_sub.add_parser("review", help="測試專用 review；live 核可請用 approve")
    review.add_argument("candidate_id")
    review.add_argument("--verdict", required=True, choices=("approved", "rejected"))
    review.add_argument("--reviewer", required=True, help="真人複核者識別")
    review.add_argument("--policy-revision", required=True, help="核可政策版本")
    review.add_argument("--reviewed-at", required=True, help="含時區的 ISO-8601 核可時間")
    review.add_argument("--expires-at", required=True, help="含時區的 ISO-8601 到期時間")
    review.add_argument("--test-only", action="store_true", help="建立 live policy 永不採信的測試 receipt")
    review.add_argument("--expected-revision", required=True, type=int, help="CAS index revision")
    review.add_argument("--idempotency-key", required=True, help="本次複核的穩定冪等鍵")

    revoke = qualification_sub.add_parser("revoke", help="經 operator 確認撤銷目前 generation")
    revoke.add_argument("candidate_id")
    revoke.add_argument("--actor", required=True, help="真人 operator／撤銷者識別")
    revoke.add_argument("--reason", required=True, help="撤銷理由")
    revoke.add_argument("--policy-revision", required=True, help="撤銷政策版本")
    revoke.add_argument("--revoked-at", required=True, help="含時區的 ISO-8601 撤銷時間")
    revoke.add_argument("--expires-at", required=True, help="含時區的 ISO-8601 receipt 到期時間")
    revoke.add_argument("--test-only", action="store_true", help="建立 live policy 永不採信的測試 receipt")
    revoke.add_argument("--expected-revision", required=True, type=int, help="CAS index revision")
    revoke.add_argument("--idempotency-key", required=True, help="本次撤銷的穩定冪等鍵")
    revoke.add_argument("--yes", action="store_true", help="明示確認；非互動呼叫必須提供")

    status = qualification_sub.add_parser("status", help="精確查詢 roster qualification；不修改 workflow")
    status.add_argument("--executor", required=True)
    status.add_argument("--model-id", required=True)
    status.add_argument("--profile-key", required=True)
    status.add_argument("--role", required=True, choices=("planning", "build", "review"))
    status.add_argument("--json", action="store_true", help="輸出 JSON")

    migration = qualification_sub.add_parser("migrate-legacy", help="保留舊 eval roster provenance，遷移狀態固定為 unknown")
    migration.add_argument("roster")
    migration.add_argument("--expected-revision", required=True, type=int, help="CAS index revision")
    migration.add_argument("--idempotency-key", required=True, help="本次遷移的穩定冪等鍵")
    return parser


def _print_text(result: dict[str, Any]) -> None:
    skip_reason = result.get("skip_reason")
    if skip_reason == "patchmud-not-found":
        sys.stdout.write(
            "skip: 找不到 patchmud 可執行檔——評測巷道為選配，維持保守預設封套"
            "（安裝 patchmud 後重跑 `cortex model profile`）\n"
        )
        return
    if skip_reason == "patchmud-version-unresolvable":
        sys.stdout.write(
            "skip: 無法解析 patchmud 版本（patchmud root 缺 VERSION）——評測指紋"
            "需要版本成分，維持保守預設封套\n"
        )
        return
    patchmud = result.get("patchmud") or {}
    deck = patchmud.get("deck") or {}
    sys.stdout.write(
        f"patchmud: {patchmud.get('bin')} v{patchmud.get('version')} "
        f"deck={deck.get('deck_id')} encounters={deck.get('encounter_count')} "
        f"sha256={str(deck.get('content_sha256'))[:12]}…\n"
    )
    for cell in result.get("cells", []):
        label = f"{cell.get('executor')}/{cell.get('model_id')} {cell.get('persona')}"
        status = cell.get("status")
        line = f"{status:>16}  {label}: {cell.get('reason', '-')}"
        detail = cell.get("detail")
        if detail:
            line += f"（{detail}）"
        sys.stdout.write(line + "\n")
        for failure in cell.get("encounter_failures", []) or []:
            sys.stdout.write(f"                  encounter 失敗：{failure}\n")
        diff = cell.get("diff")
        if diff:
            sys.stdout.write(diff if diff.endswith("\n") else diff + "\n")
    if result.get("applied"):
        sys.stdout.write(
            f"已寫入 {result.get('registry_file')}——請檢視 git diff 後自行 commit。\n"
        )
    elif any(cell.get("status") == "proposed" for cell in result.get("cells", [])):
        sys.stdout.write("未帶 --apply：以上僅為 diff 預覽，registry 未寫入。\n")


def main(argv: Sequence[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    if args.command == "qualification":
        return _qualification_main(args)
    if args.command != "profile":  # pragma: no cover - argparse required=True 已擋
        parser.error(f"unsupported model command: {args.command}")
    options = ProfileOptions(
        apply=args.apply,
        force=args.force,
        deck_id=args.deck,
        patchmud_bin=args.patchmud_bin,
        patchmud_root=Path(args.patchmud_root) if args.patchmud_root else None,
        registry_file=Path(args.registry_file) if args.registry_file else None,
        identity_filter=tuple(args.identity),
    )
    try:
        result = run_model_profile(options)
    except ValueError as exc:
        print(f"錯誤: {exc}", file=sys.stderr)
        return 2
    if args.json:
        payload = {"schema": MODEL_PROFILE_SCHEMA, "result": result}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    else:
        _print_text(result)
    if any(cell.get("status") == "failed" for cell in result.get("cells", [])):
        return 1
    return 0


def _confirm_operator_action(args: argparse.Namespace, *, action: str) -> None:
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise ValueError(f"non-interactive {action} requires explicit --yes")
    answer = input(
        f"確認 {action} candidate {args.candidate_id}，actor={args.actor!r}，"
        f"reason={args.reason!r}？[y/N] "
    )
    if answer.strip().lower() not in {"y", "yes"}:
        raise ValueError(f"{action} was not confirmed")


def _qualification_main(args: argparse.Namespace) -> int:
    from paulsha_cortex.coordinator.qualification_lifecycle import QualificationStore

    store = QualificationStore()
    try:
        if args.qualification_command == "import":
            report_path = Path(args.report)
            report_bytes = report_path.read_bytes()
            report = json.loads(report_bytes.decode("utf-8"))
            profile_binding = None
            profile_digest = None
            if args.profile_binding:
                profile_path = Path(args.profile_binding)
                profile_bytes = profile_path.read_bytes()
                profile_binding = json.loads(profile_bytes.decode("utf-8"))
                profile_digest = "sha256:" + hashlib.sha256(profile_bytes).hexdigest()
            result = store.import_report(
                report,
                source_revision=args.source_revision,
                source_artifact_digest="sha256:" + hashlib.sha256(report_bytes).hexdigest(),
                profile_key=args.profile_key,
                executor=args.executor,
                model_id=args.model_id,
                role=args.role,
                profile_binding=profile_binding,
                profile_source_revision=args.profile_source_revision,
                profile_source_artifact_digest=profile_digest,
                test_only=args.test_only,
                expected_revision=args.expected_revision,
                idempotency_key=args.idempotency_key,
            )
        elif args.qualification_command == "approve":
            _confirm_operator_action(args, action="核可")
            operator_receipt = store.issue_operator_receipt(
                args.candidate_id,
                verdict="approved",
                actor=args.actor,
                reason=args.reason,
                policy_revision=args.policy_revision,
                reviewed_at=args.reviewed_at,
                expires_at=args.expires_at,
            )
            result = store.review_candidate(
                args.candidate_id,
                verdict="approved",
                operator_receipt_id=operator_receipt["operator_receipt_id"],
                test_only=False,
                expected_revision=args.expected_revision,
                idempotency_key=args.idempotency_key,
            )
        elif args.qualification_command == "review":
            if not args.test_only:
                raise ValueError("review is test-only; use qualification approve for a live approval")
            result = store.review_candidate(
                args.candidate_id,
                verdict=args.verdict,
                reviewer=args.reviewer,
                reviewer_authority="test-only",
                policy_revision=args.policy_revision,
                reviewed_at=args.reviewed_at,
                expires_at=args.expires_at,
                test_only=True,
                expected_revision=args.expected_revision,
                idempotency_key=args.idempotency_key,
            )
        elif args.qualification_command == "revoke":
            if args.test_only:
                result = store.revoke_qualification(
                    args.candidate_id,
                    reviewer=args.actor,
                    reviewer_authority="test-only",
                    reason=args.reason,
                    revoked_at=args.revoked_at,
                    test_only=True,
                    expected_revision=args.expected_revision,
                    idempotency_key=args.idempotency_key,
                )
            else:
                _confirm_operator_action(args, action="撤銷")
                operator_receipt = store.issue_operator_receipt(
                    args.candidate_id,
                    verdict="revoked",
                    actor=args.actor,
                    reason=args.reason,
                    policy_revision=args.policy_revision,
                    reviewed_at=args.revoked_at,
                    expires_at=args.expires_at,
                )
                result = store.revoke_qualification(
                    args.candidate_id,
                    operator_receipt_id=operator_receipt["operator_receipt_id"],
                    test_only=False,
                    expected_revision=args.expected_revision,
                    idempotency_key=args.idempotency_key,
                )
        elif args.qualification_command == "status":
            result = store.qualification_status(
                args.executor, args.model_id, args.profile_key, args.role
            )
        elif args.qualification_command == "migrate-legacy":
            from paulsha_cortex._yaml import safe_load

            roster_path = Path(args.roster)
            payload = safe_load(roster_path.read_text(encoding="utf-8"))
            result = store.migrate_legacy_roster(
                payload,
                expected_revision=args.expected_revision,
                idempotency_key=args.idempotency_key,
                source_ref=roster_path.name,
            )
        else:  # pragma: no cover - required argparse choices
            raise ValueError(f"unsupported qualification command: {args.qualification_command}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"錯誤: {exc}", file=sys.stderr)
        return 2

    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0
