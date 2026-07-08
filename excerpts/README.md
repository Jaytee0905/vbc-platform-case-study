# Sanitized excerpts

Two subsystems adapted from the production codebase for review. Identifiers,
feed codes, and payer-specific details are genericized; the logic, guards, and
design comments are faithful to what runs in production. These files are
excerpts — they reference a surrounding application (ORM models, an ingestion
pipeline) that is not included, so they illustrate rather than execute.

| File | What it is |
|---|---|
| [`cadence_monitor.py`](cadence_monitor.py) | The data-integrity cadence monitor: learns each feed's delivery rhythm from its own arrival history, probes content freshness, and maintains a non-spamming open/resolve alert lifecycle. Detects the nastiest real-world failure we've seen: a payer feed that keeps delivering files on schedule while the *content* inside silently stops advancing. |
| [`model_review_loop.py`](model_review_loop.py) | The model-review agent: after every claims/fund data load, re-scores every stored cost projection against actuals, hunts bias cohorts, and files versioned tweak proposals for human approval. Its first production-shaped run caught a sparse-data cohort running ~78% over-projected. |

Design principles visible in both: never raise inside an ingestion job (guarded
hooks), never spam (idempotent alert lifecycles, skip-guards), never self-apply
a model change (human-approved version bumps), and honest math (a "prospective"
accuracy slice that only counts months the model hadn't seen yet).
