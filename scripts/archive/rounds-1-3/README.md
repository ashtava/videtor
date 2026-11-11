# Rounds 1-3 (2026-10-28/29), archived 2026-11-11

The rule-based planner that came before the shot map: `rules.py` (pick, cut and order shots by an edit type's
pace), `structure.py` (the hook and opening templates), `rerank.py` (a learned reranker on per-clip analysis) and
their tests. The commands `videtor plan`, `export`, `ingest`, `kb rerank-eval` and `kb benchmark` went with them.
Nothing in `src/` imports these; `plan/benchmark.score_picks` (a cut against a real edit) stayed and is run by
`videtor eval`. The files import `videtor.plan.rules` and so on, so they need moving back to run.
