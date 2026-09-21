"""
rubric_scorer.py
================
Deterministic scoring engine for the Lead Qualification Tool.

Contains no business numbers. Every band, weight, threshold and policy is read
from config.yaml. The only judgment call made outside this file is the
industry-tier lookup, which arrives pre-computed as a {industry_string: tier}
dict from the notebook's tiering call.

Pipeline per lead:
    normalise fields -> per-factor raw scores (1-10)
    -> fit score      = weighted mean over factors with score_weight   > 0
    -> urgency score  = weighted mean over factors with urgency_weight > 0
    -> priority score = blend(fit, urgency)
    -> priority band, rank
    -> guardrail pass (can override the decision, never the score)

Revision
--------
1.1.1  Five optional messaging fields added to ScoredLead: message,
       message_generated, message_variant, message_fail_reason,
       message_word_count. Additive only - no scoring function reads or writes
       them, and to_frame() does not emit them, so the scoring path and the
       scored CSV are unchanged byte-for-byte. A file scored without running
       the messaging stage still produces valid records.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any

import pandas as pd
import yaml

SCHEMA_COLUMNS = [
    "name",
    "company",
    "company_size",
    "industry",
    "source",
    "last_interaction_date",
]


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# missing-data handling
# ---------------------------------------------------------------------------
def is_missing(value: Any, sentinels: set[str]) -> bool:
    """True for nulls AND for sentinel strings.

    Both encodings are live in the supplied data: pandas turns the literal
    string "NA" into NaN on read, while "Unknown Sector" / "Unknown Source"
    survive as strings that .isna() will not catch.
    """
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.strip() in sentinels:
        return True
    return False


def clean_str(value: Any, sentinels: set[str]) -> str | None:
    return None if is_missing(value, sentinels) else str(value).strip()


def clean_size(value: Any, sentinels: set[str]) -> float | None:
    """company_size coerces to float wherever nulls are present, so it is kept
    as float throughout and only formatted as int for display."""
    if is_missing(value, sentinels):
        return None
    try:
        size = float(value)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def clean_date(value: Any, sentinels: set[str]) -> date | None:
    if is_missing(value, sentinels):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


# ---------------------------------------------------------------------------
# per-factor scorers -> (raw_score | None, human-readable detail)
# ---------------------------------------------------------------------------
def score_source(source: str | None, cfg: dict) -> tuple[float | None, str]:
    if source is None:
        return None, "source missing"
    cls = cfg["class_lookup"].get(source)
    if cls is None:
        return None, f"source '{source}' not in class lookup"
    return float(cfg["class_scores"][cls]), f"{source} -> {cls}"


def score_industry(
    industry: str | None, tier_map: dict[str, str], cfg: dict
) -> tuple[float | None, str]:
    if industry is None:
        return None, "industry missing"
    tier = tier_map.get(industry)
    if tier is None or tier not in cfg["tier_scores"]:
        return None, f"industry '{industry}' unclassified"
    return float(cfg["tier_scores"][tier]), f"{industry} -> {tier}"


def score_company_size(size: float | None, cfg: dict) -> tuple[float | None, str]:
    """Band selects a score RANGE; headcount is interpolated linearly inside
    it, so a larger company outscores a smaller one without either leaving
    the band. Interpolation never escapes the band's own range."""
    if size is None:
        return None, "company_size missing"

    cap = float(cfg["upper_cap"])
    for band in cfg["bands"]:
        lo = float(band["min"])
        hi = cap if band["max"] is None else float(band["max"])
        if size >= lo and (band["max"] is None or size <= hi):
            effective = min(size, cap)
            span = hi - lo
            frac = 0.0 if span <= 0 else (effective - lo) / span
            frac = max(0.0, min(1.0, frac))
            s_lo, s_hi = (float(x) for x in band["score_range"])
            raw = s_lo + frac * (s_hi - s_lo)
            detail = (
                f"{int(size)} employees -> band '{band['name']}' "
                f"[{s_lo}-{s_hi}], interpolated {round(frac * 100)}% in"
            )
            return raw, detail

    return None, f"company_size {size} matched no band"


def score_recency(
    last_interaction: date | None, processing_date: date, cfg: dict
) -> tuple[float | None, str, int | None]:
    """Piecewise-linear decay. Each segment declares a score RANGE and age is
    interpolated inside it, the same pattern score_company_size uses.

    Segment endpoints are contiguous in config, so the function is continuous:
    there is no jump at a boundary. The flat-band version this replaced scored
    a 1-day lead and a 29-day lead identically, then dropped a 31-day lead by
    3.0 raw points - a cliff in a numeric quantity that has no cliff in
    reality.
    """
    if last_interaction is None:
        return None, "last_interaction_date missing", None

    age = (processing_date - last_interaction).days
    age = max(age, 0)  # a future-dated interaction clamps to 0, never negative

    cap = float(cfg["upper_cap_days"])
    lo = 0.0
    for band in cfg["bands"]:
        hi = cap if band["max_days"] is None else float(band["max_days"])
        if band["max_days"] is None or age <= hi:
            effective = min(float(age), cap)
            span = hi - lo
            frac = 0.0 if span <= 0 else (effective - lo) / span
            frac = max(0.0, min(1.0, frac))
            s_lo, s_hi = (float(x) for x in band["score_range"])
            raw = s_lo + frac * (s_hi - s_lo)
            detail = (
                f"{age}d since contact -> segment '{band['name']}' "
                f"[{s_lo}-{s_hi}], {round(frac * 100)}% through"
            )
            return raw, detail, age
        lo = hi

    return None, f"age {age}d matched no segment", age


# ---------------------------------------------------------------------------
# result container
# ---------------------------------------------------------------------------
@dataclass
class ScoredLead:
    lead_id: str
    name: str | None
    company: str | None
    company_size: float | None
    industry: str | None
    source: str | None
    last_interaction_date: str | None

    factor_scores: dict = field(default_factory=dict)   # raw 1-10 per factor
    factor_details: dict = field(default_factory=dict)  # reasoning fragments
    factor_contributions: dict = field(default_factory=dict)

    fit_score: float | None = None
    urgency_score: float | None = None
    priority_score: float | None = None
    priority_band: str | None = None
    priority_rank: int | None = None

    recency_days: int | None = None
    missing_fields: list = field(default_factory=list)
    completeness: float = 1.0

    decision: str = "UNSET"
    guardrails_fired: list = field(default_factory=list)
    reasoning: str = ""

    # --- messaging fields --------------------------------------------------
    # Optional and additive. Nothing in the scoring path reads or writes these,
    # and to_frame() does not emit them, so scoring a file without running the
    # message stage still produces valid records and an unchanged CSV.
    message: str | None = None
    message_generated: bool = False
    message_variant: str | None = None
    message_fail_reason: str | None = None
    message_word_count: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# weighted-average core
# ---------------------------------------------------------------------------
def _weighted_mean(
    scores: dict[str, float | None], weights: dict[str, float]
) -> float | None:
    """Weighted mean over factors that are BOTH present and carry weight > 0.

    Dropping a missing factor and renormalising over the remaining weights is
    why the rubric is a weighted average rather than additive points: with
    additive points, dropping a factor silently lowers the attainable ceiling.
    """
    num = 0.0
    den = 0.0
    for name, raw in scores.items():
        w = float(weights.get(name, 0.0))
        if raw is None or w <= 0:
            continue
        num += raw * w
        den += w
    return None if den == 0 else num / den


# ---------------------------------------------------------------------------
# main entry points
# ---------------------------------------------------------------------------
def resolve_processing_date(df: pd.DataFrame, cfg: dict) -> date:
    """'auto' resolves to the newest interaction date in the file. Recency is
    never measured against the system clock - every supplied date is roughly
    960 days stale against it, which would score every lead as cold."""
    setting = cfg["runtime"]["processing_date"]
    if setting != "auto":
        return datetime.strptime(str(setting), "%Y-%m-%d").date()
    parsed = pd.to_datetime(df["last_interaction_date"], errors="coerce")
    if parsed.notna().sum() == 0:
        raise ValueError("processing_date='auto' but no parseable dates in file")
    return parsed.max().date()


def score_lead(
    row: pd.Series,
    lead_id: str,
    cfg: dict,
    tier_map: dict[str, str],
    processing_date: date,
) -> ScoredLead:
    sentinels = set(cfg["missing_data"]["sentinel_values"])
    factors = cfg["factors"]

    name = clean_str(row.get("name"), sentinels)
    company = clean_str(row.get("company"), sentinels)
    size = clean_size(row.get("company_size"), sentinels)
    industry = clean_str(row.get("industry"), sentinels)
    source = clean_str(row.get("source"), sentinels)
    last_dt = clean_date(row.get("last_interaction_date"), sentinels)

    lead = ScoredLead(
        lead_id=lead_id,
        name=name,
        company=company,
        company_size=size,
        industry=industry,
        source=source,
        last_interaction_date=last_dt.isoformat() if last_dt else None,
    )

    # --- completeness (drives the review guardrail) ------------------------
    fields = cfg["missing_data"]["completeness_fields"]
    lead.missing_fields = [
        c for c in fields if is_missing(row.get(c), sentinels)
    ]
    lead.completeness = 1.0 - (len(lead.missing_fields) / len(fields))

    # --- per-factor raw scores --------------------------------------------
    s_src, d_src = score_source(source, factors["source"])
    s_ind, d_ind = score_industry(industry, tier_map, factors["industry"])
    s_siz, d_siz = score_company_size(size, factors["company_size"])
    s_rec, d_rec, age = score_recency(last_dt, processing_date, factors["recency"])
    lead.recency_days = age

    lead.factor_scores = {
        "source": s_src,
        "industry": s_ind,
        "company_size": s_siz,
        "recency": s_rec,
    }
    lead.factor_details = {
        "source": d_src,
        "industry": d_ind,
        "company_size": d_siz,
        "recency": d_rec,
    }

    score_w = {k: float(v["score_weight"]) for k, v in factors.items()}
    urg_w = {k: float(v["urgency_weight"]) for k, v in factors.items()}

    lead.fit_score = _weighted_mean(lead.factor_scores, score_w)
    lead.urgency_score = _weighted_mean(lead.factor_scores, urg_w)

    # --- priority ----------------------------------------------------------
    blend = cfg["priority"]["blend"]
    if lead.fit_score is None:
        lead.priority_score = None
    elif lead.urgency_score is None:
        # No urgency-carrying factor survived. Fall back to fit alone rather
        # than inventing an urgency value.
        lead.priority_score = lead.fit_score
    else:
        lead.priority_score = (
            float(blend["fit"]) * lead.fit_score
            + float(blend["urgency"]) * lead.urgency_score
        ) / (float(blend["fit"]) + float(blend["urgency"]))

    bands = cfg["priority"]["bands"]
    if lead.priority_score is None:
        lead.priority_band = None
    elif lead.priority_score >= float(bands["high_min"]):
        lead.priority_band = "HIGH"
    elif lead.priority_score >= float(bands["medium_min"]):
        lead.priority_band = "MEDIUM"
    else:
        lead.priority_band = "LOW"

    # --- rounding ----------------------------------------------------------
    p = int(cfg["runtime"]["score_precision"])
    for attr in ("fit_score", "urgency_score", "priority_score"):
        val = getattr(lead, attr)
        if val is not None:
            setattr(lead, attr, round(val, p))
    lead.factor_scores = {
        k: (None if v is None else round(v, p))
        for k, v in lead.factor_scores.items()
    }

    # --- factor-traceable contributions ------------------------------------
    # A weighted average does not decompose as "+2 +2 +2 +2 = 8", so the
    # contribution of each factor is recorded explicitly instead.
    den = sum(w for k, w in score_w.items() if lead.factor_scores.get(k) is not None)
    for k, raw in lead.factor_scores.items():
        if raw is None or den == 0:
            continue
        # 2dp regardless of score_precision, so the printed contributions
        # visibly sum to the fit score instead of drifting by rounding.
        lead.factor_contributions[k] = round(raw * score_w[k] / den, 2)

    lead.reasoning = build_reasoning(lead, score_w)
    return lead


def build_reasoning(lead: ScoredLead, score_w: dict[str, float]) -> str:
    parts = []
    for k, raw in lead.factor_scores.items():
        if raw is None:
            parts.append(f"{k}: DROPPED ({lead.factor_details[k]})")
        else:
            contrib = lead.factor_contributions.get(k)
            parts.append(
                f"{k}: {raw}/10 x w{score_w[k]} -> +{contrib} "
                f"({lead.factor_details[k]})"
            )
    head = (
        f"fit {lead.fit_score}/10, urgency {lead.urgency_score}, "
        f"priority {lead.priority_score} [{lead.priority_band}]"
    )
    return head + " | " + " | ".join(parts)


# ---------------------------------------------------------------------------
# guardrails - run AFTER scoring, can override decision, never the score
# ---------------------------------------------------------------------------
def apply_guardrails(lead: ScoredLead, cfg: dict) -> ScoredLead:
    rules = {r["id"]: r for r in cfg["guardrails"]}
    cutoff = cfg["decision"]["qualify_cutoff"]
    margin = float(cfg["decision"]["review_margin"])

    # baseline decision from the cutoff, before any override
    if lead.fit_score is None:
        lead.decision = "REVIEW"
    elif cutoff is None:
        lead.decision = "UNSET"
    else:
        lead.decision = "QUALIFIED" if lead.fit_score >= float(cutoff) else "REJECTED"

    r = rules.get("no_scoreable_factors")
    if r and r["enabled"] and lead.fit_score is None:
        lead.decision = "REVIEW"
        lead.guardrails_fired.append("no_scoreable_factors")

    r = rules.get("incomplete_record")
    if r and r["enabled"] and lead.missing_fields:
        lead.decision = "REVIEW"
        lead.guardrails_fired.append("incomplete_record")

    r = rules.get("borderline_score")
    if (
        r
        and r["enabled"]
        and cutoff is not None
        and lead.fit_score is not None
        and abs(lead.fit_score - float(cutoff)) <= margin
    ):
        lead.decision = "REVIEW"
        lead.guardrails_fired.append("borderline_score")

    return lead


# ---------------------------------------------------------------------------
# batch + ranking
# ---------------------------------------------------------------------------
def _rank_key(lead: ScoredLead):
    """Full deterministic tie-break chain. Without it, two runs over the same
    file can emit different ranks and the report stops being
    reproducible."""
    return (
        -(lead.priority_score if lead.priority_score is not None else -1),
        -(lead.fit_score if lead.fit_score is not None else -1),
        lead.recency_days if lead.recency_days is not None else 10**6,
        -(lead.company_size if lead.company_size is not None else -1),
        lead.lead_id,
    )


def score_dataframe(
    df: pd.DataFrame, cfg: dict, tier_map: dict[str, str]
) -> tuple[list[ScoredLead], date]:
    processing_date = resolve_processing_date(df, cfg)
    prefix = cfg["runtime"]["lead_id_prefix"]
    width = max(3, len(str(len(df))))

    leads = [
        apply_guardrails(
            score_lead(row, f"{prefix}{i + 1:0{width}d}", cfg, tier_map, processing_date),
            cfg,
        )
        for i, (_, row) in enumerate(df.iterrows())
    ]

    for rank, lead in enumerate(sorted(leads, key=_rank_key), start=1):
        lead.priority_rank = rank

    return leads, processing_date


def to_frame(leads: list[ScoredLead]) -> pd.DataFrame:
    rows = []
    for l in leads:
        row = {
            "lead_id": l.lead_id,
            "name": l.name,
            "company": l.company,
            "company_size": l.company_size,
            "industry": l.industry,
            "source": l.source,
            "last_interaction_date": l.last_interaction_date,
            "recency_days": l.recency_days,
            "fit_score": l.fit_score,
            "urgency_score": l.urgency_score,
            "priority_score": l.priority_score,
            "priority_band": l.priority_band,
            "priority_rank": l.priority_rank,
            "decision": l.decision,
            "completeness": round(l.completeness, 2),
            "missing_fields": ";".join(l.missing_fields),
            "guardrails_fired": ";".join(l.guardrails_fired),
            "reasoning": l.reasoning,
        }
        for k, v in l.factor_scores.items():
            row[f"f_{k}"] = v          # persisted so weights can be re-swept
        rows.append(row)               # offline without another LLM call
    return pd.DataFrame(rows).sort_values("priority_rank").reset_index(drop=True)


def cutoff_sweep(leads: list[ScoredLead], monthly_volume: int) -> pd.DataFrame:
    """Qualified count and projected monthly volume at each candidate cutoff.
    Reported so the cutoff can be argued against team capacity rather than
    picked as a round number. Does not choose a cutoff."""
    scored = [l for l in leads if l.fit_score is not None]
    n = len(scored)
    rows = []
    for c in [x / 2 for x in range(8, 21)]:        # 4.0 .. 10.0 in 0.5 steps
        q = sum(1 for l in scored if l.fit_score >= c)
        rows.append(
            {
                "cutoff": c,
                "qualified_n": q,
                "n_scored": n,
                "qualified_pct": round(100 * q / n, 1) if n else None,
                "projected_monthly": round(monthly_volume * q / n) if n else None,
            }
        )
    return pd.DataFrame(rows)
