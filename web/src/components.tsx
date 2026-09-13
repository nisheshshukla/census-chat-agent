import { useEffect, useState } from "react";
import { type Judge, type Query, type Trace, judgeTurn } from "./api";
import { renderMarkdown } from "./markdown";
import { highlightSql } from "./sqlHighlight";

export type Step = { id: string; name: string; label: string; ok?: boolean };

export type Message = {
  id: string;
  role: "user" | "assistant";
  text: string;
  status?: string;
  streaming?: boolean;
  steps?: Step[];
  queries?: Query[];
  trace?: Trace;
  errorKind?: string | null;
  groundingOk?: boolean;
  requestId?: string;
  stopped?: boolean;
  suggestions?: string[];
  judge?: Judge | null;
  turnIndex?: number;
  sessionId?: string;
};

export const STARTERS = [
  "What is the population of Cook County, IL?",
  "Which Texas counties have the most residents?",
  "What share of Florida's population is 65 or older?",
  "How many households in Oregon have no internet subscription?",
  "Compare median household income in Utah and Nevada",
  "What was the population of Springfield in 2010?",
];


function fmtMs(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

function ms(v: number): string {
  return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${v.toLocaleString()} ms`;
}

function fmtCell(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return String(v);
}

export function Mark() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden="true">
      <path d="M12 3v18M3 12h18M5.6 5.6l12.8 12.8M18.4 5.6 5.6 18.4" />
    </svg>
  );
}

function GroundingBadge({ state }: { state: "grounded" | "check" }) {
  const label = state === "grounded" ? "Grounded" : "Check figures";
  return (
    <span className="tip-wrap">
      <span className={`badge ${state === "check" ? "warn" : ""}`} tabIndex={0} aria-describedby="grounding-tip">
        {state === "grounded" ? <Tick /> : null} {label}
      </span>
      <span className="tip" role="tooltip" id="grounding-tip">
        <strong>Grounding check.</strong> After the answer is written, every figure in it is matched against the query
        results (directly, or as a sum, difference, or ratio of them).
        <dl>
          <dt>Grounded</dt>
          <dd>Every figure traced to the data.</dd>
          <dt>Check figures</dt>
          <dd>At least one figure could not be traced; a note in the answer names it.</dd>
          <dt>No badge</dt>
          <dd>No query ran (a clarifying question, a boundary explanation, or a refusal).</dd>
        </dl>
      </span>
    </span>
  );
}

function Tick({ bad }: { bad?: boolean }) {
  return (
    <span className={`tick ${bad ? "bad" : ""}`} aria-hidden="true">
      <svg viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        {bad ? <path d="M2.5 2.5l5 5M7.5 2.5l-5 5" /> : <path d="M2 5.2l2 2 4-4.4" />}
      </svg>
    </span>
  );
}

function useElapsed(active: boolean): number {
  const [sec, setSec] = useState(0);
  useEffect(() => {
    if (!active) return;
    const started = Date.now();
    setSec(0);
    const id = window.setInterval(() => setSec(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(id);
  }, [active]);
  return sec;
}

function Activity({ m }: { m: Message }) {
  const steps = m.steps ?? [];
  const elapsed = useElapsed(!!m.streaming);
  if (!m.streaming || (!m.status && steps.length === 0)) return null;
  return (
    <div className="activity" role="status" aria-live="polite">
      {steps.map((s) => (
        <div key={s.id} className="activity-row">
          {s.ok === undefined ? <span className="spinner" /> : <Tick bad={s.ok === false} />}
          <span>{s.label}</span>
        </div>
      ))}
      {m.status && (
        <div className="activity-row">
          <span className="spinner" />
          <span>
            {m.status}
            {elapsed >= 4 && <span className="muted"> · {elapsed}s</span>}
          </span>
        </div>
      )}
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="ghost small"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 1200);
        });
      }}
    >
      {done ? "Copied" : "Copy SQL"}
    </button>
  );
}

function QueryPanel({ q, index }: { q: Query; index: number }) {
  const [showData, setShowData] = useState(false);
  return (
    <div className="query">
      <div className="query-head small">
        <span className="mono">
          Query {index + 1}
          {q.purpose ? ` · ${q.purpose}` : ""}
        </span>
        <span>
          {q.row_count.toLocaleString()} row{q.row_count === 1 ? "" : "s"}
          {q.truncated ? "+" : ""} · {q.cached ? "Cached" : ms(q.elapsed_ms)}
        </span>
      </div>
      <pre className="sql">
        <code dangerouslySetInnerHTML={{ __html: highlightSql(q.sql) }} />
      </pre>
      <div className="row gap">
        <CopyButton text={q.sql} />
        {q.rows.length > 0 && (
          <button type="button" className="ghost small" onClick={() => setShowData((s) => !s)}>
            {showData ? "Hide data" : `Show data (${Math.min(q.rows.length, 20)})`}
          </button>
        )}
      </div>
      {showData && (
        <div className="tablewrap">
          <table>
            <thead>
              <tr>
                {q.columns.map((c) => (
                  <th key={c}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {q.rows.slice(0, 20).map((r, i) => (
                <tr key={i}>
                  {r.map((v, j) => (
                    <td key={j} className={typeof v === "number" ? "num" : ""}>
                      {fmtCell(v)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

const STAGE_NAMES: Record<string, string> = {
  rules: "Rules",
  preretrieval: "Pre-retrieval",
  model_loop: "Model + tools",
  grounding: "Grounding",
};

function TracePanel({ t }: { t: Trace }) {
  const total = Math.max(1, t.stages.reduce((a, s) => a + s.ms, 0));
  const tokens = t.tokens;
  return (
    <div className="trace">
      <div className="stats">
        <Stat label="Total" value={ms(t.elapsed_ms)} />
        <Stat
          label="First answer token"
          value={t.first_token_ms != null ? ms(t.first_token_ms) : "n/a"}
          hint={t.speculative ? "speculative query" : undefined}
        />
        <Stat
          label="Calls"
          value={`${t.model_calls} model · ${t.tool_calls.length} tool`}
          hint={t.model_ttft_ms?.length ? `model TTFT ${t.model_ttft_ms.map(ms).join(" / ")}` : undefined}
        />
        <Stat
          label="Tokens"
          value={`${tokens.input.toLocaleString()} in · ${tokens.output.toLocaleString()} out`}
          hint={tokens.cache_read ? `${tokens.cache_read.toLocaleString()} read from cache` : "no cache read"}
        />
      </div>

      <div className="timeline" aria-label="Stage timeline">
        <div className="bar">
          {t.stages.map((s) => (
            <span
              key={s.name}
              className={`seg-${s.name}`}
              style={{ flexGrow: Math.max(s.ms, total * 0.012) }}
              title={`${STAGE_NAMES[s.name] ?? s.name}: ${ms(s.ms)}`}
            />
          ))}
        </div>
        <div className="legend">
          {t.stages.map((s) => (
            <span key={s.name}>
              <i className={`dot seg-${s.name}`} />
              {STAGE_NAMES[s.name] ?? s.name} <b>{ms(s.ms)}</b>
            </span>
          ))}
        </div>
      </div>

      {t.tool_calls.length > 0 && (
        <table className="tools">
          <thead>
            <tr>
              <th>Tool</th>
              <th>Time</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            {t.tool_calls.map((c, i) => (
              <tr key={i}>
                <td className="mono">{c.name}</td>
                <td className="num">{ms(c.ms)}</td>
                <td className={c.error ? "bad" : ""}>{c.error ? "Error" : c.cached ? "Cached" : "OK"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <dl className="meta">
        <dt>Model</dt>
        <dd className="mono">{t.fallback_model ? `${t.fallback_model} (fallback)` : t.model}</dd>
        {t.config && (
          <>
            <dt>Generation</dt>
            <dd>
              effort <b>{String(t.config.effort)}</b> · thinking {String(t.config.thinking)} · max {Number(t.config.max_output_tokens).toLocaleString()} output tokens
            </dd>
            <dt>Budgets</dt>
            <dd>
              {String(t.config.wall_clock_s)} s wall clock · {String(t.config.max_tool_calls)} tool calls · {String(t.config.statement_timeout_s)} s per SQL statement ·{" "}
              {Number(t.config.max_result_rows).toLocaleString()} rows
            </dd>
            <dt>Checks</dt>
            <dd>
              classifier {t.config.classifier_model ? String(t.config.classifier_model) : "off"} · grounding {t.config.grounding ? "on" : "off"} · speculative{" "}
              {t.config.speculative_execution ? "on" : "off"}
              {t.config.simulation ? ` · simulation: ${String(t.config.simulation)}` : ""}
            </dd>
          </>
        )}
        <dt>Classifier</dt>
        <dd>{t.classifier ?? "n/a"}</dd>
        <dt>Field search</dt>
        <dd>{t.search_backend || "n/a"}</dd>
        {t.guardrail && (
          <>
            <dt>Guardrail</dt>
            <dd>{t.guardrail}</dd>
          </>
        )}
        {t.grounding_ungrounded.length > 0 && (
          <>
            <dt>Ungrounded</dt>
            <dd className="bad">{t.grounding_ungrounded.join(", ")}</dd>
          </>
        )}
        <dt>Request</dt>
        <dd className="mono">{t.request_id}</dd>
      </dl>
    </div>
  );
}


const AUDIT_CHECKS: { key: "faithful" | "responsive" | "caveats_ok"; label: string; hint: string }[] = [
  { key: "faithful", label: "Faithful", hint: "Every figure is supported by the rows" },
  { key: "responsive", label: "Responsive", hint: "Answers the question that was asked" },
  { key: "caveats_ok", label: "Caveats", hint: "Approximations and omissions are labeled" },
];

function AuditPanel({ m, onJudge }: { m: Message; onJudge: (j: Judge) => void }) {
  const [busy, setBusy] = useState(false);
  const j = m.judge;
  const done = !!j && j.verdict !== "error";
  const elapsed = useElapsed(busy);
  const run = async () => {
    if (!m.sessionId || m.turnIndex === undefined) return;
    setBusy(true);
    onJudge(await judgeTurn(m.sessionId, m.turnIndex, done));
    setBusy(false);
  };
  return (
    <section className="audit" aria-label="Audit">
      <div className="audit-head">
        <div>
          <div className="audit-title">Audit</div>
          <div className="audit-sub">A second model checks the answer against the retrieved rows.</div>
        </div>
        <div className="audit-actions">
          {done && (
            <span className={`badge ${j!.verdict === "pass" ? "ok" : "bad"}`}>
              <Tick bad={j!.verdict !== "pass"} /> {j!.verdict === "pass" ? "Pass" : "Fail"}
            </span>
          )}
          <button type="button" className="audit-btn" disabled={busy} onClick={run}>
            {busy ? <Spinner /> : null}
            {busy ? `Auditing ${elapsed}s` : done ? "Run again" : j ? "Retry" : "Run audit"}
          </button>
        </div>
      </div>
      <div className="audit-checks">
        {AUDIT_CHECKS.map((c) => {
          const v = done ? j![c.key] : undefined;
          const state = v === undefined ? "pending" : v ? "ok" : "bad";
          return (
            <div key={c.key} className={`check ${state}`}>
              {state === "pending" ? <span className="tick off" aria-hidden="true" /> : <Tick bad={state === "bad"} />}
              <div className="check-text">
                <div className="check-label">
                  {c.label}
                  <span className="check-state">{state === "pending" ? "Not run" : state === "ok" ? "Yes" : "No"}</span>
                </div>
                <div className="check-hint">{c.hint}</div>
              </div>
            </div>
          );
        })}
      </div>
      {done && j!.issues.length > 0 && (
        <ul className="audit-notes">
          {j!.issues.map((x, i) => (
            <li key={i}>{x}</li>
          ))}
        </ul>
      )}
      {j && j.verdict === "error" && <div className="audit-notes bad">{j.issues.join("; ") || "The audit could not run."}</div>}
      {done && j!.model && (
        <div className="audit-foot">
          Judge <span className="mono">{j!.model}</span>
          {j!.elapsed_ms !== undefined && <> · turnaround {ms(j!.elapsed_ms)}</>}
        </div>
      )}
    </section>
  );
}

function Spinner() {
  return <span className="spinner" aria-hidden="true" />;
}

export function AssistantMessage({
  m,
  onFollowUp,
  onJudge,
  busy,
  latest,
}: {
  m: Message;
  onFollowUp: (q: string) => void;
  onJudge: (id: string, j: Judge) => void;
  busy: boolean;
  latest: boolean;
}) {
  const [open, setOpen] = useState<"sql" | "details" | null>(null);
  const queries = m.queries ?? [];
  const hasQueries = queries.length > 0;
  const declined = m.errorKind === "guardrail_rejected" || m.errorKind === "rate_limited";
  const isError = !!m.errorKind && !declined;
  const grounded = hasQueries && m.groundingOk !== false && !m.errorKind;
  return (
    <div className={`msg assistant ${isError ? "error" : ""} ${declined ? "declined" : ""}`}>
      <Activity m={m} />
      {m.text && (
        <div className={`body ${m.streaming ? "typing" : ""}`} dangerouslySetInnerHTML={{ __html: renderMarkdown(m.text) }} />
      )}
      {m.stopped && <p className="muted small">Stopped.</p>}
      {!m.streaming && (hasQueries || m.trace) && (
        <div className="row gap toolbar">
          {grounded && <GroundingBadge state="grounded" />}
          {hasQueries && m.groundingOk === false && <GroundingBadge state="check" />}
          {hasQueries && (
            <button type="button" className={`ghost small ${open === "sql" ? "on" : ""}`} onClick={() => setOpen(open === "sql" ? null : "sql")}>
              {open === "sql" ? "Hide SQL" : queries.length > 1 ? `Show SQL (${queries.length})` : "Show SQL"}
            </button>
          )}
          {isError && m.requestId && <span className="muted small mono">request {m.requestId}</span>}
          {m.trace && (
            <button
              type="button"
              className={`quiet right ${open === "details" ? "on" : ""}`}
              onClick={() => setOpen(open === "details" ? null : "details")}
              title="Timings, tool calls, tokens, request id"
            >
              {open === "details" ? "Hide details" : `Details · ${fmtMs(m.trace.elapsed_ms)}`}
            </button>
          )}
        </div>
      )}
      {open === "sql" && queries.map((q, i) => <QueryPanel key={i} q={q} index={i} />)}
      {open === "details" && m.trace && (
        <>
          <TracePanel t={m.trace} />
          {hasQueries && !m.errorKind && <AuditPanel m={m} onJudge={(j) => onJudge(m.id, j)} />}
        </>
      )}
      {!m.streaming && latest && !m.errorKind && (m.suggestions?.length ?? 0) > 0 && (
        <div className="chips" aria-label="Suggested follow-ups">
          {m.suggestions!.map((f) => (
            <button key={f} type="button" className="chip" disabled={busy} onClick={() => onFollowUp(f)}>
              {f}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function UserMessage({ m }: { m: Message }) {
  return (
    <div className="msg user">
      <div className="body">{m.text}</div>
    </div>
  );
}

export function EmptyState({ onPick, busy }: { onPick: (q: string) => void; busy: boolean }) {
  return (
    <div className="empty">
      <div className="logo mark">
        <Mark />
      </div>
      <h2>Ask anything about the US population.</h2>
      <p>
        Answers come from the US Open Census dataset in Snowflake: ACS 2016–2020 and 2015–2019 five-year estimates and the
        2020 Decennial Census, down to the block group. Every answer shows the SQL it ran.
      </p>
      <div className="examples">
        <div className="label">Try one</div>
        <div className="chips">
          {STARTERS.map((s) => (
            <button key={s} type="button" className="chip" disabled={busy} onClick={() => onPick(s)}>
              {s}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}


export function Login({ onDone }: { onDone: () => void }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="login">
      <div className="logo mark">
        <Mark />
      </div>
      <h2>Census Chat Agent</h2>
      <p className="muted">Enter the reviewer password from the README to continue.</p>
      <form
        className="login-form"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!pw.trim() || busy) return;
          setBusy(true);
          setErr("");
          const { login } = await import("./api");
          const r = await login(pw);
          setBusy(false);
          if (r.ok) onDone();
          else setErr(r.message);
        }}
      >
        <input
          type="password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          placeholder="Password"
          aria-label="Password"
          autoComplete="current-password"
          autoFocus
        />
        <button type="submit" className="primary" disabled={busy || !pw.trim()}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      {err && <p className="bad small">{err}</p>}
    </div>
  );
}


export function SendIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M16 4v6a2 2 0 0 1-2 2H5" />
      <path d="M8 9l-3 3 3 3" />
    </svg>
  );
}

export function MenuIcon() {
  return (
    <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
      <path d="M3 5h14M3 10h14M3 15h14" />
    </svg>
  );
}

function relTime(t: number): string {
  const m = Math.round((Date.now() - t) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
}

export function ChatsDrawer({
  open,
  onClose,
  items,
  currentId,
  onPick,
  onForget,
  onNew,
}: {
  open: boolean;
  onClose: () => void;
  items: { id: string; title: string; updated: number }[];
  currentId: string | null;
  onPick: (id: string) => void;
  onForget: (id: string) => void;
  onNew: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  return (
    <>
      <div className={`scrim ${open ? "open" : ""}`} onClick={onClose} aria-hidden="true" />
      <aside className={`drawer ${open ? "open" : ""}`} aria-label="Chats" aria-hidden={!open}>
        <div className="drawer-head">
          <span className="drawer-title">Chats</span>
          <button type="button" className="ghost small" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <button
          type="button"
          className="primary drawer-new"
          onClick={() => {
            onNew();
            onClose();
          }}
        >
          + New chat
        </button>
        <div className="drawer-list">
          {items.length === 0 && <div className="muted small menu-empty">No chats yet.</div>}
          {items.map((c) => (
            <div key={c.id} className={`menu-item ${c.id === currentId ? "on" : ""}`}>
              <button
                type="button"
                className="menu-pick"
                onClick={() => {
                  onPick(c.id);
                  onClose();
                }}
              >
                <span className="menu-title">{c.title}</span>
                <span className="muted small">{relTime(c.updated)}</span>
              </button>
              <button type="button" className="quiet" title="Remove from list" aria-label="Remove from list" onClick={() => onForget(c.id)}>
                ×
              </button>
            </div>
          ))}
        </div>
        <p className="muted small drawer-foot">Chats stay on the server for seven days after their last message, or until the next deploy.</p>
      </aside>
    </>
  );
}
