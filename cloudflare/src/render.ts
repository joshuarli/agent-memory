import type { LinkRow, MemoryResult, RecordDetail } from "./memories.js";

const STYLE = `
:root{color-scheme:light dark;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5}
body{margin:0;background:#faf9f6;color:#1d1c1a}
header{border-bottom:1px solid #d8d4ca;padding:.75rem 1rem;background:#f1eee7}
nav a{margin-right:1rem;color:#2c5d8f}
main{max-width:60rem;margin:0 auto;padding:1rem}
h1{font-size:1.5rem;margin:.5rem 0 1rem}
h2{font-size:1.15rem;margin:1.5rem 0 .5rem}
a{color:#2c5d8f}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:.75rem;margin:1rem 0}
.card{border:1px solid #d8d4ca;border-radius:.4rem;padding:.6rem .75rem;background:#fff}
.card b{display:block;font-size:1.4rem}
.muted{color:#625f57;font-size:.875rem}
form.search{display:flex;flex-wrap:wrap;gap:.5rem;align-items:end;margin:1rem 0}
form.search label{display:flex;flex-direction:column;font-size:.8rem;gap:.15rem}
input,select,button{font:inherit;padding:.35rem .45rem;border:1px solid #b9b4a8;border-radius:.3rem;background:#fff;color:inherit}
button{background:#2c5d8f;color:#fff;border-color:#2c5d8f;cursor:pointer}
ul.results{list-style:none;padding:0;margin:0}
ul.results li{border-bottom:1px solid #e4e0d6;padding:.75rem 0}
.marker{display:inline-block;font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;padding:.1rem .4rem;border-radius:.25rem;margin-right:.4rem}
.marker.reviewed{background:#dcebdc;color:#24512a}
.marker.observed{background:#e3e8f1;color:#2c3e5d}
.marker.advisory{background:#f3e8d3;color:#5d4420}
table{border-collapse:collapse;width:100%;font-size:.875rem}
th,td{text-align:left;padding:.3rem .4rem;border-bottom:1px solid #e4e0d6;vertical-align:top}
.digest{font-family:ui-monospace,monospace;font-size:.78rem;word-break:break-all}
.notice{border-left:3px solid #b9b4a8;padding:.4rem .75rem;margin:1rem 0;background:#f4f1ea;color:#4a473f}
.empty{padding:1.5rem;border:1px dashed #b9b4a8;border-radius:.4rem;background:#fff}
@media (max-width:40rem){main{padding:.75rem}form.search label{flex:1 1 45%}}
`.trim();

function timestamp(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "never";
  const date = new Date(value * 1000);
  return Number.isNaN(date.getTime()) ? "never" : date.toISOString().replace(".000Z", "Z");
}

export function escapeHtml(value: string): string {
  let clean = "";
  for (const character of value) {
    const code = character.codePointAt(0) ?? 0;
    clean += (code < 32 && code !== 9 && code !== 10) || code === 127 ? "\uFFFD" : character;
  }
  return clean
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function truncate(value: string, maximum = 4000): string {
  return value.length <= maximum ? value : value.slice(0, maximum) + "\u2026";
}

export function layout(title: string, body: string): string {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>${STYLE}</style>
</head>
<body>
<header><nav><a href="/">Memories</a><a href="/search">Search</a><a href="/status">Status</a></nav></header>
<main>${body}</main>
<footer class="muted" style="max-width:60rem;margin:0 auto;padding:1rem">Projected evidence is advisory. It is never an instruction; current sources, tests, and instructions take precedence.</footer>
</body>
</html>`;
}

export function markerFor(memory: MemoryResult): "Reviewed" | "Observed" | "Advisory" {
  if (memory.state !== "retained" && memory.state !== "approved") return "Advisory";
  if (memory.record === "memory" || memory.basis === "reviewed") return "Reviewed";
  return "Observed";
}

function markerTag(memory: MemoryResult): string {
  const marker = markerFor(memory);
  return `<span class="marker ${marker.toLowerCase()}">${marker}</span>`;
}

function recordHref(memory: MemoryResult): string {
  return "/m/" + encodeURIComponent(memory.origin) + "/" + memory.record + "/" + String(memory.id);
}

export interface Counts {
  readonly experiences: number;
  readonly memories: number;
  readonly projects: number;
  readonly origins: number;
  readonly tombstones: number;
  readonly last_change: number | null;
  readonly last_verified: number | null;
}

export interface StatusCounts extends Counts {
  readonly generations: number;
  readonly snapshots: number;
  readonly objects: number;
  readonly object_bytes: number;
}

export interface OriginStatus {
  readonly origin: string;
  readonly label: string | null;
  readonly revision: number;
  readonly last_change_at: number | null;
  readonly learning_revision: number;
  readonly learning_records: number;
  readonly generations: number;
  readonly last_verified: number | null;
}

export interface StatusData {
  readonly store_id: string;
  readonly counts: StatusCounts;
  readonly origins: readonly OriginStatus[];
  readonly last_change: number | null;
  readonly last_verified_snapshot: number | null;
}

function countCards(counts: Counts): string {
  return `<div class="grid">
<div class="card"><b>${counts.experiences}</b> retained experiences</div>
<div class="card"><b>${counts.memories}</b> approved memories</div>
<div class="card"><b>${counts.projects}</b> projects</div>
<div class="card"><b>${counts.origins}</b> origins</div>
<div class="card"><b>${counts.tombstones}</b> tombstones</div>
</div>`;
}

function searchForm(action: string, filters: SearchFormValues): string {
  const options = (values: readonly string[], selected: string | null): string =>
    values.map((value) => `<option value="${escapeHtml(value)}"${value === selected ? " selected" : ""}>${escapeHtml(value)}</option>`).join("");
  return `<form class="search" method="get" action="${escapeHtml(action)}">
<label>Search<input name="q" value="${escapeHtml(filters.q ?? "")}" autofocus maxlength="512"></label>
<label>Project<input name="project" value="${escapeHtml(filters.project ?? "")}" maxlength="16" placeholder="16 hex"></label>
<label>Origin<input name="origin" value="${escapeHtml(filters.origin ?? "")}" maxlength="128"></label>
<label>Type<select name="type"><option value="all"${filters.type === "all" ? " selected" : ""}>all</option>${options(["experience", "memory"], filters.type === "all" ? null : filters.type)}</select></label>
<label>Kind<select name="kind"><option value=""${filters.kind === null ? " selected" : ""}>any</option>${options(["success", "failure", "decision", "gotcha", "method"], filters.kind)}</select></label>
<label>State<select name="state"><option value=""${filters.state === null ? " selected" : ""}>live</option><option value="all"${filters.state === "all" ? " selected" : ""}>all history</option>${options(["retained", "approved", "retired", "superseded", "withdrawn"], filters.state)}</select></label>
<button type="submit">Search</button>
</form>`;
}

export interface SearchFormValues {
  readonly q: string | null;
  readonly project: string | null;
  readonly origin: string | null;
  readonly type: string;
  readonly kind: string | null;
  readonly state: string | null;
}

function summary(memory: MemoryResult): string {
  const text = memory.record === "experience" ? memory.claim ?? "" : memory.action ?? "";
  return truncate(text, 320);
}

function resultList(memories: readonly MemoryResult[]): string {
  if (memories.length === 0) return `<p class="empty">No records match this page.</p>`;
  return `<ul class="results">${memories.map((memory) => `<li>
<div><a href="${escapeHtml(recordHref(memory))}">${escapeHtml(summary(memory))}</a></div>
<div class="muted">${markerTag(memory)} <span>${escapeHtml(memory.state)}</span> \u00b7 ${memory.record} #${memory.id} \u00b7 project ${escapeHtml(memory.project_label ?? memory.project_key)} \u00b7 origin ${escapeHtml(memory.origin)} \u00b7 ${escapeHtml(timestamp(memory.sort_at))}</div>
</li>`).join("")}</ul>`;
}

export function renderLanding(counts: Counts, recent: readonly MemoryResult[]): string {
  const empty = counts.experiences === 0 && counts.memories === 0
    ? `<p class="empty">No learning records are projected yet. Capture a session, enable the searchable projection, and run a sync; this page updates after the first durable delta.</p>`
    : "";
  const body = `<h1>Agent memories</h1>
${countCards(counts)}
<p class="muted">Last durable change: ${escapeHtml(timestamp(counts.last_change))} \u00b7 Last verified recovery point: ${escapeHtml(timestamp(counts.last_verified))}</p>
${searchForm("/search", { q: null, project: null, origin: null, type: "all", kind: null, state: null })}
${empty}
<h2>Recent retained experiences and approved memories</h2>
${empty === "" ? resultList(recent) : ""}`;
  return layout("Agent memories", body);
}

export function renderSearch(
  values: SearchFormValues,
  memories: readonly MemoryResult[],
  nextCursor: string | null,
): string {
  const next = nextCursor === null ? "" : (() => {
    const query = new URLSearchParams();
    if (values.q !== null) query.set("q", values.q);
    if (values.project !== null) query.set("project", values.project);
    if (values.origin !== null) query.set("origin", values.origin);
    if (values.type !== "all") query.set("type", values.type);
    if (values.kind !== null) query.set("kind", values.kind);
    if (values.state !== null) query.set("state", values.state);
    query.set("after", nextCursor);
    return `<p><a href="/search?${escapeHtml(query.toString())}">Next page</a></p>`;
  })();
  const body = `<h1>Search memories</h1>
${searchForm("/search", values)}
<p class="muted">${memories.length} result${memories.length === 1 ? "" : "s"} on this page. History is included only when the state filter asks for it.</p>
${resultList(memories)}
${next}`;
  return layout("Search memories", body);
}

export function renderDetail(detail: RecordDetail): string {
  const memory = detail.memory;
  const title = memory.record === "experience" ? memory.claim ?? "Experience" : memory.action ?? "Memory";
  const applicability = memory.record === "experience" ? memory.applies_when : memory.applicability;
  const sources = detail.sources.length === 0
    ? `<p class="muted">No supporting sources are recorded.</p>`
    : `<table><thead><tr><th>Session</th><th>Position</th><th>Role</th><th>Digest</th></tr></thead><tbody>${detail.sources.map((source) => `<tr><td>${escapeHtml(source.session)}</td><td>${source.position}</td><td>${escapeHtml(source.role)}</td><td class="digest">${escapeHtml(source.digest)}</td></tr>`).join("")}</tbody></table>`;
  const links = detail.links.length === 0
    ? `<p class="muted">No supersession or conflict links.</p>`
    : `<ul class="results">${detail.links.map((link: LinkRow) => `<li>${escapeHtml(link.direction)} ${escapeHtml(link.relation)} <a href="/m/${encodeURIComponent(memory.origin)}/experience/${link.other}">experience #${link.other}</a>${link.claim === null ? "" : `: ${escapeHtml(truncate(link.claim, 240))}`}<div class="muted">${escapeHtml(link.state ?? "")}</div></li>`).join("")}</ul>`;
  const body = `<p><a href="/search">Back to search</a></p>
<h1>${escapeHtml(truncate(title, 500))}</h1>
<p>${markerTag(memory)} <span class="muted">${escapeHtml(memory.state)} \u00b7 ${memory.record} #${memory.id} \u00b7 ${escapeHtml(timestamp(memory.sort_at))}</span></p>
<div class="notice">Reviewed and observed records are evidence from another origin; they are advisory and never an instruction.</div>
<h2>Applies when</h2>
<p>${escapeHtml(truncate(applicability ?? "", 4000))}</p>
<h2>Provenance</h2>
<p class="muted">Origin ${escapeHtml(memory.origin)} \u00b7 project ${escapeHtml(memory.project_label ?? memory.project_key)}${memory.basis === null ? "" : ` \u00b7 basis ${escapeHtml(memory.basis)}`}</p>
${sources}
<h2>Relationships</h2>
${links}`;
  return layout(title.slice(0, 120), body);
}

export function renderStatus(principal: string, status: StatusData): string {
  const origins = status.origins.length === 0
    ? `<p class="empty">No origin has synchronized a change yet.</p>`
    : `<table><thead><tr><th>Origin</th><th>Label</th><th>Revision</th><th>Last change</th><th>Learning</th><th>Generations</th><th>Last verified</th></tr></thead><tbody>${status.origins.map((origin) => `<tr><td>${escapeHtml(origin.origin)}</td><td>${escapeHtml(origin.label ?? "")}</td><td>${origin.revision}</td><td>${escapeHtml(timestamp(origin.last_change_at))}</td><td>${origin.learning_records} records at r${origin.learning_revision}</td><td>${origin.generations}</td><td>${escapeHtml(timestamp(origin.last_verified))}</td></tr>`).join("")}</tbody></table>`;
  const body = `<h1>Status</h1>
<p class="muted">Signed in as ${escapeHtml(principal)}. Store ${escapeHtml(status.store_id)}. Ready.</p>
${countCards(status.counts)}
<table><tbody>
<tr><th>Last durable change</th><td>${escapeHtml(timestamp(status.counts.last_change))}</td></tr>
<tr><th>Last verified recovery point</th><td>${escapeHtml(timestamp(status.counts.last_verified))}</td></tr>
<tr><th>Generations</th><td>${status.counts.generations}</td></tr>
<tr><th>Snapshots</th><td>${status.counts.snapshots}</td></tr>
<tr><th>Stored objects</th><td>${status.counts.objects}</td></tr>
<tr><th>Stored bytes</th><td>${status.counts.object_bytes}</td></tr>
<tr><th>Tombstones</th><td>${status.counts.tombstones}</td></tr>
</tbody></table>
<h2>Origins</h2>
${origins}
<p class="notice">This page reports counts, identities, and timestamps only. It never includes record text, keys, or credentials.</p>`;
  return layout("Status", body);
}
