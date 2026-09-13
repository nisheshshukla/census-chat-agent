# Build checklist

## Scaffold
- [x] FastAPI app with `/api/health` (version, commit SHA, Snowflake ping), JSON logging, request ids
- [x] Error taxonomy with fixed user-facing messages
- [x] Dockerfile (Node build stage, Python runtime, non-root), `fly.toml` with one always-on machine
- [x] `scripts/gen_keypair.sh` and `scripts/snowflake_setup.sql` (read-only role, XS warehouse with statement timeout and credit cap, app database, key-pair service user)
- [x] CI: ruff, mypy, pytest with a coverage gate

## Data discovery and semantic layer
- [x] `scripts/introspect.py`: schema snapshot of the share (73 tables, 17,052 columns)
- [x] `scripts/build_semantic_layer.py`: `STATES`, `FIELD_DESCRIPTIONS`, `GEO_CBG` in `CENSUS_APP_DB.SEMANTIC`; local snapshots for tests
- [x] Column catalog with estimate-to-MOE pairing
- [x] Field search: curated concept map plus BM25 over descriptions; Cortex Search backend behind a flag (unavailable on trial accounts)
- [x] Geography resolver: states, counties, ambiguity lists, city names mapped to counties, question scanning
- [x] `data/schema/boundaries.md`

## Agent loop, guardrails, sessions
- [x] Deterministic input rules
- [x] Haiku classifier, raced with the first model call, checked before any tool runs
- [x] SQL validator: single SELECT, allowlist, column existence with case hints, LIMIT, no `SELECT *` on wide tables
- [x] Output grounding across the current and previous two turns
- [x] Tools: search, describe, resolve, run SQL (validated, cached)
- [x] Streaming loop with wall-clock and tool-call budgets, bounded self-correction, refusal handling
- [x] Speculative query for single-geography, single-measure questions
- [x] Sessions: context card, rolling window, persisted to Snowflake after each turn
- [x] Model-written follow-up suggestions
- [x] `/simulate` commands for each failure path

## Web UI
- [x] Streamed answer with progress steps, stop button, auto-growing composer
- [x] Formatted and highlighted SQL panel; details panel with timings, first-token latency, budgets, configuration
- [x] Grounding badge with an explanatory tooltip
- [x] Chats drawer, new chat, theme control (auto, light, dark)
- [x] Sign-in screen with a signed session cookie

## Tests and evals
- [x] Unit tests (validator, resolver, concepts, search, rules, grounding, sessions, cache, tools, classifier, Snowflake client, CLI)
- [x] Integration tests with fake model and database clients
- [x] 40-item behavioral eval set; `scripts/run_evals.py` in-process or remote; results committed
- [x] LLM-as-judge pass (`--judge`) and an on-demand Audit in the app's Details panel, verdicts stored with the turn
- [x] `scripts/smoke_test.sh` against the deployed URL

## Hardening
- [x] Sign-in, rate limits, input caps, timeouts everywhere, graceful degradation messages
- [x] Cache headers; secrets audit

## Docs
- [x] README: setup from scratch, demo script, architecture, failure-mode guide with screenshots
- [x] REFLECTION.md
