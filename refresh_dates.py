#!/usr/bin/env python3
"""Fetch OpenRouter listing dates for the existing dataset, preserving prices and scores."""

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from refresh_prices import API_BASE, canonicalize, fetch_json

HERE = Path(__file__).parent


def match_catalog_model(row: dict, by_id: dict, by_family: dict) -> dict | None:
    """Prefer the priced catalog id, then the canonical id, then the earliest family listing."""
    openrouter_id = (row.get("openrouter_id") or "").strip()
    canonical_id = row["canonical_id"]
    model = by_id.get(openrouter_id) or by_id.get(canonical_id)
    if model is not None:
        return model
    candidates = [item for item in by_family.get(canonical_id, []) if item.get("created")]
    if not candidates and openrouter_id:
        candidates = [item for item in by_family.get(openrouter_id, []) if item.get("created")]
    return min(candidates, key=lambda item: item["created"]) if candidates else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, help="Use a saved models API response")
    args = parser.parse_args()
    if args.catalog:
        catalog = json.loads(args.catalog.read_text())["data"]
    else:
        catalog = fetch_json(f"{API_BASE}/models")["data"]
    by_id = {m["id"]: m for m in catalog}
    by_family = {}
    for model in catalog:
        for slug in (model["id"], model.get("canonical_slug") or model["id"]):
            family, _ = canonicalize(slug)
            by_family.setdefault(family, []).append(model)

    retrieved_on = datetime.now(timezone.utc).date().isoformat()
    with open(HERE / "models.csv") as f:
        rows = list(csv.DictReader(f))
    out = HERE / "model_dates.csv"
    previous = {}
    if out.exists():
        with out.open() as f:
            previous = {row["canonical_id"]: row for row in csv.DictReader(f)}
    dates = []
    for row in rows:
        cid = row["canonical_id"]
        model = match_catalog_model(row, by_id, by_family)
        listed_date = ""
        if model and model.get("created"):
            listed_date = datetime.fromtimestamp(model["created"], timezone.utc).date().isoformat()
        elif cid in previous:
            # Retained models keep their last sourced date when delisted.
            dates.append(previous[cid])
            continue
        dates.append({
            "canonical_id": cid,
            "openrouter_id": model["id"] if model else (row.get("openrouter_id") or ""),
            "listed_date": listed_date,
            "retrieved_on": retrieved_on,
            "source_url": f"{API_BASE}/models",
        })
    with open(out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(dates[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(dates)
    missing = [r["canonical_id"] for r in dates if not r["listed_date"]]
    print(f"wrote {len(dates) - len(missing)}/{len(dates)} listing dates to {out}")
    if missing:
        print("unknown dates: " + ", ".join(missing))


if __name__ == "__main__":
    main()
