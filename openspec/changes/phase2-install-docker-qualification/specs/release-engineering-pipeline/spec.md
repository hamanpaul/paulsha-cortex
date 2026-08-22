# release-engineering-pipeline Delta Specification

## MODIFIED Requirements

### Requirement: GitHub Release MUST require an exact-SHA successful RC qualification

Before creating a GitHub Release, the release workflow MUST locate a successful manual RC qualification
run for the exact tag commit, download and validate `qualification.json`, and prove that its candidate SHA
and wheel hash match the release build. Missing, stale, skipped, model-mismatched, fallback or inconsistent
evidence MUST fail closed. Qualification and release workflow actions MUST use 40-hex SHA pins; RC secrets
MUST only be available through a protected RC environment.

#### Scenario: qualification belongs to another commit

- **WHEN** the newest successful qualification evidence names a candidate SHA different from the tag commit
- **THEN** release stops before tag asset publication or GitHub Release creation

#### Scenario: provider smoke was skipped or fell back

- **WHEN** any required provider stage is `SKIP`, quota-rejected, fallback-executed or reports a different runtime model
- **THEN** qualification is unsuccessful and cannot unlock release

