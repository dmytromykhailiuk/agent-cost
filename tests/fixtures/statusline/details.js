// Detail pages for the status-line context legend: collects what each context category contains
// (from the transcript) and writes one static HTML page per category, opened via OSC 8 links.
const fs = require('fs');
const path = require('path');

const HISTORY_LIMIT = 600;
const PREVIEW = 400;

const PAGES = [
  { key: 'overview', label: 'Overview', color: '#7aa2ff' },
  { key: 'system', label: 'System', color: '#9678ff' },
  { key: 'tools', label: 'Tools', color: '#5aa0ff' },
  { key: 'skills', label: 'Skills', color: '#3cc8c8' },
  { key: 'mcp', label: 'MCP', color: '#f09650' },
  { key: 'agents', label: 'Agents', color: '#e66eaa' },
  { key: 'memory', label: 'Memory/env', color: '#aaaa5a' },
  { key: 'messages', label: 'History', color: '#5ad278' },
];

function freshDetails() {
  return { system: [], tools: [], deferred: [], skills: [], agents: [], mcp: [], memory: [], history: [], historyTotal: 0 };
}

const str = (v) => (v == null ? '' : typeof v === 'string' ? v : JSON.stringify(v));
const rendered = (o) => (Array.isArray(o.rendered) ? o.rendered.map((r) => str(r.content)).join('\n') : '');
const clip = (s, n = PREVIEW) => (s.length > n ? s.slice(0, n) + '…' : s);

// "- name: description" listings (skills, agents) → [{name, text}]
function parseListing(text) {
  return text.split(/\n(?=- )/).map((l) => l.replace(/^- /, '').trim()).filter(Boolean).map((l) => {
    const i = l.indexOf(': ');
    return i > 0 && i < 80 ? { name: l.slice(0, i), text: l.slice(i + 2).trim() } : { name: l.slice(0, 60), text: l };
  });
}

function pushHistory(det, entry) {
  det.historyTotal++;
  det.history.push(entry);
  if (det.history.length > HISTORY_LIMIT) det.history.shift();
}

function ingestDetail(det, o) {
  if (o.type === 'attachment') {
    const a = o.attachment || {};
    switch (a.type) {
      case 'prompt_snapshot':
        if (Array.isArray(a.systemPrompt)) det.system = a.systemPrompt.map(str);
        if (Array.isArray(a.tools)) det.tools = a.tools.map((t) => ({ name: t.name, description: str(t.description), schema: str(t.schema) }));
        break;
      case 'skill_listing': {
        const items = parseListing(str(a.content) || rendered(o));
        det.skills = a.isInitial ? items : det.skills.concat(items);
        break;
      }
      case 'agent_listing_delta': {
        const items = parseListing((a.addedLines || []).join('\n'));
        det.agents = a.isInitial ? items : det.agents.filter((x) => !(a.removedTypes || []).includes(x.name)).concat(items);
        break;
      }
      case 'mcp_instructions_delta':
        det.mcp = det.mcp.filter((x) => !(a.removedNames || []).includes(x.name));
        (a.addedBlocks || []).forEach((b, i) => det.mcp.push({ name: (a.addedNames || [])[i] || `block ${i + 1}`, text: str(b) }));
        break;
      case 'deferred_tools_delta':
        det.deferred = det.deferred.filter((n) => !(a.removedNames || []).includes(n)).concat(a.addedNames || []);
        break;
      case 'total_tokens_reminder':
        break;
      default: {
        const text = rendered(o);
        if (text) det.memory.push({ name: a.type, text, ts: o.timestamp });
      }
    }
  } else if (o.type === 'user' || o.type === 'assistant') {
    const c = o.message?.content;
    const blocks = typeof c === 'string' ? [{ type: 'text', text: c }] : Array.isArray(c) ? c : [];
    for (const b of blocks) {
      let kind = b.type, title = '', body = '';
      if (b.type === 'text') { body = str(b.text); title = o.type === 'user' ? 'User' : 'Assistant'; }
      else if (b.type === 'tool_use') { title = `→ ${b.name}`; body = str(b.input); }
      else if (b.type === 'tool_result') { title = '← tool result'; body = Array.isArray(b.content) ? b.content.map((x) => str(x.text ?? x)).join('\n') : str(b.content); }
      else if (b.type === 'thinking') { title = 'Thinking'; body = str(b.thinking); }
      else { title = b.type; body = str(b); }
      pushHistory(det, { role: o.type, kind, title, preview: clip(body), chars: body.length, ts: o.timestamp });
    }
  } else if (o.type === 'system' && o.subtype === 'compact_boundary') {
    det.history = [];
    det.historyTotal = 0;
    pushHistory(det, { role: 'system', kind: 'compact', title: '— /compact —', preview: 'Earlier history was summarized.', chars: 0, ts: o.timestamp });
  }
}

// ---------- HTML ----------
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmtTok = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${Math.round(n)}`);
const time = (ts) => (ts ? new Date(ts).toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '');

function itemsFor(key, det, cpt) {
  const tok = (s) => Math.round(s.length / cpt);
  const byTok = (a) => a.sort((x, y) => y.tokens - x.tokens);
  switch (key) {
    case 'system':
      return det.system.map((s, i) => {
        const head = (s.match(/^#+ (.+)$/m) || [])[1] || s.trim().split('\n')[0];
        return { name: clip(head || `Section ${i + 1}`, 90), tokens: tok(s), body: s };
      });
    case 'tools':
      return byTok(det.tools.map((t) => ({ name: t.name, tokens: tok(t.description + t.schema), sub: clip(t.description.split('\n')[0], 160), body: t.description, extra: t.schema })))
        .concat(det.deferred.map((n) => ({ name: n, tokens: tok(n) + 1, sub: 'deferred — only the name is in context until loaded via ToolSearch', muted: true })));
    case 'skills':
      return byTok(det.skills.map((s) => ({ name: s.name, tokens: tok(s.name + s.text), sub: clip(s.text, 160), body: s.text })));
    case 'agents':
      return byTok(det.agents.map((s) => ({ name: s.name, tokens: tok(s.name + s.text), sub: clip(s.text, 160), body: s.text })));
    case 'mcp':
      return byTok(det.mcp.map((m) => ({ name: m.name, tokens: tok(m.text), sub: clip(m.text.replace(/^#+.*\n/, ''), 160), body: m.text })));
    case 'memory':
      return byTok(det.memory.map((m) => ({ name: m.name, tokens: tok(m.text), sub: clip(m.text.replace(/\s+/g, ' '), 160), body: m.text })));
    case 'messages':
      return det.history.slice().reverse().map((h) => ({
        name: h.title, tokens: Math.round(h.chars / cpt), sub: `${time(h.ts)} · ${h.role}`, body: h.preview, role: h.role,
      }));
  }
  return [];
}

const CSS = `
:root{--bg:#f6f6f8;--card:#fff;--text:#1d1d22;--muted:#6b6b76;--line:#e3e3ea;--track:#ececf2;--code:#f1f1f5}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#121216;--card:#1b1b21;--text:#e6e6ee;--muted:#8c8c99;--line:#2a2a33;--track:#26262e;--code:#15151a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:24px 16px 64px}
header h1{font-size:20px;margin:0 0 4px}header .meta{color:var(--muted);font-size:13px}
nav{display:flex;flex-wrap:wrap;gap:6px;margin:18px 0}
nav a{display:flex;align-items:center;gap:6px;padding:6px 10px;border:1px solid var(--line);border-radius:999px;color:var(--text);text-decoration:none;background:var(--card);font-size:13px}
nav a.on{border-color:var(--c);box-shadow:inset 0 0 0 1px var(--c)}nav a i{width:8px;height:8px;border-radius:50%;background:var(--c)}nav a span{color:var(--muted)}
.stack{display:flex;height:14px;border-radius:7px;overflow:hidden;background:var(--track);margin:6px 0 4px}.stack div{height:100%}
.legend{color:var(--muted);font-size:12px;display:flex;justify-content:space-between}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-top:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;text-decoration:none;color:var(--text);border-top:3px solid var(--c)}
.card b{display:block;font-size:20px}.card span{color:var(--muted);font-size:12px}
.tools{display:flex;gap:10px;align-items:center;margin:18px 0 10px}.tools input{flex:1;padding:8px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);font-size:14px}
.count{color:var(--muted);font-size:13px;white-space:nowrap}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:6px 0}
summary{list-style:none;cursor:pointer;padding:10px 14px;display:grid;grid-template-columns:1fr auto;gap:2px 12px;align-items:center}summary::-webkit-details-marker{display:none}
summary .n{font-weight:600;overflow-wrap:anywhere}summary .t{font-variant-numeric:tabular-nums;color:var(--muted);font-size:13px;text-align:right}
summary .s{color:var(--muted);font-size:12.5px;grid-column:1/-1}summary .b{grid-column:1/-1;height:3px;background:var(--track);border-radius:2px;overflow:hidden}summary .b div{height:100%;background:var(--c)}
details.muted summary .n{font-weight:500;color:var(--muted)}
details[open] summary{border-bottom:1px solid var(--line)}
pre{margin:0;padding:12px 14px;white-space:pre-wrap;overflow-wrap:anywhere;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--code);border-radius:0 0 10px 10px;max-height:480px;overflow:auto}
.role-user summary .n{color:#5aa0ff}.role-assistant summary .n{color:var(--text)}
.empty{color:var(--muted);padding:24px 0}
`;

function page(key, det, ctx) {
  const { cats, used, size, cpt, meta } = ctx;
  const p = PAGES.find((x) => x.key === key);
  const nav = PAGES.map((x) => {
    const v = x.key === 'overview' ? used : cats[x.key] || 0;
    return `<a href="${x.key}.html" class="${x.key === key ? 'on' : ''}" style="--c:${x.color}"><i></i>${x.label}<span>${fmtTok(v)}</span></a>`;
  }).join('');
  const stack = PAGES.slice(1).map((x) => `<div title="${x.label} ${fmtTok(cats[x.key] || 0)}" style="width:${((cats[x.key] || 0) / size) * 100}%;background:${x.color}"></div>`).join('');
  let body;
  if (key === 'overview') {
    body = `<div class="cards">${PAGES.slice(1).map((x) => `<a class="card" href="${x.key}.html" style="--c:${x.color}">${x.label}<b>${fmtTok(cats[x.key] || 0)}</b><span>${used ? Math.round(((cats[x.key] || 0) / used) * 100) : 0}% of used · ${((cats[x.key] || 0) / size * 100).toFixed(1)}% of window</span></a>`).join('')}</div>`;
  } else {
    const items = itemsFor(key, det, cpt);
    const max = Math.max(1, ...items.map((i) => i.tokens));
    const note = key === 'messages' && det.historyTotal > det.history.length ? ` · showing last ${det.history.length} of ${det.historyTotal}` : '';
    body = `<div class="tools"><input id="q" placeholder="Filter ${esc(p.label.toLowerCase())}…" autofocus><span class="count" id="cnt">${items.length} items${note}</span></div>` +
      (items.length ? items.map((i) => `<details class="${i.muted ? 'muted' : ''} ${i.role ? 'role-' + i.role : ''}" style="--c:${p.color}" data-q="${esc((i.name + ' ' + (i.sub || '') + ' ' + (i.body || '')).toLowerCase().slice(0, 4000))}">` +
        `<summary><span class="n">${esc(i.name)}</span><span class="t">~${fmtTok(i.tokens)}</span>${i.sub ? `<span class="s">${esc(i.sub)}</span>` : ''}<span class="b"><div style="width:${(i.tokens / max) * 100}%"></div></span></summary>` +
        (i.body ? `<pre>${esc(i.body)}${i.extra ? '\n\n— input schema —\n' + esc(i.extra) : ''}</pre>` : '') + `</details>`).join('')
        : `<div class="empty">Nothing recorded for this category in the transcript yet.</div>`);
  }
  return `<!doctype html><html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(p.label)} · Context</title><style>${CSS}</style></head><body><div class="wrap">
<header><h1>${esc(p.label)} <span style="color:var(--muted);font-weight:400">— ${fmtTok(key === 'overview' ? used : cats[key] || 0)} tokens</span></h1>
<div class="meta">${esc(meta.session || meta.sessionId)} · ${esc(meta.model)} · context ${fmtTok(used)} / ${fmtTok(size)} (${Math.round((used / size) * 100)}%) · updated ${new Date().toLocaleTimeString('uk-UA')} · token counts are estimates</div></header>
<div class="stack">${stack}</div><div class="legend"><span>${fmtTok(used)} used</span><span>${fmtTok(Math.max(0, size - used))} free</span></div>
<nav>${nav}</nav>${body}</div>
<script>const q=document.getElementById('q');if(q){const all=[...document.querySelectorAll('details')],c=document.getElementById('cnt'),base=c.textContent;
q.addEventListener('input',()=>{const v=q.value.trim().toLowerCase();let n=0;all.forEach(d=>{const ok=!v||d.dataset.q.includes(v);d.style.display=ok?'':'none';n+=ok});c.textContent=v?n+' matching':base;});}</script>
</body></html>`;
}

function writePages(dir, det, ctx) {
  fs.mkdirSync(dir, { recursive: true });
  for (const p of PAGES) fs.writeFileSync(path.join(dir, `${p.key}.html`), page(p.key, det, ctx));
}

// Remove page dirs / caches of sessions untouched for a week.
function cleanup(dirs, maxAgeMs = 7 * 864e5) {
  const now = Date.now();
  for (const d of dirs) {
    let names = [];
    try { names = fs.readdirSync(d); } catch { continue; }
    for (const n of names) {
      const f = path.join(d, n);
      try { if (now - fs.statSync(f).mtimeMs > maxAgeMs) fs.rmSync(f, { recursive: true, force: true }); } catch {}
    }
  }
}

module.exports = { freshDetails, ingestDetail, writePages, cleanup };
