export type Query = {
  sql: string;
  purpose: string;
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  elapsed_ms: number;
  cached: boolean;
};

export type Trace = {
  request_id: string;
  model: string;
  elapsed_ms: number;
  stages: { name: string; ms: number }[];
  tool_calls: { name: string; ms: number; cached: boolean; error: string | null; summary: string }[];
  model_calls: number;
  tokens: { input: number; output: number; cache_read: number; cache_write: number };
  search_backend: string;
  classifier: string | null;
  guardrail: string | null;
  grounding_ungrounded: string[];
  error_kind: string | null;
  fallback_model: string | null;
  first_token_ms?: number | null;
  model_ttft_ms?: number[];
  speculative?: boolean;
  config?: Record<string, unknown>;
};

export type AgentEvent =
  | { type: "session"; session_id: string; request_id: string }
  | { type: "status"; text: string }
  | { type: "token"; text: string }
  | { type: "reset" }
  | { type: "tool_call"; id: string; name: string; args: Record<string, unknown> }
  | { type: "tool_result"; id: string; name: string; ok: boolean; preview: string }
  | { type: "final"; text: string; error_kind: string | null; queries: Query[]; grounding_ok: boolean; suggestions?: string[]; turn_index?: number }
  | { type: "trace"; trace: Trace };

export type SessionTurn = {
  question: string;
  answer: string;
  queries: Query[];
  trace: Trace | Record<string, never>;
  error_kind: string | null;
  suggestions?: string[];
  judge?: Judge | null;
  turn_index?: number;
};

export type Judge = {
  verdict: "pass" | "fail" | "error";
  faithful?: boolean;
  responsive?: boolean;
  caveats_ok?: boolean;
  issues: string[];
  model?: string;
  elapsed_ms?: number;
};

export async function judgeTurn(sessionId: string, turnIndex: number, force = false): Promise<Judge> {
  const url = `/api/session/${encodeURIComponent(sessionId)}/turns/${turnIndex}/judge${force ? "?force=true" : ""}`;
  const r = await fetch(url, { method: "POST" });
  if (!r.ok) {
    const body = (await r.json().catch(() => ({}))) as { detail?: string; message?: string };
    return { verdict: "error", issues: [body.detail ?? body.message ?? `HTTP ${r.status}`] };
  }
  return ((await r.json()) as { judge: Judge }).judge;
}

const SESSION_KEY = "census-agent-session";

export function loadSessionId(): string | null {
  try {
    return localStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}

export function saveSessionId(id: string): void {
  try {
    if (!id) localStorage.removeItem(SESSION_KEY);
    else localStorage.setItem(SESSION_KEY, id);
  } catch {
  }
}

export async function fetchSession(id: string): Promise<{ session_id: string; turns: SessionTurn[] }> {
  const r = await fetch(`/api/session/${encodeURIComponent(id)}`);
  if (!r.ok) throw new Error(`session fetch failed: ${r.status}`);
  return r.json();
}

export async function resetSession(id: string): Promise<void> {
  await fetch(`/api/session/${encodeURIComponent(id)}/reset`, { method: "POST" });
}

export async function streamChat(
  message: string,
  sessionId: string | null,
  onEvent: (e: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const r = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId }),
    signal,
  });
  if (r.status === 401) {
    throw new Error("unauthorized");
  }
  if (r.status === 429) {
    const body = (await r.json().catch(() => ({}))) as { message?: string };
    onEvent({ type: "final", text: body.message ?? "Too many requests. Please wait a moment.", error_kind: "rate_limited", queries: [], grounding_ok: true });
    return;
  }
  if (!r.ok || !r.body) {
    const detail = await r.text().catch(() => "");
    throw new Error(`HTTP ${r.status}${detail ? `: ${detail.slice(0, 200)}` : ""}`);
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data: ")) {
          try {
            onEvent(JSON.parse(line.slice(6)) as AgentEvent);
          } catch {
          }
        }
      }
    }
  }
}

export async function checkAuth(): Promise<"ok" | "login" | "down"> {
  try {
    const r = await fetch("/api/me");
    if (r.status === 401) return "login";
    return r.ok ? "ok" : "down";
  } catch {
    return "down";
  }
}

export async function login(password: string): Promise<{ ok: boolean; message: string }> {
  const r = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  if (r.ok) return { ok: true, message: "" };
  const body = (await r.json().catch(() => ({}))) as { message?: string };
  return { ok: false, message: body.message ?? `Sign-in failed (${r.status})` };
}

export type Conversation = { id: string; title: string; updated: number };
const CONVOS_KEY = "census-agent-conversations";

export function loadConversations(): Conversation[] {
  try {
    const raw = localStorage.getItem(CONVOS_KEY);
    return raw ? (JSON.parse(raw) as Conversation[]) : [];
  } catch {
    return [];
  }
}

export function rememberConversation(id: string, title: string): Conversation[] {
  const list = loadConversations().filter((c) => c.id !== id);
  list.unshift({ id, title: title.slice(0, 80), updated: Date.now() });
  const trimmed = list.slice(0, 20);
  try {
    localStorage.setItem(CONVOS_KEY, JSON.stringify(trimmed));
  } catch {
  }
  return trimmed;
}

export function forgetConversation(id: string): Conversation[] {
  const list = loadConversations().filter((c) => c.id !== id);
  try {
    localStorage.setItem(CONVOS_KEY, JSON.stringify(list));
  } catch {
  }
  return list;
}
