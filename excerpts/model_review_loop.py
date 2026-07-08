"""Model-review loop (sanitized production excerpt) — the AI agent that keeps
the predictive cost model honest as data arrives.

The platform projects each member's annual medical cost (risk score × the
book's own cost-rate, credibility-weighted utilization trajectory, calibrated
so member projections redistribute but never invent book-level dollars).
Projections snapshot nightly. THIS agent runs after every claims / fund
data load and:

  1. SCORES every stored projection against the new actuals. Test point =
     the EARLIEST snapshot per (member × formula version) — the purest
     prospective measurement. Expected-to-date = projected_annual ×
     qualified_months / 12, where a month "qualifies" only when member-level
     cost allocation reconciles >=80% to the payer's own monthly statement
     (recent months under-allocate as claims complete — including them would
     flatter accuracy on numbers that will still move).

  2. Reports TWO accuracy slices, deliberately: "overall" (includes months the
     calibration already saw — book bias reads ~0 by construction, so only the
     member-level spread is informative) and "PROSPECTIVE" (only months that
     qualified AFTER the snapshot was taken — the honest number, which starts
     accruing as each new monthly fund file lands).

  3. HUNTS BIAS COHORTS across six dimensions — risk-score band, data
     sufficiency, lever count, plan, months-observed tenure, utilization
     flags. A cohort flags at n >= 15 and |bias| >= 15%.

  4. Files VERSIONED TWEAK PROPOSALS with evidence attached. Every flagged
     cohort yields a proposal (a generic fallback rule guarantees nothing
     flagged can fall through silently — the first fixture run caught a
     sparse-data cohort at -78% bias that the original bespoke rules missed).
     Proposals NEVER self-apply: a human approves, then the change ships as
     the next formula version, and historical snapshots keep their original
     version so old and new formulas are scored against each other honestly.

  5. SKIPS silently when nothing new landed (same fund period, same latest
     snapshot, same formula version as the last run) — the loop is safe to
     fire on every ingestion run.

Never raises — it runs inside the ingestion jobs.
"""
import json
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import func

from . import models as m
from .model import MODEL_VERSION, fund_actuals   # shared "actual" definition —
# the model and its reviewer must never disagree about what "actual" means.


def model_review(db, trigger=""):
    try:
        year = str(date.today().year)
        fund, _year_months, qualified_months = fund_actuals(db, year)
        if not qualified_months:
            return None
        fund_period = db.query(func.max(m.MemberFundMonth.period)).scalar() or ""
        latest_snapshot = db.query(func.max(m.PredictionSnapshot.as_of)).scalar() or ""
        if not latest_snapshot:
            return None

        # skip-guard: only run when the actuals or the model actually moved
        last_run = db.query(m.ModelReviewRun).order_by(m.ModelReviewRun.id.desc()).first()
        if (last_run and last_run.version == MODEL_VERSION
                and last_run.fund_period == fund_period
                and last_run.snapshot_as_of == latest_snapshot):
            return None

        qualified = set(qualified_months)
        n_months = len(qualified_months)
        actual_of = defaultdict(float)
        for (member_id, month), values in fund.items():
            if month in qualified:
                actual_of[member_id] += values.cost

        # earliest snapshot per (member, version) — all versions score for the
        # cross-version table; cohorts/proposals only for the CURRENT formula
        first = (db.query(m.PredictionSnapshot.member_id.label("mid"),
                          m.PredictionSnapshot.version.label("ver"),
                          func.min(m.PredictionSnapshot.as_of).label("first_day"))
                 .group_by(m.PredictionSnapshot.member_id, m.PredictionSnapshot.version)
                 .subquery())
        snapshots = (db.query(m.PredictionSnapshot)
                     .join(first, (m.PredictionSnapshot.member_id == first.c.mid)
                           & (m.PredictionSnapshot.version == first.c.ver)
                           & (m.PredictionSnapshot.as_of == first.c.first_day)).all())

        rows, errors_by_version = [], defaultdict(list)
        for snap in snapshots:
            expected = (snap.projected_annual or 0) * n_months / 12.0
            if expected < 100:                      # tiny base → meaningless percentage
                continue
            actual = actual_of.get(snap.member_id, 0.0)
            err = (actual - expected) / expected
            errors_by_version[snap.version or "?"].append(err)
            if (snap.version or "") != MODEL_VERSION:
                continue

            drivers = _json_or_empty(snap.drivers)
            # prospective slice: months that qualified AFTER this snapshot existed
            snapshot_month = (snap.as_of or "")[:7]
            prospective_months = [mo for mo in qualified_months if mo > snapshot_month]
            p_err = None
            if prospective_months:
                p_expected = (snap.projected_annual or 0) * len(prospective_months) / 12.0
                if p_expected >= 100:
                    p_actual = sum(fund[(snap.member_id, mo)].cost
                                   for mo in prospective_months
                                   if (snap.member_id, mo) in fund)
                    p_err = (p_actual - p_expected) / p_expected

            raf = snap.raf or 0
            rows.append({
                "err": err, "p_err": p_err,
                "raf_band": ("<0.8" if raf < 0.8 else "0.8-1.2" if raf < 1.2
                             else "1.2-2.0" if raf < 2 else "2.0+"),
                "data": "LOW DATA" if drivers.get("low_data") else "full data",
                "levers": _lever_bucket(drivers.get("data_score") or 0),
                "plan": snap.plan or "?",
                "flags": drivers.get("flags") or [],
                "tenure": _tenure_bucket(snap.months_observed or 0),
            })
        if not rows:
            return None

        cohorts = (_dimension("Risk-score band", rows, lambda r: r["raf_band"])
                   + _dimension("Data sufficiency", rows, lambda r: r["data"])
                   + _dimension("Lever count", rows, lambda r: r["levers"])
                   + _dimension("Plan", rows, lambda r: r["plan"])
                   + _dimension("Months observed", rows, lambda r: r["tenure"])
                   + _flag_dimension(rows))

        overall = _stats([r["err"] for r in rows])
        prospective_errs = [r["p_err"] for r in rows if r["p_err"] is not None]
        metrics = {
            "overall": overall,
            "prospective": _stats(prospective_errs) if prospective_errs else None,
            "by_version": {v: _stats(e) for v, e in sorted(errors_by_version.items())},
        }

        # EVERY flagged cohort yields a proposal — bespoke rule or generic fallback
        proposals = []
        for c in cohorts:
            if not c["flagged"]:
                continue
            correction = round(1 / (1 + c["bias_pct"] / 100), 3)
            evidence = (f"n={c['n']}, bias {c['bias_pct']:+.1f}%, "
                        f"median |err| {c['med_abs_err_pct']}%")
            proposals.append({
                "key": f"{c['dimension']}:{c['cohort']}",
                "title": f"{c['dimension']} · {c['cohort']} runs {c['bias_pct']:+.1f}% vs actuals",
                "change": _suggest_change(c, correction),
                "evidence": evidence,
                "status": "proposed",          # a human moves it forward; never self-applied
            })

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        run = m.ModelReviewRun(
            run_at=now, trigger=(trigger or "")[:300], version=MODEL_VERSION,
            fund_period=fund_period, snapshot_as_of=latest_snapshot,
            snapshots_scored=overall["n"], months=json.dumps(qualified_months),
            metrics=json.dumps(metrics), cohorts=json.dumps(cohorts),
            proposals=json.dumps(proposals), status="proposed")
        db.add(run)
        _sync_notification(db, proposals, now)   # one open alert while proposals pend
        db.commit()
        return run.id
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        print(f"  !! model review failed: {str(e)[:300]}")
        return None


# ── helpers ────────────────────────────────────────────────────────────────

def _stats(errors):
    n = len(errors)
    abs_sorted = sorted(abs(e) for e in errors)
    return {"n": n, "bias_pct": round(sum(errors) / n * 100, 1),
            "med_abs_err_pct": round(abs_sorted[n // 2] * 100, 1)}


def _dimension(label, rows, key_fn):
    groups = defaultdict(list)
    for r in rows:
        groups[key_fn(r)].append(r["err"])
    out = []
    for cohort_key in sorted(groups):
        st = _stats(groups[cohort_key])
        st.update({"dimension": label, "cohort": cohort_key,
                   "flagged": st["n"] >= 15 and abs(st["bias_pct"]) >= 15})
        out.append(st)
    return out


def _flag_dimension(rows):
    out = []
    for flag in ("high ED utilization", "polypharmacy"):
        errors = [r["err"] for r in rows if flag in r["flags"]]
        if errors:
            st = _stats(errors)
            st.update({"dimension": "Utilization flag", "cohort": flag,
                       "flagged": st["n"] >= 15 and abs(st["bias_pct"]) >= 15})
            out.append(st)
    return out


def _lever_bucket(score):
    return "0-2 levers" if score <= 2 else f"{score} levers"


def _tenure_bucket(months):
    return "0-2 mo" if months <= 2 else "3-5 mo" if months <= 5 else "6+ mo"


def _suggest_change(cohort, correction):
    if cohort["dimension"] == "Risk-score band":
        return (f"band-specific calibration ≈ ×{correction} on that band's baseline "
                "(or a piecewise cost-rate) — next-version candidate")
    if cohort["dimension"] == "Lever count":
        return (f"raise the sparse-data threshold or apply a sparse-data calibration "
                f"≈ ×{correction} — these members carry too little signal for the full formula")
    if cohort["dimension"] == "Months observed":
        return f"retune the credibility weight for this tenure band; interim ≈ ×{correction}"
    if cohort["dimension"] == "Utilization flag":
        return "retune the modifier weight against this cohort's residual"
    return f"cohort-specific calibration ≈ ×{correction} (no bespoke rule yet — review the driver)"


def _json_or_empty(raw):
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


def _sync_notification(db, proposals, now):
    """One notification-slot alert while proposals await approval; resolves
    when a run produces none — the bell never accumulates stale entries."""
    slot = (db.query(m.Alert).filter(m.Alert.source == "model",
                                     m.Alert.key == "model:proposals",
                                     m.Alert.status == "open").first())
    if proposals:
        title = f"Model review: {len(proposals)} proposal(s) await approval ({MODEL_VERSION})"
        detail = "; ".join(p["title"] for p in proposals)[:400]
        if slot:
            slot.title, slot.detail, slot.updated_at = title, detail, now
        else:
            db.add(m.Alert(source="model", key="model:proposals", severity="info",
                           title=title, detail=detail, status="open",
                           opened_at=now, updated_at=now))
    elif slot:
        slot.status, slot.resolved_at, slot.updated_at = "resolved", now, now
