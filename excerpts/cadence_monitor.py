"""Data-integrity cadence monitor (sanitized production excerpt).

Runs inside the ingestion jobs after every pull/replay — the nightly pull is
its heartbeat even on days when nothing arrives. Three detections:

  (a) OVERDUE — no new file for materially longer than the feed's own observed
      cadence. The cadence is LEARNED from arrival history (median inter-arrival
      gap, requiring >=3 samples so a brand-new feed can never false-alarm),
      not hardcoded — a daily census feed and a monthly statement feed get
      appropriate thresholds automatically.

  (b) CONTENT FROZEN — files keep arriving on schedule but the feed's max
      content date stops advancing beyond its allowed lag. This is the nastiest
      real failure mode we've seen: the payer's report generator broke while
      their delivery mechanism kept shipping files, so a delivery-only monitor
      saw a perfectly healthy feed. Each feed gets a content probe (a single
      MAX() on an indexed column — one deliberately has NO probe because the
      table is millions of rows and a MAX() there would full-scan) and a
      per-feed lag allowance reflecting its inherent latency (claims allocate
      weeks after service; statements cover the prior month).

  (c) QUARANTINE — files the loader rejected.

Findings surface as reasons on the operations dashboard's feed card AND persist
as open/resolved alert rows: one open row per (feed, condition), refreshed in
place while true, RESOLVED when cleared — so a standing outage never spams a
new alert per day, and the notification bell / weekly email always reflect
current truth. Never raises: a monitor must not be able to break ingestion.
"""
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import func

from . import models as m

# Per-feed content-freshness probes: (max content date query, allowed lag in
# days behind the latest file arrival).
CONTENT_PROBES = {
    "CENSUS":    (lambda db: db.query(func.max(m.Admission.admit_date)).scalar(), 5),
    "CLAIMS":    (lambda db: db.query(func.max(m.Claim.service_from)).scalar(), 45),
    "PHARMACY":  (lambda db: db.query(func.max(m.RxClaim.fill_date)).scalar(), 21),
    "REVENUE":   (lambda db: db.query(func.max(m.RevenueRow.payment_month)).scalar(), 45),
    "ELIG":      (lambda db: db.query(func.max(m.EligibilityMonth.month)).scalar(), 40),
    "FUND":      (lambda db: db.query(func.max(m.MemberFundMonth.month)).scalar(), 75),
    "ADHERENCE": (lambda db: db.query(func.max(m.AdherenceRow.run_date)).scalar(), 14),
    # NOTE: the encounter-diagnosis feed has NO probe on purpose — its table is
    # millions of rows with a strict index budget; it stays cadence-only.
}


def _arrival_date(filename, mtime, pulled_at):
    """Best arrival date for cadence math: the filename's own timestamp first
    (it survives bulk backfills, where pull timestamps collapse to one day),
    then source mtime, then the ledger pull time."""
    import re
    md = re.search(r"(20\d{6})", filename or "")
    candidates = [
        f"{md.group(1)[:4]}-{md.group(1)[4:6]}-{md.group(1)[6:8]}" if md else "",
        (mtime or "")[:10],
        (pulled_at or "")[:10],
    ]
    for s in candidates:
        try:
            return date.fromisoformat(s)
        except ValueError:
            continue
    return None


def _content_date(value):
    """Content dates arrive day- or month-granular; treat 'YYYY-MM' as its
    month-end so freshness lag isn't overstated by up to 30 days."""
    s = (value or "")[:10]
    if len(s) == 7:
        s += "-28"
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def feed_integrity(db):
    """Compute per-feed integrity state. Returns
    {feed_code: {feed, cadence_days, last_arrival, days_since, content_through,
                 reasons: [{kind, text}]}}.

    The ingest ledger is small (hundreds of rows) and every probe is a single
    MAX() on an indexed column — safe on the dashboard read path, so the feed
    card and the alert store can never disagree.
    """
    today = date.today()
    arrivals, names, quarantined = defaultdict(set), {}, defaultdict(int)
    for code, feed_name, filename, status, mtime, pulled_at in db.query(
            m.IngestFile.feed_code, m.IngestFile.feed, m.IngestFile.filename,
            m.IngestFile.status, m.IngestFile.mtime, m.IngestFile.pulled_at).all():
        if not code:
            continue
        names.setdefault(code, feed_name or code)
        d = _arrival_date(filename, mtime, pulled_at)
        if d:
            arrivals[code].add(d)
        if status == "quarantined":
            quarantined[code] += 1

    out = {}
    for code, days in arrivals.items():
        seq = sorted(days)
        gaps = [(b - a).days for a, b in zip(seq, seq[1:]) if (b - a).days >= 1]
        # learned cadence: median gap, >=3 samples or no overdue judgment at all
        median_gap = sorted(gaps)[len(gaps) // 2] if len(gaps) >= 3 else None
        last = seq[-1]
        since = (today - last).days
        rec = {"feed": names.get(code, code), "cadence_days": median_gap,
               "last_arrival": last.isoformat(), "days_since": since,
               "content_through": "", "reasons": []}

        # (a) overdue — generous threshold so a weekend never false-alarms a daily feed
        if median_gap is not None and since > max(round(1.8 * median_gap), median_gap + 3):
            rec["reasons"].append({
                "kind": "overdue",
                "text": f"no file in {since}d (usual cadence ~{median_gap}d)"})

        # (b) content frozen while files keep arriving
        probe = CONTENT_PROBES.get(code)
        if probe:
            try:
                content = _content_date(probe[0](db))
                if content:
                    rec["content_through"] = content.isoformat()
                    lag = (last - content).days
                    if lag > probe[1]:
                        rec["reasons"].append({
                            "kind": "stale",
                            "text": (f"content frozen at {content.isoformat()} while files "
                                     f"keep arriving ({lag}d behind; {probe[1]}d allowed)")})
            except Exception:
                pass  # a probe must never break the card

        # (c) loader rejections
        if quarantined.get(code):
            rec["reasons"].append({
                "kind": "quarantine",
                "text": f"{quarantined[code]} file(s) quarantined — check loader/format"})
        out[code] = rec
    return out


def integrity_sync(db):
    """Persist findings as open/resolved alerts — the notification center's
    store. One OPEN row per (feed, kind): still-true conditions refresh in
    place, cleared ones RESOLVE. A standing outage therefore produces exactly
    one alert for its whole lifetime, not one per day. Never raises."""
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        want = {}
        for code, state in feed_integrity(db).items():
            for reason in state["reasons"]:
                severity = "attention" if reason["kind"] in ("overdue", "stale") else "info"
                want[f"{code}:{reason['kind']}"] = (
                    severity, f"{state['feed']} ({code}): feed {reason['kind']}", reason["text"])

        open_alerts = {a.key: a for a in db.query(m.Alert)
                       .filter(m.Alert.source == "integrity", m.Alert.status == "open").all()}
        opened = resolved = 0
        for key, (severity, title, detail) in want.items():
            existing = open_alerts.pop(key, None)
            if existing is not None:                     # still true → refresh detail only
                existing.title, existing.detail, existing.updated_at = title, detail, now
            else:
                db.add(m.Alert(source="integrity", key=key, severity=severity,
                               title=title, detail=detail, status="open",
                               opened_at=now, updated_at=now))
                opened += 1
        for alert in open_alerts.values():               # condition cleared → resolve
            alert.status, alert.resolved_at, alert.updated_at = "resolved", now, now
            resolved += 1
        db.commit()
        return {"open": len(want), "opened": opened, "resolved": resolved}
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        print(f"  !! integrity sync failed: {str(e)[:300]}")
        return None
