## Changed

- **#1289 Trust Root launcher authorities**：installer plans now create and verify canonical builder/reviewer Codex controls, Manager-owned Codex/Copilot credential paths, and the Copilot OAuth environment binding. Credential inheritance migrates existing Codex/Copilot files automatically; legacy controls are quarantined before canonical controls are created. RC qualification probes installed launcher provisioning before harness setup and records the same authority check for legacy adoption.
