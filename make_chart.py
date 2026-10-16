"""Render the cost-vs-intelligence scatter.

X: price of a 100K-input / 8K-output task, log scale
Y: Artificial Analysis Intelligence Index

Marker size encodes avg_monthly_tokens: log-scaled, capped at MAX_MARKER_PX
diameter, floored at MIN_MARKER_PX. The same log scale is shared across
every chart variant so a given size means the same volume on every chart.

Writes five colorations of the same plot into dist/:
  - cost-vs-aaii                  -- open weights (blue) vs closed (orange),
                                      dataviz-skill categorical slots 1/2
                                      (CVD Delta E 24.7, normal-vision 33.6).
  - cost-vs-aaii-by-lab           -- OpenAI, Google, Anthropic and SpaceXAI
                                      get distinct colors; SpaceXAI uses the
                                      validated custom sienna also used for
                                      Alibaba in the open-lab chart. Other
                                      labs use gray at half the opacity.
  - cost-vs-aaii-by-lab-open      -- same full set of models as
                                      cost-vs-aaii-by-lab.svg (dots only
                                      change color, none are added or
                                      removed), but highlights open-weight
                                      labs instead: DeepSeek, Z.ai, MiniMax
                                      (ranked by a combination of model
                                      count and total monthly volume) on
                                      slots 1/2/3, plus Alibaba/Qwen on a
                                      custom 4th color (COLOR_QWEN, hand-
                                      picked and validated against slots
                                      1-3 since the theme's own slot 4
                                      fails the all-pairs gate -- see that
                                      constant's comment); everyone else,
                                      including closed labs, folds to
                                      "Other".

Each coloration is written four times, at the same figure size:

  dist/svg/<stem>.svg         text remains <text> (svg.fonttype none)
  dist/svg-paths/<stem>.svg   text converted to paths
  dist/pdf/<stem>.pdf         vector page; dist/pdf/all.pdf collects every chart
  dist/png/<stem>.png         300 dpi raster

The figure size is the original slide content box (metropolis theme, 16:9,
heading slide, zero left/right margin): page 841.89x473.56pt, content box
left=0.0pt top=60.0pt right=841.89pt bottom=443.56pt -> 841.89 x 383.56pt.
Shrunk 0.5% so a full-bleed placement does not clip. These files are the
unannotated chart. Titles and callouts belong in the consuming presentation.

The fourth variant, cost-vs-aaii-by-age, colors the same points by months
since OpenRouter listing using model_dates.csv and the first 70% of Plasma,
reversed (orange = recent, full purple = 12 months or older). Listing date is a launch-date proxy, not
an official release date. Age is measured in days / (365.2425 / 12), relative
to rankings_meta.end_date in refresh_meta.json, not the wall clock. Unknown
dates remain plotted in gray.

Requires Python 3.11 or newer. Run with: uv run make_chart.py

The fifth variant, cost-vs-aaii-by-hardware, colors by indicative SGD
hosting budget from build_hardware.py / hosting_tiers.json. Closed models
and unknown estimates are gray as Other. Higher tiers budget parallel sessions.
Use --hardware-only to regenerate this variant without touching the others.
"""

import argparse
import csv
import json
import math
import os
import tempfile
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.offsetbox import AnchoredOffsetbox, DrawingArea, HPacker, TextArea
from matplotlib.patches import Circle
from matplotlib.lines import Line2D
from matplotlib.backends.backend_pdf import PdfPages

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
STEM_WEIGHTS = "cost-vs-aaii"
STEM_LAB = "cost-vs-aaii-by-lab"
STEM_LAB_OPEN = "cost-vs-aaii-by-lab-open"
STEM_AGE = "cost-vs-aaii-by-age"
STEM_HARDWARE = "cost-vs-aaii-by-hardware"
PNG_DPI = 300
AGE_MAX_MONTHS = 12
INTELLIGENCE_CUTOFF = date(2026, 6, 9)
AGE_CMAP = LinearSegmentedColormap.from_list(
    "plasma_age", [plt.get_cmap("plasma")(0.7 * (1 - i / 255)) for i in range(256)],
)
# Warm desktop tiers turn blue at the cluster/server boundary.
HARDWARE_COLORS = {
    "desktop": AGE_CMAP(0.0),
    "spark": AGE_CMAP(0.2),
    "large_desktop": AGE_CMAP(0.4),
    "small_server": "#2a78d6",
    "large_server": "#1c4d89",
    "cluster": "#102b4e",
}

# dataviz skill: reference palette (validated)
COLOR_OPEN = "#2a78d6"  # categorical slot 1 (blue)
COLOR_CLOSED = "#eb6834"  # categorical slot 2 (orange)
COLOR_SLOT_3 = "#1baf7a"  # categorical slot 3 (aqua)
COLOR_OTHER = "#898781"  # neutral "Other" bucket -- muted ink, not a hue slot
# custom 4th color for the lab charts, originally at the user's request
# for brown specifically. Not part of the 8-slot reference theme (its own
# slot 4, yellow, fails the all-pairs normal-vision floor against orange:
# 13.7 < 15). This sienna was picked and checked by hand against slots
# 1-3 with validate_palette.js and clears every gate (worst all-pairs
# normal-vision 15.7 vs orange, CVD 9.2 deutan/9.6 tritan).
COLOR_QWEN = "#a0522d"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SURFACE = "#fcfcfb"

# canonical_id author prefix -> display lab name
LAB_NAMES = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "x-ai": "SpaceXAI",
    "spacexai": "SpaceXAI",
    "qwen": "Alibaba",
    "z-ai": "Z.ai",
    "moonshotai": "Moonshot AI",
    "minimax": "MiniMax",
    "deepseek": "DeepSeek",
    "tencent": "Tencent",
    "nvidia": "NVIDIA",
    "mistralai": "Mistral AI",
    "xiaomi": "Xiaomi",
    "stepfun": "StepFun",
    "meta-llama": "Meta",
    "meta": "Meta",
    "upstage": "Upstage",
    "poolside": "Poolside",
    "inclusionai": "InclusionAI",
}
# Closed-source labs highlighted individually; the fourth uses custom sienna.
TOP_LABS = ("OpenAI", "Google", "Anthropic", "SpaceXAI")
# open-weight labs highlighted on cost-vs-aaii-by-lab-open.svg: DeepSeek,
# Z.ai, MiniMax ranked by a combination of model count and total monthly
# token volume (tied 2nd-most open models but by far the most volume;
# most open models at 8; tied 2nd-most open models, edges out Z.ai on raw
# volume, respectively), plus Alibaba/Qwen called out by request using
# COLOR_QWEN, a custom 4th color -- see its comment
TOP_LABS_OPEN = {
    "DeepSeek": COLOR_OPEN,
    "Z.ai": COLOR_CLOSED,
    "MiniMax": COLOR_SLOT_3,
    "Alibaba": COLOR_QWEN,
}

# measured "Cost of AI" slide content box, in points (see module docstring)
CONTENT_W_PT = 841.89
CONTENT_H_PT = 383.56
SAFETY = 0.995  # shrink slightly so nothing overflows/clips at the page edge
FIG_W_IN = (CONTENT_W_PT * SAFETY) / 72
FIG_H_IN = (CONTENT_H_PT * SAFETY) / 72

# always labelled regardless of Pareto-frontier membership
ALWAYS_LABEL = {
    "anthropic/claude-fable-5.1", "openai/gpt-6-astra", "qwen/qwen3.8-27b",
    "moonshotai/kimi-k3",  # priciest open-weights model, not on the frontier
    "anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5",
    "z-ai/glm-5.3-flash", "z-ai/glm-5.3",
    "openai/gpt-6.1-sol", "x-ai/grok-4.7", "google/gemini-4-argon",
    "meta/muse-spark-1.3",
}
NEVER_LABEL = {
    "anthropic/claude-fable-5", "x-ai/grok-4.6", "x-ai/grok-4.5",
    "openai/gpt-5.6-sol", "openai/gpt-6-sol", "openai/gpt-6-luna",
    "xiaomi/mimo-v2.6-flash",
}
# Position labels around crowded points.
LABEL_PLACEMENTS = {
    "z-ai/glm-5.3-flash": ((0, 22), "center"),
    "google/gemini-4-argon": ((-12, 0), "right"),
    "openai/gpt-6.1-sol": ((6, 0), "left"),
    "x-ai/grok-4.7": ((-6, 32), "right"),
    "moonshotai/kimi-k3": ((-36, 0), "right"),
    "z-ai/glm-5.3": ((0, -22), "center"),
    "openai/gpt-6-astra": ((6, 10), "left"),
    "anthropic/claude-fable-5.1": ((18, -12), "left"),
    "meta/muse-spark-1.3": ((-6, 5), "right"),
}

# marker size encodes avg_monthly_tokens, log-scaled, capped at a max
# diameter in px (rendered at dpi=200, so px = points * dpi/72)
MAX_MARKER_PX = 96
MIN_MARKER_PX = 12
PX_PER_PT = 200 / 72

plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["svg.fonttype"] = "none"
plt.rcParams["pdf.fonttype"] = 42


def _diameter_px_to_s(diameter_px):
    """matplotlib scatter `s` is marker area in points^2."""
    diameter_pt = diameter_px / PX_PER_PT
    return math.pi * (diameter_pt / 2) ** 2


def load_rows():
    with open(HERE / "models.csv") as f:
        return list(csv.DictReader(f))


def prepare_rows():
    rows = load_rows()
    # Unknown scores/prices remain in the dataset, and zero-cost entries
    # cannot be represented on a logarithmic price axis.
    rows = [
        r for r in rows
        if r["aaii"] and r["price_per_task_100k_in_8k_out"]
        and float(r["price_per_task_100k_in_8k_out"]) > 0
    ]

    for r in rows:
        r["_price"] = float(r["price_per_task_100k_in_8k_out"])
        r["_aaii"] = float(r["aaii"])
        r["_lab"] = LAB_NAMES.get(r["canonical_id"].split("/")[0], r["canonical_id"].split("/")[0])

    # a couple of manually-pinned rows have avg_monthly_tokens=0 (not ranked
    # by real volume); floor them at half the smallest real volume so the
    # log scale below stays defined
    # Nominal placeholders do not set the observed-volume scale.
    volumes = [int(r["avg_monthly_tokens"]) for r in rows if r.get("volume_status") != "nominal"]
    floor_volume = min(v for v in volumes if v > 0) / 2
    for r in rows:
        r["_volume"] = floor_volume if r.get("volume_status") == "nominal" else max(int(r["avg_monthly_tokens"]), floor_volume)

    log_min = math.log10(min(r["_volume"] for r in rows))
    log_max = math.log10(max(r["_volume"] for r in rows))

    def size_for_volume(volume):
        t = (math.log10(volume) - log_min) / (log_max - log_min)
        diameter_px = MIN_MARKER_PX + t * (MAX_MARKER_PX - MIN_MARKER_PX)
        return _diameter_px_to_s(diameter_px)

    for r in rows:
        r["_size"] = size_for_volume(r["_volume"])

    return rows


def compute_labels(rows):
    """Pareto frontier (lower price AND higher AAII is strictly better) plus
    ALWAYS_LABEL, restricted to whatever subset of rows is passed in -- so a
    filtered chart (e.g. open-weights only) doesn't label a point that isn't
    actually plotted on it."""
    frontier = []
    for r in sorted(rows, key=lambda r: r["_price"]):
        if not frontier or r["_aaii"] > frontier[-1]["_aaii"]:
            frontier.append(r)

    to_label = list(frontier)
    labelled_ids = {r["canonical_id"] for r in to_label}
    for r in rows:
        if r["canonical_id"] in ALWAYS_LABEL and r["canonical_id"] not in labelled_ids:
            to_label.append(r)
            labelled_ids.add(r["canonical_id"])
    return [r for r in to_label if r["canonical_id"] not in NEVER_LABEL]


def snapshot_as_of():
    """Measure listing age against the committed rankings window, not the wall clock."""
    meta = json.loads((HERE / "refresh_meta.json").read_text())
    end = (meta.get("rankings_meta") or {}).get("end_date")
    if not end:
        raise SystemExit("refresh_meta.json is missing rankings_meta.end_date")
    return date.fromisoformat(end)


def atomic_savefig(fig, path, **kwargs):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=path.suffix, delete=False) as handle:
        temporary = handle.name
    try:
        fig.savefig(temporary, facecolor=SURFACE, **kwargs)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_outputs(fig, stem, combined=None):
    """Write text SVG, path SVG, PDF, and PNG for one chart."""
    previous = plt.rcParams["svg.fonttype"]
    try:
        plt.rcParams["svg.fonttype"] = "none"
        atomic_savefig(fig, DIST / "svg" / f"{stem}.svg", format="svg")
        plt.rcParams["svg.fonttype"] = "path"
        atomic_savefig(fig, DIST / "svg-paths" / f"{stem}.svg", format="svg")
    finally:
        plt.rcParams["svg.fonttype"] = previous
    atomic_savefig(fig, DIST / "pdf" / f"{stem}.pdf", format="pdf")
    atomic_savefig(fig, DIST / "png" / f"{stem}.png", format="png", dpi=PNG_DPI)
    if combined is not None:
        combined.savefig(fig, facecolor=SURFACE)
    print(
        f"wrote {stem} at {FIG_W_IN:.3f}in x {FIG_H_IN:.3f}in "
        f"({FIG_W_IN * 72:.1f}pt x {FIG_H_IN * 72:.1f}pt)"
    )


def render(rows, to_label, groups, stem, age_as_of=None, marker_alpha=0.85, hardware_tiers=None, combined=None):
    """groups: list of (predicate, color, legend_label), in legend order."""
    fig, ax = plt.subplots(figsize=(FIG_W_IN, FIG_H_IN), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    for predicate, color, label in groups:
        # Astra and Fable 5.1 share coordinates; draw Astra separately above all groups.
        points = [r for r in rows if predicate(r) and r["canonical_id"] != "openai/gpt-6-astra"]
        muted = label == "Other"
        ax.scatter(
            [r["_price"] for r in points], [r["_aaii"] for r in points],
            s=[r["_size"] for r in points], facecolors=color,
            alpha=marker_alpha / 2 if muted else marker_alpha, label=label,
            edgecolors=SURFACE, linewidths=1.1, zorder=3,
        )

    astra = next((r for r in rows if r["canonical_id"] == "openai/gpt-6-astra"), None)
    if groups and astra is not None:
        _, color, label = next(group for group in groups if group[0](astra))
        ax.scatter(
            [astra["_price"]], [astra["_aaii"]], s=[astra["_size"]], color=color,
            alpha=marker_alpha / 2 if label == "Other" else marker_alpha,
            edgecolors=SURFACE, linewidths=1.1, zorder=3.1,
        )

    if age_as_of is not None:
        dated = [r for r in rows if r.get("_months_ago") is not None]
        dated.sort(key=lambda r: r["canonical_id"] == "openai/gpt-6-astra")
        unknown = [r for r in rows if r.get("_months_ago") is None]
        if dated:
            ax.scatter(
                [r["_price"] for r in dated], [r["_aaii"] for r in dated],
                s=[r["_size"] for r in dated], c=[r["_months_ago"] for r in dated],
                cmap=AGE_CMAP, norm=Normalize(0, AGE_MAX_MONTHS, clip=True),
                alpha=0.85, edgecolors=SURFACE, linewidths=1.1, zorder=3,
            )
        if unknown:
            ax.scatter(
                [r["_price"] for r in unknown], [r["_aaii"] for r in unknown],
                s=[r["_size"] for r in unknown], color=COLOR_OTHER,
                alpha=0.85, label="Listing date unknown", edgecolors=SURFACE,
                linewidths=1.1, zorder=3,
            )

    # selective direct labels: Pareto frontier + explicitly requested points,
    # never every point
    for r in to_label:
        label = r["display_name"]
        offset, alignment = LABEL_PLACEMENTS.get(r["canonical_id"], ((6, 5), "left"))
        if hardware_tiers is not None:
            if r["canonical_id"] == "deepseek/deepseek-v4.1-flash":
                offset, alignment = ((-6, 8), "right")
            elif r["canonical_id"] == "moonshotai/kimi-k3":
                label = "Kimi K3 (2.8T)"
        ax.annotate(
            label,
            (r["_price"], r["_aaii"]),
            textcoords="offset points",
            xytext=offset, ha=alignment,
            va="center" if offset[1] == 0 else "baseline",
            fontsize=7.7,
            color=INK_SECONDARY,
            arrowprops={"arrowstyle": "-", "color": INK_SECONDARY, "linewidth": 0.5}
            if r["canonical_id"] in {
                "anthropic/claude-fable-5.1", "z-ai/glm-5.3-flash",
                "moonshotai/kimi-k3", "x-ai/grok-4.7",
            } else None,
            zorder=4,
        )

    ax.set_xscale("log")
    ax.set_xlabel(
        "API cost per task (US$, 100K input / 8K output), log scale" if hardware_tiers is not None else "Cost per task (100K input / 8K output tokens), log scale",
        color=INK_SECONDARY, fontsize=13,
    )
    ax.set_ylabel(
        "Artificial Analysis Intelligence Index",
        color=INK_SECONDARY, fontsize=13,
    )

    ax.grid(True, which="major", color=GRID, linewidth=0.8, zorder=0)
    ax.grid(True, which="minor", color=GRID, linewidth=0.4, zorder=0, alpha=0.6)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("bottom", "left"):
        ax.spines[spine].set_color(AXIS)
    ax.tick_params(colors=INK_MUTED, labelsize=11)

    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:.3f}" if v < 0.01 else f"${v:.2f}"))
    ax.set_ylim(top=60)

    if age_as_of is not None:
        prior_scores = [
            r["_aaii"] for r in rows
            if r.get("_listed_date")
            and date.fromisoformat(r["_listed_date"]) < INTELLIGENCE_CUTOFF
        ]
        if prior_scores:
            prior_max = max(prior_scores)
            ax.axhline(prior_max, color=INK_SECONDARY, linewidth=1.1, linestyle="--", zorder=2)
            cutoff_label = f"{INTELLIGENCE_CUTOFF.day} {INTELLIGENCE_CUTOFF:%b %Y}"
            ax.text(
                0.02, prior_max + 1,
                f"Highest intelligence prior to {cutoff_label}",
                transform=ax.get_yaxis_transform(), color=INK_SECONDARY,
                fontsize=9.5, va="bottom", zorder=5,
            )

    if hardware_tiers is not None:
        handles = [
            Line2D([], [], linestyle="none", marker="o", markersize=6.5,
                   color=HARDWARE_COLORS[tier["id"]], label=tier["cost_label"])
            for tier in hardware_tiers
        ]
        handles += [
            Line2D([], [], linestyle="none", marker="o", markersize=6.5,
                   color=COLOR_OTHER, alpha=0.5, label="Other"),
        ]
        ax.legend(handles=handles, loc="lower right", bbox_to_anchor=(0.995, 0.075),
                  frameon=False, fontsize=9, ncol=2, columnspacing=1.3,
                  handletextpad=0.5, labelcolor=INK_PRIMARY,
                  title="Indicative hosting hardware (SGD)", title_fontsize=9.5)
        ax.text(0.98, 0.025, "Higher tiers: 4 / 8 / 16 sessions budgeted",
                transform=ax.transAxes, ha="right", fontsize=8.5, color=INK_SECONDARY)
    elif ax.get_legend_handles_labels()[0]:
        ax.legend(loc="lower right", frameon=False, fontsize=9.5, labelcolor=INK_PRIMARY)

    # Keep the size explanation inside the plot on every chart variant.
    text_props = {"color": INK_SECONDARY, "fontsize": 9.5}
    small_circle = DrawingArea(6, 12)
    small_circle.add_artist(Circle((3, 6), 2, fill=False, edgecolor=INK_SECONDARY))
    large_circle = DrawingArea(12, 12)
    large_circle.add_artist(Circle((6, 6), 5, fill=False, edgecolor=INK_SECONDARY))
    size_note = HPacker(
        children=[
            small_circle,
            TextArea("→", textprops=text_props),
            large_circle,
            TextArea("OpenRouter volume", textprops=text_props),
        ],
        align="center", pad=0, sep=4,
    )
    note = AnchoredOffsetbox(
        loc="lower left", child=size_note, frameon=False,
        bbox_to_anchor=(0.015, 0.015), bbox_transform=ax.transAxes,
        borderpad=0, pad=0.3,
    )
    note.set_zorder(5)
    ax.add_artist(note)

    # Methodology and caveats are in README.md.
    fig.tight_layout(pad=0.4)
    write_outputs(fig, stem, combined)
    plt.close(fig)


def render_hardware(rows, combined=None):
    from build_hardware import main as build_hardware

    build_hardware()
    with (HERE / "model_hardware.csv").open() as f:
        hosting = {row["canonical_id"]: row for row in csv.DictReader(f)}
    tiers = json.loads((HERE / "hosting_tiers.json").read_text())["tiers"]
    for row in rows:
        detail = hosting[row["canonical_id"]]
        row["_hosting_status"] = detail["hosting_status"]
        row["_hardware_tier"] = detail["hardware_tier"]
    # Draw grey Other points first so hosting-tier colors remain visible.
    groups = [
        (lambda r: r["_hosting_status"] in {"api_only", "unknown"}, COLOR_OTHER, "Other"),
    ]
    groups += [
        (lambda r, key=tier["id"]: r["_hardware_tier"] == key,
         HARDWARE_COLORS[tier["id"]], tier["cost_label"])
        for tier in tiers
    ]
    labels = compute_labels(rows)
    deepseek_flash = next((r for r in rows if r["canonical_id"] == "deepseek/deepseek-v4.1-flash"), None)
    if deepseek_flash is not None and deepseek_flash not in labels:
        labels.append(deepseek_flash)
    render(rows, labels, groups, STEM_HARDWARE, hardware_tiers=tiers, combined=combined)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardware-only", action="store_true", help="Regenerate only the hosting hardware chart")
    args = parser.parse_args()
    rows = prepare_rows()
    if args.hardware_only:
        render_hardware(rows)
        return
    to_label = compute_labels(rows)
    print(f"labelled ({len(to_label)}): " + ", ".join(r["display_name"] for r in to_label))

    pdf_dir = DIST / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=pdf_dir, suffix=".pdf", delete=False) as handle:
        combined_temporary = handle.name
    try:
        with PdfPages(combined_temporary) as combined:
            render(
                rows, to_label,
                [
                    (lambda r: r["open_weights"] == "true", COLOR_OPEN, "Open weights"),
                    (lambda r: r["open_weights"] == "false", COLOR_CLOSED, "Closed"),
                ],
                STEM_WEIGHTS,
                combined=combined,
            )

            # both by-lab variants plot the SAME full set of models -- only which
            # labs get a distinct color (vs. folding into "Other") changes
            lab_colors = dict(zip(TOP_LABS, (COLOR_OPEN, COLOR_CLOSED, COLOR_SLOT_3, COLOR_QWEN)))
            lab_groups = [
                (lambda r, lab=lab: r["_lab"] == lab, color, lab)
                for lab, color in lab_colors.items()
            ]
            lab_groups.append((lambda r: r["_lab"] not in lab_colors, COLOR_OTHER, "Other"))
            render(rows, to_label, lab_groups, STEM_LAB, marker_alpha=0.75, combined=combined)

            open_lab_groups = [
                (lambda r, lab=lab: r["_lab"] == lab, color, lab)
                for lab, color in TOP_LABS_OPEN.items()
            ]
            open_lab_groups.append((lambda r: r["_lab"] not in TOP_LABS_OPEN, COLOR_OTHER, "Other"))
            render(rows, to_label, open_lab_groups, STEM_LAB_OPEN, marker_alpha=0.55, combined=combined)

            with open(HERE / "model_dates.csv") as f:
                dates = {r["canonical_id"]: r["listed_date"] for r in csv.DictReader(f)}
            as_of = snapshot_as_of()
            for row in rows:
                listed = dates.get(row["canonical_id"])
                row["_listed_date"] = listed
                row["_months_ago"] = None
                if listed:
                    listed_date = date.fromisoformat(listed)
                    if listed_date > as_of:
                        raise ValueError(f"Future listing date for {row['canonical_id']}: {listed}")
                    row["_months_ago"] = (as_of - listed_date).days / (365.2425 / 12)
            render(rows, to_label, [], STEM_AGE, age_as_of=as_of, combined=combined)
            render_hardware(rows, combined=combined)
        os.replace(combined_temporary, pdf_dir / "all.pdf")
    finally:
        if os.path.exists(combined_temporary):
            os.unlink(combined_temporary)
    print(f"age chart as of {as_of.isoformat()}")


if __name__ == "__main__":
    main()
