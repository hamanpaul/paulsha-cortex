# new-project-template

> GitHub template skeleton for repositories that follow `hamanpaul/paulsha-conventions`. After generating a new repository, replace the project title and tailor the metadata for that project.

## Install

Use GitHub template creation or `gh repo create --template hamanpaul/new-project-template` to start a new repository. After generation, review `.paul-project.yml`, `README.md`, `CHANGELOG.md`, and `VERSION` before the first pull request.

## Usage

This template keeps the bootstrap minimal: policy metadata, changelog/version scaffolding, synchronized agent convention files, and a pinned `Policy Check` workflow. In generated repositories, run `python3 -m policy_check --repo .` before opening a pull request, then replace or extend the bootstrap metadata as the real project tooling becomes clear.

## Version

`VERSION` is the single source of truth for repository versioning. Update it together with `CHANGELOG.md` according to the selected `policy_profile`.
