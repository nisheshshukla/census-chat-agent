
const KEYWORDS = new Set(
  (
    "select from where join inner left right full outer cross on as group by order having limit offset " +
    "and or not in is null like ilike between exists case when then else end with distinct union all " +
    "desc asc over partition qualify using cast interval true false top values"
  ).split(" "),
);

const TOKEN = new RegExp(
  [
    String.raw`(--[^\n]*)`,
    String.raw`('(?:[^'\\]|\\.)*')`,
    String.raw`("(?:[^"\\]|\\.)*")`,
    String.raw`(\b\d+(?:\.\d+)?\b)`,
    String.raw`([A-Za-z_][A-Za-z0-9_$]*)(?=\s*\()`,
    String.raw`([A-Za-z_][A-Za-z0-9_$]*)`,
    String.raw`([<>=!]+|[-+*/%.,();])`,
  ].join("|"),
  "g",
);

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function highlightSql(sql: string): string {
  let out = "";
  let last = 0;
  for (const m of sql.matchAll(TOKEN)) {
    const i = m.index ?? 0;
    out += esc(sql.slice(last, i));
    const [tok, cm, str, qid, num, fn, word, op] = m;
    if (cm) out += `<span class="sql-cm">${esc(cm)}</span>`;
    else if (str) out += `<span class="sql-str">${esc(str)}</span>`;
    else if (qid) out += `<span class="sql-id">${esc(qid)}</span>`;
    else if (num) out += `<span class="sql-num">${esc(num)}</span>`;
    else if (fn) out += KEYWORDS.has(fn.toLowerCase()) ? `<span class="sql-kw">${esc(fn)}</span>` : `<span class="sql-fn">${esc(fn)}</span>`;
    else if (word) out += KEYWORDS.has(word.toLowerCase()) ? `<span class="sql-kw">${esc(word)}</span>` : esc(word);
    else if (op) out += `<span class="sql-op">${esc(op)}</span>`;
    else out += esc(tok);
    last = i + tok.length;
  }
  return out + esc(sql.slice(last));
}
