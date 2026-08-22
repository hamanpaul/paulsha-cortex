# fix-copilot-review-verdict-spool

- **Copilot foreign-review verdict spool permissions are now file-scoped**: the
  headless reviewer may write only its exact `verdict.json` and run the declared
  `rg`/`python3` checks, without broad path or tool bypasses.
