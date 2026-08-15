# d6-trust-root-spec

- **v4 重構計畫 R0.5 D6：trust root 隔離 spec 定案（spec-only，不含實作）**——
  新增 `docs/superpowers/specs/trust-root-isolation-spec.md`，定案 0.2.0 穩定版
  **不可豁免 join gate** 的完整契約（R1–R12）。本票不實作任何一行程式碼、不改
  部署、不動 systemd、不改 auth。
  - **完整資產盤點**：逐檔核對後列出 Tier-0／Tier-1／Tier-2 三級 durable state 與
    八類 mutation ingress，附 writer／reader／consumer inventory，涵蓋
    **builder／reviewer／planner 三個 headless persona**（非僅 builder）。
    盤點揭露的關鍵事實：路徑解析鏈本身即信任根
    （`config/runtime.py:89`，process env → bootstrap env → `$HOME/.agents`，
    bootstrap env 由 installer 裸寫、無 mode 檢查，`deploy/installer.py:162`）；
    全 repo **零 HMAC／零簽章**，所有 evidence 皆為自我雜湊，只證明位元組未壞、
    不證明產生者；`chmod 0400`／`0444` 一線對 owner 全部無效。
  - **最短攻擊路徑（本票新揭露）**：review verdict 是 reviewer 模型寫在 worktree
    內的 `.psc-review-verdict.json`（`coordinator/review.py:22-23,176-185`），
    pre-seed 守衛只在啟動前檢查——同 UID 下 **builder 可直接代寫 reviewer 的
    verdict**，不需偷任何 capability 即可為自己的 candidate 產出「通過」的 foreign
    review。此層在 `#484` 之外、比其更根本：即使 reviewer 被正確限制成 read-only
    仍然成立。
  - **control ingress 現況**：`<control_root>/requests/` 是全部 7 種 request type
    與 22 種 work-action 的入口，唯一門檻是「能否在該目錄建檔」；`requested_by`
    只驗非空字串（`control/contract.py:229-231`），卻是 `abandon`／
    `retire-delivered`／`reset-reclaim-budget` 的 audit actor fallback
    （`work_actions.py:3649,3766,3961`）並被烘進不可變 attestation
    （`work_actions.py:1166`）；處理順序由**本地可控的 mtime** 決定
    （`manager_daemon.py:1431-1436`）。
  - **enforcement plane 入界**：unit／`EnvironmentFile`／`service-manager.sh`／
    `sys.executable`／site-packages 全在同 UID（實測部分為 group-writable）；
    三個 unit **無任何 systemd 加固指令**；`EnvironmentFile=-` 缺檔靜默容忍是一條
    無聲重導路徑；`PSC_MANAGER_INSTALLER`／`PSC_REPLY_BRIDGE`／
    `PSC_DIGEST_DELIVERY_CMD` 三個「env 指名並執行任意程式」的入口無 typed-argv
    守衛（對照 `gate_ledger.py:68-80` 已有的守衛模式，證明覆蓋不全而非模式缺席）。
  - **anti-collusion 來源可寫**：`independence_domain` 不是 persona 欄位而是 model
    identity 欄位，host overlay 可壓過 packaged registry
    （`model_identities.py:604-631`）；combo override 亦 instance-local 壓過
    packaged（`deck/schema.py:80-87`）；persona overlay 的
    `tool_allowlist_additions` **只加不減、無上限、無白名單**
    （`persona/context.py:37-39`）。
  - **路線裁決**：完整比較 (a) OS/MAC 邊界與 (b) 簽章＋強制驗簽，**建議以 (a) 為
    0.2.0 的必要且充分基礎，(b) 降為 Phase 3 的 defense-in-depth 與跨信任域可攜
    性**。核心論證：(b) 的三個前提——金鑰保密、verifier 完整性、單調計數器——
    **全部必須由 (a) 提供**，因此「只做 (b)」在本環境不是成本較高的方案，而是
    不成立的方案；(a) 預設封閉、(b) 預設開放，而本 repo 光 `delivery-journal.json`
    就有六處重複路徑推導，「所有 consumer 都驗簽」的維護面遠大於 (a)。
  - **簽章規格仍完整定案**（供 Phase 3 與 Elevated tier）：canonical encoding
    （簽 bytes 非 hexdigest、拒 float／NaN／重複 key、收斂 repo 內兩套分歧的
    canonical 參數）、domain separation（`psc-sig-v1` 前綴＋封閉 `record_type`
    enum）、anti-replay（`subject`／`run_id`／`authority_revision`／單調 `seq`）、
    key rotation/revocation（overlap window、撤銷後**禁止自動 re-sign**）、
    舊 unsigned state 以 `legacy-import` 遷移（**禁止自動遷移**，且該標記不得滿足
    任何 ship gate）、缺簽壞簽一律 fail-closed 轉 blocked。
  - **operator 授權通道**：不得用同 UID 可讀的檔案／env secret；敏感 action
    （封閉清單）不接受未認證 file-queue request；逐案核可為 **action-bound＋
    single-use＋短效（≤300s）＋本體不落地**的 capability；reviewer 身分一律由
    Manager registry 的 `(job_id, persona, independence_domain, immutable verdict)`
    推導，payload 欄位降為顯示性。
  - **E2E 測試矩陣**：四族——capability-theft（四路）、durable-state-tampering
    （逐項 × modify/truncate/delete/replace/symlink-swap/rollback）、
    enforcement-plane-tampering（10 案，每案**實際重啟服務**後驗證，含 verifier
    downgrade 與 `EnvironmentFile` 刪除）、inherited FD／ptrace／`/proc` 路徑；
    **強制附 negative control**，避免環境壞掉時假綠。
  - **落地三階段**：Phase 1 不需 root 可先行（登記表＋機械等式、payload 欄位降級、
    reviewer 身分推導、overlay 白名單化、unit 加固與 env 守衛、Manager 自檢先 WARN、
    **降級運轉安全網**）；Phase 2 為 join gate 實體（獨立 UID、路徑分樹、verdict
    改受控通道、部署遷出同 UID pipx tree、降權啟動器、自檢切 fail-closed、四族
    全綠）；Phase 3 為簽章與跨主機可攜。
  - **issue 吸收對照**：`#484`（reviewer 未強制 read-only）→ Phase 2 **取代**，
    但既有修法照做不等 D6，且本 spec 指出 `#484` 單獨修復**不足以**使 foreign
    review 可信（builder 仍可代寫 verdict）；`#480`（persona `effective_tools`）
    → **降級為補強**，agent 就是那個進程，`effective_tools` MUST NOT 被引用為
    trust boundary，另補 overlay 無界提權缺口；`#489`（persona-scope 邊界）
    → **補強且成為輸入**，其 per-slice allowlist 是 Phase 2 權限產生器的來源，
    並指出 `write_paths: ["**"]` 目前涵蓋 `.cortex/work-items.yaml`（correlation
    authority）與 `.github/**`。
  - 另列 10 項需 operator 拍板的未決問題（受信任身分形態、headless UID 粒度、
    root 設定執行方式、舊 state 處置、降級運轉預設值、verdict 交付通道形態、
    簽章排程、跨系統檔案契約歸屬、多 instance 共用 HOME 形態、WSL2 以外環境）。
