## ADDED Requirements

### Requirement: Rollback classification MUST distinguish retained managed subtrees

The rollback unknown-state scan MUST treat only filesystem relationships proven by the archived
receipt inventory as managed. An intentionally retained content-addressed venv or fresh checkout and
the carrier parents required to reach it MUST NOT make an otherwise safe rollback fail. Every
foreign sibling outside those receipt-bound relationships MUST still be reported, and specialized
toolchain member attestation MUST remain authoritative. Qualification MUST complete rollback and a
clean reinstall before adding any non-transactional runtime scaffold fixture.

#### Scenario: fresh rollback retains only receipt-bound durable subtrees

- **WHEN** rollback leaves a receipt-bound content-addressed venv or fresh checkout under a parent created by the same receipt
- **THEN** the parent MUST NOT be reported as unknown solely because that managed subtree remains
- **THEN** any foreign sibling under that parent MUST still make rollback fail closed

#### Scenario: qualification needs runtime scaffold state

- **WHEN** later service/runtime probes need harness-authored scaffold fixtures
- **THEN** qualification MUST create those fixtures only after rollback and clean reinstall succeed
