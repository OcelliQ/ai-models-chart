"""Focused checks for the cost-chart refresh. No network and no credential files."""

import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import refresh_dates
import refresh_prices as rp
import refresh_all


def _model(model_id, slug, prompt, completion, name=None, hugging_face_id=None, outputs=None):
    return {
        "id": model_id,
        "canonical_slug": slug,
        "name": name or model_id,
        "pricing": {"prompt": str(prompt), "completion": str(completion)},
        "context_length": 1050000,
        "hugging_face_id": hugging_face_id,
        "created": 1790702882,
        "architecture": {"output_modalities": outputs or ["text"]},
    }


class RefreshTests(unittest.TestCase):
    def test_env_file_reads_only_openrouter_key_without_executing_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            marker = Path(directory) / "must-not-exist"
            path.write_text(f'OTHER=$(touch {marker})\nexport OPENROUTER_API_KEY="test-key" # comment\n')
            self.assertEqual(refresh_all.read_openrouter_key(path), "test-key")
            self.assertFalse(marker.exists())
            path.write_text("UNRELATED=value\n")
            with self.assertRaises(ValueError):
                refresh_all.read_openrouter_key(path)

    def test_existing_only_excludes_new_ranked_scored_and_pinned_models(self):
        catalog = rp.Catalog([
            _model("openai/old", "openai/old", 0.000001, 0.000002),
            _model("new/ranked", "new/ranked", 0.000001, 0.000002),
            _model("new/scored", "new/scored", 0.000001, 0.000002),
            _model("openai/gpt-6.1-sol", "openai/gpt-6.1-sol", 0.000002, 0.00001),
        ])
        previous = [{"canonical_id": "openai/old", "avg_monthly_tokens": "50", "price_per_m_input": "1", "price_per_m_output": "2"}]
        rows, _ = rp.build_live_rows(catalog, {"openai/old": 123, "new/ranked": 500}, {"openai/old": 3}, {"new/scored": {"intelligence_index": 50}}, previous, top=200, existing_only=True)
        self.assertEqual([row["canonical_id"] for row in rows], ["openai/old"])
        self.assertEqual(rows[0]["avg_monthly_tokens"], "123")
        self.assertEqual(rows[0]["volume_status"], "observed")

    def test_catalog_only_preserves_membership_usage_scores_and_rankings_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous_path = root / "previous.csv"
            out_path = root / "live.csv"
            overrides_path = root / "overrides.csv"
            meta_path = root / "meta.json"
            catalog_path = root / "catalog.json"
            rows = []
            for cid, status in [("openai/gpt-6.1-sol", "live"), ("old/delisted", "live"), ("google/gemini-4-argon", "external"), ("qwen/qwen3.8-27b", "override")]:
                row = dict.fromkeys(rp.LIVE_FIELDS, "")
                row.update(canonical_id=cid, price_per_m_input="4", price_per_m_output="20", price_status=status, avg_monthly_tokens="123", volume_status="observed", volume_rank="7", observed_days="20", inclusion="ranked")
                rows.append(row)
            rp._write_csv(previous_path, rows, rp.LIVE_FIELDS)
            overrides_path.write_text("canonical_id,aaii\nopenai/gpt-6.1-sol,51.8\n")
            overrides_before = overrides_path.read_bytes()
            rankings_meta = {"as_of": "2026-09-30", "end_date": "2026-09-29"}
            meta_path.write_text(json.dumps({"rankings_meta": rankings_meta}))
            catalog_path.write_text(json.dumps({"data": [
                _model("openai/gpt-6.1-sol", "openai/gpt-6.1-sol", 0.000002, 0.00001),
                _model("openai/gpt-6.1-sol:batch", "openai/gpt-6.1-sol", 0.000001, 0.000005),
                _model("qwen/qwen3.8-27b", "qwen/qwen3.8-27b", 0.000003, 0.000004),
                _model("new/model", "new/model", 0.000001, 0.000002),
            ]}))
            args = rp.parse_args(["--catalog-only", "--catalog", str(catalog_path), "--previous", str(previous_path), "--out", str(out_path), "--overrides", str(overrides_path), "--meta-out", str(meta_path)])
            meta = rp.refresh(args)
            with out_path.open() as handle:
                refreshed = list(csv.DictReader(handle))
            self.assertEqual([row["canonical_id"] for row in refreshed], [row["canonical_id"] for row in rows])
            for before, after in zip(rows, refreshed):
                for field in ("avg_monthly_tokens", "volume_status", "volume_rank", "observed_days", "inclusion"):
                    self.assertEqual(after[field], before[field])
            self.assertEqual(refreshed[0]["price_per_m_input"], "2")
            self.assertEqual(refreshed[0]["price_per_m_output"], "10")
            self.assertEqual(refreshed[1]["price_status"], "stale")
            self.assertEqual(refreshed[1]["price_per_m_input"], "4")
            self.assertEqual(refreshed[2]["price_status"], "external")
            self.assertEqual(refreshed[2]["price_per_m_output"], "20")
            self.assertEqual(refreshed[3]["price_per_m_input"], "0.01")
            self.assertEqual(refreshed[3]["price_per_m_output"], "0.02")
            self.assertEqual(overrides_path.read_bytes(), overrides_before)
            self.assertEqual(meta["rankings_meta"], rankings_meta)

    def test_external_pin_preserves_nominal_usage_until_observed(self):
        previous = [{
            "canonical_id": "google/gemini-4-argon",
            "openrouter_name": "Google: Gemini 4 Argon",
            "avg_monthly_tokens": "1",
            "price_per_m_input": "4",
            "price_per_m_output": "20",
            "context_length": "1000000",
            "price_status": "external",
            "volume_status": "nominal",
        }]
        rows, summary = rp.build_live_rows(rp.Catalog([]), {}, {}, {}, previous, top=200)
        argon = rows[0]
        self.assertEqual(argon["price_status"], "external")
        self.assertEqual(argon["price_per_m_input"], "4")
        self.assertEqual(argon["price_per_m_output"], "20")
        self.assertEqual(argon["openrouter_id"], "")
        self.assertEqual(argon["inclusion"], "pinned")
        self.assertEqual(argon["pinned"], "true")
        self.assertEqual(argon["volume_status"], "nominal")
        self.assertEqual(argon["avg_monthly_tokens"], "1")
        self.assertEqual(argon["volume_rank"], "")
        self.assertEqual(argon["observed_days"], "")
        self.assertEqual(summary["stale"], [])

        catalog = rp.Catalog([_model("google/gemini-4-argon", "google/gemini-4-argon-20260930", 0.000004, 0.00002)])
        refreshed, _ = rp.build_live_rows(catalog, {argon["canonical_id"]: 500}, {argon["canonical_id"]: 1}, {}, rows, top=200)
        self.assertEqual(refreshed[0]["price_status"], "live")
        self.assertEqual(refreshed[0]["volume_status"], "observed")
        self.assertEqual(refreshed[0]["avg_monthly_tokens"], "500")
        self.assertEqual(refreshed[0]["volume_rank"], "1")

    def test_default_top_is_200(self):
        args = rp.parse_args([])
        self.assertEqual(args.top, 200)

    def test_batch_and_free_do_not_set_the_price(self):
        catalog = rp.Catalog(
            [
                _model("openai/gpt-6.1-sol", "openai/gpt-6.1-sol-20260929", 0.000002, 0.00001, name="OpenAI: GPT-6.1 Sol"),
                _model("openai/gpt-6.1-sol:batch", "openai/gpt-6.1-sol-20260929", 0.000001, 0.000005, name="OpenAI: GPT-6.1 Sol (batch)"),
                _model("qwen/qwen3.8-27b", "qwen/qwen3.8-27b-20260814", 0.0000000249, 0.00000435, name="Qwen: Qwen3.8 27B"),
                _model("qwen/qwen3.8-27b:free", "qwen/qwen3.8-27b-20260814", 0, 0, name="Qwen: Qwen3.8 27B (free)"),
            ]
        )
        sol = catalog.resolve_standard("openai/gpt-6.1-sol-20260929:batch")
        self.assertEqual(sol["id"], "openai/gpt-6.1-sol")
        self.assertEqual(rp._standard_prices(sol), (0.000002, 0.00001))
        qwen = catalog.resolve_standard("qwen/qwen3.8-27b:free")
        self.assertEqual(qwen["id"], "qwen/qwen3.8-27b")
        prices = rp._price_fields("qwen/qwen3.8-27b", qwen, None)
        self.assertEqual(prices["price_status"], "override")
        self.assertEqual(prices["price_per_m_input"], "0.01")
        self.assertEqual(prices["price_per_m_output"], "0.02")

    def test_exact_slug_beats_date_normalization(self):
        catalog = rp.Catalog(
            [
                _model("deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-flash-20260423", 0.0000001, 0.0000002, name="DeepSeek: V4 Flash 0423"),
                _model("deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-v4-flash-20260731", 0.0000003, 0.0000004, name="DeepSeek: V4 Flash 0731"),
            ]
        )
        self.assertEqual(
            catalog.resolve_standard("deepseek/deepseek-v4-flash-20260731")["id"],
            "deepseek/deepseek-v4-flash-0731",
        )
        self.assertEqual(
            catalog.resolve_standard("deepseek/deepseek-v4-flash-20260423")["id"],
            "deepseek/deepseek-v4-flash",
        )

    def test_anthropic_ranking_order_matches_catalog_id(self):
        catalog = rp.Catalog(
            [_model("anthropic/claude-sonnet-4.6", "anthropic/claude-sonnet-4.6-20260217", 0.000003, 0.000015)]
        )
        self.assertEqual(
            catalog.resolve_standard("anthropic/claude-4.6-sonnet")["id"],
            "anthropic/claude-sonnet-4.6",
        )

    def test_daily_volume_uses_month_equivalents_and_ignores_other(self):
        catalog = rp.Catalog([_model("openai/gpt-6-luna", "openai/gpt-6-luna-20260922", 0.000001, 0.000002, name="OpenAI: GPT-6 Luna")])
        rows = []
        for day in ("2026-09-01", "2026-09-02"):
            rows.append({"date": day, "model_permaslug": "openai/gpt-6-luna-20260922", "total_tokens": "100"})
            rows.append({"date": day, "model_permaslug": "other", "total_tokens": "999999"})
        volumes, observed, stats = rp.aggregate_volume(rows, catalog, {})
        equivalents = 2 / rp.DAYS_PER_MONTH
        self.assertEqual(volumes["openai/gpt-6-luna"], round(200 / equivalents))
        self.assertEqual(observed["openai/gpt-6-luna"], 2)
        self.assertEqual(stats["other_rows"], 2)
        self.assertEqual(stats["priced_families"], 1)

    def test_missing_days_are_not_imputed(self):
        catalog = rp.Catalog([_model("openai/gpt-6-luna", "openai/gpt-6-luna-20260922", 0.000001, 0.000002)])
        rows = [
            {"date": "2026-09-01", "model_permaslug": "other", "total_tokens": "1"},
            {"date": "2026-09-02", "model_permaslug": "openai/gpt-6-luna", "total_tokens": "100"},
        ]
        volumes, _observed, stats = rp.aggregate_volume(rows, catalog, {})
        equivalents = stats["window_days"] / rp.DAYS_PER_MONTH
        self.assertEqual(stats["window_days"], 2)
        self.assertEqual(volumes["openai/gpt-6-luna"], round(100 / equivalents))

    def test_short_rank_adds_benchmark_cohort_without_a_rank(self):
        catalog = rp.Catalog(
            [
                _model("openai/ranked", "openai/ranked-20260101", 0.000002, 0.000002, name="OpenAI: Ranked"),
                _model("openai/scored", "openai/scored-20260101", 0.000003, 0.000003, name="OpenAI: Scored"),
                _model("openai/unscored", "openai/unscored", 0.000004, 0.000004, name="OpenAI: Unscored"),
            ]
        )
        benchmarks = rp.index_benchmarks(
            [
                {
                    "model_permaslug": "openai/scored-20260101",
                    "display_name": "Scored (max)",
                    "intelligence_index": 51.8,
                },
                {
                    "model_permaslug": "openai/nearby-not-this-model",
                    "display_name": "Other",
                    "intelligence_index": 99,
                },
            ],
            catalog,
        )
        self.assertEqual(set(benchmarks), {"openai/scored"})
        previous = [
            {
                "canonical_id": "retired/model",
                "openrouter_name": "Retired",
                "avg_monthly_tokens": "15",
                "price_per_m_input": "1",
                "price_per_m_output": "2",
                "context_length": "10",
                "hugging_face_id": "",
            }
        ]
        rows, summary = rp.build_live_rows(
            catalog,
            {"openai/ranked": 500},
            {"openai/ranked": 3},
            benchmarks,
            previous,
            top=2,
        )
        by_id = {row["canonical_id"]: row for row in rows}
        self.assertEqual(by_id["openai/ranked"]["inclusion"], "ranked")
        self.assertEqual(by_id["openai/ranked"]["volume_rank"], "1")
        self.assertEqual(by_id["openai/scored"]["inclusion"], "benchmark_cohort")
        self.assertEqual(by_id["openai/scored"]["volume_status"], "not_observed")
        self.assertEqual(by_id["openai/scored"]["avg_monthly_tokens"], "0")
        self.assertEqual(by_id["openai/scored"]["volume_rank"], "")
        self.assertNotIn("openai/unscored", by_id)
        self.assertEqual(by_id["retired/model"]["inclusion"], "retained")
        self.assertEqual(by_id["retired/model"]["price_status"], "stale")
        self.assertEqual(by_id["retired/model"]["price_per_m_input"], "1")
        self.assertEqual(by_id["retired/model"]["volume_status"], "preserved")
        self.assertEqual(summary["ranked"], 1)
        self.assertEqual(summary["benchmark_cohort"], 1)
        # Repeating a refresh must keep the unobserved benchmark cohort honest.
        repeated, repeat_summary = rp.build_live_rows(
            catalog, {"openai/ranked": 500}, {"openai/ranked": 3}, benchmarks, rows, top=2
        )
        repeated_by_id = {row["canonical_id"]: row for row in repeated}
        self.assertEqual(repeated_by_id["openai/scored"]["inclusion"], "benchmark_cohort")
        self.assertEqual(repeated_by_id["openai/scored"]["volume_status"], "not_observed")
        self.assertEqual(repeat_summary["benchmark_cohort"], 1)

    def test_prior_scores_stay_and_new_score_uses_exact_benchmark(self):
        catalog = rp.Catalog(
            [
                _model("qwen/qwen3.8-27b", "qwen/qwen3.8-27b-20260814", 0.0000000249, 0.00000435, name="Qwen: Qwen3.8 27B"),
                _model("openai/gpt-6.1-sol", "openai/gpt-6.1-sol-20260929", 0.000002, 0.00001, name="OpenAI: GPT-6.1 Sol"),
                _model("x-ai/grok-4.7", "x-ai/grok-4.7-20260916", 0.000002, 0.000006, name="xAI: Grok 4.7"),
            ]
        )
        benchmarks = rp.index_benchmarks(
            [
                {"model_permaslug": "qwen/qwen3.8-27b-20260814", "display_name": "Qwen3.8 27B (xhigh)", "intelligence_index": 33.7},
                {"model_permaslug": "openai/gpt-6.1-sol-20260929", "display_name": "GPT-6.1 Sol (max)", "intelligence_index": 51.8},
                {"model_permaslug": "x-ai/grok-4.7-20260916", "display_name": "Grok 4.7 (xhigh)", "intelligence_index": 46.4},
            ],
            catalog,
        )
        existing = [
            {
                "canonical_id": "qwen/qwen3.8-27b",
                "display_name": "Qwen3.8 27B",
                "aaii": "37",
                "open_weights": "true",
                "country": "China",
                "notes": "old estimate",
            },
            {
                "canonical_id": "x-ai/grok-4.6",
                "display_name": "Grok 4.6",
                "aaii": "44",
                "open_weights": "false",
                "country": "USA",
                "notes": "kept score",
            },
            {
                "canonical_id": "openai/gpt-6-astra",
                "display_name": "GPT-6 Astra",
                "aaii": "53",
                "open_weights": "false",
                "country": "USA",
                "notes": "existing",
            },
        ]
        live = [
            {"canonical_id": "qwen/qwen3.8-27b", "openrouter_id": "qwen/qwen3.8-27b", "inclusion": "ranked"},
            {"canonical_id": "openai/gpt-6.1-sol", "openrouter_id": "openai/gpt-6.1-sol", "inclusion": "ranked"},
            {"canonical_id": "x-ai/grok-4.7", "openrouter_id": "x-ai/grok-4.7", "inclusion": "ranked"},
            {"canonical_id": "x-ai/grok-4.6", "openrouter_id": "x-ai/grok-4.6", "inclusion": "ranked"},
        ]
        merged = rp.merge_overrides(existing, live, catalog, benchmarks, {"as_of": "2026-09-30T12:00:56.392Z", "source_url": "https://artificialanalysis.ai"})
        by_id = {row["canonical_id"]: row for row in merged}
        self.assertEqual(by_id["qwen/qwen3.8-27b"]["aaii"], "37")
        self.assertIn("33.7", by_id["qwen/qwen3.8-27b"]["notes"])
        self.assertIn("$0.01/$0.02", by_id["qwen/qwen3.8-27b"]["notes"])
        self.assertEqual(by_id["x-ai/grok-4.6"]["aaii"], "44")
        self.assertIn("Grok 4.x", by_id["x-ai/grok-4.6"]["notes"])
        self.assertEqual(by_id["openai/gpt-6.1-sol"]["aaii"], "51.8")
        self.assertEqual(by_id["openai/gpt-6.1-sol"]["country"], "USA")
        self.assertEqual(by_id["openai/gpt-6.1-sol"]["open_weights"], "false")
        self.assertEqual(by_id["x-ai/grok-4.7"]["aaii"], "46.4")

    def test_named_models_survive_outside_the_cutoff(self):
        catalog = rp.Catalog(
            [
                _model("openai/big", "openai/big", 0.000002, 0.000002, name="OpenAI: Big"),
                _model("openai/gpt-6.1-sol", "openai/gpt-6.1-sol-20260929", 0.000002, 0.00001, name="OpenAI: GPT-6.1 Sol"),
            ]
        )
        previous = [
            {
                "canonical_id": "moonshotai/kimi-k3",
                "openrouter_name": "MoonshotAI: Kimi K3",
                "avg_monthly_tokens": "9",
                "price_per_m_input": "0.5",
                "price_per_m_output": "2",
                "context_length": "100",
                "hugging_face_id": "moonshot/kimi",
            }
        ]
        rows, _summary = rp.build_live_rows(catalog, {"openai/big": 10}, {"openai/big": 1}, {}, previous, top=1)
        by_id = {row["canonical_id"]: row for row in rows}
        self.assertEqual(by_id["moonshotai/kimi-k3"]["pinned"], "true")
        self.assertEqual(by_id["moonshotai/kimi-k3"]["inclusion"], "retained")
        self.assertEqual(by_id["openai/gpt-6.1-sol"]["inclusion"], "pinned")
        self.assertEqual(by_id["openai/gpt-6.1-sol"]["pinned"], "true")
        self.assertEqual(by_id["openai/gpt-6.1-sol"]["volume_status"], "not_observed")

    def test_snapshot_cli_writes_provenance_without_calling_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = {
                "data": [
                    _model("openai/gpt-6.1-sol", "openai/gpt-6.1-sol-20260929", 0.000002, 0.00001, name="OpenAI: GPT-6.1 Sol"),
                    _model("openai/old", "openai/old-20260101", 0.000001, 0.000001, name="OpenAI: Old"),
                ]
            }
            rankings = {
                "data": [
                    {"date": "2026-09-29", "model_permaslug": "openai/old-20260101", "total_tokens": "50"},
                    {"date": "2026-09-29", "model_permaslug": "other", "total_tokens": "10"},
                ],
                "meta": {"as_of": "2026-09-30T14:20:26.162Z", "version": "v1", "start_date": "2026-09-29", "end_date": "2026-09-29"},
            }
            benchmarks = {
                "data": [
                    {"model_permaslug": "openai/gpt-6.1-sol-20260929", "display_name": "GPT-6.1 Sol (max)", "intelligence_index": 51.8}
                ],
                "meta": {"as_of": "2026-09-30T12:00:56.392Z", "source": "artificial-analysis", "source_url": "https://artificialanalysis.ai"},
            }
            month = {"data": [{"date": "2026-08-01", "model_permaslug": "other", "total_tokens": "1"}], "meta": {"as_of": "2026-09-30T14:20:20.315Z"}}
            previous = root / "previous.csv"
            with previous.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["canonical_id", "openrouter_name", "avg_monthly_tokens", "price_per_m_input", "price_per_m_output", "context_length", "hugging_face_id"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "canonical_id": "gone/model",
                        "openrouter_name": "Gone",
                        "avg_monthly_tokens": "4",
                        "price_per_m_input": "3",
                        "price_per_m_output": "4",
                        "context_length": "8",
                        "hugging_face_id": "",
                    }
                )
            for name, payload in (
                ("catalog.json", catalog),
                ("rankings.json", rankings),
                ("benchmarks.json", benchmarks),
                ("month.json", month),
            ):
                (root / name).write_text(json.dumps(payload))
            out = root / "live.csv"
            overrides = root / "overrides.csv"
            meta = root / "meta.json"
            rp.main(
                [
                    "--catalog",
                    str(root / "catalog.json"),
                    "--rankings",
                    str(root / "rankings.json"),
                    "--benchmarks",
                    str(root / "benchmarks.json"),
                    "--month-snapshot",
                    str(root / "month.json"),
                    "--previous",
                    str(previous),
                    "--overrides",
                    str(overrides),
                    "--out",
                    str(out),
                    "--meta-out",
                    str(meta),
                    "--top",
                    "2",
                ]
            )
            saved = json.loads(meta.read_text())
            self.assertFalse(saved["month_snapshot_used_for_volume"])
            self.assertEqual(saved["rankings_meta"]["as_of"], "2026-09-30T14:20:26.162Z")
            self.assertEqual(saved["month_snapshot_meta"]["as_of"], "2026-09-30T14:20:20.315Z")
            self.assertIn("top 50", saved["volume_method"])
            with out.open(newline="") as handle:
                live = list(csv.DictReader(handle))
            self.assertEqual({row["canonical_id"] for row in live}, {"openai/old", "openai/gpt-6.1-sol", "gone/model"})
            sol = next(row for row in live if row["canonical_id"] == "openai/gpt-6.1-sol")
            self.assertEqual(sol["price_per_m_input"], "2")
            self.assertEqual(sol["price_per_m_output"], "10")
            self.assertEqual(sol["inclusion"], "benchmark_cohort")
            gone = next(row for row in live if row["canonical_id"] == "gone/model")
            self.assertEqual(gone["price_status"], "stale")
            self.assertEqual(gone["price_per_m_input"], "3")

    def test_listing_date_prefers_priced_catalog_id(self):
        catalog_models = [
            {"id": "openai/gpt-6.1-sol", "canonical_slug": "openai/gpt-6.1-sol-20260929", "created": 1790702882},
            {"id": "older/family", "canonical_slug": "older/family-20260101", "created": 1767225600},
        ]
        by_id = {model["id"]: model for model in catalog_models}
        by_family = {}
        for model in catalog_models:
            for slug in (model["id"], model["canonical_slug"]):
                family, _suffix = rp.canonicalize(slug)
                by_family.setdefault(family, []).append(model)
        matched = refresh_dates.match_catalog_model(
            {"canonical_id": "openai/gpt-6.1-sol-20260929", "openrouter_id": "openai/gpt-6.1-sol"},
            by_id,
            by_family,
        )
        self.assertEqual(matched["id"], "openai/gpt-6.1-sol")
        listed = datetime.fromtimestamp(matched["created"], timezone.utc).date().isoformat()
        self.assertEqual(listed, "2026-09-29")


if __name__ == "__main__":
    unittest.main()
