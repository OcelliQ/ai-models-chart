#!/usr/bin/env python3
"""Refresh OpenRouter pricing and censored usage volume for the cost chart.

Volume comes from the daily rankings feed. Each day lists the top 50 models
plus one aggregated ``other`` row, so models outside that day's top 50 are
censored rather than observed at zero. For each matched family:

    avg_monthly_tokens = round(
        sum(observed daily total_tokens)
        / (distinct days in the window / (365.2425 / 12))
    )

The denominator is the whole window, so a family that misses the top 50 on
some days is a lower bound, not a full-month measurement. ``--top`` (default
200) caps how many of those priced families are called ranked. It cannot
invent ranks the feed does not identify. When fewer than ``--top`` families
have a current standard price, catalog models with a real Artificial Analysis
score are added as ``benchmark_cohort`` with no rank and no measured volume.

Prices come from the standard catalog row. ``:free`` and ``:batch`` variants
never supply the price. ``qwen/qwen3.8-27b`` keeps the instructor prices
$0.01 / $0.02 per million. Prior rows stay in the file even outside the
cutoff; a delisted row keeps its last price and ``price_status=stale``.

Snapshots (no key required):

    python3 refresh_prices.py \\
        --catalog /tmp/clm-openrouter-catalog.json \\
        --rankings /tmp/clm-openrouter-daily.json \\
        --benchmarks /tmp/clm-openrouter-benchmarks.json \\
        --month-snapshot /tmp/clm-openrouter-month.json

A live rankings fetch needs OPENROUTER_API_KEY in the environment. This
script does not read .env or any other credential file.

Use --catalog-only to refresh prices and catalog fields for existing rows
without changing usage, scores, or model membership. No API key is required.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API_BASE = "https://openrouter.ai/api/v1"
HERE = Path(__file__).parent
DAYS_PER_MONTH = 365.2425 / 12

DATE_SUFFIX = re.compile(r"-\d{8}$")
ANTHROPIC_TIER_VERSION_SWAP = re.compile(
    r"^anthropic/claude-(\d[\d.]*)-(sonnet|opus|haiku|fable)$"
)
NONSTANDARD_TIERS = {"free", "batch"}
STEALTH_AUTHORS = {"stealth", "openrouter"}
EXCLUDED_IDS = {"cohere/north-mini-code"}
# Instructor-specified teaching price, not the live catalog rate.
PRICE_OVERRIDES = {"qwen/qwen3.8-27b": (0.01, 0.02)}
# Preserve named chart models and the requested unlabelled Grok rows.
HIGHLIGHT_IDS = {
    "anthropic/claude-fable-5",
    "openai/gpt-6-astra",
    "qwen/qwen3.8-27b",
    "moonshotai/kimi-k3",
    "anthropic/claude-opus-5.5",
    "anthropic/claude-sonnet-5.5",
    "qwen/qwen3.8-max-0902",
    "z-ai/glm-5.3-flash",
    "z-ai/glm-5.3",
    "google/gemini-4-argon",
    "openai/gpt-6.1-sol",
    "x-ai/grok-4.7",
    "x-ai/grok-4.6",
    "x-ai/grok-4.5",
}
GROK_PIN_IDS = {"x-ai/grok-4.7", "x-ai/grok-4.6", "x-ai/grok-4.5"}
GROK_PIN_SENTENCE = (
    "Pinned Grok 4.x row: Grok 4.7 is labelled; Grok 4.6 and 4.5 are included without labels."
)
LIVE_FIELDS = [
    "canonical_id",
    "openrouter_name",
    "avg_monthly_tokens",
    "price_per_m_input",
    "price_per_m_output",
    "context_length",
    "hugging_face_id",
    "openrouter_id",
    "price_status",
    "inclusion",
    "volume_status",
    "pinned",
    "volume_rank",
    "observed_days",
]
OVERRIDE_FIELDS = [
    "canonical_id",
    "display_name",
    "aaii",
    "open_weights",
    "country",
    "notes",
]


def canonicalize(permaslug: str) -> tuple[str, str]:
    """Split 'author/model-name-20260706:free' -> ('author/model-name', ':free')."""
    base, _, suffix = permaslug.partition(":")
    base = DATE_SUFFIX.sub("", base)
    # Rankings name Anthropic models "claude-{version}-{tier}" while the
    # catalog uses "claude-{tier}-{version}".
    match = ANTHROPIC_TIER_VERSION_SWAP.match(base)
    if match:
        version, tier = match.groups()
        base = f"anthropic/claude-{tier}-{version}"
    return base, (":" + suffix if suffix else "")


def _split_tier(slug: str) -> tuple[str, str]:
    base, _, tier = slug.partition(":")
    return base, tier.lower()


def _is_standard_id(model_id: str) -> bool:
    if model_id.startswith("~") or model_id.startswith("@"):
        return False
    _, tier = _split_tier(model_id)
    return tier not in NONSTANDARD_TIERS


def _is_llm(model: dict) -> bool:
    model_id = model["id"]
    if not _is_standard_id(model_id):
        return False
    author = model_id.split("/", 1)[0]
    if author in STEALTH_AUTHORS or model_id in EXCLUDED_IDS:
        return False
    outputs = (model.get("architecture") or {}).get("output_modalities") or []
    if outputs != ["text"]:
        return False
    lowered = model_id.lower()
    if any(token in lowered for token in ("embed", "rerank", "whisper", "tts", "/audio", "image")):
        return False
    return True


def _standard_prices(model: dict) -> tuple[float, float] | None:
    pricing = model.get("pricing") or {}
    try:
        prompt = float(pricing.get("prompt") or 0)
        completion = float(pricing.get("completion") or 0)
    except (TypeError, ValueError):
        return None
    if prompt <= 0 and completion <= 0:
        return None
    return prompt, completion


def format_per_million(per_token: float) -> str:
    text = f"{per_token * 1_000_000:.8f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def format_score(value: object) -> str:
    if isinstance(value, bool) or value is None or value == "":
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.10f}".rstrip("0").rstrip(".")
    return str(value)


def display_name_for(model: dict) -> str:
    name = (model.get("name") or model["id"]).strip()
    if ": " in name:
        return name.split(": ", 1)[1]
    return name


def load_document(path: Path) -> dict:
    payload = json.loads(path.read_text())
    if isinstance(payload, list):
        return {"data": payload, "meta": None}
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload
    raise SystemExit(f"unrecognized snapshot shape: {path}")


def load_api_key() -> str | None:
    """Environment only. Does not open .env or any credential file."""
    return os.environ.get("OPENROUTER_API_KEY") or None


def fetch_json(url: str, api_key: str | None = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def fetch_catalog() -> dict:
    return fetch_json(f"{API_BASE}/models")


def fetch_rankings(api_key: str, months: int) -> dict:
    end = date.today() - timedelta(days=1)
    if months > 0:
        start = _months_start(end, months)
    else:
        start = date(end.year, 1, 1)
    url = (
        f"{API_BASE}/datasets/rankings-daily?period=day"
        f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
    )
    return fetch_json(url, api_key)


def _months_start(end: date, months: int) -> date:
    year, month = end.year, end.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def filter_recent_months(rows: list[dict], months: int) -> list[dict]:
    if months <= 0 or not rows:
        return rows
    end = max(date.fromisoformat(row["date"][:10]) for row in rows)
    start = _months_start(end, months)
    return [row for row in rows if date.fromisoformat(row["date"][:10]) >= start]


class Catalog:
    def __init__(self, models: list[dict]):
        self.models = [model for model in models if model.get("id")]
        self.by_id = {model["id"]: model for model in self.models}
        self.by_slug: dict[str, list[dict]] = defaultdict(list)
        for model in self.models:
            slug = model.get("canonical_slug") or ""
            if slug:
                self.by_slug[slug].append(model)

    def resolve_standard(self, permaslug: str) -> dict | None:
        """Match exact id, then exact canonical_slug, then one normalized family.

        Normalization (date suffix and Anthropic tier order) runs only when it
        still identifies a single standard model. ``:free`` and ``:batch``
        never win over a standard row that shares the slug.
        """
        if not permaslug or permaslug == "other":
            return None
        base, _tier = _split_tier(permaslug)
        if base == "other":
            return None
        exact: list[dict] = []
        if base in self.by_id:
            exact.append(self.by_id[base])
        exact.extend(self.by_slug.get(base, []))
        chosen = _one_standard(exact, base)
        if chosen is not None:
            return chosen
        slugs = {model.get("canonical_slug") for model in exact if model.get("canonical_slug")}
        siblings: list[dict] = []
        for slug in slugs:
            siblings.extend(self.by_slug.get(slug, []))
        chosen = _one_standard(siblings, base)
        if chosen is not None:
            return chosen
        norm, _suffix = canonicalize(base)
        hits = []
        for model in self.models:
            if not _is_standard_id(model["id"]):
                continue
            keys = [model["id"], model.get("canonical_slug") or ""]
            if any(key and (key == norm or canonicalize(key)[0] == norm) for key in keys):
                hits.append(model)
        unique = {model["id"]: model for model in hits}
        if len(unique) == 1:
            return next(iter(unique.values()))
        short = [model for model in unique.values() if model["id"] == norm]
        if len(short) == 1:
            return short[0]
        return None


def _one_standard(models: list[dict], base: str) -> dict | None:
    unique = {model["id"]: model for model in models if _is_standard_id(model["id"])}
    if not unique:
        return None
    if len(unique) == 1:
        return next(iter(unique.values()))
    return min(
        unique.values(),
        key=lambda model: (
            0 if model["id"] == base or model.get("canonical_slug") == base else 1,
            len(model["id"]),
            model["id"],
        ),
    )


def _dataset_id(model: dict, existing_by_model_id: dict[str, str]) -> str:
    return existing_by_model_id.get(model["id"], model["id"])


def index_benchmarks(rows: list[dict], catalog: Catalog) -> dict[str, dict]:
    """Exact id or canonical_slug only, so neighboring checkpoints do not share a score."""
    matched: dict[str, dict] = {}
    for row in rows:
        if row.get("intelligence_index") is None or row.get("intelligence_index") == "":
            continue
        slug = row.get("model_permaslug") or ""
        model = catalog.resolve_standard(slug)
        if model is None:
            continue
        if slug != model["id"] and slug != (model.get("canonical_slug") or ""):
            continue
        current = matched.get(model["id"])
        if current is None or _prefer_benchmark(row, current):
            matched[model["id"]] = row
    return matched


def _prefer_benchmark(candidate: dict, current: dict) -> bool:
    def rank(row: dict) -> tuple:
        name = (row.get("display_name") or "").lower()
        if "(max" in name:
            effort = 0
        elif "xhigh" in name:
            effort = 1
        else:
            effort = 2
        return (effort, -float(row["intelligence_index"]))

    return rank(candidate) < rank(current)


def aggregate_volume(
    rows: list[dict], catalog: Catalog, existing_by_model_id: dict[str, str]
) -> tuple[dict[str, int], dict[str, int], dict]:
    window = sorted({row["date"][:10] for row in rows})
    equivalents = (len(window) / DAYS_PER_MONTH) if window else 1.0
    totals: dict[str, int] = defaultdict(int)
    days: dict[str, set[str]] = defaultdict(set)
    stats = {
        "ranking_rows": len(rows),
        "other_rows": 0,
        "skipped_stealth": 0,
        "skipped_non_llm": 0,
        "skipped_unpriced_or_unmatched": 0,
        "unmatched_slugs": defaultdict(int),
    }
    for row in rows:
        slug = row.get("model_permaslug") or ""
        if slug == "other":
            stats["other_rows"] += 1
            continue
        base, _tier = _split_tier(slug)
        author = base.split("/", 1)[0].lstrip("~")
        if author in STEALTH_AUTHORS:
            stats["skipped_stealth"] += 1
            continue
        model = catalog.resolve_standard(slug)
        if model is None or not _is_llm(model) or _standard_prices(model) is None:
            stats["skipped_unpriced_or_unmatched"] += 1
            if model is None or not _is_llm(model):
                stats["unmatched_slugs"][slug] += int(row.get("total_tokens") or 0)
            continue
        canonical_id = _dataset_id(model, existing_by_model_id)
        totals[canonical_id] += int(row["total_tokens"])
        days[canonical_id].add(row["date"][:10])
    volumes = {
        canonical_id: int(round(total / equivalents))
        for canonical_id, total in totals.items()
    }
    observed = {canonical_id: len(seen) for canonical_id, seen in days.items()}
    stats["window_start"] = window[0] if window else ""
    stats["window_end"] = window[-1] if window else ""
    stats["window_days"] = len(window)
    stats["month_equivalents"] = equivalents
    stats["priced_families"] = len(volumes)
    top_unmatched = sorted(stats["unmatched_slugs"].items(), key=lambda item: -item[1])[:20]
    stats["unmatched_top"] = [{"model_permaslug": slug, "observed_tokens": tokens} for slug, tokens in top_unmatched]
    del stats["unmatched_slugs"]
    return volumes, observed, stats


def _existing_model_index(catalog: Catalog, existing_ids: set[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for canonical_id in sorted(existing_ids):
        model = catalog.resolve_standard(canonical_id)
        if model is None:
            continue
        mapping.setdefault(model["id"], canonical_id)
    return mapping


def _price_fields(canonical_id: str, model: dict | None, previous: dict | None) -> dict:
    override = PRICE_OVERRIDES.get(canonical_id)
    if model is not None and _standard_prices(model) is not None:
        prompt, completion = _standard_prices(model)
        prices = {
            "price_per_m_input": format_per_million(prompt),
            "price_per_m_output": format_per_million(completion),
            "context_length": model.get("context_length") or "",
            "hugging_face_id": model.get("hugging_face_id") or "",
            "openrouter_name": model.get("name") or canonical_id,
            "openrouter_id": model["id"],
            "price_status": "live",
        }
    elif previous is not None:
        prices = {
            "price_per_m_input": previous.get("price_per_m_input", ""),
            "price_per_m_output": previous.get("price_per_m_output", ""),
            "context_length": previous.get("context_length", ""),
            "hugging_face_id": previous.get("hugging_face_id", ""),
            "openrouter_name": previous.get("openrouter_name", canonical_id),
            "openrouter_id": "",
            # Manually sourced provider prices were never catalog listings.
            "price_status": "external" if previous.get("price_status") == "external" else "stale",
        }
    else:
        return {}
    if override is not None and prices["price_status"] != "stale":
        prices["price_per_m_input"] = format_per_million(override[0] / 1_000_000)
        prices["price_per_m_output"] = format_per_million(override[1] / 1_000_000)
        prices["price_status"] = "override"
    return prices


def _blank_volume(previous: dict | None) -> dict:
    if previous and previous.get("volume_status") == "nominal":
        return {
            "avg_monthly_tokens": previous["avg_monthly_tokens"],
            "volume_status": "nominal",
            "observed_days": "",
            "volume_rank": "",
        }
    if previous and int(previous.get("avg_monthly_tokens") or 0) > 0:
        return {
            "avg_monthly_tokens": previous["avg_monthly_tokens"],
            "volume_status": "preserved",
            "observed_days": "",
            "volume_rank": "",
        }
    return {
        "avg_monthly_tokens": "0",
        "volume_status": "not_observed",
        "observed_days": "",
        "volume_rank": "",
    }


def build_live_rows(
    catalog: Catalog,
    volumes: dict[str, int],
    observed_days: dict[str, int],
    benchmarks: dict[str, dict],
    previous_rows: list[dict],
    top: int,
    existing_only: bool = False,
) -> tuple[list[dict], dict]:
    previous = {row["canonical_id"]: row for row in previous_rows}
    existing_by_model_id = _existing_model_index(catalog, set(previous))
    model_by_canonical: dict[str, dict] = {}
    for model in catalog.models:
        if not _is_standard_id(model["id"]):
            continue
        model_by_canonical[_dataset_id(model, existing_by_model_id)] = model

    ranked_ids = [
        canonical_id
        for canonical_id, _volume in sorted(volumes.items(), key=lambda item: (-item[1], item[0]))
        if not existing_only or canonical_id in previous
    ][:top]
    rows: list[dict] = []
    present: set[str] = set()

    def add(canonical_id: str, inclusion: str, volume_bits: dict) -> None:
        if canonical_id in present:
            return
        model = model_by_canonical.get(canonical_id) or catalog.resolve_standard(canonical_id)
        if model is not None and not _is_standard_id(model["id"]):
            model = None
        prices = _price_fields(canonical_id, model, previous.get(canonical_id))
        if not prices:
            return
        row = {"canonical_id": canonical_id, "inclusion": inclusion, "pinned": "false"}
        row.update(prices)
        row.update(volume_bits)
        rows.append(row)
        present.add(canonical_id)

    for rank, canonical_id in enumerate(ranked_ids, start=1):
        add(
            canonical_id,
            "ranked",
            {
                "avg_monthly_tokens": str(volumes[canonical_id]),
                "volume_status": "observed",
                "observed_days": str(observed_days.get(canonical_id, "")),
                "volume_rank": str(rank),
            },
        )
    cohort_ids: list[str] = []
    if len(ranked_ids) < top:
        candidates = []
        for model in catalog.models:
            if not _is_llm(model) or _standard_prices(model) is None:
                continue
            benchmark = benchmarks.get(model["id"])
            if benchmark is None:
                continue
            canonical_id = _dataset_id(model, existing_by_model_id)
            if existing_only and canonical_id not in previous:
                continue
            if canonical_id in present:
                continue
            candidates.append((float(benchmark["intelligence_index"]), canonical_id))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        for _score, canonical_id in candidates[: top - len(ranked_ids)]:
            add(
                canonical_id,
                "benchmark_cohort",
                {
                    "avg_monthly_tokens": "0",
                    "volume_status": "not_observed",
                    "observed_days": "",
                    "volume_rank": "",
                },
            )
            cohort_ids.append(canonical_id)
    for canonical_id, prev in previous.items():
        if canonical_id in present:
            continue
        if canonical_id in volumes:
            bits = {
                "avg_monthly_tokens": str(volumes[canonical_id]),
                "volume_status": "observed",
                "observed_days": str(observed_days.get(canonical_id, "")),
                "volume_rank": "",
            }
        else:
            bits = _blank_volume(prev)
        inclusion = "pinned" if prev.get("price_status") == "external" and canonical_id in HIGHLIGHT_IDS else "retained"
        add(canonical_id, inclusion, bits)

    for canonical_id in sorted(HIGHLIGHT_IDS):
        if existing_only and canonical_id not in previous:
            continue
        if canonical_id in present:
            continue
        bits = _blank_volume(None)
        if canonical_id in volumes:
            bits = {
                "avg_monthly_tokens": str(volumes[canonical_id]),
                "volume_status": "observed",
                "observed_days": str(observed_days.get(canonical_id, "")),
                "volume_rank": "",
            }
        add(canonical_id, "pinned", bits)
    for row in rows:
        if row["canonical_id"] in HIGHLIGHT_IDS:
            row["pinned"] = "true"
    summary = {
        "ranked": sum(1 for row in rows if row["inclusion"] == "ranked"),
        "retained": sum(1 for row in rows if row["inclusion"] == "retained"),
        "benchmark_cohort": sum(1 for row in rows if row["inclusion"] == "benchmark_cohort"),
        "pinned_only": sum(1 for row in rows if row["inclusion"] == "pinned"),
        "pinned_marked": sum(1 for row in rows if row["pinned"] == "true"),
        "stale": [row["canonical_id"] for row in rows if row["price_status"] == "stale"],
        "price_overrides": [row["canonical_id"] for row in rows if row["price_status"] == "override"],
        "cohort_ids": cohort_ids,
        "rows": len(rows),
        "top_requested": top,
        "ranked_shortfall": max(0, top - len(ranked_ids)),
    }
    return rows, summary


def _author_countries(override_rows: list[dict]) -> dict[str, str]:
    found: dict[str, set[str]] = defaultdict(set)
    for row in override_rows:
        country = (row.get("country") or "").strip()
        if country:
            found[row["canonical_id"].split("/", 1)[0]].add(country)
    return {author: next(iter(countries)) for author, countries in found.items() if len(countries) == 1}


def _qwen_note(model: dict | None, benchmark: dict | None) -> str:
    if model is not None and _standard_prices(model) is not None:
        prompt, completion = _standard_prices(model)
        live = f"${format_per_million(prompt)}/${format_per_million(completion)}"
    else:
        live = "unavailable"
    measured = ""
    if benchmark is not None:
        measured = (
            f" The benchmarks snapshot lists {benchmark.get('display_name') or 'Qwen3.8 27B'} "
            f"at {format_score(benchmark.get('intelligence_index'))}; that measured value is not "
            "substituted for the instructor estimate."
        )
    return (
        "AAII stored here remains the instructor estimate 37 from the Qwen3.6-27B predecessor, "
        "kept because the original row was an explicit estimate rather than a measured score."
        f"{measured} Price is the instructor override $0.01/$0.02 per million input/output, "
        f"not the live standard catalog price ({live} per million)."
    )


def merge_overrides(
    existing: list[dict],
    live_rows: list[dict],
    catalog: Catalog,
    benchmarks: dict[str, dict],
    benchmark_meta: dict | None,
) -> list[dict]:
    by_id = {row["canonical_id"]: dict(row) for row in existing}
    countries = _author_countries(existing)
    as_of = (benchmark_meta or {}).get("as_of", "")
    source = (benchmark_meta or {}).get("source_url", "https://artificialanalysis.ai")
    for live in live_rows:
        canonical_id = live["canonical_id"]
        model = None
        openrouter_id = live.get("openrouter_id") or ""
        if openrouter_id:
            model = catalog.by_id.get(openrouter_id)
        if model is None:
            model = catalog.resolve_standard(canonical_id)
        benchmark = benchmarks.get(model["id"]) if model is not None else None
        if canonical_id in by_id:
            continue
        if model is None:
            continue
        score = format_score(benchmark.get("intelligence_index")) if benchmark else ""
        author = canonical_id.split("/", 1)[0]
        if score:
            note = (
                f"AAII {score} is {benchmark.get('display_name') or display_name_for(model)} "
                f"from Artificial Analysis via the OpenRouter benchmarks snapshot"
                f"{(' as of ' + as_of) if as_of else ''}, source {source}, "
                f"permaslug {benchmark.get('model_permaslug')}."
            )
        else:
            note = (
                "No intelligence_index matched this catalog id in the benchmarks snapshot; "
                "AAII left blank."
            )
        if live["inclusion"] == "benchmark_cohort":
            note += (
                " Benchmark cohort only: not a usage rank, and avg_monthly_tokens is not a measurement."
            )
        if canonical_id == "openai/gpt-6.1-sol":
            note += (
                " Pinned. Standard catalog price is $2/$10 per million; the :batch variant is $1/$5 and is not used."
            )
        if canonical_id in GROK_PIN_IDS:
            note += " " + GROK_PIN_SENTENCE
        by_id[canonical_id] = {
            "canonical_id": canonical_id,
            "display_name": display_name_for(model),
            "aaii": score,
            "open_weights": "true" if (model.get("hugging_face_id") or "").strip() else "false",
            "country": countries.get(author, ""),
            "notes": note,
        }
    if "qwen/qwen3.8-27b" in by_id:
        model = catalog.by_id.get("qwen/qwen3.8-27b") or catalog.resolve_standard("qwen/qwen3.8-27b")
        benchmark = benchmarks.get(model["id"]) if model is not None else None
        row = by_id["qwen/qwen3.8-27b"]
        row["aaii"] = "37"
        row["notes"] = _qwen_note(model, benchmark)
    for canonical_id in sorted(GROK_PIN_IDS):
        row = by_id.get(canonical_id)
        if row is None:
            continue
        notes = row.get("notes") or ""
        if GROK_PIN_SENTENCE not in notes:
            row["notes"] = (notes + " " + GROK_PIN_SENTENCE).strip()
    order = [row["canonical_id"] for row in live_rows]
    extras = sorted(set(by_id) - set(order))
    return [by_id[canonical_id] for canonical_id in order + extras if canonical_id in by_id]


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build_meta(
    catalog_source: str,
    rankings_source: str,
    rankings_meta: dict | None,
    month_doc: dict | None,
    benchmark_source: str,
    benchmark_meta: dict | None,
    volume_stats: dict,
    summary: dict,
) -> dict:
    return {
        "catalog_source": catalog_source,
        "rankings_source": rankings_source,
        "rankings_meta": rankings_meta,
        "month_snapshot_used_for_volume": False,
        "month_snapshot_meta": None if month_doc is None else month_doc.get("meta"),
        "month_snapshot_rows": None if month_doc is None else len(month_doc.get("data") or []),
        "benchmark_source": benchmark_source,
        "benchmark_meta": benchmark_meta,
        "volume_method": (
            "avg_monthly_tokens = round(sum of observed daily total_tokens / "
            "(distinct days in the rankings window / (365.2425/12))). "
            "Each day contributes only its top 50 model_permaslug rows; the aggregated "
            "other row is not expanded. Days a family misses the top 50 are censored, "
            "not filled with zeros, so the average is a lower bound."
        ),
        "volume": volume_stats,
        "selection": summary,
        "limitation": (
            None
            if summary["ranked_shortfall"] == 0
            else (
                f"The censored daily top-50 feed identifies {volume_stats['priced_families']} "
                f"LLM families with a current standard price, which is under the requested "
                f"top {summary['top_requested']}. Ranked rows stop at "
                f"{summary['ranked']}. {summary['benchmark_cohort']} additional catalog "
                "models with a real benchmark score were added as benchmark_cohort with "
                "volume_status=not_observed. They are not top-200 usage ranks and their "
                "zero avg_monthly_tokens is a sentinel, not a measured volume."
            )
        ),

    }


def refresh_catalog_only(args: argparse.Namespace, catalog: Catalog, catalog_source: str) -> dict:
    if not args.previous or not Path(args.previous).exists():
        raise SystemExit("error: --catalog-only requires an existing --previous CSV")
    with Path(args.previous).open(newline="") as handle:
        previous_rows = list(csv.DictReader(handle))
    rows = []
    changed = []
    for previous in previous_rows:
        canonical_id = previous["canonical_id"]
        model = catalog.resolve_standard(previous.get("openrouter_id") or canonical_id)
        if model is None:
            model = catalog.resolve_standard(canonical_id)
        row = dict(previous)
        row.update(_price_fields(canonical_id, model, previous))
        if any(str(value) != str(previous.get(field, "")) for field, value in row.items()):
            changed.append(canonical_id)
        rows.append(row)
    meta_path = Path(args.meta_out) if args.meta_out else None
    meta = json.loads(meta_path.read_text()) if meta_path and meta_path.exists() else {}
    meta["catalog_source"] = catalog_source
    meta["catalog_refresh"] = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source_url": f"{API_BASE}/models",
        "catalog_models": len(catalog.models),
        "existing_rows": len(rows),
        "changed_ids": changed,
        "stale_ids": [row["canonical_id"] for row in rows if row["price_status"] == "stale"],
        "usage_and_scores_preserved": True,
    }
    _write_csv(Path(args.out), rows, LIVE_FIELDS)
    if meta_path:
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"refreshed catalog fields for {len(rows)} existing models ({len(changed)} changed) to {args.out}", file=sys.stderr)
    return meta


def refresh(args: argparse.Namespace) -> dict:
    if args.catalog:
        catalog_doc = load_document(args.catalog)
        catalog_source = str(args.catalog)
    else:
        catalog_doc = fetch_catalog()
        catalog_source = f"{API_BASE}/models"
    catalog = Catalog(catalog_doc["data"])

    if args.catalog_only:
        return refresh_catalog_only(args, catalog, catalog_source)

    if args.rankings:
        rankings_doc = load_document(args.rankings)
        rankings_source = str(args.rankings)
    else:
        api_key = load_api_key()
        if not api_key:
            print(
                "error: rankings snapshot not provided and OPENROUTER_API_KEY is not in the environment",
                file=sys.stderr,
            )
            sys.exit(1)
        rankings_doc = fetch_rankings(api_key, args.months)
        rankings_source = f"{API_BASE}/datasets/rankings-daily"
    ranking_rows = filter_recent_months(rankings_doc["data"], args.months)
    month_doc = load_document(args.month_snapshot) if args.month_snapshot else None
    if args.benchmarks:
        benchmark_doc = load_document(args.benchmarks)
        benchmark_source = str(args.benchmarks)
    else:
        benchmark_doc = {"data": [], "meta": None}
        benchmark_source = ""
    benchmarks = index_benchmarks(benchmark_doc["data"], catalog)

    previous_path = Path(args.previous) if args.previous else None
    previous_rows = []
    if previous_path and previous_path.exists():
        with previous_path.open(newline="") as handle:
            previous_rows = list(csv.DictReader(handle))
    existing_by_model_id = _existing_model_index(catalog, {row["canonical_id"] for row in previous_rows})
    volumes, observed_days, volume_stats = aggregate_volume(ranking_rows, catalog, existing_by_model_id)
    live_rows, summary = build_live_rows(
        catalog, volumes, observed_days, benchmarks, previous_rows, args.top,
        existing_only=args.existing_only,
    )
    existing_overrides = []
    overrides_path = Path(args.overrides) if args.overrides else None
    if overrides_path and overrides_path.exists():
        with overrides_path.open(newline="") as handle:
            existing_overrides = list(csv.DictReader(handle))
    override_rows = merge_overrides(
        existing_overrides, live_rows, catalog, benchmarks, benchmark_doc.get("meta")
    )
    meta = build_meta(
        catalog_source,
        rankings_source,
        rankings_doc.get("meta"),
        month_doc,
        benchmark_source,
        benchmark_doc.get("meta"),
        volume_stats,
        summary,
    )
    meta["existing_models_only"] = args.existing_only
    if args.existing_only:
        meta["limitation"] = (
            f"Refresh restricted to {len(previous_rows)} existing models. "
            f"{summary['ranked']} have observed usage in the censored daily top-50 feed. "
            "Unobserved models retain prior volumes or nominal placeholders; "
            "missing daily ranks are not measured zero usage. No new models were added."
        )
    _write_csv(Path(args.out), live_rows, LIVE_FIELDS)
    if overrides_path:
        _write_csv(overrides_path, override_rows, OVERRIDE_FIELDS)
    meta_path = Path(args.meta_out) if args.meta_out else None
    if meta_path:
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(
        f"wrote {len(live_rows)} rows ({summary['ranked']} ranked, "
        f"{summary['benchmark_cohort']} benchmark_cohort, {summary['retained']} retained, "
        f"{summary['pinned_only']} pinned-only) to {args.out}",
        file=sys.stderr,
    )
    if summary["ranked_shortfall"]:
        print(meta["limitation"], file=sys.stderr)
    return meta


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int, default=0, help="0 uses the whole rankings snapshot")
    parser.add_argument("--top", type=int, default=200)
    parser.add_argument("--existing-only", action="store_true", help="Refresh only models already in the previous CSV")
    parser.add_argument("--catalog-only", action="store_true", help="Refresh catalog fields for existing models, preserving usage and scores; no API key required")
    parser.add_argument("--catalog", type=Path, help="Saved GET /api/v1/models JSON")
    parser.add_argument("--rankings", type=Path, help="Saved rankings-daily JSON, preferably period=day")
    parser.add_argument("--benchmarks", type=Path, help="Saved Artificial Analysis benchmark snapshot")
    parser.add_argument(
        "--month-snapshot",
        type=Path,
        help="Monthly rankings JSON recorded for provenance only; not used for volume",
    )
    parser.add_argument("--previous", type=Path, default=HERE / "openrouter_live.csv")
    parser.add_argument("--overrides", type=Path, default=HERE / "model_overrides.csv")
    parser.add_argument("--out", type=Path, default=HERE / "openrouter_live.csv")
    parser.add_argument("--meta-out", type=Path, default=HERE / "refresh_meta.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    refresh(parse_args(argv))


if __name__ == "__main__":
    main()
