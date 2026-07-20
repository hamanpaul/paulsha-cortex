## Context

現有Monitor已有workspace scan、ProjectState、in-memory SnapshotStore、Unix socket與systemd service；Coordinator已有single-writer control queue、versioned jobs/slices、deterministic verification、foreign review與CompletionRecord。這些是本change的anchor，不建立第二套daemon或平行delivery engine。

規範性公共型別、override/frontmatter、snapshot與CLI JSON schema完整列於`CONTEXT.md`；accepted end-to-end行為列於superpowers source spec；逐檔TDD與commit boundaries列於implementation plan。

## Goals / Non-Goals

**Goals:**

- 將GitHub issue、repo Todo artifacts、OpenSpec、workflow與delivery evidence投影為四態Work Item。
- Provider部分失敗時沿用last-good並阻止destructive transition。
- 以confirmed authority授權claim/merge/done；inferred只供顯示/explain。
- 讓Manager以claim key冪等地協調planner/builder/reviewer/ship steps。
- 保存Deck persona binding並強制planner peer、builder與reviewer的domain separation。
- 以current-HEAD GitHub evidence與remote default branch狀態閉合done。
- 對現有ProjectState、Slice與CompletionRecord提供明確migration/compatibility。

**Non-Goals:**

- v1不自動建立缺少的GitHub issue。
- v1不支援GitHub以外forge的terminal delivery。
- 不把heuristic association升級為authority。
- 不使用GitHub auto-merge，不新增batch merge queue或替代branch protection平台。
- 不導入event-sourcing/database；先使用atomic JSON與immutable evidence。
- 不讓Copilot review取代ForeignReview，也不讓同一secondary同時滿足兩個gate。

## Decisions

### 1. Provider snapshot先正規化source，再做correlation/reduction

每個provider輸出`ProviderSnapshot(provider_id, status, attempt/success timestamps, revision, sources, diagnostics)`。GitHub provider以authenticated `gh api`取得issues、PRs、closing refs、reviews、threads、checks與default branch trees；repo provider掃描固定artifact globs並排除`openspec/changes/archive/**`；workflow provider只讀Manager registry/evidence。

Monitor將provider snapshot寫入`$PSC_MONITOR_STATE_ROOT/work-items.snapshot.json`，default root為`$PSC_AGENTS_ROOT/monitor`，schema固定`work-items-snapshot/v1`。成功才替換該provider sources；failure保留last-good sources並標degraded。Reducer使用「所有provider的last-good source集合 + current health」重建WorkItems，因此failure不能看成empty/removal。

對受degraded provider影響的work item，保留prior lifecycle state、加入`degraded` facet，禁止new dispatch/done/merge。900秒無GitHub success是Manager hard gate；300秒是default refresh，不是freshness guarantee。

### 2. Correlation分authority graph與display clusters

Source以`kind + canonical ref`形成stable `source_id`。Confirmed edges只來自`.cortex/work-items.yaml`、`work_item` frontmatter、GitHub closing reference與Manager metadata。Stable work ID依序為explicit work item、`issue:<owner>/<repo>#N`、source locator。

Title/slug/branch/issue token heuristic必須至少兩個independent signals、沒有competing candidate，才可產生inferred cluster。Inferred source保留`confidence=inferred`，不能成為claim/merge/done input。`--explain`回傳accepted/rejected signals、competitors、exclusions與reducer trace。

Override parser只讀repo root `.cortex/work-items.yaml` version 1；unknown key、path escape、duplicate或ownership collision fail-closed。Link/unlink由Manager原子更新；unlink一定建立negative exclusion。

### 3. Lifecycle不是Workflow phase

WorkItem lifecycle固定`topic|todo|ongoing|done`，public顯示`on-going`。Workflow phase固定`claim|define|plan|build|verify|review|ship`。`queued/blocked/needs_human`不創建新lifecycle state。

Strict closure是合取：mapped PR以merge commit進default branch、所有mapped issues closed、OpenSpec在遠端default branch archive且active path消失、Todo tasks完成、CompletionRecord schema/hash/revisions有效。Local uncommitted overlay可使topic顯示todo，但永遠不能證明done。

若issue reopen或active OpenSpec同名再出現，reducer依source truth退回topic/todo；CompletionRecord保留audit但不覆蓋current contradiction。

### 4. CLI與socket共用versioned serializer

CLI `--json`與socket work-item payload共用`cortex-work/v1` serializer與canonical ordering。`list`預設只列topic/todo/on-going；`--all`含done。`--state`接受`ongoing`或`on-going`，輸出只用`on-going`。

Socket unary envelope沿用`{ok,data|error}`，新增request kind `list_work_items`、`get_work_item`、`explain_work_item`；subscription新增`subscribe_work_items`，event為`work_snapshot|work_change`並帶sequence。舊ProjectState requests保留一個release cycle，help與response diagnostic標deprecated。

### 5. Workflow registry v2做原子備份migration

Manager首次讀v1 registry時，在同目錄以content hash與timestamp建立不可覆寫backup，fsync後才以atomic replace寫v2。V1 jobs/slices映射為`legacy_records`，不得推測work_id、不得自動附掛WorkflowRun。Unknown/malformed schema拒絕migration且原檔不變。

`WorkflowRun`保存work ID、claim key、combo、phase、steps、issue/OpenSpec/PR refs、attempts與evidence refs。每個`WorkflowStep`保存phase、persona、card、executor/model/domain、inputs/outputs、gate result。Active claim key唯一；restart/duplicate control request回既有run。

非Manager step以manifest card逐張建立durable Job，完整綁定run/claim/repo/source revision/phase/card/persona/model identity。Headless terminal poll由Job log產生canonical coordinator-root evidence並把`{kind,path,hash}`原子綁回Job；control caller不能提供evidence locator。Restart只需讀registry即可poll目前card、驗證canonical evidence並resume；同phase尚有未passed card時不前進。

### 6. Claim manual-first、label opt-in

`cortex work start`可啟動confirmed Todo。Auto claim要求同一work item同時具有confirmed Todo與confirmed GitHub issue，且issue帶`cortex:auto-on-going`。Issue-only即使帶label仍是topic，不派工；Todo缺issue設`needs_human:missing_issue`，不自動建issue。Operator link後`resume`重用run/claim key。

`work auto --enable|--disable`使用GitHub REST管理label。移除label只阻止尚未claim工作，不中止active run。Provider stale/degraded、association conflict或non-GitHub forge皆fail-closed。

### 7. Persona completeness與independence是manifest gate

Deck compiler把card `persona_binding`原樣寫入workflow manifest。新增planner persona；default combo `feature-oneshot`依序執行planner、builder、reviewer、manager。Artifact必須有frontmatter `status: accepted`、必要章節且沒有blocking decision marker才算accepted。Marker只接受獨立行`TBD`、`[TBD]`、`Decision: TBD`、`決策：未定`或Open Questions中的實際項目；inline說明與fenced code忽略。缺accepted spec/design/plan或存在marker時，primary planner先產question pack，secondary planner只回evidence，primary整合。

所有planner invocation（包含manifest plan card）在temporary disposable checkout執行，Claude固定`plan`並停用tools、Codex固定`--sandbox read-only`、agy固定plan/sandbox；tree snapshot涵蓋檔案內容、empty dirs、directory symlinks與stable metadata，nonzero/exception亦不可跳過。即使snapshot因目錄權限失敗，Manager仍先恢復安全traversal，再依baseline還原entries、mode與xattrs；restore fault fail-closed。Primary integration只回structured content；scan時將canonical ref/kind/work item/content hash authority持久化至WorkflowRun，replacement必須逐欄符合該authority與manifest output，不接受caller hash或filename推測。新檔使用原子no-clobber；artifact、immutable或既存同內容brainstorm evidence、expected gate ref與registry phase update寫入durable intent journal。Registry未commit才rollback；已commit則restart逐operation驗type/hash/mode/evidence，drift設`needs_human`並保留journal。

Model identity registry顯式映射executor/model/domain；secondary選擇`agy/google → claude/anthropic → codex/openai`，排除primary domain。`agy` argv固定`agy --print --mode plan --sandbox ...`等價結構，不允許unsafe bypass。Malformed output、unknown identity、same-domain或live probe failure皆停止在needs_human。

Builder與Foreign Reviewer沿用Candidate verification/review，但reviewer必須不同domain且在detached exact HEAD。Claude reviewer以`dontAsk`、safe-mode、僅Bash工具面與structured JSON output執行non-mutating tests，不使用Plan Mode或Candidate customization；CLI-only settings停用remote/MCP、拒讀home/runtime sockets且只重開Candidate/Python user-site、隔離credentials並deny-write Candidate，review subprocess採最小正向環境allowlist與非login shell。缺Claude Code 2.1.187+、必要CLI surface、bubblewrap/socat/srt、live native或Unix-socket seccomp smoke，或要求unsandboxed fallback皆停止。Protected-path bind targets只存在deterministic disposable session root，exact Candidate固定在其`candidate/` checkout，兩者分離以維持material tree與Git status乾淨。每張card dispatch時保存output目錄baseline；Verify/review terminal payload必須列出符合manifest output glob且可由repo root重讀/hash的真實report，report必須是該job後新建或相對baseline更新，frontmatter精確綁WorkflowRun、card與Candidate。Canonical coordinator evidence保存current/baseline hash且locator只屬gate ref，不算report output。Brainstorm peer evidence與ForeignReview evaluation保存不同step/gate refs，不可互換；Copilot也只屬ship gate。Exact-bound reviewer若`exited-0`但缺payload，只能由explicit operator resume在重驗Candidate snapshot後保留舊Job並重派，periodic無此權限。

### 8. Ship以每個HEAD為review epoch

Manager先以官方`openspec archive -y <change>`產生archive diff，確認tasks全勾、canonical specs與doc refs，再加入changelog fragment。PR metadata使用zh-TW conventional title/body/labels，issue引用用`Closes #N`。

快速`python3 -m policy_check --repo .`後執行`PSC_PREFLIGHT_CMD`；初次帶draft metadata，既有PR修正帶`--pr N`。Preflight須涵蓋pytest、`openspec validate --all`與PR-context policy。`--skip-tests`只接受同tree hash、fresh、full-suite evidence。

Push/PR後等待checks terminal-green，並要求恰好一種typed current-HEAD delivery review。Copilot路徑request `@copilot`並驗review `commit_id == HEAD`、非error；maintainer路徑由Manager重讀PR HEAD後寫immutable attestation。兩者都要求threads resolved/outdated且不能替代ForeignReview。每次push使舊review authority失效；Copilot finding最多兩輪、每HEAD 15分鐘，超時或第三輪轉needs_human。

Merge前重新讀HEAD、mergeability、checks、threads、closing issues與archive diff，若任一revision改變就重跑相應gate。只用`gh pr merge --merge`，不使用`--auto`。

V1 target cardinality 要求current authority恰為一張PR、一個active OpenSpec與一個Todo。若在任何delivery binding建立前因缺少或多出target而停在`needs_human`，operator可先修正repo-local confirmed correlation再explicit resume；Manager重綁同一run的journal後只清除這個特定stop。已建立binding或其他stop reason不取得此恢復權限。

Official archive產生的新Candidate會失效舊verify/review evidence；若fresh reviewer發現archive後才出現的Candidate缺陷，operator可用exact CAS執行`retry-build`。Registry只允許保留identity精確為Manager deterministic archive的已通過ship step，重開最後builder與後續gate；planning reconciliation只把已移走的active artifact對應到同hash且唯一的official archive path，維持immutable brainstorm authority。Policy-commit或其他ship side effect一旦通過便拒絕rewind。修正commit仍須是archived Candidate的descendant，且不得重建active change或冒稱terminal closure。

被後續terminal canary取代的舊run以queued `work abandon`明確淘汰，不直接改registry。Action要求exact run ID CAS、current WorkAuthority refs、actor與bounded reason；只接受沒有active Job、PR ref、passed ship step或CompletionRecord的pre-delivery run。Manager將reason寫入immutable evidence後把run設為`superseded`；此狀態不等於done，不產生CompletionRecord，也不授權把未完成OpenSpec tasks勾成完成。

### 9. Done在merge後以remote snapshot重證

Merge後fetch default branch，驗merge commit ancestry、mapped issue closed、active OpenSpec消失、archive存在、Todo complete，再寫versioned CompletionRecord。Record綁定work ID、workflow/run/step IDs、source revisions、PR/head/merge SHA、issue states、archive tree、Todo revisions與gate evidence hashes。

Monitor只在GitHub/provider fresh且Record驗證通過時投影done；record缺失、stale、source contradiction或provider degraded皆不升done。

### 10. Planning handoff以input snapshot跨worktree

WorkflowStep額外保存`skill_ref`與structured action/commit/test policy。Brainstorm publication先以canonical evidence的scope與artifact ref/kind/hash原子擴充WorkflowRun planning authority，並保存不可變發證source revision；PR refresh只能更新run目前source revision。每份canonical Job envelope同樣綁定該Job dispatch時保存的immutable source revision；reader以Job欄位重驗，不以後續前進的run current revision回寫或誤判drift。Legacy active run只能由相同evidence reconcile，不能掃mutable檔案猜測；brainstorm-required卻缺evidence時fail-closed。Manager再於dispatch前把目前card與同phase較早card的inputs合併，逐glob解析regular non-symlink檔案並保存pattern/path/hash/authority/content locator。若builder worktree缺accepted planning artifact，只能從WorkflowRun planning authority驗hash後原子seed。Codex固定`workspace-write`；workflow的`commit_policy=required`及legacy fanout／dispatch／retry-build的builder persona取得明確commit capability。Linked worktree的`.git`為external marker時，launcher先清除inherited Git repository selectors，再以Git解析並驗證current worktree gitdir與common metadata關係，只將該gitdir、shared objects、current branch ref/reflog parent directories以`--add-dir`開放。Planner、verify與review不取得這些Git directories；symlink、detached HEAD、invalid或escape metadata皆拒絕required-commit launch。Operator source drift、destination conflict、同glob未授權替代檔、dispatch exception或128 KiB prompt bound超限皆保留`needs_human`並停止在job建立前。

Prompt固定為`workflow-card-prompt/v1`，包含resolved source content與terminal schema。Plan/build的`outputs` schema只允許符合declared outputs的repo-relative artifact path字串，禁止action/summary等描述性物件；manifest未宣告outputs時，prompt把該欄固定為`[]`。Active v1 run可繼承同phase input contract以恢復pending cards，但既有passed card不回填新證據、不偽稱通過新gate。Terminalize再次驗snapshot hash，canonical job evidence保存相同snapshot。

Verify/review identity除了foreign domain，也必須在schema v2明示`review` capability；legacy v1 identity只保留planning能力，不推測review權。Reviewer launcher使用executor read-only mode，且工作目錄為Manager建立的exact Candidate disposable clone；checkout後移除所有Git remotes，原Candidate完整tree snapshot在所有terminal與launch failure路徑重驗後才清除clone。Agent terminal只提供substantive verification summary/details、review findings/reason與inline report body；Manager從durable Job注入slice/Candidate、builder/reviewer job IDs與launch identities，正規化finding ID/blocking/state。Report只允許phase專屬Markdown root，並以durable intent journal將multi-report CAS、canonical evidence與registry bind包成可rollback/roll-forward transaction；report frontmatter另綁job ID，讓同run/card retry不會誤用舊report。Agent不得直接修改Candidate或report。

Plan/build workflow card的headless process exit 0但terminal明示`failed`或`needs_human`時，Manager保留原job/log且不建立passed evidence；periodic維持`needs_human` stop。只有explicit operator resume可在schema與run/card binding一致時重派同一run/card，malformed或錯誤binding不可藉此旁路。

升級前誤派planning-only canonical Agy的verify/review Job若以generic v1 `passed` terminal結束，該payload永不視為evidence。只有explicit operator resume可在latest Job精確綁定run/claim/repo/source/card/phase/persona/current Candidate、canonical Agy identity與manifest outputs雙向完整時授權單次fresh dispatch；periodic與一般retry flag均不可取得此migration authority。Terminal parser只額外接受整份內容恰為單一JSON fenced object，不從任意prose擷取JSON。

Build phase由多張會commit的card逐步形成Candidate。Manager接受每張card的terminal前，必須重讀worktree exact HEAD，並驗新Candidate等於或為目前Candidate的descendant後才原子推進；首張build card以持久化dispatch base為baseline。Verify/review繼續要求job Candidate完全等於凍結的run Candidate。

### 11. `needs_human`是operator resume boundary

Dispatcher仍負責把dead PID/no sentinel fail-closed成failed job；Manager將run設`needs_human`後，periodic runner只回`operator-resume-required`，不得清facet或重派。只有control queue收到explicit work/workflow resume才清facet、保留舊failed job並重試相同run/card。

### 12. Current-HEAD delivery review是typed union

ForeignReview仍是獨立必備gate。其後delivery review authority恰為`copilot`或`maintainer-review`之一。`review-attest`只經Manager queue建立：先重讀authenticated PR HEAD，再寫repo/work/run/authority digest/PR/candidate/actor/verdict綁定的immutable evidence。Ship重驗run gate ref/hash與current HEAD，GitHub checks/threads/mergeability/archive/closing refs完全沿用。

既有Copilot authorization維持v1 replay；maintainer路徑使用merge authorization v2，保存實際review kind/ref/hash。WorkflowRun已綁定且path/hash完整的exact-HEAD maintainer evidence只可重入既有`copilot-*` needs-human stop；external merge、target cardinality或其他stop仍fail-closed。Manager不得把maintainer evidence寫成Copilot kind，CompletionRecord的trusted evidence也保留同一typed union，並要求Copilot／maintainer恰好一種。Delivery journal已精確記錄`merged`或`done`後，resume只把完整repo/work/run/Candidate/merge commit/authorization binding視為post-merge routing hint，略過已被official archive移走的active planning path重驗；`done`涵蓋ship validator已完成而Manager尚未finalize WorkflowRun的crash window。它不授予authority，CompletionRecord與remote closure仍由ship validator完整fail-closed重驗。若WorkflowRun已finalize為`done/ship`但CompletionRecord綁定較舊authority，explicit work resume只在同repo/work恰有一個terminal run時選取它，允許production ship validator以current authority重驗；Manager不dispatch任何workflow card，且僅接受含完整completion binding的`passed`結果原子更新同一run。Provider已有該PR的terminal source revision時，Manager不得再加入synthetic open revision；pending、needs-human、malformed或ambiguous terminal run一律保留舊completion並停止。若default snapshot在cached `done`後前進，Manager以current facts產生新的semantic draft；terminal validator優先使用該replacement完整重驗並在成功後更新journal的CompletionRecord ref/hash，未提供replacement時仍讀取與驗證cached record。Terminal closure重播允許immutable authorization保留merge當下的pre-terminal WorkAuthority digest，因issue/PR closed與archive會使current digest自然前進；只有`merged|done`分支可使用此transition，merge前仍要求current digest精確相等，所有其他authorization binding持續完整驗證。Completion Draft以排除`completed_at`的normalized closure語意hash作為immutable revision key；同語意retry沿用首次時間戳與既有檔案，default branch、provider或WorkAuthority前進則建立新revision並保留舊draft供audit，symlink、malformed或同key不同語意一律fail-closed。Workflow card的verify與review各有自己的dispatch slice identity；Manager先完整驗證原始canonical envelope，再以共同WorkflowRun ID派生closure evidence，讓CompletionRecord的slice、Candidate與builder/reviewer jobs可被同一個strict reader交叉驗證，而不改寫原始per-card evidence。Post-archive repair的final Candidate必須單調延伸Manager archive commit；terminal ship audit因此只對已由registry標記passed的`openspec-archive`接受Git驗證過的ancestor job，`policy-commit`仍精確綁final Candidate，非ancestor或ancestry查詢失敗一律拒絕。Interactive runtime另以`PSC_INSTANCE`選取installer bootstrap env，讓CLI與service共享instance-scoped run root；invalid env fail-closed。

## Failure handling

- GitHub auth/rate-limit/timeout：沿用GitHub last-good、degraded facet、禁claim/merge/done。
- Repo scan race/mount error：沿用該repo last-good；成功完整scan前不remove。
- Override/frontmatter collision：repo provider degraded；explain列出每個claim edge。
- Registry migration crash：backup保留，atomic destination不是完整v2就拒啟動，不猜測修復。
- Agent/model/agy unavailable：needs_human，不降級到same-domain。
- Copilot timeout/error/old HEAD：needs_human或重請current epoch，不把COMMENTED當approval gate shortcut。
- HEAD/check/thread race：停止merge，重建epoch並重跑gate。

## Migration Plan

1. Baseline：修issue #4 scan stability；刪除已archive change的stale active copy；核對issue #10。
2. PR A：provider/snapshot/reducer/correlation/override/read CLI/socket；不啟用mutation。
3. PR B：planner/persona manifest/registry v2/agy/brainstorm；auto仍off。
4. PR C：manual/label claim、preflight/Copilot/merge/remote closure與work actions。
5. PR D：doctor/help/README/service/migration docs、archive此change、live docs-only canary。

## Risks / Trade-offs

- GitHub provider成本與rate-limit：批次GraphQL/REST、ETag/revision cache、300s default refresh；stale時安全停機。
- JSON snapshot/registry成長：保留schema/hash/atomic write；有實測scale證據再導入journal。
- 四PR跨期相容：PR A只read、PR B只建workflow能力、PR C才mutation；feature flags/auto-off避免半套ship。
- Copilot bounded wait拖慢delivery：這是current-HEAD assurance成本；超時交operator，不偷換reviewer。
- Remote archive與local overlay矛盾：done只相信remote default branch，local只作todo overlay。

## Open Questions

無blocking open question。Override path、frontmatter key、snapshot path/schema、CLI JSON schema、refresh/freshness、review rounds/timeouts、merge strategy與forge scope均已在本change鎖定。
