# Agent notes

This repository publishes five unannotated cost-versus-intelligence charts and the code that draws them. Consumers place the files in their own slides. Do not add titles, callouts, arrows, or speaker notes to the figures.

## Render

From the repository root:

```sh
uv run make_chart.py
```

Outputs, one stem per coloring:

- `dist/svg/<stem>.svg` keeps `<text>` elements (`svg.fonttype` none). DejaVu Sans is not embedded.
- `dist/svg-paths/<stem>.svg` converts text to paths. This is the import file for PowerPoint, Keynote, and Google Slides.
- `dist/pdf/<stem>.pdf` and `dist/pdf/all.pdf`.
- `dist/png/<stem>.png` at 300 dpi.

`dist/` is gitignored. GitHub Actions uploads it as the `charts` artifact. CI does not call OpenRouter.

Stems: `cost-vs-aaii`, `cost-vs-aaii-by-lab`, `cost-vs-aaii-by-lab-open`, `cost-vs-aaii-by-age`, `cost-vs-aaii-by-hardware`.

## What to edit

A new coloring is another `render()` call in `main()`. Plot the same rows. Change color, not membership, unless the task says otherwise.

- `ALWAYS_LABEL`, `NEVER_LABEL`, and `LABEL_PLACEMENTS` in `make_chart.py` control names.
- `TOP_LABS` and `TOP_LABS_OPEN` choose which labs get their own color. Everyone else is "Other".
- `CONTENT_W_PT` and `CONTENT_H_PT` are the figure size. Leave them when only the data changes.
- The plotted table is `models.csv`, built by `build_dataset.py` from `openrouter_live.csv` and `model_overrides.csv`.
- The age chart reads `model_dates.csv` and measures months against `rankings_meta.end_date` in `refresh_meta.json`.
- The hardware chart reads `model_hardware.csv`, rebuilt from `hardware-research/` and `hosting_tiers.json` at render time.

`python3 refresh_all.py --env-file .env` updates prices, volumes, and listing dates for models already in the table. It preserves curated intelligence scores. It needs `OPENROUTER_API_KEY` and is not part of CI.

## Checks

```sh
uv run python -m unittest discover -s . -p 'test_*.py'
```
