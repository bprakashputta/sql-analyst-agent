// sql-analyst-agent UI. Vanilla JS, no build step. Every value from the API or the database is
// inserted with textContent / createTextNode, never innerHTML (column names and cell values are
// untrusted data).
"use strict";

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
const SERIES = ["var(--s1)", "var(--s2)", "var(--s3)"];

const EXAMPLES = {
  chinook: [
    "Which five countries bring in the most revenue, and how much each?",
    "Monthly revenue in 2023",
    "Who are our best customers?",
    "Average invoice total by sales support agent",
  ],
  generic: ["How many rows are in each table?"],
};

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v);
  }
  for (const k of kids) n.append(k);
  return n;
}
function svg(tag, attrs = {}) {
  const n = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}
const isNum = (v) => typeof v === "number" && Number.isFinite(v);

function fmt(v, opts = {}) {
  if (v === null || v === undefined) return "NULL";
  if (!isNum(v)) return String(v);
  const a = Math.abs(v);
  if (opts.compact && a >= 10000) {
    return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(v);
  }
  if (Number.isInteger(v)) return v.toLocaleString("en-US");
  const digits = a >= 100 ? 2 : a >= 1 ? 2 : 4;
  return v.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function niceTicks(max, count = 4) {
  if (max <= 0) return [0];
  const raw = max / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const ticks = [];
  for (let t = 0; t <= max + step * 0.001; t += step) ticks.push(+t.toFixed(10));
  if (ticks[ticks.length - 1] < max) ticks.push(+(ticks[ticks.length - 1] + step).toFixed(10));
  return ticks;
}

// ---------------------------------------------------------------- tooltip
const tip = $("tip");
function showTip(evt, title, rows) {
  tip.replaceChildren(el("div", { class: "t", text: title }));
  rows.forEach(([name, value, color]) => {
    const key = el("i");
    key.style.background = color;
    tip.append(el("div", { class: "r" }, key, el("b", { text: value }), el("span", { text: name })));
  });
  tip.hidden = false;
  const box = $("chart-block").getBoundingClientRect();
  const x = evt.clientX - box.left, y = evt.clientY - box.top;
  const w = tip.offsetWidth;
  tip.style.left = Math.min(Math.max(0, x + 14), box.width - w) + "px";
  tip.style.top = y + 16 + "px";
}
function hideTip() { tip.hidden = true; }

// ---------------------------------------------------------------- charts
function renderChart(spec, columns, rows) {
  const block = $("chart-block"), host = $("chart"), legend = $("legend");
  host.replaceChildren(); legend.replaceChildren(); hideTip();
  const noChart = !spec || spec.type === "none" || !rows.length;
  document.querySelector(".cells").classList.toggle("no-chart", noChart);
  if (noChart) { block.hidden = true; return; }
  block.hidden = false;
  $("chart-why").textContent = spec.reason;
  const xi = spec.x ? columns.indexOf(spec.x) : -1;
  const yi = spec.y.map((c) => columns.indexOf(c));
  $("chart-title").textContent = spec.type === "number" ? "Value" :
    spec.y.join(", ") + (spec.x ? " by " + spec.x : "");
  if (spec.y.length > 1) {
    spec.y.forEach((name, i) => {
      const key = el("i", { class: spec.type === "line" ? "line" : "" });
      key.style.background = SERIES[i];
      legend.append(el("span", {}, key, document.createTextNode(name)));
    });
  }
  if (spec.type === "number") return statTile(host, spec.y[0], rows[0][0]);
  if (spec.type === "bar") return barChart(host, spec, rows, xi, yi[0]);
  if (spec.type === "grouped_bar") return groupedBar(host, spec, rows, xi, yi);
  if (spec.type === "line") return lineChart(host, spec, rows, xi, yi);
  if (spec.type === "scatter") return scatter(host, spec, rows, xi, yi[0]);
}

function statTile(host, label, value) {
  host.append(el("div", { class: "stat" }, el("div", { class: "v", text: fmt(value) }),
    el("div", { class: "k", text: label })));
}

function barChart(host, spec, rows, xi, yi) {
  let data = rows.map((r) => ({ label: String(r[xi] ?? "NULL"), v: isNum(r[yi]) ? r[yi] : 0 }));
  if (spec.sort === "value_desc") data.sort((a, b) => b.v - a.v);
  const W = Math.max(host.clientWidth, 300), band = 30, barH = Math.min(18, band - 8);
  const labelW = Math.min(170, W * 0.36, Math.max(70, Math.max(...data.map((d) => d.label.length)) * 7));
  const maxChars = Math.floor((labelW - 12) / 6.6);
  const right = 64, top = 6, H = top + data.length * band + 24;
  const max = Math.max(...data.map((d) => d.v), 0);
  const ticks = niceTicks(max, Math.max(2, Math.min(4, Math.floor((W - labelW - right) / 64))));
  const x = (v) => labelW + (v / ticks[ticks.length - 1]) * (W - labelW - right);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${spec.y[0]} by ${spec.x}` });
  ticks.forEach((t) => {
    s.append(svg("line", { class: "gridline", x1: x(t), x2: x(t), y1: top, y2: H - 22 }));
    const tx = svg("text", { x: x(t), y: H - 6, "text-anchor": "middle" });
    tx.textContent = fmt(t, { compact: true });
    s.append(tx);
  });
  data.forEach((d, i) => {
    const y = top + i * band + (band - barH) / 2;
    const g = svg("g", { tabindex: "0" });
    const w = Math.max(0, x(d.v) - x(0));
    const r = Math.min(4, w / 2);
    g.append(svg("path", {
      class: "mark", fill: SERIES[0],
      d: `M${x(0)},${y}h${w - r}a${r},${r} 0 0 1 ${r},${r}v${barH - 2 * r}a${r},${r} 0 0 1 -${r},${r}h-${w - r}z`,
    }));
    const lab = svg("text", { class: "lab", x: labelW - 10, y: y + barH / 2 + 4, "text-anchor": "end" });
    lab.textContent = d.label.length > maxChars ? d.label.slice(0, maxChars - 1) + "…" : d.label;
    const val = svg("text", { class: "val", x: x(d.v) + 6, y: y + barH / 2 + 4 });
    val.textContent = fmt(d.v);
    g.append(lab, val, svg("rect", { class: "hit", x: 0, y: top + i * band, width: W, height: band }));
    const on = (e) => { g.classList.add("hov"); showTip(e, d.label, [[spec.y[0], fmt(d.v), SERIES[0]]]); };
    g.addEventListener("pointermove", on);
    g.addEventListener("focus", (e) => on({ clientX: g.getBoundingClientRect().right, clientY: g.getBoundingClientRect().top }));
    g.addEventListener("pointerleave", () => { g.classList.remove("hov"); hideTip(); });
    g.addEventListener("blur", () => { g.classList.remove("hov"); hideTip(); });
    s.append(g);
  });
  s.append(svg("line", { class: "axis", x1: x(0), x2: x(0), y1: top, y2: H - 22 }));
  host.append(s);
}

function groupedBar(host, spec, rows, xi, yis) {
  const W = Math.max(host.clientWidth, 300), H = 280, left = 48, bottom = 44, top = 10;
  const max = Math.max(0, ...rows.flatMap((r) => yis.map((i) => (isNum(r[i]) ? r[i] : 0))));
  const ticks = niceTicks(max);
  const y = (v) => H - bottom - (v / ticks[ticks.length - 1]) * (H - bottom - top);
  const slot = (W - left) / rows.length, barW = Math.min(24, (slot - 12) / yis.length - 2);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": spec.title || "grouped bars" });
  ticks.forEach((t) => {
    s.append(svg("line", { class: "gridline", x1: left, x2: W, y1: y(t), y2: y(t) }));
    const tx = svg("text", { x: left - 8, y: y(t) + 4, "text-anchor": "end" });
    tx.textContent = fmt(t, { compact: true });
    s.append(tx);
  });
  rows.forEach((r, ri) => {
    const cx = left + slot * ri + slot / 2, gw = yis.length * (barW + 2) - 2;
    const g = svg("g", { tabindex: "0" });
    yis.forEach((ci, k) => {
      const v = isNum(r[ci]) ? r[ci] : 0, bx = cx - gw / 2 + k * (barW + 2), h = y(0) - y(v), rr = Math.min(4, h / 2, barW / 2);
      g.append(svg("path", { class: "mark", fill: SERIES[k],
        d: `M${bx},${y(0)}v-${h - rr}a${rr},${rr} 0 0 1 ${rr},-${rr}h${barW - 2 * rr}a${rr},${rr} 0 0 1 ${rr},${rr}v${h - rr}z` }));
    });
    const lab = svg("text", { class: "lab", x: cx, y: H - bottom + 18, "text-anchor": "middle" });
    const text = String(r[xi] ?? "NULL");
    lab.textContent = text.length > 12 ? text.slice(0, 11) + "…" : text;
    g.append(lab, svg("rect", { class: "hit", x: cx - slot / 2, y: top, width: slot, height: H - top - bottom + 24 }));
    const on = (e) => { g.classList.add("hov"); showTip(e, text, yis.map((ci, k) => [spec.y[k], fmt(r[ci]), SERIES[k]])); };
    g.addEventListener("pointermove", on);
    g.addEventListener("pointerleave", () => { g.classList.remove("hov"); hideTip(); });
    s.append(g);
  });
  s.append(svg("line", { class: "axis", x1: left, x2: W, y1: y(0), y2: y(0) }));
  host.append(s);
}

function lineChart(host, spec, rows, xi, yis) {
  const data = rows.map((r) => ({ x: r[xi], ys: yis.map((i) => (isNum(r[i]) ? r[i] : null)) }))
    .sort((a, b) => (a.x > b.x ? 1 : a.x < b.x ? -1 : 0));
  const W = Math.max(host.clientWidth, 300), H = 280, left = 52, right = 70, bottom = 34, top = 12;
  const max = Math.max(0, ...data.flatMap((d) => d.ys.filter((v) => v !== null)));
  const ticks = niceTicks(max);
  const n = data.length;
  const x = (i) => left + (n === 1 ? 0 : (i / (n - 1)) * (W - left - right));
  const y = (v) => H - bottom - (v / ticks[ticks.length - 1]) * (H - bottom - top);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${spec.y.join(", ")} over ${spec.x}` });
  ticks.forEach((t) => {
    s.append(svg("line", { class: "gridline", x1: left, x2: W - right, y1: y(t), y2: y(t) }));
    const tx = svg("text", { x: left - 8, y: y(t) + 4, "text-anchor": "end" });
    tx.textContent = fmt(t, { compact: true });
    s.append(tx);
  });
  const every = Math.max(1, Math.ceil(n / Math.floor((W - left - right) / 70)));
  data.forEach((d, i) => {
    if (i !== n - 1 && (i % every || n - 1 - i < every)) return;
    const tx = svg("text", { x: x(i), y: H - 10, "text-anchor": "middle" });
    tx.textContent = String(d.x).slice(0, 10);
    s.append(tx);
  });
  yis.forEach((_, k) => {
    const pts = data.map((d, i) => (d.ys[k] === null ? null : [x(i), y(d.ys[k])])).filter(Boolean);
    s.append(svg("path", { d: "M" + pts.map((p) => p.join(",")).join("L"), fill: "none", stroke: SERIES[k],
      "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    const last = pts[pts.length - 1];
    if (last) {
      s.append(svg("circle", { cx: last[0], cy: last[1], r: 4, fill: SERIES[k], stroke: "var(--card)", "stroke-width": 2 }));
      const v = svg("text", { class: "val", x: last[0] + 8, y: last[1] + 4 });
      v.textContent = fmt(data[data.length - 1].ys[k], { compact: true });
      s.append(v);
    }
  });
  s.append(svg("line", { class: "axis", x1: left, x2: W - right, y1: y(0), y2: y(0) }));
  const cross = svg("line", { class: "cross", y1: top, y2: H - bottom, visibility: "hidden" });
  const dots = yis.map((_, k) => svg("circle", { r: 4, fill: SERIES[k], stroke: "var(--card)", "stroke-width": 2, visibility: "hidden" }));
  s.append(cross, ...dots);
  const hit = svg("rect", { class: "hit", x: left, y: top, width: W - left - right, height: H - top - bottom });
  hit.addEventListener("pointermove", (e) => {
    const box = s.getBoundingClientRect();
    const px = ((e.clientX - box.left) / box.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - left) / (W - left - right)) * (n - 1))));
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    dots.forEach((dot, k) => {
      const v = data[i].ys[k];
      dot.setAttribute("visibility", v === null ? "hidden" : "visible");
      if (v !== null) { dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(v)); }
    });
    showTip(e, String(data[i].x), yis.map((_, k) => [spec.y[k], fmt(data[i].ys[k]), SERIES[k]]));
  });
  hit.addEventListener("pointerleave", () => {
    cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); hideTip();
  });
  s.append(hit);
  host.append(s);
}

function scatter(host, spec, rows, xi, yi) {
  const pts = rows.filter((r) => isNum(r[xi]) && isNum(r[yi]));
  const W = Math.max(host.clientWidth, 300), H = 300, left = 52, bottom = 36, top = 10, right = 16;
  const xt = niceTicks(Math.max(...pts.map((r) => r[xi]), 0)), yt = niceTicks(Math.max(...pts.map((r) => r[yi]), 0));
  const x = (v) => left + (v / xt[xt.length - 1]) * (W - left - right);
  const y = (v) => H - bottom - (v / yt[yt.length - 1]) * (H - bottom - top);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${spec.y[0]} against ${spec.x}` });
  yt.forEach((t) => {
    s.append(svg("line", { class: "gridline", x1: left, x2: W - right, y1: y(t), y2: y(t) }));
    const tx = svg("text", { x: left - 8, y: y(t) + 4, "text-anchor": "end" }); tx.textContent = fmt(t, { compact: true }); s.append(tx);
  });
  xt.forEach((t) => {
    const tx = svg("text", { x: x(t), y: H - 12, "text-anchor": "middle" }); tx.textContent = fmt(t, { compact: true }); s.append(tx);
  });
  pts.forEach((r) => {
    const g = svg("g");
    g.append(svg("circle", { class: "mark", cx: x(r[xi]), cy: y(r[yi]), r: 4, fill: SERIES[0], stroke: "var(--card)", "stroke-width": 2 }));
    const h = svg("circle", { class: "hit", cx: x(r[xi]), cy: y(r[yi]), r: 12 });
    h.addEventListener("pointermove", (e) => showTip(e, `${spec.x} ${fmt(r[xi])}`, [[spec.y[0], fmt(r[yi]), SERIES[0]]]));
    h.addEventListener("pointerleave", hideTip);
    g.append(h); s.append(g);
  });
  host.append(s);
}

// ---------------------------------------------------------------- result panels
const SQL_KW = new Set(("SELECT FROM WHERE GROUP BY ORDER HAVING LIMIT OFFSET JOIN LEFT RIGHT FULL OUTER INNER CROSS ON AS AND OR " +
  "WITH DESC ASC CASE WHEN THEN ELSE END DISTINCT UNION ALL IN NOT NULL IS BETWEEN LIKE ILIKE EXISTS OVER PARTITION " +
  "FILTER NULLS FIRST LAST TRUE FALSE INTERVAL USING").split(" "));
const SQL_TOKEN = /('(?:[^']|'')*')|("(?:[^"]|"")*")|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)(?=\s*\()|([A-Za-z_][A-Za-z0-9_]*)|(<=|>=|<>|!=|::|[=<>*+\-\/%,()|])/g;
let lastSql = "";
function highlightLine(line) {
  const out = el("span", { class: "ln" });
  let last = 0;
  for (const m of line.matchAll(SQL_TOKEN)) {
    if (m.index > last) out.append(document.createTextNode(line.slice(last, m.index)));
    const [tok, str, , num, fn, word, op] = m;
    let cls = "";
    if (str) cls = "str";
    else if (num) cls = "nm";
    else if (fn) cls = SQL_KW.has(fn.toUpperCase()) ? "kw" : "fn";
    else if (word && SQL_KW.has(word.toUpperCase())) cls = "kw";
    else if (op) cls = "op";
    out.append(cls ? el("span", { class: cls, text: tok }) : document.createTextNode(tok));
    last = m.index + tok.length;
  }
  if (last < line.length) out.append(document.createTextNode(line.slice(last)));
  return out;
}
function renderSql(sql) {
  const pre = $("sql");
  pre.replaceChildren();
  lastSql = sql || "";
  if (!sql) { pre.append(el("span", { class: "empty-q", text: "-- no query ran" })); return; }
  const pretty = sql.replace(/\s+(FROM|WHERE|GROUP BY|ORDER BY|HAVING|LIMIT|JOIN|LEFT JOIN|INNER JOIN|UNION)\b/g, "\n$1");
  lastSql = pretty;
  pretty.split("\n").forEach((line) => pre.append(highlightLine(line)));
}

const NODE_TEXT = {
  link_schema: (t) => ["Schema linking", t.scope],
  generate_sql: (t) => t.outcome ? ["Write SQL", t.outcome] : ["Write SQL", t.error ? "model error" : `attempt ${t.attempt}` + (t.cached ? " (cached reply)" : "")],
  guard: (t) => t.ok ? ["Guard", `single SELECT, ${t.tables.length} table(s)` + (t.limit_applied ? `, LIMIT ${t.limit_applied}` : "")] : ["Guard refused", t.reason],
  execute: (t) => t.error ? ["Execute failed", t.error] : ["Execute", `${t.rows} row(s)${t.truncated ? ", truncated" : ""}`],
  review: (t) => ["Review", t.verdict + (t.problem ? ": " + t.problem : "")],
  retry: (t) => ["Self-correct", `after ${t.reason}` + (t.escalate_schema ? ", widened to full schema" : "")],
  give_up: (t) => ["Gave up", `${t.attempts} attempts`],
  chart: (t) => ["Chart", t.type === "none" ? "table only" : t.type],
  widen_schema: () => ["Widen schema", "full schema before asking the user"],
  explain: (t) => ["Explain", t.numbers_checked ? "numbers checked against result" : t.error ? "unavailable" : `unverified: ${(t.unverified || []).join(", ")}`],
  clarify: () => ["Clarify", "asked the user"],
};
function renderTrace(trace) {
  const ol = $("trace");
  ol.replaceChildren();
  trace.forEach((t, i) => {
    const [title, detail] = (NODE_TEXT[t.node] || (() => [t.node, ""]))(t);
    const bad = t.error || t.ok === false || t.node === "retry" || t.node === "give_up";
    const li = el("li", { class: bad ? "bad" : "good" },
      el("span", { class: "n", text: String(i + 1) }),
      el("span", { class: "what" }, el("b", { text: title }), document.createTextNode(" "), el("span", { text: detail || "" })),
      el("span", { class: "ms", text: t.ms >= 1000 ? (t.ms / 1000).toFixed(1) + " s" : Math.round(t.ms) + " ms" }));
    ol.append(li);
  });
}

function renderTable(columns, rows, truncated) {
  const table = $("table");
  table.replaceChildren();
  document.querySelector(".table-block").hidden = !columns.length;
  if (!columns.length) return;
  const numeric = columns.map((_, i) => rows.length && rows.every((r) => r[i] === null || isNum(r[i])));
  const head = el("tr", {}, el("th", { class: "rn", "aria-label": "row" }));
  columns.forEach((c, i) => head.append(el("th", { class: numeric[i] ? "n" : "", text: c })));
  const body = el("tbody");
  rows.slice(0, 500).forEach((r) => {
    const tr = el("tr", {}, el("td", { class: "rn", text: String(body.children.length + 1) }));
    r.forEach((v, i) => tr.append(el("td", { class: v === null ? "null" : numeric[i] ? "n" : "", text: v === null ? "null" : numeric[i] ? fmt(v) : String(v), title: v === null ? "" : String(v) })));
    body.append(tr);
  });
  table.append(el("thead", {}, head), body);
  $("table-label").textContent = `Result · ${rows.length.toLocaleString()} row${rows.length === 1 ? "" : "s"}${truncated ? " (capped)" : ""} × ${columns.length} col${columns.length === 1 ? "" : "s"}`;
}

function render(res) {
  document.body.classList.add("has-result");
  $("result").hidden = false;
  $("error").hidden = true;
  const clarify = $("clarify");
  clarify.hidden = true;
  clarify.replaceChildren();
  const caveats = $("caveats");
  caveats.replaceChildren();
  const badge = $("status-badge");
  badge.className = "badge " + res.status;
  badge.textContent = { answered: "Answered", clarify: "Needs clarification", declined: "Declined", failed: "Failed" }[res.status] || res.status;
  if (res.status === "clarify") {
    $("answer").textContent = res.clarifying_question;
    clarify.hidden = false;
    clarify.append(el("p", { text: "Pick one, or rephrase the question." }));
    const opts = el("div", { class: "opts" });
    res.options.forEach((o) => {
      const b = el("button", { class: "opt", type: "button", text: o });
      b.addEventListener("click", () => ask(res.question, o));
      opts.append(b);
    });
    clarify.append(opts);
  } else if (res.status === "failed") {
    $("answer").textContent = "I couldn't answer that safely. " + (res.error || "");
  } else {
    $("answer").textContent = res.answer || `${res.row_count} row(s) returned.`;
  }
  const seen = new Set();
  [...(res.assumptions || []), ...(res.caveats || [])]
    .filter((c) => { const k = c.toLowerCase().slice(0, 40); if (seen.has(k)) return false; seen.add(k); return true; })
    .slice(0, 3)
    .forEach((c) => caveats.append(el("li", { text: c })));
  if (res.unverified_numbers && res.unverified_numbers.length) {
    caveats.append(el("li", { class: "warn", text: `Not found in the result: ${res.unverified_numbers.join(", ")}. Check the table.` }));
  }
  $("notes-block").hidden = caveats.children.length === 0;
  renderSql(res.sql);
  const tries = res.attempts.length;
  $("sql-meta").textContent = res.sql
    ? `${res.row_count} row(s) · ${tries} attempt${tries === 1 ? "" : "s"} · ${res.schema_scope || ""}`
    : res.status === "clarify" ? "Waiting for your answer: no SQL has run."
    : res.status === "declined" ? "Declined: this assistant only reads data." : "";
  renderTrace(res.trace);
  const u = res.usage;
  const replay = u.llm_calls > 0 && u.cached_calls === u.llm_calls ? " · replayed from the response cache" : "";
  $("usage").textContent = `${u.llm_calls} model call${u.llm_calls === 1 ? "" : "s"} · ${u.input_tokens.toLocaleString()} in / ${u.output_tokens.toLocaleString()} out tokens · $${u.usd.toFixed(5)} · ${(res.elapsed_ms / 1000).toFixed(1)} s · ${u.model}${replay}`;
  renderChart(res.chart, res.columns, res.rows);
  renderTable(res.columns, res.rows, res.truncated);
  refreshSpend();
}

// ---------------------------------------------------------------- schema browser
const TABLE_ICO = '<svg class="t-ico" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><rect x="1.5" y="2.5" width="13" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M1.5 6.5h13M6 6.5v7" stroke="currentColor" stroke-width="1.4"/></svg>';
const CARET = '<svg class="caret" viewBox="0 0 12 12" aria-hidden="true"><path d="M4.5 3 7.5 6l-3 3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
function renderSchema(tables) {
  const ul = $("schema");
  ul.replaceChildren();
  [...tables].sort((a, b) => a.name.localeCompare(b.name)).forEach((t, i) => {
    const sum = el("summary");
    sum.insertAdjacentHTML("beforeend", CARET + TABLE_ICO); // static markup only, no data
    sum.append(el("span", { class: "t-name", text: t.name }),
      el("span", { class: "t-rows", text: fmt(t.rows, { compact: true }), title: `${t.rows.toLocaleString()} rows` }));
    const cols = el("ul", { class: "cols" });
    t.columns.forEach((c) => cols.append(el("li", { "data-name": c.toLowerCase() },
      el("span", { class: "c-ico", "aria-hidden": "true" }), el("span", { class: "c-name", text: c }))));
    const det = el("details", { "data-name": t.name.toLowerCase() }, sum, cols);
    if (i < 3) det.open = true;
    ul.append(el("li", {}, det));
  });
}
function filterSchema(q) {
  q = q.trim().toLowerCase();
  document.querySelectorAll("#schema > li").forEach((li) => {
    const det = li.firstElementChild;
    const tableHit = !q || det.dataset.name.includes(q);
    let colHit = false;
    det.querySelectorAll(".cols li").forEach((c) => {
      const own = !!q && c.dataset.name.includes(q);
      if (own) colHit = true;
      c.hidden = !(tableHit || own);
    });
    li.hidden = !(tableHit || colHit);
    if (q && colHit) det.open = true;
  });
}

// ---------------------------------------------------------------- wiring
async function ask(question, clarification) {
  const go = $("go");
  go.disabled = true;
  $("result").classList.add("loading");
  go.querySelector("span").textContent = "Running…";
  try {
    const r = await fetch("/api/ask", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ question, clarification: clarification || null }) });
    const body = await r.json();
    if (!r.ok) throw new Error(body.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : r.statusText);
    render(body);
    const params = new URLSearchParams({ q: question });
    history.replaceState(null, "", "?" + params.toString());
  } catch (e) {
    $("error").hidden = false;
    $("error").textContent = "Request failed: " + e.message;
  } finally {
    go.disabled = false;
    go.querySelector("span").textContent = "Run";
    $("result").classList.remove("loading");
  }
}

async function refreshSpend() {
  try {
    const s = await (await fetch("/api/spend")).json();
    $("spend").textContent = `$${s.spent_usd.toFixed(4)} / $${s.cap_usd.toFixed(2)}`;
    $("spend-bar").style.width = Math.min(100, (100 * s.spent_usd) / (s.cap_usd || 1)).toFixed(1) + "%";
  } catch { /* the badge is optional */ }
}

async function init() {
  try {
    const s = await (await fetch("/api/schema")).json();
    $("dialect").textContent = s.dialect;
    $("table-count").textContent = s.tables.length;
    const names = s.tables.map((t) => t.name.toLowerCase());
    const chinook = names.includes("invoice") && names.includes("track");
    $("db-name").textContent = chinook ? `chinook · ${s.dialect}` : s.dialect;
    renderSchema(s.tables);
    (chinook ? EXAMPLES.chinook : EXAMPLES.generic).forEach((q) => {
      const b = el("button", { class: "chip", type: "button", text: q });
      b.addEventListener("click", () => { $("q").value = q; ask(q); });
      $("examples").append(b);
    });
  } catch { /* schema panel is optional */ }
  refreshSpend();
  const q = new URLSearchParams(location.search).get("q");
  if (q) { $("q").value = q; ask(q); }
}

$("ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("q").value.trim();
  if (q.length >= 3) ask(q);
});
$("q").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); $("ask").requestSubmit(); }
});
$("copy").addEventListener("click", () => navigator.clipboard?.writeText(lastSql));
$("theme").addEventListener("click", () => {
  const root = document.documentElement;
  root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
  try { localStorage.setItem("theme", root.dataset.theme); } catch { /* private mode */ }
});
try {
  const t = new URLSearchParams(location.search).get("theme") || localStorage.getItem("theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
} catch { /* ignore */ }
function setSchemaOpen(open) {
  document.body.classList.toggle("schema-open", open);
  $("scrim").hidden = !open;
  $("schema-toggle").setAttribute("aria-expanded", String(open));
}
$("schema-toggle").addEventListener("click", () => setSchemaOpen(!document.body.classList.contains("schema-open")));
$("scrim").addEventListener("click", () => setSchemaOpen(false));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") setSchemaOpen(false); });
$("schema-filter").addEventListener("input", (e) => filterSchema(e.target.value));
init();
