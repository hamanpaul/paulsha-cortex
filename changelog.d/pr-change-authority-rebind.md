# PR change authority rebind

- Keep an accepted build resumable when the same open delivery PR's review, comments, checks, or Manager-pushed head changes. Rebind the delivery journal only after verifying the Manager push and the current PR head, and persist a content-addressed receipt.
- Reject a changed PR target, Todo mapping or source revision, OpenSpec mapping, or closed issue with a specific reason. Review disposition also accepts only the exact Candidate or its verified Manager autosync descendant.
