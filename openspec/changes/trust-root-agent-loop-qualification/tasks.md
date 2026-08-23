---
status: accepted
work_item: trust-root-agent-loop-qualification
---

## 1. RED regression

- [x] 1.1 在 `tests/test_trust_root_agent_loop_qualification_716.py` 新增
      `agent-loop-probe` RED contract，釘住真實 `codex exec` template-dispatch seam、
      repository command / child process / forbidden path / forbidden host /
      no-unsafe-fallback 覆蓋，以及 SKIP / fallback / quota / model mismatch 的
      fail-closed qualification evidence。

## 2. Qualification harness

- [ ] 2.1 在 `paulsha_cortex/trust_root/permgen.py` 產生 `agent-loop-probe`，重用
      `build_codex_argv`、`prepare_systemd_template`、`build_job_env`、
      `build_job_spec`、`write_job_spec` 與 `systemctl start --wait` 的真實派工接縫。
- [ ] 2.2 讓 probe 記錄 exact unit / profile / child tree / exit reason，並綁定
      executor/model、unit hash、candidate SHA 與 artifact hash。
- [ ] 2.3 讓 probe 對 repository command、child process、forbidden path、
      forbidden host、no-unsafe-fallback 五面向各有正反判準，不允許 scripted
      sandbox bypass。

## 3. Closeout

- [ ] 3.1 跑 focused qualification tests 與 `python3 -m pytest -q`，留下 RED→GREEN
      gate evidence。
- [ ] 3.2 交付 changelog entry 與最終 closeout。
