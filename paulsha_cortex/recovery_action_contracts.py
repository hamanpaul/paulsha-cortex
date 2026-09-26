"""正式 recovery action 註冊表與入口選擇集合。

此模組只登錄 action 名稱與命名空間，不執行 recovery，也不授與 authority。
契約版本與行為矩陣見 ``docs/recovery-action-contract-matrix.md``。
"""

from __future__ import annotations

from dataclasses import dataclass


RECOVERY_ACTION_CONTRACT_VERSION = "recovery-action-contract/v1"


@dataclass(frozen=True)
class RecoveryActionFamily:
    """一列 recovery 契約家族在各正式入口使用的 action 名稱。"""

    coordinator_work: tuple[str, ...] = ()
    recover_work: tuple[str, ...] = ()
    coordinator_slice: tuple[str, ...] = ()
    recover_slice: tuple[str, ...] = ()


# 13 個 work recovery 契約家族。legacy reviewer retry 家族內的
# retry-verify／retry-review 是兩個不同 action，並非互相替代的 alias。
RECOVERY_ACTION_FAMILIES: dict[str, RecoveryActionFamily] = {
    "resume": RecoveryActionFamily(
        coordinator_work=("resume",), recover_work=("resume",)
    ),
    "retry-build": RecoveryActionFamily(
        coordinator_work=("retry-build",),
        recover_work=("retry-build",),
        coordinator_slice=("retry-build",),
        recover_slice=("retry-build",),
    ),
    "retry-card": RecoveryActionFamily(
        coordinator_work=("retry-card",), recover_work=("retry-card",)
    ),
    "legacy-reviewer-retry": RecoveryActionFamily(
        coordinator_work=("retry-verify", "retry-review"),
        coordinator_slice=("retry-verify", "retry-review"),
        recover_slice=("retry-verify", "retry-review"),
    ),
    "recover-planning": RecoveryActionFamily(
        coordinator_work=("recover-planning",)
    ),
    "recover-pre-candidate": RecoveryActionFamily(
        coordinator_work=("recover-pre-candidate",),
        recover_work=("recover-pre-candidate",),
        coordinator_slice=("recover-pre-candidate",),
        recover_slice=("recover-pre-candidate",),
    ),
    "recover-repair-commit": RecoveryActionFamily(
        coordinator_work=("recover-repair-commit",),
        recover_work=("recover-repair-commit",),
    ),
    "regenerate-gates": RecoveryActionFamily(
        coordinator_work=("regenerate-gates",),
        recover_work=("regenerate-gates",),
    ),
    "abandon": RecoveryActionFamily(
        coordinator_work=("abandon",),
        recover_work=("abandon",),
        coordinator_slice=("abandon",),
        recover_slice=("abandon",),
    ),
    "retire-delivered": RecoveryActionFamily(
        coordinator_work=("retire-delivered",),
        recover_work=("retire-delivered",),
    ),
    "recover-superseded": RecoveryActionFamily(
        coordinator_work=("recover-superseded",),
        recover_work=("recover-superseded",),
    ),
    "reset-reclaim-budget": RecoveryActionFamily(
        coordinator_work=("reset-reclaim-budget",),
        recover_work=("reset-reclaim-budget",),
    ),
    "refreeze-base": RecoveryActionFamily(
        coordinator_work=("refreeze-base",), recover_work=("refreeze-base",)
    ),
}


# Slice-only supersede is tracked separately from the 13 work-action families;
# it has its own binding-revision CAS contract.
RECOVERY_SLICE_EXTENSIONS = {"supersede": "slice-supersede"}


RECOVERY_WORK_ACTIONS = frozenset(
    action
    for family in RECOVERY_ACTION_FAMILIES.values()
    for action in family.coordinator_work
)
RECOVERY_SLICE_ACTIONS = frozenset(
    action
    for family in RECOVERY_ACTION_FAMILIES.values()
    for action in family.coordinator_slice
) | frozenset(RECOVERY_SLICE_EXTENSIONS)


# Work action 註冊順序也決定 coordinator CLI help 的穩定順序。每個名稱必須
# 分類為 recovery family 或具名的非 recovery operation；測試會和矩陣雙向比對。
WORK_ACTION_CLASSIFICATION: dict[str, str] = {
    "link": "non-recovery:source-mapping",
    "unlink": "non-recovery:source-mapping",
    "start": "non-recovery:workflow-start",
    "resume": "resume",
    "retry-build": "retry-build",
    "retry-card": "retry-card",
    "retry-verify": "legacy-reviewer-retry",
    "retry-review": "legacy-reviewer-retry",
    "recover-planning": "recover-planning",
    "recover-pre-candidate": "recover-pre-candidate",
    "recover-repair-commit": "recover-repair-commit",
    "regenerate-gates": "regenerate-gates",
    "abandon": "abandon",
    "retire-delivered": "retire-delivered",
    "close-delivered": "non-recovery:delivery-closeout",
    "recover-superseded": "recover-superseded",
    "reset-reclaim-budget": "reset-reclaim-budget",
    "refreeze-base": "refreeze-base",
    "auto": "non-recovery:workflow-automation",
    "ship": "non-recovery:delivery",
    "review-attest": "non-recovery:review-attestation",
    "verify-attest": "non-recovery:verification-attestation",
    "review-disposition": "non-recovery:review-disposition",
    "intake": "non-recovery:work-intake",
}

WORK_ACTION_CHOICES = tuple(WORK_ACTION_CLASSIFICATION)
WORK_ACTIONS = frozenset(WORK_ACTION_CHOICES)
NON_RECOVERY_WORK_ACTIONS = frozenset(
    action
    for action, classification in WORK_ACTION_CLASSIFICATION.items()
    if classification.startswith("non-recovery:")
)


SLICE_ACTION_CLASSIFICATION: dict[str, str] = {
    "retry-build": "retry-build",
    "retry-verify": "legacy-reviewer-retry",
    "retry-review": "legacy-reviewer-retry",
    "recover-pre-candidate": "recover-pre-candidate",
    "abandon": "abandon",
    "supersede": "slice-supersede",
}
SLICE_ACTION_CHOICES = tuple(SLICE_ACTION_CLASSIFICATION)
SLICE_ACTIONS = frozenset(SLICE_ACTION_CHOICES)


# `cortex recover` 是有界 porcelain 別名。未列入者必須使用 `cortex work`。
RECOVER_WORK_ACTION_CHOICES = tuple(
    dict.fromkeys(
        action
        for family in RECOVERY_ACTION_FAMILIES.values()
        for action in family.recover_work
    )
)
RECOVER_SLICE_ACTION_CHOICES = SLICE_ACTION_CHOICES

RECOVERY_EXPECTED_CANDIDATE_ACTIONS = frozenset(
    {"retry-build", "retry-verify", "retry-review", "recover-repair-commit"}
)
