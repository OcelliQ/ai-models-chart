#!/usr/bin/env python3
"""Build indicative hosting-cost bands from counts and resident-memory research.

This never rewrites models.csv or fetches live data. JSON research checkpoints
and hosting_tiers.json are the curated sources. Writes are atomic.
"""

import csv
import io
import json
import math
import os
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIELDS = [
    "canonical_id", "hosting_status", "classification_basis", "hardware_tier",
    "hardware_cost_min_sgd", "hardware_cost_max_sgd", "hardware_cost_label",
    "hardware_description", "concurrent_sessions", "total_params_b", "count_status",
    "weights_gib", "single_session_memory_gib", "required_memory_gib",
    "quantization", "context_tokens", "sources", "checked_on", "notes",
]


def atomic_write(path, text):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        try:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


def parameter_tier(params_b, tiers):
    if not math.isfinite(params_b) or params_b <= 0:
        raise ValueError(f"Invalid parameter count: {params_b}")
    return next(i for i, tier in enumerate(tiers) if tier["max_params_b"] is None or params_b <= tier["max_params_b"])


def memory_for_sessions(params_b, single_session_memory, sessions, policy):
    """Shared weights once; nominal per-session memory added for concurrency."""
    headroom = policy["per_session_headroom_gb"] * 1e9 / 2**30
    weights = params_b * 1e9 * policy["precision_bits"] / 8 / 2**30
    fallback = weights * policy["weight_overhead_factor"] + sessions * headroom
    researched = single_session_memory + (sessions - 1) * headroom
    return max(fallback, researched)


def load_research():
    state = json.loads((HERE / "hardware-research/state.json").read_text())
    results = {}
    for group in state["groups"]:
        directory = HERE / "hardware-research" / group
        for path in sorted(directory.glob("*.json")):
            if path.name in {"input.json", "progress.json"}:
                continue
            result = json.loads(path.read_text())
            canonical_id = result["canonical_id"]
            if canonical_id in results:
                raise ValueError(f"Duplicate hosting research for {canonical_id}")
            if result["status"] not in {"estimated", "verified", "unknown"}:
                raise ValueError(f"Invalid research status for {canonical_id}")
            for field in ("weights_gib", "required_memory_gib"):
                value = result.get(field)
                if value is not None and (not math.isfinite(value) or value <= 0):
                    raise ValueError(f"Invalid {field} for {canonical_id}")
            if result["status"] != "unknown":
                if not result.get("sources") or not result.get("required_memory_gib"):
                    raise ValueError(f"Unsourced hosting estimate for {canonical_id}")
                if result.get("context_tokens") != state["context_tokens"]:
                    raise ValueError(f"Wrong context workload for {canonical_id}")
                if result["required_memory_gib"] < (result.get("weights_gib") or 0):
                    raise ValueError(f"Memory smaller than weights for {canonical_id}")
            results[canonical_id] = result
    return results


def classify(model, research, parameters, policy):
    result = {field: "" for field in FIELDS}
    result["canonical_id"] = model["canonical_id"]
    if model["open_weights"] == "false":
        result.update(hosting_status="api_only", notes="Weights unavailable; no public self-hosting configuration.")
        return result
    result["hosting_status"] = "unknown"
    result["notes"] = "No sourced hosting research yet."
    if research:
        for field in ("weights_gib", "quantization", "context_tokens", "checked_on", "notes"):
            result[field] = research.get(field) if research.get(field) is not None else ""
        result["single_session_memory_gib"] = research.get("required_memory_gib") or ""
        result["sources"] = " | ".join(research.get("sources", []))
    if not parameters or not parameters["total_params_b"]:
        return result
    params_b = float(parameters["total_params_b"])
    result.update(total_params_b=params_b, count_status=parameters["count_status"], context_tokens=policy["context_tokens"])
    source_urls = set(result["sources"].split(" | ")) | set(parameters["sources"].split("|"))
    result["sources"] = " | ".join(sorted(url.strip() for url in source_urls if url.strip()))
    if int(model.get("context_length") or 0) < policy["context_tokens"]:
        result["notes"] += " Catalog context is shorter than the 108K-token chart task; no qualifying fit assigned."
        return result
    tiers = policy["tiers"]
    override = policy.get("model_overrides", {}).get(model["canonical_id"])
    if override:
        tier = next(t for t in tiers if t["id"] == override["hardware_tier"])
        source_urls.update(override["sources"])
        result.update(
            hosting_status="estimated", classification_basis="user_override",
            hardware_tier=tier["id"], hardware_cost_min_sgd=tier["cost_min_sgd"],
            hardware_cost_max_sgd=tier["cost_max_sgd"] if tier["cost_max_sgd"] is not None else "",
            hardware_cost_label=tier["cost_label"], hardware_description=tier["hardware_label"],
            concurrent_sessions=tier["concurrent_sessions"],
            weights_gib="", single_session_memory_gib="", required_memory_gib="",
            quantization=override["quantization"], checked_on=override["checked_on"],
            sources=" | ".join(sorted(url.strip() for url in source_urls if url.strip())),
            notes=override["notes"],
        )
        return result
    initial = parameter_tier(params_b, tiers)
    single_memory = (research or {}).get("required_memory_gib") or 0
    for index in range(initial, len(tiers)):
        tier = tiers[index]
        sessions = tier["concurrent_sessions"]
        memory = memory_for_sessions(params_b, single_memory, sessions, policy)
        if tier["planning_memory_gib"] is None or memory <= tier["planning_memory_gib"]:
            break
    result.update(
        hosting_status="estimated",
        classification_basis="memory_adjusted" if index > initial else "parameter_fallback",
        hardware_tier=tier["id"], hardware_cost_min_sgd=tier["cost_min_sgd"],
        hardware_cost_max_sgd=tier["cost_max_sgd"] if tier["cost_max_sgd"] is not None else "",
        hardware_cost_label=tier["cost_label"], hardware_description=tier["hardware_label"],
        concurrent_sessions=sessions, required_memory_gib=round(memory, 1),
    )
    if not result["quantization"]:
        result["quantization"] = "~4-bit full model"
    if not result["checked_on"]:
        result["checked_on"] = parameters["checked_on"]
    result["notes"] += f" Indicative SGD budget band from total parameters; {sessions} concurrent 108K-token sessions budgeted with shared weights and nominal 16GB/session headroom."
    if index > initial:
        result["notes"] += " Raised above parameter fallback to accommodate the resident-memory estimate."
    result["notes"] += " This is not a vendor quote, a verified deployment, or a throughput guarantee."
    return result


def main():
    state = json.loads((HERE / "hardware-research/state.json").read_text())
    if state.get("proposed_fallback", {}).get("approval_status") != "approved":
        raise SystemExit("Hosting assignments await approval of the proposed parameter-to-cost mapping.")
    with (HERE / "models.csv").open() as f:
        models = list(csv.DictReader(f))
    policy = json.loads((HERE / "hosting_tiers.json").read_text())
    with (HERE / "model_parameters.csv").open() as f:
        parameters = {row["canonical_id"]: row for row in csv.DictReader(f)}
    research = load_research()
    rows = [classify(model, research.get(model["canonical_id"]), parameters.get(model["canonical_id"]), policy) for model in models]
    # Both requested Flash models must remain above the one-Spark tier,
    # including any explicitly approved model-specific exception.
    for row in rows:
        if row["canonical_id"] in {"deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3-flash"}:
            if row["hardware_tier"] in {"desktop", "spark"}:
                raise ValueError(f"Approved minimum tier violated: {row['canonical_id']}")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(HERE / "model_hardware.csv", output.getvalue())
    scored_ids = {m["canonical_id"] for m in models if m["aaii"]}
    counts = Counter(r["hardware_tier"] or r["hosting_status"] for r in rows if r["canonical_id"] in scored_ids)
    print(f"wrote model_hardware.csv: {len(rows)} rows; scored classifications: {dict(counts)}")


if __name__ == "__main__":
    main()
