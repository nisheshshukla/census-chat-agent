import { useCallback, useEffect, useRef, useState } from "react";
import {
  type AgentEvent,
  type Conversation,
  type Judge,
  type Trace,
  checkAuth,
  fetchSession,
  forgetConversation,
  loadConversations,
  loadSessionId,
  rememberConversation,
  saveSessionId,
  streamChat,
} from "./api";
import { AssistantMessage, ChatsDrawer, EmptyState, Login, Mark, MenuIcon, type Message, SendIcon, UserMessage } from "./components";

let counter = 0;
const nextId = () => `m${Date.now().toString(36)}${(counter++).toString(36)}`;

const STEP_LABELS: Record<string, (a: Record<string, unknown>) => string> = {
  run_census_sql: (a) => `Query${a.purpose ? `: ${String(a.purpose).slice(0, 70)}` : ""}`,
  search_census_fields: (a) => `Searching fields for “${String(a.query ?? "").slice(0, 50)}”`,
  describe_table: (a) => `Reading table ${String(a.table_name ?? "")}`,
  resolve_geography: (a) => `Resolving “${String(a.name ?? "").slice(0, 50)}”`,
};

type Theme = "auto" | "light" | "dark";
const THEME_KEY = "census-agent-theme";

function loadTheme(): Theme {
  try {
    const t = localStorage.getItem(THEME_KEY);
    return t === "light" || t === "dark" ? t : "auto";
  } catch {
    return "auto";
  }
}

function applyTheme(t: Theme): void {
  const root = document.documentElement;
  if (t === "auto") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", t);
  try {
    localStorage.setItem(THEME_KEY, t);
  } catch {
  }
}

function useNarrow(): boolean {
  const [narrow, setNarrow] = useState(() => window.matchMedia("(max-width: 600px)").matches);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 600px)");
    const on = () => setNarrow(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return narrow;
}

export default function App() {
  const narrow = useNarrow();
  const [theme, setTheme] = useState<Theme>(() => loadTheme());
  useEffect(() => applyTheme(theme), [theme]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(() => loadSessionId());
  const [restoring, setRestoring] = useState(true);
  const [auth, setAuth] = useState<"checking" | "ok" | "login">("checking");
  const [convos, setConvos] = useState<Conversation[]>(() => loadConversations());
  const [drawer, setDrawer] = useState(false);

  useEffect(() => {
    checkAuth().then((a) => setAuth(a === "login" ? "login" : "ok"));
  }, []);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const stickToBottom = useRef(true);

  const loadConversation = useCallback(async (id: string): Promise<boolean> => {
    try {
      const s = await fetchSession(id);
      const restored: Message[] = [];
      for (const t of s.turns) {
        restored.push({ id: nextId(), role: "user", text: t.question });
        restored.push({
          id: nextId(),
          role: "assistant",
          text: t.answer,
          queries: t.queries,
          trace: "request_id" in t.trace ? (t.trace as Trace) : undefined,
          errorKind: t.error_kind,
          groundingOk: "grounding_ungrounded" in t.trace ? (t.trace as Trace).grounding_ungrounded.length === 0 : undefined,
          requestId: "request_id" in t.trace ? (t.trace as Trace).request_id : undefined,
          suggestions: t.suggestions ?? [],
          judge: t.judge ?? null,
          turnIndex: t.turn_index,
          sessionId: id,
        });
      }
      setMessages(restored);
      return s.turns.length > 0;
    } catch {
      return false;
    }
  }, []);

  useEffect(() => {
    if (auth !== "ok") return;
    const id = loadSessionId();
    if (!id) {
      setRestoring(false);
      return;
    }
    loadConversation(id).finally(() => setRestoring(false));
  }, [auth, loadConversation]);

  useEffect(() => {
    const el = listRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const onScroll = useCallback(() => {
    const el = listRef.current;
    if (!el) return;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }, []);

  const update = useCallback((id: string, fn: (m: Message) => Message) => {
    setMessages((ms) => ms.map((m) => (m.id === id ? fn(m) : m)));
  }, []);

  const autosize = useCallback(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, []);

  const send = useCallback(
    async (text: string) => {
      const q = text.trim();
      if (!q || busy) return;
      setInput("");
      requestAnimationFrame(autosize);
      setBusy(true);
      stickToBottom.current = true;
      const userId = nextId();
      const asstId = nextId();
      setMessages((ms) => [
        ...ms,
        { id: userId, role: "user", text: q },
        { id: asstId, role: "assistant", text: "", status: "Sending", streaming: true, steps: [] },
      ]);
      const firstQuestion = messages.find((m) => m.role === "user")?.text;
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      try {
        await streamChat(
          q,
          sessionId,
          (e: AgentEvent) => {
            switch (e.type) {
              case "session":
                if (e.session_id !== sessionId) {
                  setSessionId(e.session_id);
                  saveSessionId(e.session_id);
                }
                setConvos(rememberConversation(e.session_id, firstQuestion ?? q));
                update(asstId, (m) => ({ ...m, requestId: e.request_id, sessionId: e.session_id }));
                break;
              case "status":
                update(asstId, (m) => ({ ...m, status: e.text }));
                break;
              case "token":
                update(asstId, (m) => ({ ...m, text: m.text + e.text, status: undefined }));
                break;
              case "reset":
                update(asstId, (m) => ({ ...m, text: "" }));
                break;
              case "tool_call": {
                const label = (STEP_LABELS[e.name] ?? (() => e.name))(e.args);
                update(asstId, (m) => ({ ...m, status: undefined, steps: [...(m.steps ?? []), { id: e.id, name: e.name, label }] }));
                break;
              }
              case "tool_result":
                update(asstId, (m) => ({
                  ...m,
                  steps: (m.steps ?? []).map((s) => (s.id === e.id ? { ...s, ok: e.ok } : s)),
                }));
                break;
              case "final":
                update(asstId, (m) => ({
                  ...m,
                  text: e.text,
                  queries: e.queries,
                  errorKind: e.error_kind,
                  groundingOk: e.grounding_ok,
                  suggestions: e.suggestions ?? [],
                  turnIndex: e.turn_index,
                  status: undefined,
                }));
                break;
              case "trace":
                update(asstId, (m) => ({ ...m, trace: e.trace, streaming: false }));
                break;
            }
          },
          ctrl.signal,
        );
        update(asstId, (m) => (m.streaming ? { ...m, streaming: false, status: undefined } : m));
      } catch (err) {
        if (err instanceof Error && err.message === "unauthorized") {
          setMessages((ms) => ms.filter((m) => m.id !== userId && m.id !== asstId));
          setAuth("login");
          return;
        }
        const aborted = err instanceof DOMException && err.name === "AbortError";
        const msg = err instanceof Error ? err.message : String(err);
        update(asstId, (m) => ({
          ...m,
          streaming: false,
          status: undefined,
          stopped: aborted,
          errorKind: aborted ? m.errorKind : "internal",
          text: aborted ? m.text : m.text || `I couldn't reach the server (${msg}). Please try again.`,
        }));
      } finally {
        setBusy(false);
        abortRef.current = null;
        inputRef.current?.focus();
      }
    },
    [busy, sessionId, update, autosize, messages],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  const newConversation = useCallback(() => {
    abortRef.current?.abort();
    setSessionId(null);
    saveSessionId("");
    setMessages([]);
    setBusy(false);
    inputRef.current?.focus();
  }, []);

  const pickConversation = useCallback(
    async (id: string) => {
      abortRef.current?.abort();
      setBusy(false);
      const ok = await loadConversation(id);
      if (ok) {
        setSessionId(id);
        saveSessionId(id);
      } else {
        setConvos(forgetConversation(id));
        setMessages([]);
      }
    },
    [loadConversation],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && abortRef.current) stop();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [stop]);

  if (auth === "login") {
    return (
      <main className="shell">
        <div className="column">
          <Login onDone={() => setAuth("ok")} />
        </div>
      </main>
    );
  }

  return (
    <main className="shell">
      <ChatsDrawer
        open={drawer}
        onClose={() => setDrawer(false)}
        items={convos}
        currentId={sessionId}
        onPick={pickConversation}
        onForget={(id) => setConvos(forgetConversation(id))}
        onNew={newConversation}
      />
      <header className="topbar">
        <div className="column">
          <div className="brand">
            <button type="button" className="ghost icon" onClick={() => setDrawer(true)} aria-label="Open chats" title="Chats">
              <MenuIcon />
            </button>
            <span className="logo" aria-hidden="true">
              <Mark />
            </span>
            <div>
              <h1>Census Chat Agent</h1>
              <span className="muted small">US Open Census on Snowflake · every answer shows its SQL</span>
            </div>
          </div>
          <div className="header-actions">
            <div className="seg" role="group" aria-label="Theme">
              {(["auto", "light", "dark"] as Theme[]).map((t) => (
                <button key={t} type="button" className={theme === t ? "on" : ""} onClick={() => setTheme(t)} aria-pressed={theme === t}>
                  {t === "auto" ? "Auto" : t === "light" ? "Light" : "Dark"}
                </button>
              ))}
            </div>
            <button type="button" className="primary small" onClick={newConversation} disabled={messages.length === 0 && !busy}>
              + New chat
            </button>
          </div>
        </div>
      </header>

      <div className="list" ref={listRef} onScroll={onScroll}>
        <div className="column">
          {!restoring && messages.length === 0 && <EmptyState onPick={send} busy={busy} />}
          {messages.map((m) =>
            m.role === "user" ? (
              <UserMessage key={m.id} m={m} />
            ) : (
              <AssistantMessage
                key={m.id}
                m={m}
                onFollowUp={send}
                onJudge={(id, j: Judge) => update(id, (x) => ({ ...x, judge: j }))}
                busy={busy}
                latest={m.id === messages[messages.length - 1]?.id}
              />
            ),
          )}
        </div>
      </div>

      <div className="composer-wrap">
        <div className="column">
          <form
            className="composer"
            onSubmit={(e) => {
              e.preventDefault();
              send(input);
            }}
          >
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                autosize();
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send(input);
                }
              }}
              placeholder={narrow ? "Ask about the US population…" : "Ask about population, income, housing, commuting… for a state or county"}
              rows={1}
              maxLength={2000}
              aria-label="Your question"
              autoFocus
            />
            {busy ? (
              <button type="button" className="stop" onClick={stop} aria-label="Stop generating">
                Stop
              </button>
            ) : (
              <button type="submit" className="primary send" disabled={!input.trim()} aria-label="Send" title="Send (Enter)">
                <SendIcon />
              </button>
            )}
          </form>
          <p className="hint">Enter to send · Shift+Enter for a new line · Esc to stop</p>
        </div>
      </div>
    </main>
  );
}
