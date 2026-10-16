#!/usr/bin/env python3
"""Refresh existing models' OpenRouter prices, usage and dates, then render all charts.

Run from this directory:
    python3 refresh_all.py --env-file .env

Only OPENROUTER_API_KEY is loaded from the requested file. Other assignments
are ignored, and no shell code or variable expansion is evaluated.
Curated scores and teaching overrides are preserved by refresh_prices.py.
"""

import argparse
import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import refresh_prices as prices

HERE = Path(__file__).resolve().parent


def read_openrouter_key(path: Path) -> str:
    with path.open() as handle:
        for line in handle:
            match = re.match(r"^\s*(?:export\s+)?OPENROUTER_API_KEY\s*=(.*)$", line)
            if match is None:
                continue
            values = shlex.split(match.group(1), comments=True)
            if len(values) != 1 or not values[0]:
                raise ValueError("OPENROUTER_API_KEY must have one nonempty value")
            return values[0]
    raise ValueError("OPENROUTER_API_KEY was not found in the requested file")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="Load only OPENROUTER_API_KEY from this file")
    parser.add_argument("--months", type=int, default=0, help="0 uses year-to-date daily rankings")
    args = parser.parse_args()
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("error: uv is required to run the chart generator")
    try:
        key = read_openrouter_key(args.env_file) if args.env_file else prices.load_api_key()
    except (OSError, ValueError):
        raise SystemExit("error: could not load OPENROUTER_API_KEY from the requested file") from None
    if not key:
        raise SystemExit("error: provide --env-file or set OPENROUTER_API_KEY")
    with (HERE / "openrouter_live.csv").open() as handle:
        before = {row["canonical_id"]: row for row in csv.DictReader(handle)}
    previous_meta = json.loads((HERE / "refresh_meta.json").read_text())
    with (HERE / "model_overrides.csv").open() as handle:
        scores_before = {row["canonical_id"]: row["aaii"] for row in csv.DictReader(handle)}
    print("Fetching the public model catalog and authenticated daily usage rankings...", flush=True)
    try:
        catalog = prices.fetch_catalog()
        rankings = prices.fetch_rankings(key, args.months)
    except urllib.error.HTTPError as error:
        raise SystemExit(f"error: OpenRouter returned HTTP {error.code}; no data files were changed") from None
    except urllib.error.URLError:
        raise SystemExit("error: could not reach OpenRouter; no data files were changed") from None
    # Pass the key in memory only; neither snapshots nor subprocess arguments contain it.
    del key
    retrieved_at = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix="clm-openrouter-") as directory:
        snapshot_dir = Path(directory)
        catalog_path = snapshot_dir / "catalog.json"
        rankings_path = snapshot_dir / "rankings.json"
        catalog_path.write_text(json.dumps(catalog))
        rankings_path.write_text(json.dumps(rankings))
        meta = prices.refresh(prices.parse_args([
            "--existing-only", "--catalog", str(catalog_path),
            "--rankings", str(rankings_path), "--months", str(args.months),
        ]))
        meta.update(
            catalog_source=f"{prices.API_BASE}/models",
            rankings_source=f"{prices.API_BASE}/datasets/rankings-daily",
            retrieved_at=retrieved_at,
            curated_scores_preserved=True,
        )
        # The new request does not fetch benchmarks; retain their historical provenance.
        for field in ("benchmark_source", "benchmark_meta"):
            meta[field] = previous_meta.get(field)
        (HERE / "refresh_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        subprocess.run([sys.executable, str(HERE / "build_dataset.py")], check=True)
        subprocess.run([sys.executable, str(HERE / "refresh_dates.py"), "--catalog", str(catalog_path)], check=True)
    subprocess.run([uv, "run", str(HERE / "make_chart.py")], check=True)
    with (HERE / "openrouter_live.csv").open() as handle:
        after = {row["canonical_id"]: row for row in csv.DictReader(handle)}
    assert after.keys() == before.keys(), "Model membership changed during existing-model refresh"
    with (HERE / "model_overrides.csv").open() as handle:
        scores_after = {row["canonical_id"]: row["aaii"] for row in csv.DictReader(handle)}
    assert scores_after == scores_before, "Curated scores changed during refresh"
    changed = sum(before[cid]["avg_monthly_tokens"] != row["avg_monthly_tokens"] for cid, row in after.items())
    observed = sum(row["volume_status"] == "observed" for row in after.values())
    print(f"Refreshed {len(after)} existing models: {changed} token volumes changed; {observed} models have observed usage.")
    for cid in ("openai/gpt-6.1-sol", "google/gemini-4-argon"):
        row = after.get(cid)
        if row:
            print(f"{cid}: {row['avg_monthly_tokens']} tokens/month ({row['volume_status']})")


if __name__ == "__main__":
    main()
