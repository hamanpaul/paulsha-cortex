# generated-asset-attestation

- [x] Generate a machine-readable attestation inventory for the generated
  Manager／Monitor／job units, root-owned shim, template polkit rule,
  gitconfigs, and Manager GitHub credential surfaces without exposing
  credential content.
- [x] Compare the generated inventory with the installed runtime, fail on
  functional drift, and demote comment-only text drift to warnings.
- [x] Add regression coverage for the three-way-to-four-way trust-root,
  redacted Manager GitHub credential surfaces, comment-only runtime drift, and
  missing GitHub credential surfaces.
- [x] Run the generated-asset attestation pytest coverage and the authoritative
  local archive gate for the active change.

Delivery, required CI, merge, issue closure, archive, and done remain
Manager-owned post-archive actions.
