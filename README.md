# Lead Qualification Tool

Scores a CSV of inbound B2B leads against a four-factor rubric, decides each as QUALIFIED, REVIEW or REJECTED with a traceable reason, and queues the qualified ones by priority. Each qualified lead gets a personalised outreach message written on Groq's free tier, then a report with reasons, statistics and samples (build 1.2.0-dev).

Rubric, weights, decisions and experiments designed by Adithya; implementation pair-programmed with Claude (Anthropic).

Built as the Mini Project in the IIT Roorkee PG Certificate in Forward Deployed AI Engineering.

## How to run

Built and verified on Python 3.12; 3.10 or later should work.

```bash
git clone https://github.com/adithya-prabhu292/lead-qualification-tool.git
cd lead-qualification-tool
python -m venv .venv
```

Activate it — `source .venv/bin/activate` (macOS, Linux) or `.\.venv\Scripts\Activate.ps1` (PowerShell) — then `pip install -r requirements.txt`.

Now pick **one** of the two runs below. Both write to `output/`; `--out-dir` writes elsewhere and `--config` selects a config.

### Replay the sample, no API key needed

Seed `output/` from `sample_run/` first; without it every qualified lead is reported as a failure naming the missing key.

macOS and Linux:

```bash
mkdir -p output && cp sample_run/sample_50_message_cache.json output/
python cli.py leads/leads_sample_50.csv
```

Windows PowerShell:

```powershell
mkdir output; Copy-Item sample_run\sample_50_message_cache.json output\
python cli.py leads/leads_sample_50.csv
```

Takes seconds, and ends with `messages: 23 of 23 written | 23 from cache, 0 live | 0 failed`.

### Run live with an API key

macOS and Linux:

```bash
export GROQ_API_KEY="your-key"
python cli.py leads/leads_sample_50.csv
```

Windows PowerShell:

```powershell
$env:GROQ_API_KEY = "your-key"
python cli.py leads/leads_sample_50.csv
```

A 100-lead file takes roughly 15–20 minutes, set by free-tier pacing.

### Google Colab

Cell 1 — clone and install:

```python
!git clone https://github.com/adithya-prabhu292/lead-qualification-tool.git
%cd lead-qualification-tool
!pip install -q -r requirements.txt
```

Cell 2 — seed the cache for a key-less replay:

```python
!mkdir -p output && cp sample_run/sample_50_message_cache.json output/
```

Or, to run live, set a real key. Do not leave a placeholder: any non-empty string counts as a key, so the call fails with HTTP 401 instead of taking the no-key path.

```python
import os; os.environ["GROQ_API_KEY"] = "paste-a-real-key"
```

Cell 3 — run:

```python
!python cli.py leads/leads_sample_50.csv
```

Output lands in `output/` inside the session and is lost when it disconnects, so download what you need. The replay takes seconds; a live 50-lead run about 6–8 minutes.

## Input format

A CSV with six columns: `name`, `company`, `company_size`, `industry`, `source`, `last_interaction_date` (YYYY-MM-DD).

`source` must be one of `Inbound demo request`, `Content download`, `Webinar attendee`, `Referral`, `LinkedIn outreach`, `Sales call`.

A value the tool cannot use sends the lead to REVIEW: blank cells, placeholders such as `NA` or `Unknown`, and values present but unrecognised — a `source` outside that list, an unclassifiable industry, an unreadable size or date.

## The rubric

Each lead gets a fit score out of 10 from industry fit, company size, recency and source, weighted 1.75 / 1.25 / 1.0 / 0.75. From 7.0 it qualifies; within ±0.25 of the cutoff, or with an unusable record, REVIEW; the rest REJECTED. Queue position blends fit (0.8) and urgency (0.2).

## Outputs

Named from the input stem (`leads/leads_sample_50.csv` → `sample_50_…`):

- `<stem>_output_report.json` — summary, queue, per-lead records, samples
- `<stem>_output_report.csv` — one row per lead, queue order, for reps
- `<stem>_scored.csv` — per-lead scores, reasoning
- `<stem>_message_cache.json` — this input’s message cache
- `<stem>_run_report.json`, `<stem>_message_run_report.json` — LLM diagnostics

## Repo layout

```
├── cli.py                          command line entry point
├── pipeline.py                     stages, tiering call, token pacer
├── config.yaml                     every rubric value, prompt and label
├── rubric_scorer.py                scoring, guardrails, queue position
├── message_generator.py            outreach messages
├── report_builder.py               reasons, statistics, reports
├── industry_tier_cache.json        cached industry tiers
├── requirements.txt                pandas, PyYAML, requests
├── leads/                          input CSVs
└── sample_run/                     outputs from a 50-lead run
```

## Documentation

- [CASE_STUDY.md](CASE_STUDY.md) — the business problem and what the tool achieves
- [ARCHITECTURE.md](ARCHITECTURE.md) — pipeline, scoring, LLM calls, caching, failure handling
- [DECISIONS.md](DECISIONS.md) — what was observed, decided and shipped

## Key limitations

Full list in [DECISIONS.md](DECISIONS.md#limitations-and-blockers).

- No conversion outcomes exist, so the rubric is defensible, not validated.
- Calibrated on 29 leads; the other datasets are not a blind test.
- The rubric, prompts and caches suit one target organisation.
- Command line only; free-tier pacing sets the runtime.
- The message leak check matches ordinary words, so it can reject a valid one.
