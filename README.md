# Model intelligence, price, and volume

Unannotated charts of API price against the Artificial Analysis Intelligence Index. Marker area is log monthly token volume. The figure is 11.634 in × 5.301 in, the content box under a 16:9 slide title, with no title and no callouts drawn on the chart. Place and annotate it in the consuming presentation.

`uv run make_chart.py` writes five colorings, each in four formats:

| Directory | Format |
| --- | --- |
| `dist/svg/` | SVG with real `<text>` elements. Labels stay editable. The font is Matplotlib's DejaVu Sans and is not embedded, so the viewer needs that font. |
| `dist/svg-paths/` | The same SVG with text converted to paths. Use this when importing into PowerPoint, Keynote, or Google Slides. |
| `dist/pdf/` | One vector page per chart, plus `all.pdf`. Text is embedded TrueType. |
| `dist/png/` | 300 dpi PNG. |

The stems are `cost-vs-aaii` (open weights vs closed), `cost-vs-aaii-by-lab` (OpenAI, Google, Anthropic, SpaceXAI), `cost-vs-aaii-by-lab-open` (DeepSeek, Z.ai, MiniMax, Alibaba), `cost-vs-aaii-by-age`, and `cost-vs-aaii-by-hardware`.

GitHub Actions renders these from the committed tables on every push and pull request and uploads `dist/` as the `charts` artifact. The workflow does not call OpenRouter. To refresh prices and volumes locally, run `python3 refresh_all.py --env-file .env`. That command reads only `OPENROUTER_API_KEY`.

Age colors use `rankings_meta.end_date` in `refresh_meta.json`, so a rebuild on a later day does not shift the age chart.

## Chart data

Price per task (100K input / 8K output tokens) versus the Artificial Analysis Intelligence Index. The 2026-09-30 refresh has 183 rows: 168 families ranked from OpenRouter's censored daily top-50 feed, plus 15 catalog models that have a real benchmark score but no measured rank. All 101 previous rows are inside the ranked 168. Nothing in this table is a fabricated top-200 rank.

The 2026-10-01 curated addition of Gemini 4 Argon brings the working dataset to 184 rows, with 139 scored models. The original refresh metadata still describes the 2026-09-30 snapshot; this addition does not refresh other models' prices, scores, or usage.

The 2026-10-02 curated score for Muse Spark 1.3 brings the plotted count to 140, with the same 184 dataset rows. Muse Spark 1.1, 1.2, and 1.3 are plotted; only the latest, 1.3, is named explicitly. Its AAII 48 at max reasoning comes from the [Artificial Analysis model page](https://artificialanalysis.ai/models/muse-spark-1-3), read on 2 October under Intelligence Index v4.3.2. This is a later benchmark reading than the stored September snapshot, so the chart mixes benchmark readings rather than providing a synchronized evaluation. Its saved standard prices ($1.25/$4.25 per million input/output tokens, $0.159 per chart task), measured usage, and listing date remain unchanged. The Contributor tiers retain blank scores and are not plotted because no separate AAII score was found; standard-tier scores are not copied to those rows.

## Pipeline

The latest authenticated refresh on 2026-10-05 updates the same 184 existing models and regenerates all five charts. Daily rankings cover 2026-01-01 through 2026-10-04: 169 models have observed usage and their token volumes changed, 14 remain unobserved, and Gemini 4 Argon retains its nominal placeholder. GPT-6.1 Sol now uses the computed monthly average of 78,219,347,668 tokens from this window, superseding its interim 780B rolling total. One model's price changed since the earlier catalog-only pass. Curated intelligence scores and teaching overrides are preserved.

Run the complete refresh from the repository root:

```sh
python3 refresh_all.py --env-file .env
```

`refresh_all.py` reads only `OPENROUTER_API_KEY` from the requested file without displaying it or executing shell assignments. It fetches the catalog and authenticated daily rankings, runs `refresh_prices.py --existing-only`, rebuilds the dataset and listing dates, and regenerates all chart variants. `--months 0` (the default) uses year-to-date rankings; a positive value selects that many calendar months. Credentials stay in memory; metadata records public source URLs and the observed window. Models without fresh observed usage retain their prior volume or placeholder.

The earlier 2026-10-05 catalog-only refresh checked the same 184 existing models against the 466-entry public catalog. Prices changed for 18 models; context limits did not change. Listing dates were checked again. That initial pass preserved usage because `OPENROUTER_API_KEY` was unavailable in the environment.

For an existing-model catalog refresh without a rankings API key, run from this directory:

```sh
python3 refresh_prices.py --catalog-only
python3 build_dataset.py
python3 refresh_dates.py
uv run make_chart.py
```

`--catalog-only` updates prices and catalog fields without adding models or changing usage or scores. A `catalog_refresh` entry in `refresh_meta.json` records the check separately from the original rankings and benchmark snapshots. Delisted models retain their last prices; external provider prices and the instructor price override are preserved.

```text
refresh_prices.py  -->  openrouter_live.csv + refresh_meta.json
                                                          \
model_overrides.csv  -----------------------------------  +--> build_dataset.py --> models.csv
(curated AAII, open weights, country, display name)      /
                                                    make_chart.py --> dist/svg/cost-vs-aaii.svg
```

`refresh_dates.py` rewrites `model_dates.csv` from the catalog, preserves sourced dates for delisted rows, and does not change prices or scores.

```bash
python3 refresh_prices.py \
  --catalog /tmp/clm-openrouter-catalog.json \
  --rankings /tmp/clm-openrouter-daily.json \
  --benchmarks /tmp/clm-openrouter-benchmarks.json \
  --month-snapshot /tmp/clm-openrouter-month.json
python3 build_dataset.py
python3 refresh_dates.py --catalog /tmp/clm-openrouter-catalog.json
uv run make_chart.py
```

`--top` defaults to 200. `--months 0` uses the whole rankings snapshot. A live rankings fetch is still possible when `OPENROUTER_API_KEY` is set in the environment. This script does not read a `.env` file or any other credential file.

## What the 2026-09-30 snapshots can and cannot support

Daily rankings file: 13,872 rows, `2026-01-01` through `2026-09-29` (272 days), `meta.as_of` `2026-09-30T14:20:26.162Z`. Every day has 51 rows: the top 50 `model_permaslug`s plus one aggregated `other` row. Official documentation describes the same cap: [Daily token totals for top 50 models](https://openrouter.ai/docs/api/api-reference/datasets/get-rankings-daily). There is no parameter for ranks 51–200. An unauthenticated call returns 401. The public [rankings page](https://openrouter.ai/rankings) embeds a short leaderboard (20 models in the week payload captured with these snapshots), not a top-200 series.

Volume for a matched family is the sum of its observed daily `total_tokens`, divided by calendar-month equivalents of the whole window:

`distinct_days / (365.2425 / 12)` = 272 / (365.2425 / 12) = 8.9365 months.

Days a family misses that day's top 50 are censored, not filled in as zero, so `avg_monthly_tokens` is a lower bound. `:free` and `:batch` rows add to the family total when they appear, but the price always comes from the standard catalog row. Matching tries the exact id, then the exact `canonical_slug`, and only then a date-suffix / Anthropic tier normalization when that still identifies one standard model.

That feed exposes 168 LLM families with a current non-zero standard price. That is short of 200. The other 32 slots are not backfilled with invented ranks. Fifteen further catalog models have an exact Artificial Analysis `intelligence_index` in the benchmarks snapshot and were added with `inclusion=benchmark_cohort`, `volume_status=not_observed`, `volume_rank` blank, and `avg_monthly_tokens=0`. The zero is a sentinel, the same convention previously used for manually pinned rows, not a measured volume. No more scored, currently priced catalog models were left to add, so the truthful table stops at 183 rows.

The monthly snapshot (`2026-01-01` through `2026-08-31`, 408 rows, also 51 per month, `as_of` `2026-09-30T14:20:20.315Z`) is recorded in `refresh_meta.json` and is not used for the average. Benchmarks: 100 rows, `as_of` `2026-09-30T12:00:56.392Z`, source [Artificial Analysis](https://artificialanalysis.ai) via OpenRouter. 94 attach to a current standard id by exact slug. Scores are copied only for models that did not already have a curated score. 45 of the 168 ranked rows still have a blank AAII because this snapshot has no exact score for them.

Catalog prices are the 464-entry public [models catalog](https://openrouter.ai/api/v1/models) snapshot. Long-context `pricing.overrides` are not the normal price. No live row uses a `:free` or `:batch` id.

## Pins and scores that this refresh did not invent

Previous curated AAII values, display names, open-weights flags, and countries were left as they were. The one note change on an old score is `qwen/qwen3.8-27b`: the stored AAII remains the instructor estimate 37. The benchmarks snapshot lists Qwen3.8 27B (xhigh) at 33.7 and that measured value was not substituted. The price remains the instructor override $0.01 / $0.02 per million (`price_status=override`). The live standard catalog price in this snapshot is $0.0249 / $4.35 per million.

`openai/gpt-6.1-sol` is pinned. It was absent from the September snapshot's censored top-50 window and entered as `benchmark_cohort`; it now has observed usage and `inclusion=ranked` after the authenticated October refresh. Standard price $2 / $10 per million (the `:batch` row is $1 / $5 and was not used), context 1,050,000, listed 2026-09-29. AAII 51.8 is GPT-6.1 Sol (max), permaslug `openai/gpt-6.1-sol-20260929`, from the benchmarks snapshot. Primary pages: [OpenRouter model](https://openrouter.ai/openai/gpt-6.1-sol), [models API](https://openrouter.ai/api/v1/models), [Artificial Analysis](https://artificialanalysis.ai).

On 2026-10-05, GPT-6.1 Sol's chart volume was initially updated to 780,000,000,000 tokens from the rounded **780B tokens last 30 days** figure in [OpenRouter's public activity panel](https://openrouter.ai/compare/moonshotai/kimi-k3/openai/gpt-6.1-sol), with `volume_status=observed_30d`. The subsequent authenticated refresh superseded this interim override with the daily-feed monthly average and `volume_status=observed`.

`make_chart.py` always labels Gemini 4 Argon, GPT-6.1 Sol, Grok 4.7, Muse Spark 1.3, and Claude Fable 5.1, alongside the existing named models (including GLM 5.3 Flash and GLM 5.3). Claude Fable 5, GPT-5.6 Sol, GPT-6 Sol, GPT-6 Luna, MiMo-V2.6-Flash, Grok 4.6 and Grok 4.5 are plotted without labels, even if they enter the Pareto frontier. GLM 5.3 Flash is labelled above its circle with a leader line. The renderer accepts decimal AAII scores such as 51.8. GPT-6 Astra and Claude Fable 5.1 share $1.40/task and AAII 53; Astra is drawn above Fable 5.1, and their labels are separated vertically.

`google/gemini-4-argon` is a manually sourced, pinned Google model with AAII 53 at high reasoning (the highest available), a 1M-token context window, and release date 2026-09-30, from the [Artificial Analysis model page](https://artificialanalysis.ai/models/gemini-4-argon/). The chart uses standard Google API prices of $4 / $20 per million input/output tokens, giving $0.56 for its fixed 100K/8K task. This excludes the temporary 50% promotion to $2 / $10 ($0.28/task), at the user's request; both rates are documented in [Artificial Analysis's launch analysis](https://artificialanalysis.ai/articles/gemini-4-argon-google-top-three-labs). The model is absent from the public OpenRouter catalog checked on 2026-10-01, so `canonical_id` is an internal dataset key and `openrouter_id` is blank. `price_status=external` distinguishes its sourced provider price from an OpenRouter listing. At the user's request, `avg_monthly_tokens=1` and `volume_status=nominal` provide a tiny placeholder until measured usage is available; it has no usage rank or observed days. Nominal usage draws at the minimum marker size and does not affect the observed-volume scale. Refreshes preserve the external price and nominal usage until catalog or usage data becomes available. The age chart uses the sourced release date for Argon, rather than an OpenRouter listing date.

The catalog has Grok 4.7, 4.6, and 4.5 (`x-ai/grok-4.7`, `x-ai/grok-4.6`, `x-ai/grok-4.5`). All three are pinned ranked rows. Stored scores: 4.7 is new at 46.4 (xhigh, benchmark snapshot); 4.6 stays 44 and 4.5 stays 39. SpaceXAI has its own color in the closed-source lab chart. The closed-source lab chart uses alpha 0.75 for highlighted labs and grey at alpha 0.375 for Other labs, half the highlighted opacity. The open-weight lab chart uses alpha 0.55 and 0.275 respectively. The `meta/` and `meta-llama/` prefixes both map to Meta.

No Apple or Copilot rows were added.

## Columns that matter

`inclusion` is `ranked`, `retained`, `benchmark_cohort`, or `pinned`. `price_status` is `live`, `override`, `external`, or `stale`; `external` is a manually sourced provider price for a model absent from OpenRouter. A delisted prior row would keep its last price and be `stale`; this snapshot still prices every prior row, so none are stale. `volume_status=preserved` would mean an older average kept because this window did not observe the model. `volume_status=observed` is the censored daily calculation above; `nominal` is an explicit placeholder, not measured usage.

## Chart renderer

`make_chart.py` draws five colorings and labels the Pareto frontier plus `ALWAYS_LABEL`. The original open/closed variant uses blue for open weights and orange for closed models. Marker area is log monthly volume, and rows with `avg_monthly_tokens=0` are drawn at the floor size. The figure is sized to the measured content box (`CONTENT_W_PT` / `CONTENT_H_PT`). The age chart reads `model_dates.csv`: exact catalog id, then the earliest family listing. Age is days / (365.2425 / 12), relative to `rankings_meta.end_date`. Listing dates are launch-date proxies, as described in the [OpenRouter model schema](https://openrouter.ai/docs/guides/overview/models).

Run the chart with `uv run make_chart.py`. Only the launch-date chart draws the dashed “Highest intelligence prior to 9 Jun 2026” line at AAII 42, set by Claude Opus 4.8 (listed 27 May). Claude Fable 5, listed 9 June, raised the dataset maximum to 50. This comparison uses current stored scores grouped by listing dates; it is not a historical benchmark snapshot.

## Indicative minimum hosting cost

`cost-vs-aaii-by-hardware` is the fifth coloring. It retains the same 140 scored models, API task prices, intelligence scores, bubble sizes, and native dimensions. Six discrete colors encode indicative hardware purchase budgets in SGD, labelled by each band's upper bound; the final band is open-ended. The first three desktop tiers retain warm colors from the age chart's Plasma palette, ending in magenta at ≤S$25k. The larger desktop cluster / small-server tier switches to blue at ≤S$100k, with progressively darker blues for the two higher tiers. The horizontal axis remains API cost per task in USD; it does not measure the operating cost of local inference. Closed models and models with incomplete hosting evidence share one grey category, “Other.” The legend identifies the budgets as indicative, rather than supplier quotes or proven minimum purchases.

The parameter-count fallback was approved for teaching estimates on 2 October 2026, with Kimi K3 as the high-end example. Count the complete resident model, including every MoE expert and lookup/Engram/N-gram table, rather than only parameters activated per token. The default rule assumes approximately 4-bit or higher precision and excludes expert/lookup disk streaming, pruning, and sub-4-bit compression. Explicit user-approved per-model overrides in `hosting_tiers.json` can depart from that fallback, with the configuration and caveats preserved in source notes. A qualifying model must have enough catalog context for the illustrative 100K-input/8K-output task. Quantization may change intelligence relative to the plotted AAII score.

| Total parameters, fallback ceiling | Indicative hardware budget (SGD) | Reference tier                        | Concurrent sessions budgeted |
| ---------------------------------- | -------------------------------- | ------------------------------------- | ---------------------------- |
| 32B                                | ≤S$5k                            | Desktop                               | 1                            |
| 180B                               | ≤S$10k                           | One Spark                             | 1                            |
| 360B                               | ≤S$25k                           | Two Sparks / large desktop            | 1                            |
| 800B                               | ≤S$100k                          | Larger desktop cluster / small server | 4                            |
| 1.6T                               | ≤S$500k                          | Large server                          | 8                            |
| Above 1.6T                         | >S$500k                          | High-end server / cluster             | 16                           |

Weights are shared across concurrent sessions. The fallback starts with packed 4-bit weight bytes, adds 25% for weight-format/runtime overhead, then a nominal 16GB per session for context/runtime. Each session budgets 108K tokens. Where the per-model research has a larger single-session resident estimate, that estimate is used and an additional 16GB is reserved for each extra session. Coarse memory allowances in `hosting_tiers.json` can raise the parameter-based tier. These allowances are planning assumptions, not exact hardware configurations or cost-to-memory quotations; the highest tier has no fixed capacity or price ceiling. Parallelism means sessions budgeted in memory, with no measured latency or tokens-per-second promise. Model-specific KV cache, runtime support, sharding balance, network fabric, and actual serving performance require deployment validation. Electricity, staffing, and ongoing hosting expenses are excluded.

GLM 5.3 Flash uses 320B total parameters and remains in the two-Spark / large-desktop tier. DeepSeek V4.1 Flash retains 748B total parameters (552B backbone plus 196B Engram) and now uses a user-approved ≤S$25k two-Spark override. The [MiaAI two-Spark recipe](https://github.com/MiaAI-Lab/DeepSeek-v4.1-Flash-EXL3-2x-DGX-Sparks) and [coolbho3k recipe](https://github.com/coolbho3k/DeepSeek-v4.1-Flash-2x-DGX-Spark) use approximately 2.9–3-bit quantization and disk-backed Engram tables, an explicit exception to the default 4-bit/full-resident rule. The original resident-memory estimate remains in the research checkpoint; generated hosting memory fields are blank for the override because that estimate does not describe this configuration. Its plotted AAII score is not verified for the quantized deployment. Both Flash models remain excluded from the one-Spark tier. Kimi K3 uses 2.8T total parameters, native MXFP4 weights, and the highest tier. Primary cards: [GLM 5.3 Flash](https://huggingface.co/zai-org/GLM-5.3-Flash), [DeepSeek V4.1 Flash](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash), [Kimi K3](https://huggingface.co/moonshotai/Kimi-K3). Exact model/version caveats and source URLs remain in the count CSV and research checkpoints.

`model_parameters.csv` preserves counts for all 71 scored open-weight models: 58 sourced counts, 12 family/config estimates, and one unknown (GLM 4.7 Flash). The generated hosting sidecar covers all 184 dataset rows. Of the 140 plotted models, 68 receive estimated cost bands, 69 are API only, and 3 remain unknown: GLM 4.7 Flash has no defensible exact count, while the saved catalog contexts for DeepSeek R1 and Qwen3 30B A3B Thinking 2507 are shorter than the illustrative task. The 69 API-only and 3 unknown rows share the grey “Other” chart category. The source count confidence is separate from the hosting confidence: every hosting cost is estimated.

```text
hardware-research/*/parameter_counts.csv --> collect_parameter_counts.py --> model_parameters.csv
hardware-research/*/*.json + hosting_tiers.json + models.csv --> build_hardware.py --> model_hardware.csv
models.csv + model_hardware.csv --> make_chart.py --> dist/svg/cost-vs-aaii-by-hardware.svg
```

```sh
python3 collect_parameter_counts.py
python3 build_hardware.py
uv run make_chart.py --hardware-only
python3 -m unittest discover -s . -p test_hardware.py
```

The renderer rebuilds `model_hardware.csv` automatically, so a normal `uv run make_chart.py` regenerates all five variants without a separate hosting build step. Price refreshes leave the curated parameter and memory research untouched; a newly scored open model with no saved research remains unknown. Model JSON checkpoints, per-group count CSVs, progress records, and `hardware-research/state.json` make research resumable after interruption. The generated count CSV, hosting CSV, and SVG use atomic replacement so readers never see a half-written artifact.

## Other caveats

- Stealth and anonymous authors (`stealth/*`, `openrouter/*`) are excluded. So are non-text outputs and embedding, rerank, and audio ids. `cohere/north-mini-code` stays excluded because it is free-tier only.
- The 100K/8K task is illustrative, chosen so the top of the chart clears $1/task. GPT-6.1 Sol at the standard price is $0.28/task. The Qwen3.8 27B override is $0.0012/task.
- AAII was rescaled by Artificial Analysis in 2026. Scores already in `model_overrides.csv` were not recomputed. Where a new score names an effort tier, that tier is in `notes`.
