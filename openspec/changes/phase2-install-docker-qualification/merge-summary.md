# Governed Merge Summary

Mode: `governed`

Target baseline: `e4eea57def3e915b42ea1113618a00059e9ee774`

## Sources

| Source | Scope |
|---|---|
| `a7af29d37f7876253c45af4d4e98ae90031fb8c2` | provider preflight/runtime identity and delivery-gate evidence |
| `d66371da134a87844dde8673462a234559caab61` | descriptor-safe paths, credentials, and receipt loading |
| `69b7d2f0248c635be520e4e3f4227d2a89e5b071` | exact config schema and private account/group validation |
| `0fca6c203342d8cf6ea9b31727167030d83daad4` | repository Git isolation and candidate-venv tree binding |
| `8fd20d889be174059c8747a8cc1ee5446f84b5fa` | adoption provenance, toolchain rollback, ACL and sudo policy inspection |
| `bff66fdaa92f8adca69d5b2b155000bb42503afa` | durable activation, rollback checkpoints, and action replay |

Every source was produced from the exact target baseline in an isolated worktree and
returned as one Conventional Commit.

## Conflict classification and resolution

- Textual: repository-inspection helpers and toolchain rollback helpers selected the
  same insertion point in `backend.py`. Both independent implementations were kept.
- Textual: their backend regression tests selected the same insertion point. Both test
  groups were retained without changing assertions.
- Textual: candidate-venv attestation and daemon-reload replay helpers selected the
  same insertion point in `core.py`. Both helpers and both call paths were retained.
- Behavioral: new root-owned receipt loading invalidated two recovery tests that had
  emulated ownership through pathname `lstat`. The tests now replace only the explicit
  receipt-authority validators; production fd-based validation remains unchanged.
- Behavioral: the local `0002` umask made a qualification fixture emit `0775`
  directories. The fixture now fixes its intended `0755` modes; production rejection
  of group/other-writable toolchain members remains fail-closed.

No unrelated refactor, production deployment change, service mutation, credential
discovery, or release action was included.

## Integrated validation

- installer and qualification focused suite: 180 passed;
- trust-root suite: 935 passed, 12 skipped, 95 subtests passed;
- Python syntax and `git diff --check`: passed during conflict resolution.

Full repository preflight, fresh adversarial review, packaging, and exact-SHA Docker
qualification remain separate post-merge gates.
