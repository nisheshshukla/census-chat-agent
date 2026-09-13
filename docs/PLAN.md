# Plan: Census Chat Agent

## What the assignment is testing

| Hint in the brief | What it implies |
|---|---|
| "metadata and join tables that connect different categories" | Census tables have cryptic column names (`B01001e1`). A field-description table maps them to English; a FIPS table maps block-group ids to state and county names. Hard-coding a few columns fails nuanced questions. |
| "see enough of the schema without hard-coding every column" | Thousands of columns will not fit a prompt. The design is retrieval over column metadata: a semantic layer the agent searches at runtime. |
| "reasonable but unanswerable given the dataset" | Data is ACS 5-year at block-group level. No cities, ZIPs, projections, or individuals. The agent must know its boundaries and decline clearly. |
| "fast-fail path" | Off-topic and adversarial input is rejected before any query or long model loop. |
| "deployment truth" | A public URL from the first phase, not the last. |

Domain nuance: ACS values are estimates with margins of error (`e` / `m` column pairs). Counts sum across block groups; medians do not. The agent uses a weighted approximation and says so, which is the concrete form of "correct and slow over fast and wrong."

## Architecture

```
Browser (chat UI, SSE stream, progress steps, SQL and details panels)
   │
   ▼
FastAPI app (single container, public URL, in-app sign-in)
   ├── /api/chat (POST, SSE)          ── sessions: memory cache + Snowflake table
   ├── /api/health, /api/session, /api/login
   │
   ├── Guardrails (dedicated layer)
   │     1. deterministic rules (empty, length, injection, destructive SQL)
   │     2. topic classifier (Haiku), run concurrently with the first model call
   │
   ├── Pre-retrieval: field search + geography resolution + speculative query
   ├── Agent loop (Claude Opus 5, tool use, wall-clock and tool-call budgets)
   │     search_census_fields · resolve_geography · describe_table · run_census_sql
   ├── SQL validator (sqlglot): single SELECT, table allowlist, column existence, LIMIT
   ├── Output grounding: numbers in the answer trace to query results
   │
   └── Snowflake (read-only role, XS warehouse, statement timeout)
          Marketplace share + CENSUS_APP_DB.SEMANTIC (field descriptions, geography, sessions)
```

Design choices and the alternatives considered are in `REFLECTION.md`.

## Stack

- Python 3.12, FastAPI, `anthropic`, `snowflake-connector-python`, `sqlglot`, `pydantic`, `pytest`, `ruff`, `mypy`.
- Vite + React with plain CSS, built into the container.
- Claude Opus 5 for the agent loop (adaptive thinking, low effort), Haiku 4.5 for the classifier.
- Fly.io, one always-on machine, remote builds. Secrets via platform environment.
- Structured JSON logs with request and session ids; per-turn trace exposed in the UI.

## Phases and exit criteria

| Phase | Exit criteria |
|---|---|
| Scaffold | Health endpoint, Dockerfile, Fly config, Snowflake service-user script, CI. Deployed. |
| Data discovery and semantic layer | Schema snapshot committed; field search returns the right column for common concepts; geography resolver handles states, counties, ambiguity, and city names. Dataset boundaries documented. |
| Agent loop, guardrails, sessions | End-to-end answer with SQL shown; off-topic rejected without a query; medians handled with a labeled approximation; follow-ups use prior context; budgets enforced. |
| Web UI | Streamed progress and answer, SQL and details panels, chat history, error states as messages. |
| Tests and evals | Unit and integration tests without network; behavioral eval set with results committed. |
| Hardening | Sign-in, rate limits, input caps, timeouts, graceful degradation, secrets audit. |
| Docs | README with setup, demo script, and failure-mode guide; reflection. |

Deploy after every phase; the public URL is the source of truth.

## Latency budget

| Stage | Budget |
|---|---|
| First SSE event | under 200 ms |
| Classifier and pre-retrieval | concurrent with the first model call |
| Speculative query (one geography, one summable measure) | before the first model call, so the answer needs one call |
| Model call | 2 to 5 s |
| Snowflake query | 0.5 to 2 s, 20 s statement timeout |
| Hard cap per turn | 45 s; the 60 s requirement is a failure condition, not a target |

Supporting mechanisms: prompt caching on the static system prompt and tools; result caches for a static dataset; cancellation on client disconnect; a concurrency guard.

## Guardrail and edge-case matrix

| Class | Example | Expected behavior |
|---|---|---|
| Answerable | Population of Texas | Sum over the state prefix, cite table and vintage |
| Nuanced | Median household income in Cook County | Weighted approximation with an explicit caveat |
| Follow-up | "And Illinois?" | Reuses the prior measure from the context card |
| Ambiguous geography | "Springfield", "Washington County" | Ask which one, list candidates |
| Ambiguous measure | "Income in Vermont" | Answer with the default and state the assumption |
| Partial match | Population of Los Angeles | Explain the county granularity, offer the county |
| Unanswerable | Growth since 2010, projections, ZIP codes | Explain the boundary, answer what exists |
| Off-topic | "Write me a poem" | Declined before any query |
| Adversarial | Injection, destructive SQL, role-play | Rejected by rules; the validator refuses anything but a single SELECT |
| Infrastructure failure | Snowflake or model unreachable, budget exhausted | Explanatory message with a request id, no crash |

## Testing strategy

- Unit tests for the deterministic parts: validator, resolver, concept map, rules, grounding arithmetic, sessions, cache.
- Integration tests with fake model and database clients for the orchestration: event contract, guardrails, degradation, budgets, auth, persistence.
- Behavioral evals, live, as a YAML set with expected behaviors and hand-verified numbers; results committed.
- LLM-as-judge over every answered item: a second model checks faithfulness to the retrieved rows, responsiveness, and labeled caveats. The same judge is available in the app as an on-demand audit of any answer.
- Smoke test against the deployed URL.

## Out of scope

- Cortex Analyst integration (discussed as the Snowflake-native production path).
- Charts and maps.
- Multi-user accounts; a shared password gates the demo.
- Shared caches across machines (Redis); sessions already persist in Snowflake.
