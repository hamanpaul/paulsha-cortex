- 修正 Phase 2 generated-vs-installed attestation：shim／toolchain wrapper shebang 與
  `;`-prefixed shell 內容、polkit 的 `#`／未閉合 block／`;` statement 都會 fail closed；
  只有各 category 真正支援的獨立註解可降為 comment-only warning。
- Transactional installer 新增明示 `--prior-receipt` 升級交接：只接受上一版同 roots、同
  repository remote、已 applied/qualified 的 root-owned receipt，逐 account 與全部 filesystem
  kind 驗證 provenance；asset／repository／toolchain／venv slot 與 active link 會在任何 mutation
  前完成 sweep；既存 exact toolchain／self-marked venv slot 也不得取代 receipt-bound tree
  provenance。不受 roots/plan/receipt override 影響的 host-global transaction lock 會序列化
  apply／credential／activate／verify／rollback；runbook 另持有跨 service snapshot、stop、
  apply、verify 與 restore 的 host-global maintenance lease，以 reviewed-plan token admission
  擋下 lease 外 mutation；每次使用 canonical parent 下唯一且預先確認不存在的 effective
  receipt，並在停服務前將 receipt 與 service pre-state 寫入跨 reboot 的 root-private snapshot。
  abort full rollback 只處理本次 receipt；helper 單獨失效仍只接受原 token，整個 shell
  hard-crash 則由停服務前原子封存的 root-only reviewed plan 在 fresh shell 執行 explicit
  exact-plan recovery，不重建 immutable input／venv；maintenance snapshot 也採
  complete-before-publish。rollback 安全後才能 restore services。
  既存 toolchain 同一路徑換 bytes 因 backend 無原子覆換能力，會在 sweep 零 mutation 拒絕，
  升級須使用新的 versioned path。
  adoption 與 venv rollback link snapshot 會寫入新 receipt；venv slot 另以
  `planned → building → ready` inode/tree authority 在 final-name rename 前 checkpoint，hard-crash
  可只清 exact receipt-bound staging 或採信已發布 slot。保留 content-addressed slot 後也能完成
  journal 收斂，供 retry／rollback／下次升級使用；跨兩次 metadata-only 升級仍會沿用最初
  mount inode authority，不讓第三次升級收編同內容的 foreign mount。
- 唯一 production runbook 改以 qualification input 離線建立、root-owned 且 tree-hash sealed
  的 candidate CLI；manifest 與 actual wheelhouse 必須相等，每顆 wheel 以 hash-required、
  no-dependency-discovery requirements 安裝。驗證後只開 read/traverse、移除 exact
  `lib64 -> lib` 後拒絕其餘 symlink；plan/digest/render/apply 使用 trusted 絕對路徑與 closed
  environment，且只停止存在的 units；stop 後任何
  command failure、EXIT、INT、TERM 或 apply 失敗都會以 trap rollback 新 receipt 並恢復原先
  active units。
- RC artifact 改為保留完整 `qualification-input`；release gate 重驗 exact inventory、
  全部 hash 與 canonical install config，並同時發佈 RC-qualified wheel、deterministic
  `*-install-input.tar.gz` 與永久 qualification manifest；三個 asset 都核對 GitHub REST
  digest，INT/TERM/一般失敗都回收本次 draft/tag；annotated tag 內的 durable transaction marker
  讓下次 run 只清理由同 workflow 建立、仍為 draft 且 exact-SHA 的 hard-kill 殘留，不碰 foreign
  tag 或 non-draft release。
- Deployment canary 固定 `codex/gpt-5.3-codex-spark`／`xhigh`，byte-compare 完整 Manager-owned
  wrapper script，只接受 exact Bash envelope 內 bound worktree 的 absolute Git HEAD probe，
  並把 probe job subject、workflow final candidate、Cortex release SHA 分開綁定；per-job
  `CODEX_HOME`、exact PATH／Git selector denylist／safe.directory 與 Codex app-server persisted
  thread metadata 驗證實際 model／effort／provider／cwd。獨立 validator 另由 workflow 外部
  傳入 repo／work-id／issue，綁定唯一 probe log、128 字元 ID 上限、驗證前後不變的 bundle
  digest 與已驗 bytes 的 artifact set；#681/#695 對齊現行 authority，#716
  在對應 Cortex build 的 live canary 成功前保持 open。
- `worktree-isolation` 的第一張 builder card 使用獨立 autonomous preamble，不塞入指定命令；
  qualification 會重建並逐 byte 比對完整 canonical prompt／terminal schema，避免測試只驗
  fixture 自己寫入的假 contract。
