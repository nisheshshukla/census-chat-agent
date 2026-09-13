# Reflection

Written after the eval run, before submission. This is the honest assessment.

## Development process

I used AI throughout, deliberately. I designed with it before writing a single line of code: a plan, a set of decision records, and a task checklist existed before any application code, and the plan was revised twice on purpose. Then it assisted in the implementation while I directed the architecture, made the tradeoff calls recorded below, reviewed the data-semantics rules and the system prompt line by line, and drove the deployed app myself. Once when the constraint became clear that an AI agent would do the building with me directing, which collapsed a 16-hour human schedule into a few hours of agent time gated on deploys. And once when I asked what a production system would need beyond features, and the answer was disciplines: a published latency budget, deterministic checks in front of the model, a structured context card instead of "the transcript", an error taxonomy, and evals as a committed artifact.

The dataset drove the architecture more than the model did. Introspection found 73 tables and 17,052 columns across two ACS vintages plus the 2020 Decennial redistricting file, mixed-case column names like `B01003e1` that must be double-quoted, and a metadata table that describes every column as a hierarchy path. The assignment's hint about "metadata and join tables" pointed straight at this.

## Key decisions, as tradeoffs

Each decision had at least one credible alternative. The table is the summary; the bullets under each heading say what was considered, what was given up, and why the trade was right for a 24-hour deliverable.

| Decision | Chose | Over |
|---|---|---|
| Orchestration | a Claude tool-use loop | Cortex Analyst or Cortex Agents |
| Retrieval | a curated concept map in front of full-text search, with Cortex Search behind a flag | Cortex Search as the only backend |
| Guardrails | three checkpoints | a prompt paragraph or a single LLM judge |
| Data semantics | correct-and-labeled | fast-and-plausible |
| Latency | speculative execution and measured first-token time | a faster model |
| Conversation state | a structured context card plus a rolling window | the raw transcript |
| Deployment | Fly.io with one always-on machine | Snowpark Container Services or a scale-to-zero platform |
| Authentication | an in-app sign-in with a signed cookie | browser basic auth |

### Orchestration: a Claude tool-use loop over Cortex Analyst or Cortex Agents

- **Considered:** Cortex Analyst with a hand-authored semantic model; Cortex Agents; a single text-to-SQL prompt with the schema inlined.
- **Gave up:** the Snowflake-native story where nothing leaves the platform, and the semantic-model YAML a Snowflake customer would eventually want.
- **Why:** a curated semantic model for 8,000+ columns was not feasible in the window and would have hidden the hard part, which is finding the right column and using it correctly. The tool-use loop gave me control over budgets, streaming, and three separately testable guardrail checkpoints. The semantic tables I built are exactly what would seed a Cortex semantic model later, so the trade is reversible.

### Retrieval: a curated concept map in front of full-text search, with Cortex Search behind a flag

- **Considered:** Cortex Search as the only backend; embeddings I host myself; a hand-curated column list for the top thirty questions.
- **Gave up:** semantic recall on paraphrases ("people who bike to work") that keyword search misses; and, after the fact, Cortex Search itself, because trial accounts cannot run its embedding function.
- **Why:** the curated map guarantees the thirty-five concepts that start most questions resolve to the canonical column and is verified by a test; BM25 over the descriptions covers the long tail deterministically; and the fallback interface meant discovering the Cortex limitation at build time cost one config default rather than a redesign. The procedural lesson is that a five-minute capability probe should precede designing around a platform feature.

### Guardrails: three checkpoints rather than a prompt paragraph

- **Considered:** instructions in the system prompt only; one LLM judge on the output.
- **Gave up:** simplicity, and a little latency for the classifier call.
- **Why:** a data agent fails in three distinct ways (answering what it shouldn't, running what it shouldn't, stating what the data didn't say), and each is cheaper to catch at its own layer: deterministic rules for injection and destructive SQL in under a millisecond, a Haiku classifier for topic, a sqlglot validator with column-existence checks so a hallucinated name fails locally with a precise message, and a numeric grounding check on the answer. The classifier's cost was then hidden by running it concurrently with the first model call and checking its verdict before any tool executes.

### Data semantics: correct-and-labeled over fast-and-plausible

- **Considered:** letting the model sum whatever it finds; refusing all median questions.
- **Gave up:** a clean single number for county and state medians.
- **Why:** block-group medians do not sum. The agent computes a household- or population-weighted average of block-group medians and says so every time, percentages are ratios of sums, and margins of error combine by root-sum-of-squares, so the agent can volunteer that two counties are statistically indistinguishable. This is the decision I would defend hardest: the assignment explicitly warns about "fast and wrong."

### Latency: speculative execution and measured first-token time over a faster model

- **Considered:** Sonnet for the planning call; a template answer for simple questions with no model; accepting the two-call shape.
- **Gave up:** one extra Snowflake query on the critical path for the simplest question shape, and some prompt-cache economy.
- **Why:** the first answer token could only arrive after a planning call, a query, and an answer call. Running the obvious query before the model is called, when pre-retrieval already knows the one geography and the one summable measure, lets the model answer in a single call: 2.7 s to first token instead of about 6 s for that shape, visible to the user as the query behind the answer and counted as grounding evidence. Every answer's Details panel reports first-token time and per-call model latency, because a latency budget you cannot see is not a budget.

### Conversation state: a structured context card plus a rolling window over the raw transcript

- **Considered:** sending the full transcript; server-side compaction only.
- **Gave up:** nothing the reviewers can see; some implementation effort.
- **Why:** "and Texas?" should resolve by substitution against known state, not by hoping the model re-reads history. The card is unit-testable; the transcript window is capped by size and older turns are summarized. Sessions are cached in memory and persisted to a table in the app's Snowflake database after every turn, written after the response is sent so persistence costs the user nothing and can never fail a turn; a session missing from memory is loaded on demand, so deploys keep conversations. I started with memory only and added persistence when a reviewer asked whether conversations would outlive a session; the honest answer was no, and the fix was an hour.

### Deployment: Fly.io with one always-on machine over Snowpark Container Services or a scale-to-zero platform

- **Considered:** SPCS (the Snowflake-native answer); Cloud Run or Render (cold starts).
- **Gave up:** the "credentials never leave Snowflake" story.
- **Why:** the container is 12-factor and moves unchanged; what the demo needed was a reliable public URL with no cold start against a 60-second limit, deployable from a CLI. SPCS is the production migration and is named as such.

### Authentication: an in-app sign-in with a signed cookie over browser basic auth

- **Considered:** basic auth only (my first version); no auth.
- **Gave up:** an afternoon.
- **Why:** a reviewer could not get past the browser's auth dialog even though the credentials verified against the API. Browser dialogs are fragile in ways I cannot debug remotely; a sign-in screen is not. Basic auth stayed for scripts.

**Two things the eval run taught me that I would not have guessed.** First, the SQL result cache got zero hits on the in-process second pass of identical questions (the deployed run later reached 12 of 15, because repeat questions within one process do hit). The model writes semantically identical but textually different SQL each time (alias names, column order), so an exact-text cache key is nearly useless for a text-to-SQL agent. The right key is semantic (tables, columns, filters, aggregation) after canonicalizing the AST, which sqlglot can do; I left the exact-match cache in place because it still helps repeated follow-ups within a session, and I am saying plainly that it did not deliver what the design record promised. Second, the grounding check flagged "$10,900 higher" in a Utah-versus-Nevada comparison because it only derived differences within a row, not across rows. That was a correct answer flagged as suspect, which is the wrong direction for a trust signal; cross-row differences and ratios are now derived, with a test.

**What screenshots caught that evals could not.** Driving the UI end to end for the README found three real bugs in one pass: the markdown renderer silently dropped any line starting with an asterisk, which happened to be the grounding caveat; header buttons wrapped once the chat count grew; and failure simulations ran the speculative query first, so a "Snowflake down" card showed a successful query. Behavioral evals score the API; nothing but looking scores the product.

## What I would improve or do differently with more time

1. **A stronger judge.** The eval runner now includes an LLM-as-judge pass: a second model reads the question, the rows the agent retrieved, and the answer, and rules on faithfulness, responsiveness, and whether approximations are labeled. It closes the gap the numeric grounding check leaves (a wrong comparative word with correct numbers). What it cannot do is judge truth beyond the retrieved rows, because it sees only what the agent saw; the next step is a judge with independent access to the data, and a small human-labeled set to calibrate the judge itself.
2. **Cortex Search on a paid account, with a recall comparison.** The code is there; the evidence is not. I want the side-by-side against BM25 on paraphrased questions ("people who bike to work", "homes worth over 500k").
3. **Precomputed rollups for the top measures at state and county level.** Every question today sums 240k rows. An XS warehouse does it in about a second, so it did not matter for this demo, but a materialized `STATE_COUNTY_ROLLUP` would make most answers sub-second and make the median approximation a lookup.
4. **Redis for the SQL cache and rate limits**, so the app can run more than one machine; sessions already live in Snowflake. Everything that needs it is behind an interface; nothing is wired.
5. **A clarification memory.** When the agent asks "which Cook County?" and the user says "Illinois", it works. If the user instead asks something unrelated, the pending question is lost. A small pending-clarification slot on the context card would fix it.
6. **Snowpark Container Services as the deployment target** for a Snowflake customer, so credentials never leave the platform. The container is 12-factor and would move unchanged.

## Edge cases and failure modes identified but not fully addressed

- **Comparative claims with correct numbers.** Grounding catches invented numbers, not wrong words.
- **Block-group boundary changes between vintages.** The prompt restricts change-over-time to state and county level, but nothing stops the model from writing a CBG-level join across vintages if a user insists. The validator does not know about vintages.
- **Puerto Rico in national totals.** The share includes PR (FIPS 72); "US population" sums include it, and the answer does not always say so. The Decennial figure of 334.7M versus the commonly quoted 331.4M is exactly this. A note in the prompt would fix most cases; a flag on GEO_CBG would fix all of them.
- **Ambiguous measure defaults.** "Income in Vermont" is answered with median household income and a stated assumption. Whether that is the right default is a product decision I made, not a fact.
- **Classifier false positives.** A greeting is routed to a capabilities message rather than the model, which is right; a terse "Texas" alone after an unrelated turn could be misread. The classifier fails open on timeout, so the worst case is a slower answer, not a wrong refusal.
- **Very wide results.** More than 60 rows are truncated for the model with a note to aggregate; the UI shows the first 20. A user asking for "all 3,222 counties" gets a partial table and an explanation, not a complete export.
- **Concurrency.** Eight concurrent turns are allowed per process; the ninth gets a busy message. Eleven reviewers at once would occasionally see it. A queue with position feedback would be better than a refusal.

## Testing approach and what I would add

Three levels, each chosen for what it can prove:

Coverage is 92% of the Python package with 138 tests, and CI fails below 80%. The uncovered remainder is mostly defensive branches in the Snowflake connector wrapper and the agent loop's rarer exits.

- **Unit tests (deterministic, no network)** for the parts where a wrong answer is a bug, not a judgment: the SQL validator's allow/deny table with precise error messages, the geography resolver's ambiguity handling, the curated concept map's columns actually existing in the catalog (a stale entry fails CI, not a demo), rules, grounding arithmetic, session windowing, the cache.
- **Integration tests with fake model and database clients** for the orchestration: event sequence over SSE, tool call then result then answer, session carry-over into the next prompt, classifier fast-fail with no SQL executed, rule rejection with no model call, Snowflake-down degradation with no leaked detail, invalid SQL fed back and corrected, tool-budget enforcement, grounding caveat, model refusal, auth, rate limits. These run in about a second and need no secrets.
- **Behavioral evals, live**, as a YAML set with expected behavior classes and hand-verified numbers, plus an LLM-as-judge pass over every answered item that scores faithfulness to the retrieved rows, responsiveness, and labeled caveats. The same judge is exposed in the app as an on-demand audit of any answer, with the verdict stored on the conversation, so a reviewer can challenge a specific answer rather than trust an aggregate. Results are committed including failures. This is the only level that tests the model's judgment, and it is the one I would grow first.

What I would add: a judge with independent data access and a human-labeled calibration set; a fixture-replay mode for the evals so CI can run them against recorded model outputs and catch regressions in the orchestration around the prompt; property tests for the validator (any generated SELECT over the allowlist validates, any DML is rejected); and a load test at the concurrency limit to see the busy path under real timing.

## Where I invested and what I left out

Invested: the semantic layer and retrieval, the validator, the data-semantics rules, and the eval set. Those are what make the answers trustworthy.

Left out deliberately: an embedded/supplemental variant inside Snowsight or Streamlit-in-Snowflake (the assignment asks for a public web interface; the API is the reusable part), charts and maps (the numbers are the product), persistent storage (one machine, eleven reviewers, one day), multi-user accounts (a shared password is enough for a review), and Cortex Analyst integration (discussed, not built). Each is one sentence in the decision records rather than half a day of work.
