# Lead Qualification Tool

Scores a CSV of inbound B2B leads against a four-factor rubric, decides each one as QUALIFIED, REVIEW or REJECTED with a traceable reason, and orders qualified leads into a priority queue. For every QUALIFIED lead it writes a personalised first outreach message using Groq's free tier, then produces a report with reasons, statistics and sample messages (build 1.1.2).

Rubric, weights, decisions and experiments designed by Adithya; implementation pair-programmed with Claude (Anthropic).

Built as the Mini Project in the IIT Roorkee PG Certificate in Forward Deployed AI Engineering.

## How to run (Google Colab)

1. Open `Lead_Qualification_Tool.ipynb` in Colab.
2. Upload to the session folder: `config.yaml`, `rubric_scorer.py`, `message_generator.py`, `report_builder.py`, `industry_tier_cache.json`, a CSV from `leads/`, and optionally the matching cache from `sample_run/`.
3. Add `GROQ_API_KEY` to Colab secrets.
4. Run blocks 1–3. Set `INPUT_CSV` in block 3 to your file name.
5. Blocks 4–5 display the report and download the output files.

A 100-lead file takes roughly 15–20 minutes; free-tier pacing sets the runtime.

## Input format

A CSV with six columns: `name`, `company`, `company_size`, `industry`, `source`, `last_interaction_date` (YYYY-MM-DD).

`source` must be one of: `Inbound demo request`, `Content download`, `Webinar attendee`, `Referral`, `LinkedIn outreach`, `Sales call`.

Blank cells and placeholders such as `NA` or `Unknown` are treated as missing, and send the lead to REVIEW.

## The rubric

Each lead gets a fit score out of 10 from industry fit, company size, recency of last interaction and lead source, weighted 1.75 / 1.25 / 1.0 / 0.75. A fit score of 7.0 or above qualifies, a score within ±0.25 of the cutoff or an incomplete record goes to REVIEW, and the rest are REJECTED. Queue position comes from a priority score that blends fit (0.8) with urgency (0.2).

## Outputs

Each file is named from the input file's stem (`leads/leads_sample_50.csv` → `sample_50_…`):

- `<stem>_output_report.json` — the full report: summary, queue, per-lead records, sample messages, run diagnostics
- `<stem>_output_report.csv` — one row per lead in queue order, for a sales team
- `<stem>_scored.csv` — per-lead scores and reasoning
- `<stem>_run_report.json` — scoring and tiering diagnostics
- `<stem>_message_run_report.json` — messaging diagnostics
- `<stem>_message_cache.json` — message cache for this input file

## Repo layout

```
├── Lead_Qualification_Tool.ipynb   run the pipeline here
├── config.yaml                     every rubric value, prompt and label
├── rubric_scorer.py                scoring, guardrails, queue position
├── message_generator.py            outreach messages
├── report_builder.py               reasons, statistics, report files
├── industry_tier_cache.json        cached industry tiers
├── leads/                          input CSVs
└── sample_run/                     outputs from a 50-lead run
```

## Documentation

- [CASE_STUDY.md](CASE_STUDY.md) — the business problem, what the tool does and what it achieves
- [ARCHITECTURE.md](ARCHITECTURE.md) — how build 1.1.2 works: pipeline, scoring, LLM calls, caching, failure handling
- [DECISIONS.md](DECISIONS.md) — what was observed, decided and shipped, with limitations

## Key limitations

Full list in [DECISIONS.md](DECISIONS.md#limitations-and-blockers).

- No conversion outcomes exist, so the rubric is defensible but not validated for accuracy.
- Calibrated on 29 leads; the other datasets are not a blind test.
- The rubric, prompts and caches are specific to one target organisation.
- Runs only as a notebook session, and free-tier pacing sets the runtime.
