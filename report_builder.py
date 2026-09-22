"""
report_builder.py - Lead Qualification Tool, report stage.

Build 1.1.2. Consumes the in-memory `list[ScoredLead]` produced by scoring
(`rubric_scorer.score_all_leads`) and mutated in place by messaging
(`message_generator.generate_messages`), and emits the report in three
surfaces: JSON, CSV, and a rendered console view.

Three properties this module holds to, carried from the other two:

  * No API call.        This stage is pure. The run cell orchestrates the two
                        stages that do call the API; nothing here does.
  * No business text.   Every label, every reason string, every output-name
                        pattern and every threshold reads from `config.yaml`.
                        What is in this file is mechanism only.
  * Messages are final. This stage selects and formats. It never rewrites a
                        message, trims one to the word target, or regenerates.

A field the report needs that scoring or messaging does not carry is DERIVED
here, in this module's own output. Neither upstream module is modified to carry it.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

BUILD = "1.1.2"


# ===========================================================================
# config accessors
#
# Everything below reads bands, classes, weights and labels out of config
# rather than restating them. The rubric is frozen; this module must not
# become a second place where its numbers live.
# ===========================================================================
def _rcfg(cfg: dict) -> dict:
    try:
        return cfg["report"]
    except KeyError:
        raise KeyError(
            "config.yaml has no `report:` block - stage 3 reads its reason "
            "vocabulary and output-name patterns from there"
        ) from None


def _lowest_scoring_key(score_map: dict) -> str:
    """The class/tier name carrying the lowest score. This is what 'lowest
    band' means for a flat lookup, derived rather than named in code."""
    return min(score_map, key=lambda k: float(score_map[k]))


def _lowest_band(bands: list[dict]) -> dict:
    """The band/segment whose score range sits lowest. Works for both the
    ascending company_size ranges and the descending recency ranges."""
    return min(bands, key=lambda b: min(float(x) for x in b["score_range"]))


def _band_for_size(size: float, fcfg: dict) -> dict | None:
    """Same containment test score_company_size() uses, on the same config."""
    cap = float(fcfg["upper_cap"])
    for band in fcfg["bands"]:
        lo = float(band["min"])
        hi = cap if band["max"] is None else float(band["max"])
        if size >= lo and (band["max"] is None or size <= hi):
            return band
    return None


def _segment_for_age(age: int, fcfg: dict) -> dict | None:
    """Same containment test score_recency() uses. Age, not score: the recency
    segments share endpoints (cool ends at 4.0 where cold begins), so a score
    of exactly 4.0 is ambiguous between two segments and the raw age is not."""
    cap = float(fcfg["upper_cap_days"])
    for band in fcfg["bands"]:
        hi = cap if band["max_days"] is None else float(band["max_days"])
        if band["max_days"] is None or float(age) <= hi:
            return band
    return None


def _tier_from_score(raw: float | None, tier_scores: dict) -> str | None:
    """Invert the tier -> score map. The three tier scores are distinct, so
    the inversion is well defined; a collision returns the first match and is
    flagged by _assert_invertible() at build time."""
    if raw is None:
        return None
    for tier, sc in tier_scores.items():
        if abs(float(raw) - float(sc)) < 1e-9:
            return tier
    return None


def _assert_invertible(cfg: dict) -> list[str]:
    """Reason derivation for source and industry inverts a score back to its
    class. That is only sound while the scores are distinct. Returns warnings
    rather than raising - a degraded reason is better than a dead report."""
    warn = []
    for factor, key in (("source", "class_scores"), ("industry", "tier_scores")):
        m = cfg["factors"][factor][key]
        if len({float(v) for v in m.values()}) != len(m):
            warn.append(
                f"{factor}.{key} has duplicate scores - outcome reasons for "
                f"{factor} cannot be derived unambiguously"
            )
    return warn


def _max_attainable(cfg: dict) -> dict[str, float]:
    """Best raw score each factor can reach, read off config. Used only to
    size the shortfall that orders multiple reasons."""
    F = cfg["factors"]
    return {
        "source": max(float(v) for v in F["source"]["class_scores"].values()),
        "industry": max(float(v) for v in F["industry"]["tier_scores"].values()),
        "company_size": max(
            max(float(x) for x in b["score_range"]) for b in F["company_size"]["bands"]
        ),
        "recency": max(
            max(float(x) for x in b["score_range"]) for b in F["recency"]["bands"]
        ),
    }


def _shortfall(factor: str, raw: float | None, cfg: dict) -> float:
    """(max_attainable - actual) x weight / sum(weights).

    Orders multiple reasons on one lead by how much each one actually cost,
    so the rep reads the binding constraint first. Not a score, not emitted
    as one - an ordering key that happens to be interpretable.
    """
    if raw is None:
        return 0.0
    weights = {k: float(v["score_weight"]) for k, v in cfg["factors"].items()}
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    gap = _max_attainable(cfg)[factor] - float(raw)
    return round(gap * weights[factor] / total, 4)


# ===========================================================================
# outcome reasons
# ===========================================================================
def _fmt_context(lead, cfg: dict) -> dict:
    """Values the detail templates in config may interpolate. Anything a
    template can name has to appear here, formatted the way it should read."""
    p = int(cfg["runtime"].get("score_precision", 1))
    size = lead.company_size

    def r(v):
        return round(float(v), p) if v is not None else None

    return {
        "lead_id": lead.lead_id,
        "name": lead.name,
        "company": lead.company,
        # headcount is a float on the dataclass; a rep should read "8", not "8.0"
        "company_size": (
            int(size) if size is not None and float(size).is_integer() else size
        ),
        "industry": lead.industry,
        "source": lead.source,
        "recency_days": lead.recency_days,
        "fit_score": r(lead.fit_score),
        "urgency_score": r(lead.urgency_score),
        "priority_score": r(lead.priority_score),
        "priority_band": lead.priority_band,
        "qualify_cutoff": cfg["decision"]["qualify_cutoff"],
        "review_margin": cfg["decision"]["review_margin"],
        "missing_fields": ", ".join(lead.missing_fields) if lead.missing_fields else "none",
        "completeness": float(lead.completeness),
    }


def _reason_record(code: str, lead, cfg: dict, shortfall: float | None) -> dict:
    """One reason, resolved against the closed vocabulary in config.

    A code with no entry in reason_lookup is a bug in the derivation, not a
    new category - it is surfaced in the record rather than silently dropped.
    """
    entry = _rcfg(cfg)["reason_lookup"].get(code)
    if entry is None:
        return {
            "code": code,
            "label": "UNMAPPED REASON CODE",
            "detail": f"'{code}' is not in report.reason_lookup - vocabulary is closed",
            "shortfall": shortfall or 0.0,
        }
    try:
        detail = str(entry.get("detail", "")).format(**_fmt_context(lead, cfg))
    except (KeyError, ValueError, IndexError) as e:
        detail = f"[detail template error for {code}: {type(e).__name__} {e}]"
    return {
        "code": code,
        "label": entry.get("label", code),
        "detail": detail,
        "shortfall": shortfall if shortfall is not None else 0.0,
    }


def classify_outcome(lead, cfg: dict) -> list[dict]:
    """Pure. Why this lead landed where it did, as reason records drawn from
    the closed vocabulary in `report.reason_lookup`. Never free text.

    REVIEW  reads its reasons directly off `guardrails_fired`.
    REJECTED has no source field - `guardrails_fired` is empty on every
             rejected lead and `reasoning` is prose, not countable - so the
             reasons are derived by asking which factors sat in their LOWEST
             band or class. Lowest band only: a wider threshold labels
             in-ICP companies "below target size" and makes the low-score
             default unreachable.
    QUALIFIED carries none.
    """
    R = _rcfg(cfg)
    F = cfg["factors"]

    if lead.decision == "REVIEW":
        mapping = R["guardrail_reason_codes"]
        codes = [mapping[g] for g in lead.guardrails_fired if g in mapping]
        # dict.fromkeys: order-preserving dedupe, in guardrail firing order
        return [_reason_record(c, lead, cfg, None) for c in dict.fromkeys(codes)]

    if lead.decision != "REJECTED":
        return []

    codes = R["rejection_factor_codes"]
    fs = lead.factor_scores
    hits: list[tuple[str, str]] = []

    cls = F["source"]["class_lookup"].get(lead.source)
    if cls is not None and cls == _lowest_scoring_key(F["source"]["class_scores"]):
        hits.append(("source", codes["source"]))

    tier = _tier_from_score(fs.get("industry"), F["industry"]["tier_scores"])
    if tier is not None and tier == _lowest_scoring_key(F["industry"]["tier_scores"]):
        hits.append(("industry", codes["industry"]))

    if lead.company_size is not None:
        band = _band_for_size(float(lead.company_size), F["company_size"])
        if band is not None and band["name"] == _lowest_band(F["company_size"]["bands"])["name"]:
            hits.append(("company_size", codes["company_size"]))

    if lead.recency_days is not None:
        seg = _segment_for_age(int(lead.recency_days), F["recency"])
        if seg is not None and seg["name"] == _lowest_band(F["recency"]["bands"])["name"]:
            hits.append(("recency", codes["recency"]))

    if not hits:
        # Rejected on the weighted average with no single weak factor. Live,
        # not theoretical: under the lowest-band rule the minimum fit for a
        # lead triggering no rule is 5.53, well below the 7.0 cutoff.
        return [_reason_record(R["rejection_default_code"], lead, cfg, None)]

    records = [
        _reason_record(code, lead, cfg, _shortfall(factor, fs.get(factor), cfg))
        for factor, code in hits
    ]
    # widest shortfall first; code as a stable secondary key so two reasons of
    # equal cost order identically across runs
    records.sort(key=lambda r: (-r["shortfall"], r["code"]))
    return records


# ===========================================================================
# per-lead record
# ===========================================================================
def _lead_record(lead, cfg: dict, reasons: list[dict]) -> dict:
    p = int(cfg["runtime"].get("score_precision", 1))

    def r(v):
        return round(float(v), p) if v is not None else None

    fit = r(lead.fit_score)
    return {
        "lead_id": lead.lead_id,
        "name": lead.name,
        "company": lead.company,
        "company_size": lead.company_size,
        "industry": lead.industry,
        "source": lead.source,
        "last_interaction_date": lead.last_interaction_date,
        "recency_days": lead.recency_days,

        # an alias for fit_score. Both keys are emitted so a reader using
        # either term finds the field without a translation step.
        "qualification_score": fit,
        "fit_score": fit,
        "urgency_score": r(lead.urgency_score),
        "priority_score": r(lead.priority_score),
        # a queue position, not a qualification ranking - see the decision
        # grouping in queue[], which is what stops the interleave reading
        # as an error
        "priority_rank": lead.priority_rank,
        "priority_band": lead.priority_band,
        "decision": lead.decision,
        "reasoning": lead.reasoning,
        "factor_scores": {k: r(v) for k, v in lead.factor_scores.items()},
        "factor_contributions": {k: r(v) for k, v in lead.factor_contributions.items()},
        "guardrails_fired": list(lead.guardrails_fired),

        "outcome_reasons": reasons,
        "outcome_reason_codes": [x["code"] for x in reasons],
        "outcome_reason_text": (
            "\n".join(f"{x['label']} - {x['detail']}" for x in reasons) if reasons else None
        ),

        "completeness": round(float(lead.completeness), 2),
        "missing_fields": list(lead.missing_fields),

        "outreach_message": lead.message,
        "message_variant": lead.message_variant,
        "message_generated": bool(lead.message_generated),
        "message_fail_reason": lead.message_fail_reason,
        "message_word_count": lead.message_word_count,
    }


# ===========================================================================
# summary
# ===========================================================================
def _rate(value: float | None, n: int, denominator: str) -> dict:
    """Every rate in the report carries its n and names its denominator.
    A bare percentage is not reportable."""
    return {"value": value, "n": n, "denominator": denominator}


def build_summary(leads: list, cfg: dict, source_file: str = "",
                  processing_date: Any = None, records: list[dict] | None = None) -> dict:
    records = records or []
    n_total = len(leads)
    # total_processed and scored are separate numbers. A lead with every
    # factor missing has fit_score None: it is processed but not scored, and
    # a rate that divides by the wrong one reports a figure the system does
    # not emit.
    n_scored = sum(1 for l in leads if l.fit_score is not None)

    decisions = {d: 0 for d in _rcfg(cfg)["display"]["decision_order"]}
    for l in leads:
        decisions[l.decision] = decisions.get(l.decision, 0) + 1

    bands: dict[str, int] = {}
    for l in leads:
        key = l.priority_band if l.priority_band is not None else "UNBANDED"
        bands[key] = bands.get(key, 0) + 1

    denom = "all leads processed"
    qual_pct = round(100 * decisions.get("QUALIFIED", 0) / n_total, 1) if n_total else None
    rev_pct = round(100 * decisions.get("REVIEW", 0) / n_total, 1) if n_total else None

    # reason frequencies, counted over the leads that can carry each vocabulary
    rej_codes = set(_rcfg(cfg)["rejection_factor_codes"].values()) | {
        _rcfg(cfg)["rejection_default_code"]
    }
    rev_codes = set(_rcfg(cfg)["guardrail_reason_codes"].values())

    def _freq(codes: set[str], decision: str) -> list[dict]:
        pool = [r for r in records if r["decision"] == decision]
        n_pool = len(pool)
        counts: dict[str, int] = {}
        for rec in pool:
            for c in rec["outcome_reason_codes"]:
                if c in codes:
                    counts[c] = counts.get(c, 0) + 1
        lookup = _rcfg(cfg)["reason_lookup"]
        out = [
            {
                "code": c,
                "label": lookup.get(c, {}).get("label", c),
                "n": n,
                "pct": round(100 * n / n_pool, 1) if n_pool else None,
                "denominator": f"{decision} leads (n={n_pool})",
            }
            for c, n in counts.items()
        ]
        out.sort(key=lambda d: (-d["n"], d["code"]))
        return out

    qualified = [l for l in leads if l.decision == "QUALIFIED"]
    n_generated = sum(1 for l in qualified if l.message_generated)

    biz = cfg["decision"]["business_context"]
    monthly = int(biz["monthly_inbound_leads"])

    return {
        "source_file": source_file,
        "processing_date": str(processing_date) if processing_date else None,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rubric_version": cfg["meta"]["version"],
        "build": BUILD,

        "total_processed": n_total,
        "scored": n_scored,
        "unscoreable": n_total - n_scored,

        "decisions": decisions,
        "qualified_pct": _rate(qual_pct, n_total, denom),
        "review_pct": _rate(rev_pct, n_total, denom),

        "common_rejection_reasons": _freq(rej_codes, "REJECTED"),
        "review_reasons": _freq(rev_codes, "REVIEW"),

        "priority_bands": bands,
        "messages": {
            "generated": n_generated,
            "failed": len(qualified) - n_generated,
            "denominator": f"qualified leads (n={len(qualified)})",
        },
        "capacity_context": {
            "monthly_inbound_leads": monthly,
            "currently_worked_per_month": int(biz["currently_worked_per_month"]),
            "manual_minutes_per_decision": biz["manual_minutes_per_decision"],
            # a projection from one file at one observed rate, not a forecast
            # projected from the raw counts, never from the rounded pct above -
            # rounding twice moves the review projection by a whole lead
            "projected_qualified_monthly": (
                round(monthly * decisions.get("QUALIFIED", 0) / n_total) if n_total else None
            ),
            "projected_review_monthly": (
                round(monthly * decisions.get("REVIEW", 0) / n_total) if n_total else None
            ),
            "basis": f"observed rate on {source_file or 'this file'}, n={n_total}",
        },
    }


# ===========================================================================
# sample messages
# ===========================================================================
def select_samples(leads: list, cfg: dict) -> tuple[list[dict], str | None]:
    """Deterministic, so repeated runs select the same samples.

      1. highest-ranked generated message from each variant present
      2. guarantee two v2_engagement samples when two exist
      3. fill to the cap by priority_rank ascending

    Drawn only from leads where message_generated is True, which by
    construction means QUALIFIED with a successful generation.
    """
    R = _rcfg(cfg)["samples"]
    cap = int(R["max"])
    floor = int(R["min"])
    guaranteed = R.get("guarantee_two_of")

    pool = sorted(
        (l for l in leads if l.message_generated and l.message),
        key=lambda l: (l.priority_rank if l.priority_rank is not None else 10 ** 6),
    )

    chosen: list = []

    def take(lead):
        if lead not in chosen and len(chosen) < cap:
            chosen.append(lead)

    seen_variants = []
    for l in pool:
        if l.message_variant not in seen_variants:
            seen_variants.append(l.message_variant)
            take(l)

    if guaranteed:
        same = [l for l in pool if l.message_variant == guaranteed]
        if len(same) >= 2 and sum(1 for l in chosen if l.message_variant == guaranteed) < 2:
            for l in same:
                if l not in chosen:
                    take(l)
                    break

    for l in pool:
        if len(chosen) >= cap:
            break
        take(l)

    chosen.sort(key=lambda l: (l.priority_rank if l.priority_rank is not None else 10 ** 6))

    samples = [
        {
            "lead_id": l.lead_id,
            "company": l.company,
            "industry": l.industry,
            "source": l.source,
            "message_variant": l.message_variant,
            "priority_rank": l.priority_rank,
            "message_word_count": l.message_word_count,
            "message": l.message,
        }
        for l in chosen
    ]

    # never fabricate, never pad, never crash
    note = None
    if len(samples) < floor:
        note = f"only {len(samples)} generated messages available on this file"
    return samples, note


# ===========================================================================
# queue
# ===========================================================================
def build_queue(leads: list, cfg: dict, records: list[dict]) -> list[dict]:
    """Decision-grouped, rank-ordered. QUALIFIED first: successful records
    lead. The grouping is what makes the rank interleave in leads[] legible -
    decision comes from fit_score, queue position from priority_score, and
    the two genuinely cross over."""
    by_id = {r["lead_id"]: r for r in records}
    groups = []
    for decision in _rcfg(cfg)["display"]["decision_order"]:
        members = [
            by_id[l.lead_id] for l in leads
            if l.decision == decision and l.lead_id in by_id
        ]
        members.sort(key=lambda r: (r["priority_rank"] if r["priority_rank"] is not None else 10 ** 6))
        groups.append({"decision": decision, "n": len(members), "leads": members})
    return groups


# ===========================================================================
# run diagnostics
# ===========================================================================
def _diagnostics(cfg: dict, n_leads: int, stage1_meta: dict | None,
                 stage2_report: dict | None) -> dict:
    """Every LLM figure names its stage and its batch_size. Two batch_size
    keys exist with different meanings - llm.batch_size counts unique
    industry STRINGS per tiering call, llm_messages.batch_size counts LEADS
    per message call - and an unlabelled rate is unreadable."""
    s1 = stage1_meta or {}
    tier_tok = s1.get("tokens", {}) or {}
    msg_tok = ((stage2_report or {}).get("rates", {}) or {}).get("tokens", {}) or {}

    total = int(tier_tok.get("total_tokens", 0)) + int(msg_tok.get("total_tokens", 0))
    return {
        "tiering": {
            "stage": "1 - industry tier normalisation",
            "batch_size_key": "llm.batch_size",
            "batch_size": cfg["llm"]["batch_size"],
            "model": cfg["llm"]["model"],
            "temperature": cfg["llm"]["temperature"],
            "n_strings_total": s1.get("n_strings_total"),
            "n_from_cache": s1.get("n_from_cache"),
            "n_classified_live": s1.get("n_classified_live"),
            "batches": s1.get("batches"),
            "parse_failures": s1.get("parse_failures"),
            "tokens": tier_tok or None,
        },
        "messaging": {
            "stage": "2 - outreach message generation",
            "batch_size_key": "llm_messages.batch_size",
            "batch_size": cfg["llm_messages"]["batch_size"],
            "model": cfg["llm"]["model"],
            "temperature": cfg["llm_messages"]["temperature"],
            "max_tokens": cfg["llm_messages"]["max_tokens"],
            # verbatim from message_run_report.rates{}
            "rates": (stage2_report or {}).get("rates"),
            "n_cached": (stage2_report or {}).get("n_cached"),
            "n_sent": (stage2_report or {}).get("n_sent"),
            "n_failed": (stage2_report or {}).get("n_failed"),
            "lead_id_drift": (stage2_report or {}).get("lead_id_drift"),
        },
        "combined": {
            "total_tokens": total or None,
            "tokens_per_100_leads": (
                round(100 * total / n_leads) if total and n_leads else None
            ),
            "denominator": f"both LLM stages, {n_leads} leads processed",
        },
    }


# ===========================================================================
# build
# ===========================================================================
def build_report(leads: list, cfg: dict, source_file: str = "",
                 stage1_meta: dict | None = None, stage2_report: dict | None = None,
                 log: Callable[[str], Any] = print) -> dict:
    """Assemble the report from live ScoredLead objects. No file is read back
    and no join is performed - `leads` already carries both stages' fields."""
    warnings = _assert_invertible(cfg)
    for w in warnings:
        log(f"[warn] {w}")

    records = [_lead_record(l, cfg, classify_outcome(l, cfg)) for l in leads]
    records.sort(key=lambda r: (r["priority_rank"] if r["priority_rank"] is not None else 10 ** 6))

    samples, note = select_samples(leads, cfg)
    processing_date = (stage1_meta or {}).get("processing_date")

    report = {
        "summary": build_summary(leads, cfg, source_file, processing_date, records),
        "queue": build_queue(leads, cfg, records),
        "leads": records,
        "sample_messages": samples,
        "sample_note": note,
        "run_diagnostics": _diagnostics(cfg, len(leads), stage1_meta, stage2_report),
        "build_warnings": warnings,
    }
    return report


# ===========================================================================
# write
# ===========================================================================
def derive_stem(input_csv: str | Path, cfg: dict) -> str:
    """Every output name derives from the input stem, so no run can overwrite
    another run's record and a new CSV gets a fresh message cache with no
    clearing step and no flag."""
    stem = Path(str(input_csv)).stem
    prefix = _rcfg(cfg)["naming"].get("strip_prefix") or ""
    if prefix and stem.startswith(prefix):
        stem = stem[len(prefix):]
    return stem or "run"


def output_paths(cfg: dict, stem: str, workdir: str | Path = ".") -> dict[str, Path]:
    """Every path this pipeline writes, resolved from config patterns."""
    wd = Path(workdir)
    return {
        key: wd / str(pattern).format(stem=stem)
        for key, pattern in _rcfg(cfg)["naming"].items()
        if key != "strip_prefix"
    }


def write_report(report: dict, cfg: dict, stem: str,
                 workdir: str | Path = ".") -> list[str]:
    """Writes the JSON deliverable and the flat CSV. Returns paths written."""
    paths = output_paths(cfg, stem, workdir)
    written: list[str] = []

    jp = paths["output_json"]
    jp.write_text(json.dumps(report, indent=2, default=str))
    written.append(str(jp))

    columns = list(_rcfg(cfg)["csv_columns"])
    cp = paths["output_csv"]
    # utf-8-sig, not utf-8. Messages carry em dashes and curly apostrophes;
    # without a BOM Excel reads the file as CP-1252 and renders them as
    # mojibake. The message text itself is never altered - messages are final.
    with open(cp, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        # queue order: what a sales team opens, QUALIFIED at the top
        for group in report["queue"]:
            for rec in group["leads"]:
                row = dict(rec)
                row["outcome_reason_codes"] = ";".join(rec["outcome_reason_codes"])
                row["outcome_reason_text"] = rec["outcome_reason_text"] or ""
                w.writerow(row)
    written.append(str(cp))
    return written


# ===========================================================================
# render
# ===========================================================================
def _bar(n: int, peak: int, width: int = 12) -> str:
    if not peak:
        return ""
    return "\u2588" * max(1, round(width * n / peak)) if n else ""


def _wrap(text: str, width: int, indent: str) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(indent + cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(indent + cur)
    return lines


def render_report(report: dict, log: Callable[[str], Any] = print,
                  max_rows: int | None = None) -> None:
    """The sales-team view. Plain characters only - a five-row distribution
    does not justify a plotting dependency."""
    s = report["summary"]
    W = 66
    rule = "\u2550" * W

    log(rule)
    log(f"  LEAD INTELLIGENCE REPORT - {s['source_file']}")
    log(f"  Rubric {s['rubric_version']} - build {s['build']}")
    log(f"  Scored against {s['processing_date']}")
    log(rule)
    log("")

    d = s["decisions"]
    n = s["total_processed"]

    def pct(k):
        return f"{100 * d.get(k, 0) / n:.1f}%" if n else "n/a"

    left = [
        f"PROCESSED   {n} leads",
        f"SCORED      {s['scored']} leads",
        f"UNSCOREABLE {s['unscoreable']}" if s["unscoreable"] else "",
    ]
    pad = max(len(x) for x in left) + 4
    for lab, text in zip(("QUALIFIED", "REVIEW", "REJECTED"), left):
        log(f"  {text:<{pad}}{lab:<12}{d.get(lab, 0):>4}  ({pct(lab)})")
    log("")

    c = s["capacity_context"]
    log(f"  At {c['monthly_inbound_leads']:,} inbound/month this projects to ~{c['projected_qualified_monthly']} qualified")
    log(f"  and ~{c['projected_review_monthly']} to human review, against a team currently working")
    log(f"  ~{c['currently_worked_per_month']}. Projection from one file, n={s['total_processed']}.")
    log("")

    titles = {
        "QUALIFIED": "PRIORITY QUEUE - QUALIFIED",
        "REVIEW": "FLAGGED FOR HUMAN REVIEW",
        "REJECTED": "NOT PURSUED",
    }

    for group in report["queue"]:
        dec = group["decision"]
        head = f"\u2500\u2500\u2500\u2500 {titles.get(dec, dec)} "
        log(head + "\u2500" * max(0, W - len(head)))

        if dec == "REJECTED":
            reasons = s["common_rejection_reasons"]
            peak = max([r["n"] for r in reasons], default=0)
            for r in reasons:
                log(f"  {r['code']:<14}{str(r['label'])[:32]:<34}{r['n']:>3}  {_bar(r['n'], peak)}")
            log(f"  {'':<14}{'':<34}{'':>3}  n={group['n']} leads not pursued")
            log("")
            continue

        if dec == "REVIEW":
            for r in s["review_reasons"]:
                log(f"  {r['code']:<14}{str(r['label'])[:32]:<34}{r['n']:>3}")
            log("")

        rows = group["leads"] if max_rows is None else group["leads"][:max_rows]
        if not rows:
            log("  (none)")
            log("")
            continue

        log(f"  {'rank':>4}  {'lead':<5} {'company':<26}{'size':>6}  {'industry':<14}{'score':>6}  band")
        for r in rows:
            size = "" if r["company_size"] is None else f"{int(r['company_size']):,}"
            score = "  n/a" if r["qualification_score"] is None else f"{r['qualification_score']:>5.1f}"
            log(
                f"  {str(r['priority_rank']):>4}  {r['lead_id']:<5} "
                f"{str(r['company'])[:26]:<26}{size:>6}  "
                f"{str(r['industry'])[:14]:<14}{score}  {r['priority_band'] or ''}"
            )
            if dec == "REVIEW":
                for x in r["outcome_reasons"]:
                    log(f"        {x['code']:<14} {x['detail']}")
        if max_rows is not None and len(group["leads"]) > max_rows:
            log(f"  ... {len(group['leads']) - max_rows} more (full list in the CSV)")
        log("")

    head = "\u2500\u2500\u2500\u2500 SAMPLE OUTREACH "
    log(head + "\u2500" * max(0, W - len(head)))
    if report["sample_note"]:
        log(f"  {report['sample_note']}")
    for i, sm in enumerate(report["sample_messages"], start=1):
        log(
            f"  #{i}  {sm['lead_id']}  {str(sm['company'])[:28]:<28} "
            f"[{sm['message_variant']}]  rank {sm['priority_rank']}  {sm['message_word_count']}w"
        )
        for line in _wrap(sm["message"], W - 8, "      "):
            log(line)
        log("")

    log(rule)
