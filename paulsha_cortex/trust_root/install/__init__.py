"""Public API for the Phase 2 trust-root installer."""

from .core import (
    AccountCollisionError,
    ActivationError,
    AttestationReport,
    CredentialImportError,
    CredentialMetadata,
    InstallDriftError,
    InstallError,
    InstallPlanError,
    InstallReceipt,
    PreflightReport,
    RollbackReport,
    UnsafeInstallPathError,
    VerificationResult,
    activate_receipt,
    apply_plan,
    atomic_write_json,
    attest_generated_inventory,
    bind_bundle_artifacts,
    build_install_plan,
    canonical_receipt_path,
    canonical_plan_bytes,
    import_credential,
    new_install_receipt,
    plan_sha256,
    rollback_receipt,
    validate_apply_plan,
    validate_bundle_manifest,
    validate_preflight,
    verify_receipt,
)
from .backend import LocalInstallBackend, SystemInstallBackend

__all__ = [name for name in globals() if not name.startswith("_")]
