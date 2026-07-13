## ADDED Requirements

### Requirement: Periodic manager不得自動signal broker
manager daemon與periodic/oneshot tick MUST NOT自動apply global broker reaper。broker cleanup MUST只能由local operator command觸發。

#### Scenario: Normal manager tick
- **WHEN** manager執行periodic或manual tick且operator未呼叫cleanup command
- **THEN** manager不執行broker reaper
- **THEN** manager不對任何broker送signal

### Requirement: Cleanup apply必須由cwd root縮限
broker cleanup command MUST預設dry-run。operator要求apply時MUST提供resolved cwd root；候選live cwd不在該root、無法解析或僅有字串prefix相似時MUST skip。

#### Scenario: Dry-run列出候選
- **WHEN** operator未提供`--apply`
- **THEN** command可回報符合條件的候選
- **THEN** command不送任何signal

#### Scenario: 另一project的orphan-like broker
- **WHEN** broker cmdline與parent符合pattern但live cwd不在operator指定root
- **THEN** command skip該PID
- **THEN** fake或real signal seam不被呼叫

### Requirement: Signal前必須重新驗process identity
apply path MUST在送signal前立即重新讀PID start-time、cmdline、parent與cwd，並與候選snapshot比較；任何改變、消失或讀取失敗 MUST skip。系統MUST只送SIGTERM且MUST NOT自動升級SIGKILL。

#### Scenario: PID在scan與apply之間被reuse
- **WHEN** live start-time、cmdline、parent或cwd任一值與候選snapshot不同
- **THEN** command skip該PID
- **THEN** 不送SIGTERM或SIGKILL

#### Scenario: Identity完全一致
- **WHEN** operator明確apply、cwd在root內且所有live identity欄位與snapshot一致
- **THEN** command只對該PID送一次SIGTERM
- **THEN** command不等待後自動送SIGKILL
