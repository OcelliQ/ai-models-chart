#!/usr/bin/env python3
"""Consolidate durable parameter-count research without assigning hosting costs."""

import csv
import io
import json
from pathlib import Path

from build_hardware import atomic_write

HERE = Path(__file__).resolve().parent
FIELDS = [
    "canonical_id", "total_params_b", "backbone_params_b", "lookup_params_b",
    "count_status", "sources", "checked_on", "notes",
]


def main():
    research = HERE / "hardware-research"
    state = json.loads((research / "state.json").read_text())
    counts = {}
    expected = set()
    for group in state["groups"]:
        directory = research / group
        expected.update(r["canonical_id"] for r in json.loads((directory / "input.json").read_text()))
        with (directory / "parameter_counts.csv").open() as f:
            for row in csv.DictReader(f):
                canonical_id = row["canonical_id"]
                if canonical_id in counts:
                    raise ValueError(f"Duplicate parameter count: {canonical_id}")
                if row["count_status"] not in {"sourced", "estimated", "unknown"}:
                    raise ValueError(f"Invalid count status: {canonical_id}")
                if row["total_params_b"]:
                    if float(row["total_params_b"]) <= 0 or not row["sources"]:
                        raise ValueError(f"Invalid or unsourced parameter count: {canonical_id}")
                elif row["count_status"] != "unknown":
                    raise ValueError(f"Missing parameter count: {canonical_id}")
                if row["backbone_params_b"] and row["lookup_params_b"]:
                    parts = float(row["backbone_params_b"]) + float(row["lookup_params_b"])
                    if abs(parts - float(row["total_params_b"])) > 0.1:
                        raise ValueError(f"Parameter decomposition mismatch: {canonical_id}")
                counts[canonical_id] = row
    if set(counts) != expected:
        raise ValueError(f"Incomplete parameter research: missing={sorted(expected - set(counts))}, extra={sorted(set(counts) - expected)}")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(counts[key] for key in sorted(counts))
    atomic_write(HERE / "model_parameters.csv", output.getvalue())
    print(f"wrote model_parameters.csv: {len(counts)} models, {sum(bool(r['total_params_b']) for r in counts.values())} parameter counts")


if __name__ == "__main__":
    main()
