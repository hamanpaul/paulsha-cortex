# 765-builder-binding-era

- **`#765` 補遺：review 採信對 builder job 的綁定改為跨 era——#216 AC5 的語意
  落實。** authority restart 只 invalidate verify/review、build 產物的 Candidate
  跨 era 保留；builder job 因此合法地屬於較早 era。原本的 claim_key／
  source_revision 等值檢查讓每一次 authority 前進（PR 建立、openspec link）把
  已採信的 build 產物變成孤兒（實機：`review evaluation builder binding
  mismatch: workflow_claim_key`）。真正的綁定＝run_id＋repo＋
  `subject_head == candidate`（candidate 由 harvest fast-forward 與 gate ledger
  錨定）；mismatch 錯誤同時補上 job/兩側值。
