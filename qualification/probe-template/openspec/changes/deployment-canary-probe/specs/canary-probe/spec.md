## ADDED Requirements

### Requirement: Blank label fallback

`normalize_label` SHALL return `unnamed` when the trimmed label is empty.

#### Scenario: All-whitespace label

- **WHEN** `normalize_label("   ")` is called
- **THEN** it returns `unnamed`
