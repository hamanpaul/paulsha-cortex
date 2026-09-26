"""#454：patchmud ranked 榜 → 封套四欄位映射純函式的單元測試。

覆蓋 spec（docs/superpowers/specs/envelope-mapping-spec.md）的驗收面：
同一 report 重跑結果 bit-identical、量不到的欄位標 default 且理由可追、
clear-rate-ladder-v1 門檻邊界（含整數算術的非二進位分母案例）、planner red
結構性釘入、fail-closed 輸入驗證、禁止 import patchmud。
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from pathlib import Path
import unittest

from paulsha_cortex.coordinator import envelope_mapping
from paulsha_cortex.coordinator.envelope_mapping import (
    ACCEPTANCE_MODES_DOMAIN,
    BAND_RULE_ID,
    CONSISTENCY_SCOPE_DOMAIN,
    DEFAULT_ENVELOPE,
    ENVELOPE_FIELDS,
    EnvelopeMappingError,
    REASON_BELOW_GREEN_FLOOR,
    REASON_IDENTITY_NOT_IN_REPORT,
    REASON_INCOMPLETE_DECK_SAMPLE,
    REASON_MEASURED_CLEAR_RATE,
    REASON_PERSONA_DIMENSION_UNMEASURED,
    SOURCE_DEFAULT,
    SOURCE_MEASURED,
    map_report_to_envelope,
)
from paulsha_cortex.coordinator.workflow import MODEL_CHAIN_PERSONAS
from paulsha_cortex.deck.schema import BAND_LEVELS


def make_report(
    *,
    clears: int = 6,
    runs: int = 8,
    model: str = "haiku",
    loadout: str = "P0T0R0",
    schema_version: int = 2,
    extra_rows: list | None = None,
    status: str = "ok",
) -> dict:
    """Cortex mapper 的最小 report v2 cohort fixture。"""
    cohort = {
        "role": "builder",
        "benchmark_type": "issue-resolution",
        "profile_id": "epk:v1:resolved:" + "a" * 64,
        "deck_digest": "sha256:" + "b" * 64,
        "evaluator_revision": "sha256:" + "c" * 64,
        "cohort_identity_complete": True,
        "coverage_complete": True,
        "ranked": True,
    }
    rows = [
        {
            **cohort,
            "model": model,
            "loadout": loadout,
            "runs": runs,
            "clears": clears,
            "value": clears / runs,
        }
    ]
    if extra_rows:
        rows.extend({**cohort, **row} for row in extra_rows)
    return {
        "schema_version": schema_version,
        "runs_included": runs,
        "runs_skipped": [],
        "runs": [
            {
                "run_id": f"fixture-run-{index}",
                **cohort,
                "deck_id": "pilot-v1",
                "model": model,
                "loadout": loadout,
                "clear": 1 if index < clears else 0,
                "power_total": 65.0,
                "cost": "NA",
                "work_tokens": "NA",
                "observable_tokens": 10000,
                "control": 100.0,
                "ftr": 0.0,
                "tau_uncalibrated": True,
            }
            for index in range(runs)
        ],
        "leaderboards": {
            "clear_rate": {"status": status, "rows": rows},
            "power": {"status": "ok", "rows": []},
        },
    }


def make_deck(
    *,
    deck_id: str = "pilot-v1",
    content_sha256: str = "a" * 64,
    encounter_count: int = 8,
    measured_personas: tuple = ("builder",),
) -> dict:
    return {
        "deck_id": deck_id,
        "content_sha256": content_sha256,
        "encounter_count": encounter_count,
        "measured_personas": list(measured_personas),
    }


def call(report: dict, **overrides) -> dict:
    kwargs = {
        "executor": "claude",
        "model_id": "sonnet",
        "persona": "builder",
        "deck": make_deck(),
        "patchmud_version": "0.0.1+abdf808",
        "execution_profile_key": "epk:v1:resolved:" + "a" * 64,
        "role": "builder",
        "benchmark_type": "issue-resolution",
        "deck_digest": "sha256:" + "b" * 64,
        "evaluator_revision": "sha256:" + "c" * 64,
    }
    kwargs.update(overrides)
    if "role" not in overrides:
        kwargs["role"] = kwargs["persona"]
    return map_report_to_envelope(report, **kwargs)


class DeterminismTests(unittest.TestCase):
    def test_same_report_maps_bit_identical(self) -> None:
        report = make_report(clears=6, runs=8)
        first = call(report)
        second = call(report)
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, sort_keys=True, ensure_ascii=False),
            json.dumps(second, sort_keys=True, ensure_ascii=False),
        )

    def test_inputs_are_not_mutated(self) -> None:
        report = make_report(clears=6, runs=8)
        deck = make_deck()
        report_before = copy.deepcopy(report)
        deck_before = copy.deepcopy(deck)
        call(report, deck=deck)
        self.assertEqual(report, report_before)
        self.assertEqual(deck, deck_before)

    def test_output_lists_do_not_alias_default_envelope(self) -> None:
        report = make_report(clears=0, runs=8)
        result = call(report, persona="planner", deck=make_deck())
        result["envelope"]["consistency_scope"].append("hacked")
        result["envelope"]["accepts_bands"].append("hacked")
        self.assertEqual(
            DEFAULT_ENVELOPE["planner"]["consistency_scope"],
            CONSISTENCY_SCOPE_DOMAIN,
        )
        self.assertEqual(
            DEFAULT_ENVELOPE["planner"]["accepts_bands"], tuple(BAND_LEVELS)
        )


class ClearRateLadderTests(unittest.TestCase):
    """clear-rate-ladder-v1：yellow ≥ 3/4、green ≥ 1/4，邊界皆含（≥）。"""

    def _bands(self, clears: int, runs: int) -> list:
        report = make_report(clears=clears, runs=runs)
        return call(report)["envelope"]["accepts_bands"]

    def test_at_or_above_three_quarters_grants_green_yellow(self) -> None:
        self.assertEqual(self._bands(6, 8), ["green", "yellow"])  # 恰為 3/4
        self.assertEqual(self._bands(7, 8), ["green", "yellow"])
        self.assertEqual(self._bands(8, 8), ["green", "yellow"])

    def test_between_quarter_and_three_quarters_grants_green_only(self) -> None:
        self.assertEqual(self._bands(5, 8), ["green"])
        self.assertEqual(self._bands(4, 8), ["green"])  # #455 haiku 實測 4/8
        self.assertEqual(self._bands(2, 8), ["green"])  # 恰為 1/4

    def test_below_quarter_grants_nothing(self) -> None:
        self.assertEqual(self._bands(1, 8), [])
        self.assertEqual(self._bands(0, 8), [])

    def test_thresholds_use_exact_integer_arithmetic(self) -> None:
        # 非 2 的冪分母：9/12 == 3/4 恰達 yellow；8/12 差一關只到 green；
        # 3/12 == 1/4 恰達 green；2/12 落空。浮點比較在這類邊界上不可靠。
        deck = make_deck(encounter_count=12)
        self.assertEqual(
            call(make_report(clears=9, runs=12), deck=deck)["envelope"][
                "accepts_bands"
            ],
            ["green", "yellow"],
        )
        self.assertEqual(
            call(make_report(clears=8, runs=12), deck=deck)["envelope"][
                "accepts_bands"
            ],
            ["green"],
        )
        self.assertEqual(
            call(make_report(clears=3, runs=12), deck=deck)["envelope"][
                "accepts_bands"
            ],
            ["green"],
        )
        self.assertEqual(
            call(make_report(clears=2, runs=12), deck=deck)["envelope"][
                "accepts_bands"
            ],
            [],
        )

    def test_measured_bands_follow_band_levels_order(self) -> None:
        bands = self._bands(8, 8)
        self.assertEqual(bands, [level for level in BAND_LEVELS if level in bands])

    def test_below_floor_marks_not_registry_writable(self) -> None:
        result = call(make_report(clears=1, runs=8))
        self.assertEqual(result["envelope"]["accepts_bands"], [])
        self.assertEqual(
            result["provenance"]["source"]["accepts_bands"], SOURCE_MEASURED
        )
        self.assertEqual(
            result["provenance"]["reasons"]["accepts_bands"],
            REASON_BELOW_GREEN_FLOOR,
        )
        self.assertFalse(result["provenance"]["registry_writable"])

    def test_measured_result_is_registry_writable(self) -> None:
        result = call(make_report(clears=6, runs=8))
        self.assertTrue(result["provenance"]["registry_writable"])
        self.assertEqual(
            result["provenance"]["reasons"]["accepts_bands"],
            REASON_MEASURED_CLEAR_RATE,
        )
        self.assertEqual(
            result["provenance"]["observation"]["band_rule"], BAND_RULE_ID
        )


class PlannerRedPinTests(unittest.TestCase):
    """red 對 planner 是 #223 收斂路徑的路由必需，非門檻可測值。"""

    def test_planner_measured_bands_pin_red(self) -> None:
        report = make_report(clears=8, runs=8)
        report["leaderboards"]["clear_rate"]["rows"][0]["role"] = "planner"
        deck = make_deck(measured_personas=("planner",))
        result = call(report, persona="planner", deck=deck)
        self.assertEqual(
            result["envelope"]["accepts_bands"], ["green", "yellow", "red"]
        )
        self.assertTrue(result["provenance"]["observation"]["red_pinned"])

    def test_planner_below_floor_gets_no_red_pin(self) -> None:
        report = make_report(clears=0, runs=8)
        report["leaderboards"]["clear_rate"]["rows"][0]["role"] = "planner"
        deck = make_deck(measured_personas=("planner",))
        result = call(report, persona="planner", deck=deck)
        self.assertEqual(result["envelope"]["accepts_bands"], [])
        self.assertFalse(result["provenance"]["observation"]["red_pinned"])
        self.assertFalse(result["provenance"]["registry_writable"])

    def test_builder_never_gets_red(self) -> None:
        result = call(make_report(clears=8, runs=8))
        self.assertNotIn("red", result["envelope"]["accepts_bands"])
        self.assertFalse(result["provenance"]["observation"]["red_pinned"])


class DefaultFallbackTests(unittest.TestCase):
    def assert_all_default(self, result: dict, accepts_reason: str) -> None:
        self.assertEqual(
            result["provenance"]["source"],
            {field: SOURCE_DEFAULT for field in ENVELOPE_FIELDS},
        )
        self.assertEqual(
            result["provenance"]["reasons"]["accepts_bands"], accepts_reason
        )
        self.assertFalse(result["provenance"]["registry_writable"])

    def test_persona_dimension_unmeasured_falls_back_to_default(self) -> None:
        # pilot-v1 只量 builder 維度：planner／reviewer 連 accepts_bands 都
        # 沒有可信來源（issue #454 相依段），一律回 #453 預設。
        report = make_report(clears=8, runs=8)
        for persona, expected_bands in (
            ("planner", list(BAND_LEVELS)),
            ("reviewer", list(BAND_LEVELS[:2])),
        ):
            with self.subTest(persona=persona):
                result = call(report, persona=persona)
                self.assert_all_default(
                    result, REASON_PERSONA_DIMENSION_UNMEASURED
                )
                self.assertEqual(
                    result["envelope"]["accepts_bands"], expected_bands
                )

    def test_identity_not_in_report_falls_back_to_default(self) -> None:
        result = call(
            make_report(),
            execution_profile_key="epk:v1:resolved:" + "d" * 64,
        )
        self.assert_all_default(result, REASON_IDENTITY_NOT_IN_REPORT)
        result = call(make_report(), deck_digest="sha256:" + "e" * 64)
        self.assert_all_default(result, REASON_IDENTITY_NOT_IN_REPORT)

    def test_incomplete_deck_sample_falls_back_to_default(self) -> None:
        # #455 §4.3：8 關全跑不抽樣；部分樣本不得產出實測封套。
        result = call(make_report(clears=4, runs=4))
        self.assert_all_default(result, REASON_INCOMPLETE_DECK_SAMPLE)
        self.assertEqual(result["provenance"]["observation"]["runs"], 4)

    def test_unmeasurable_fields_stay_default_even_when_measured(self) -> None:
        result = call(make_report(clears=8, runs=8))
        envelope = result["envelope"]
        self.assertIsNone(envelope["invariant_ceiling"])  # sentinel，非 0
        self.assertEqual(
            envelope["consistency_scope"], list(CONSISTENCY_SCOPE_DOMAIN)
        )
        self.assertEqual(
            envelope["acceptance_modes"], list(ACCEPTANCE_MODES_DOMAIN)
        )
        for field in ("invariant_ceiling", "consistency_scope", "acceptance_modes"):
            self.assertEqual(
                result["provenance"]["source"][field], SOURCE_DEFAULT
            )
            self.assertIn(
                "not-measurable", result["provenance"]["reasons"][field]
            )


class ProvenanceTests(unittest.TestCase):
    def test_fingerprint_is_the_455_sextuple_without_pricing(self) -> None:
        result = call(make_report())
        fingerprint = result["provenance"]["fingerprint"]
        self.assertEqual(
            fingerprint,
            {
                "executor": "claude",
                "model_id": "sonnet",
                "persona": "builder",
                "deck_id": "pilot-v1",
                "deck_content_sha256": "a" * 64,
                "patchmud_version": "0.0.1+abdf808",
            },
        )
        self.assertNotIn("pricing", json.dumps(fingerprint))

    def test_observation_carries_integer_counts_not_floats(self) -> None:
        result = call(make_report(clears=6, runs=8))
        observation = result["provenance"]["observation"]
        self.assertIs(type(observation["runs"]), int)
        self.assertIs(type(observation["clears"]), int)
        self.assertEqual(observation["model"], "haiku")
        self.assertEqual(observation["loadout"], "P0T0R0")
        self.assertEqual(observation["report_schema_version"], 2)


class DefaultEnvelopeConstantTests(unittest.TestCase):
    """守 #453 R1–R3 定值本身（比照 #453 R6-T2 精神：變異必轉紅）。"""

    def test_personas_cover_model_chain_exactly(self) -> None:
        self.assertEqual(set(DEFAULT_ENVELOPE), set(MODEL_CHAIN_PERSONAS))

    def test_453_values(self) -> None:
        self.assertEqual(
            DEFAULT_ENVELOPE["planner"]["accepts_bands"],
            ("green", "yellow", "red"),
        )
        self.assertEqual(
            DEFAULT_ENVELOPE["builder"]["accepts_bands"], ("green", "yellow")
        )
        self.assertEqual(
            DEFAULT_ENVELOPE["reviewer"]["accepts_bands"], ("green", "yellow")
        )
        for persona in DEFAULT_ENVELOPE:
            self.assertIsNone(DEFAULT_ENVELOPE[persona]["invariant_ceiling"])
            self.assertEqual(
                DEFAULT_ENVELOPE[persona]["consistency_scope"],
                CONSISTENCY_SCOPE_DOMAIN,
            )
            self.assertEqual(
                DEFAULT_ENVELOPE[persona]["acceptance_modes"],
                ACCEPTANCE_MODES_DOMAIN,
            )

    def test_band_strings_reuse_deck_schema_band_levels(self) -> None:
        for persona in DEFAULT_ENVELOPE:
            for band in DEFAULT_ENVELOPE[persona]["accepts_bands"]:
                self.assertIn(band, BAND_LEVELS)


class FailClosedTests(unittest.TestCase):
    def test_unsupported_schema_version_rejected(self) -> None:
        with self.assertRaises(EnvelopeMappingError):
            call(make_report(schema_version=3))

    def test_v1_report_is_opaque_and_not_rankable(self) -> None:
        with self.assertRaisesRegex(EnvelopeMappingError, "opaque"):
            map_report_to_envelope(
                make_report(schema_version=1),
                executor="claude",
                model_id="sonnet",
                persona="builder",
                deck=make_deck(),
                patchmud_version="0.0.1",
                report_model="haiku",
                report_loadout="P0T0R0",
            )

    def test_legacy_model_loadout_query_cannot_rank_v2(self) -> None:
        with self.assertRaises(EnvelopeMappingError):
            map_report_to_envelope(
                make_report(),
                executor="claude",
                model_id="sonnet",
                persona="builder",
                deck=make_deck(),
                patchmud_version="0.0.1",
                report_model="haiku",
                report_loadout="P0T0R0",
            )

    def test_non_mapping_report_rejected(self) -> None:
        with self.assertRaises(EnvelopeMappingError):
            call(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_missing_clear_rate_board_rejected(self) -> None:
        report = make_report()
        del report["leaderboards"]["clear_rate"]
        with self.assertRaises(EnvelopeMappingError):
            call(report)

    def test_non_ok_board_status_rejected(self) -> None:
        with self.assertRaises(EnvelopeMappingError):
            call(make_report(status="skipped"))

    def test_invalid_persona_rejected(self) -> None:
        with self.assertRaises(EnvelopeMappingError):
            call(make_report(), persona="shipper")

    def test_clears_above_runs_rejected(self) -> None:
        report = make_report()
        report["leaderboards"]["clear_rate"]["rows"][0]["clears"] = 9
        with self.assertRaises(EnvelopeMappingError):
            call(report)

    def test_boolean_counts_rejected(self) -> None:
        report = make_report()
        report["leaderboards"]["clear_rate"]["rows"][0]["clears"] = True
        with self.assertRaises(EnvelopeMappingError):
            call(report)

    def test_duplicate_aggregation_key_rows_rejected(self) -> None:
        report = make_report(
            extra_rows=[
                {"model": "haiku", "loadout": "P0T0R0", "runs": 8, "clears": 2}
            ]
        )
        with self.assertRaises(EnvelopeMappingError):
            call(report)

    def test_deck_missing_keys_rejected(self) -> None:
        for missing in (
            "deck_id",
            "content_sha256",
            "encounter_count",
            "measured_personas",
        ):
            deck = make_deck()
            del deck[missing]
            with self.subTest(missing=missing):
                with self.assertRaises(EnvelopeMappingError):
                    call(make_report(), deck=deck)

    def test_deck_with_invalid_measured_persona_rejected(self) -> None:
        deck = make_deck(measured_personas=("builder", "shipper"))
        with self.assertRaises(EnvelopeMappingError):
            call(make_report(), deck=deck)

    def test_empty_identity_strings_rejected(self) -> None:
        for field in (
            "executor",
            "model_id",
            "patchmud_version",
            "execution_profile_key",
        ):
            with self.subTest(field=field):
                with self.assertRaises(EnvelopeMappingError):
                    call(make_report(), **{field: "  "})


# PatchMUD golden fixtures are copied byte-for-byte from revision
# 421fadc7dc16b6ee9020bdc2333ff6fe856accaa; manifest digests are verified below.
_PATCHMUD_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "patchmud"
_PATCHMUD_REPORT_ROOT = _PATCHMUD_FIXTURE_ROOT / "report-v2"


def _fixture_report(name: str) -> dict:
    return json.loads((_PATCHMUD_REPORT_ROOT / name).read_text(encoding="utf-8"))


def _v2_query(row: dict) -> dict:
    return {
        "execution_profile_key": row["profile_id"],
        "role": row["role"],
        "benchmark_type": row["benchmark_type"],
        "deck_digest": row["deck_digest"],
        "evaluator_revision": row["evaluator_revision"],
    }


def call_v2(report: dict, query: dict, **overrides) -> dict:
    kwargs = {
        "executor": "claude",
        "model_id": "sonnet",
        "persona": "builder",
        "deck": make_deck(encounter_count=1),
        "patchmud_version": "0.0.1",
        **query,
    }
    kwargs.update(overrides)
    return map_report_to_envelope(report, **kwargs)


class PatchMUDReportV2FixtureTests(unittest.TestCase):
    def test_copied_fixture_manifest_and_all_digests_match_source(self) -> None:
        manifest_path = _PATCHMUD_FIXTURE_ROOT / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["source_revision"],
            "421fadc7dc16b6ee9020bdc2333ff6fe856accaa",
        )
        for item in manifest["fixtures"]:
            with self.subTest(path=item["path"]):
                relative = Path(item["path"])
                self.assertFalse(relative.is_absolute())
                self.assertNotIn("..", relative.parts)
                raw = (_PATCHMUD_FIXTURE_ROOT / relative).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), item["sha256"])

    def test_v2_uses_complete_cohort_identity_not_model_and_loadout(self) -> None:
        report = _fixture_report("positive.json")
        rows = report["leaderboards"]["clear_rate"]["rows"]
        fixer, quitter = rows
        # Same legacy pair on purpose: only the full v2 cohort key distinguishes them.
        quitter["model"] = fixer["model"]
        quitter["loadout"] = fixer["loadout"]
        fixer["coverage_complete"] = True
        quitter["coverage_complete"] = True
        deck = make_deck(encounter_count=1)

        fixed = call_v2(report, _v2_query(fixer), deck=deck)
        failed = call_v2(report, _v2_query(quitter), deck=deck)

        self.assertEqual(fixed["provenance"]["observation"]["profile_id"], fixer["profile_id"])
        self.assertEqual(fixed["provenance"]["observation"]["clears"], 1)
        self.assertEqual(fixed["envelope"]["accepts_bands"], ["green", "yellow"])
        self.assertEqual(failed["provenance"]["observation"]["profile_id"], quitter["profile_id"])
        self.assertEqual(failed["provenance"]["observation"]["clears"], 0)
        self.assertEqual(failed["provenance"]["reasons"]["accepts_bands"], REASON_BELOW_GREEN_FLOOR)

    def test_every_v2_cohort_key_component_is_required_for_a_match(self) -> None:
        report = _fixture_report("positive.json")
        target = report["leaderboards"]["clear_rate"]["rows"][0]
        query = _v2_query(target)
        for field in (
            "role",
            "benchmark_type",
            "profile_id",
            "deck_digest",
            "evaluator_revision",
        ):
            changed = copy.deepcopy(report)
            changed["leaderboards"]["clear_rate"]["rows"][0][field] += "-other"
            with self.subTest(field=field):
                result = call_v2(changed, query)
                self.assertEqual(
                    result["provenance"]["reasons"]["accepts_bands"],
                    REASON_IDENTITY_NOT_IN_REPORT,
                )

    def test_incomplete_v2_coverage_is_not_promoted_to_measured(self) -> None:
        report = _fixture_report("positive.json")
        target = report["leaderboards"]["clear_rate"]["rows"][0]
        result = call_v2(report, _v2_query(target))
        self.assertEqual(
            result["provenance"]["reasons"]["accepts_bands"],
            REASON_INCOMPLETE_DECK_SAMPLE,
        )
        self.assertEqual(result["provenance"]["observation"]["profile_id"], target["profile_id"])

    def test_v1_and_unknown_report_versions_fail_closed(self) -> None:
        legacy = json.loads(
            (_PATCHMUD_FIXTURE_ROOT / "legacy-v1" / "report.json").read_text(
                encoding="utf-8"
            )
        )
        for version, report in ((1, legacy), (3, _fixture_report("negative.json"))):
            with self.subTest(version=version), self.assertRaises(EnvelopeMappingError):
                map_report_to_envelope(
                    {**report, "schema_version": version},
                    executor="claude",
                    model_id="sonnet",
                    persona="builder",
                    deck=make_deck(encounter_count=1),
                    patchmud_version="0.0.1",
                    **_v2_query(_fixture_report("positive.json")["leaderboards"]["clear_rate"]["rows"][0]),
                )


class PurityTests(unittest.TestCase):
    def test_module_never_imports_patchmud(self) -> None:
        # 定案 D4：映射歸屬 cortex 側、對 patchmud 零 import（spec R4）。
        source = inspect.getsource(envelope_mapping)
        # 只鎖 import 語句（docstring 內的說明性文字不算）。
        self.assertNotRegex(source, r"(?m)^\s*(import|from)\s+patchmud")
        loaded = [
            name
            for name in list(__import__("sys").modules)
            if name == "patchmud" or name.startswith("patchmud.")
        ]
        self.assertEqual(loaded, [])


if __name__ == "__main__":
    unittest.main()
