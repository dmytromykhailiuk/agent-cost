#!/usr/bin/env node
// Claude Code status-line dashboard: model, plan limits + reset countdowns, context window breakdown.
// Context breakdown is estimated from the session transcript (prompt_snapshot + attachments),
// calibrated against the real current context token count.
const fs = require('fs');
const path = require('path');
const os = require('os');

const { freshDetails, ingestDetail, writePages, cleanup } = require('./details');

const CACHE_DIR = path.join(os.homedir(), '.claude', 'statusline', 'cache');
const PAGES_DIR = path.join(os.homedir(), '.claude', 'statusline', 'details');
const CHARS_PER_TOKEN = 3.8; // fallback before the first API response

let input = '';
process.stdin.on('data', (d) => (input += d));
process.stdin.on('end', () => {
  let data = {};
  try { data = JSON.parse(input); } catch {}
  try { process.stdout.write(render(data)); } catch (e) { process.stdout.write(`statusline error: ${e.message}`); }
});

// ---------- styling ----------
const rgb = (r, g, b) => `\x1b[38;2;${r};${g};${b}m`;
const R = '\x1b[0m', B = '\x1b[1m';
const DIM = rgb(110, 110, 120), TEXT = rgb(200, 200, 210), SEP = `${DIM} │ ${R}`;
const GREEN = rgb(80, 200, 120), YELLOW = rgb(235, 190, 60), RED = rgb(240, 85, 85), ACCENT = rgb(120, 170, 255);
const levelColor = (usedPct) => (usedPct >= 85 ? RED : usedPct >= 60 ? YELLOW : GREEN);

const CATS = [
  { key: 'system', label: 'System', color: rgb(150, 120, 255) },
  { key: 'tools', label: 'Tools', color: rgb(90, 160, 255) },
  { key: 'skills', label: 'Skills', color: rgb(60, 200, 200) },
  { key: 'mcp', label: 'MCP', color: rgb(240, 150, 80) },
  { key: 'agents', label: 'Agents', color: rgb(230, 110, 170) },
  { key: 'memory', label: 'Memory/env', color: rgb(170, 170, 90) },
  { key: 'messages', label: 'History', color: rgb(90, 210, 120) },
];
const FREE = rgb(60, 60, 68);

const PROFILE_COLORS = {
  green: GREEN, magenta: rgb(220, 110, 230), orange: rgb(245, 150, 70), blue: ACCENT,
  cyan: rgb(80, 210, 220), yellow: YELLOW, red: RED,
};

function claudeProfile() {
  const raw = process.env.CLAUDE_CONFIG_DIR;
  const real = (p) => { try { return fs.realpathSync(p); } catch { return path.resolve(p); } };
  let profiles = {};
  try { profiles = JSON.parse(fs.readFileSync(path.join(os.homedir(), '.claude', 'shared', 'claude-profiles.json'), 'utf8')).profiles || {}; } catch {}
  if (!raw || real(raw) === real(path.join(os.homedir(), '.claude'))) return { name: 'personal', color: profiles.personal?.color || 'green' };
  const cur = real(raw.replace(/^~(?=\/|$)/, os.homedir()));
  for (const [name, p] of Object.entries(profiles)) {
    if (p?.config_dir && real(p.config_dir) === cur) return { name, color: p.color || 'magenta' };
  }
  const base = path.basename(cur);
  return { name: base.startsWith('.claude-') ? base.slice(8) : base, color: 'magenta' };
}

const fmtTok = (n) => (n >= 1e6 ? `${+(n / 1e6).toFixed(n % 1e6 ? 1 : 0)}M` : n >= 1000 ? `${(n / 1000).toFixed(n >= 1e5 ? 0 : 1)}k` : `${Math.round(n)}`);

function countdown(resetsAt) {
  if (!resetsAt) return null;
  let s = resetsAt - Math.floor(Date.now() / 1000);
  if (s <= 0) return 'now';
  const d = Math.floor(s / 86400); s %= 86400;
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
}
const clock = (ts, withDay) => {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit' });
  return withDay ? `${d.toLocaleDateString('uk-UA', { weekday: 'short' })} ${t}` : t;
};

function bar(usedPct, width) {
  const p = Math.max(0, Math.min(100, usedPct));
  const filled = p > 0 ? Math.max(1, Math.round((p / 100) * width)) : 0;
  return `${levelColor(p)}${'━'.repeat(filled)}${FREE}${'━'.repeat(width - filled)}${R}`;
}

// ---------- layout ----------
// Claude Code passes the terminal width in $COLUMNS; keep a small margin for its own padding.
const COLS = Math.max(40, (parseInt(process.env.COLUMNS, 10) || 120) - 4);
const visibleLen = (s) => [...s.replace(/\x1b\[[0-9;]*m/g, '').replace(/\x1b\]8;;[^\x1b]*\x1b\\/g, '')].length;
// OSC 8 hyperlink (Cmd+click in terminals that support it).
const link = (url, text) => `\x1b]8;;${url}\x1b\\${text}\x1b]8;;\x1b\\`;

// Join blocks with `sep`, moving whole blocks to a continuation row when the row would overflow.
function wrap(blocks, sep, firstIndent = '', indent = '   ') {
  const rows = [];
  let row = firstIndent, rowLen = visibleLen(firstIndent), empty = true;
  for (const b of blocks) {
    const bl = visibleLen(b), sl = visibleLen(sep);
    if (!empty && rowLen + sl + bl > COLS) {
      rows.push(row);
      row = indent; rowLen = indent.length; empty = true;
    }
    row += (empty ? '' : sep) + b;
    rowLen += (empty ? 0 : sl) + bl;
    empty = false;
  }
  rows.push(row);
  return rows.join('\n');
}

// ---------- render ----------
function render(d) {
  const lines = [];

  // Line 1: model + limits
  const parts = [];
  const effort = d.effort?.level ? ` ${DIM}·${R} ${TEXT}${d.effort.level}${R}` : '';
  const think = d.thinking?.enabled ? ` ${DIM}· thinking${R}` : '';
  const fast = d.fast_mode ? ` ${YELLOW}⚡fast${R}` : '';
  const prof = claudeProfile();
  parts.push(`${B}${PROFILE_COLORS[prof.color] || PROFILE_COLORS.magenta}[${prof.name}]${R} ${B}${ACCENT}◆ ${d.model?.display_name ?? 'Claude'}${R}${effort}${think}${fast}`);

  const rl = d.rate_limits ?? {};
  const limit = (label, w, withDay) => {
    if (w?.used_percentage == null) return `${DIM}${label} n/a${R}`;
    const used = w.used_percentage, left = Math.max(0, 100 - used);
    const cd = countdown(w.resets_at);
    const when = cd ? ` ${DIM}⟳${R} ${TEXT}${cd}${R} ${DIM}(${clock(w.resets_at, withDay)})${R}` : '';
    return `${DIM}${label}${R} ${bar(used, 10)} ${levelColor(used)}${B}${left}%${R}${DIM} left${R}${when}`;
  };
  parts.push(limit('5h', rl.five_hour, false));
  parts.push(limit('7d', rl.seven_day, true));
  if (rl.spend_limit?.used_percentage != null) parts.push(limit('$', rl.spend_limit, true));
  if (d.cost?.total_cost_usd != null) parts.push(`${DIM}$${d.cost.total_cost_usd.toFixed(2)}${R}`);
  lines.push(wrap(parts, SEP));

  // Line 2 + 3: context window
  const cw = d.context_window ?? {};
  const size = cw.context_window_size || 200000;
  const cu = cw.current_usage;
  const used = cu ? (cu.input_tokens || 0) + (cu.cache_creation_input_tokens || 0) + (cu.cache_read_input_tokens || 0) : null;
  const breakdown = estimateBreakdown(d, used);
  const total = used ?? breakdown.total;
  const usedPct = cw.used_percentage ?? (total / size) * 100;

  // Bar shrinks on narrow terminals so the ctx block itself still fits on one row.
  const W = Math.max(10, Math.min(50, COLS - 45));
  let segs = '', acc = 0, accCells = 0;
  for (const c of CATS) {
    const v = breakdown.cats[c.key] || 0;
    if (!v) continue;
    acc += v;
    const cells = Math.max(1, Math.round((acc / size) * W)) - accCells;
    if (cells <= 0) continue;
    segs += c.color + '█'.repeat(cells);
    accCells += cells;
  }
  segs += FREE + '░'.repeat(Math.max(0, W - accCells)) + R;
  const ctxParts = [
    (breakdown.pagesDir ? link(`file://${breakdown.pagesDir}/overview.html`, `${DIM}ctx${R}`) : `${DIM}ctx${R}`) + ` ${segs} ${levelColor(usedPct)}${B}${Math.round(usedPct)}%${R} ${TEXT}${fmtTok(total)}${R}${DIM}/${fmtTok(size)}${R}`,
    `${GREEN}${fmtTok(Math.max(0, size - total))}${R}${DIM} free${R}`,
  ];
  if (d.prompt_cache?.warm && d.prompt_cache.expires_at) ctxParts.push(`${DIM}cache ✓ ${countdown(d.prompt_cache.expires_at)}${R}`);
  lines.push(wrap(ctxParts, SEP));

  const legend = CATS.filter((c) => breakdown.cats[c.key] > 0).map((c) => {
    const v = breakdown.cats[c.key];
    const share = total ? Math.round((v / total) * 100) : 0;
    const item = `${c.color}■${R} ${TEXT}${c.label}${R} ${DIM}${fmtTok(v)} · ${share}%${R}`;
    return breakdown.pagesDir ? link(`file://${breakdown.pagesDir}/${c.key}.html`, item) : item;
  });
  if (legend.length) {
    legend[0] = `${DIM}${breakdown.estimated ? '≈ ' : ''}${R}${legend[0]}`;
    lines.push(wrap(legend, '  ', '   '));
  }

  return lines.join('\n');
}

// ---------- context breakdown (incremental transcript parse, cached per session) ----------
function freshState() {
  return {
    offset: 0, system: 0, tools: 0, skills: 0, mcp: 0, agents: 0, memory: 0,
    msgChars: 0,
  };
}

const strLen = (v) => (v == null ? 0 : typeof v === 'string' ? v.length : JSON.stringify(v).length);
const renderedLen = (o) => (Array.isArray(o.rendered) ? o.rendered.reduce((a, r) => a + strLen(r.content), 0) : 0);

function ingest(st, o) {
  if (o.type === 'attachment') {
    const a = o.attachment || {};
    const len = renderedLen(o);
    switch (a.type) {
      case 'prompt_snapshot':
        if (a.systemPrompt) st.system = strLen(a.systemPrompt);
        if (Array.isArray(a.tools)) st.tools = strLen(a.tools);
        break;
      case 'skill_listing': st.skills = a.isInitial ? len : st.skills + len; break;
      case 'mcp_instructions_delta': st.mcp += len; break;
      case 'deferred_tools_delta': st.tools += len; break;
      case 'agent_listing_delta': st.agents = a.isInitial ? len : st.agents + len; break;
      case 'total_tokens_reminder': st.msgChars += len; break;
      default: st.memory += len;
    }
  } else if (o.type === 'user' || o.type === 'assistant') {
    st.msgChars += strLen(o.message?.content);
  } else if (o.type === 'system' && o.subtype === 'compact_boundary') {
    st.msgChars = 0;
  }
}

// Returns { st, det, changed }; `det` (per-item details for the HTML pages) is only loaded when
// the transcript grew, so the common no-change refresh stays cheap.
function loadState(d) {
  const tp = d.transcript_path;
  if (!tp || !d.session_id) return { st: freshState(), det: null, changed: false };
  const cacheFile = path.join(CACHE_DIR, `${d.session_id}.json`);
  const detFile = path.join(CACHE_DIR, `${d.session_id}.details.json`);
  let st, det = null, changed = false;
  try { st = JSON.parse(fs.readFileSync(cacheFile, 'utf8')); } catch { st = freshState(); }
  let size;
  try { size = fs.statSync(tp).size; } catch { return { st, det, changed }; }
  if (size < st.offset) st = freshState(); // transcript rewritten
  if (size > st.offset) {
    try { det = JSON.parse(fs.readFileSync(detFile, 'utf8')); } catch {}
    if (!det) { st = freshState(); det = freshDetails(); } // (re)build details from the start

    const fd = fs.openSync(tp, 'r');
    const buf = Buffer.alloc(size - st.offset);
    fs.readSync(fd, buf, 0, buf.length, st.offset);
    fs.closeSync(fd);
    const lastNl = buf.lastIndexOf(0x0a);
    if (lastNl >= 0) {
      for (const line of buf.subarray(0, lastNl).toString('utf8').split('\n')) {
        if (!line) continue;
        let o;
        try { o = JSON.parse(line); } catch { continue; }
        ingest(st, o);
        ingestDetail(det, o);
      }
      st.offset += lastNl + 1;
      changed = true;
      try {
        fs.mkdirSync(CACHE_DIR, { recursive: true });
        fs.writeFileSync(cacheFile, JSON.stringify(st));
        fs.writeFileSync(detFile, JSON.stringify(det));
      } catch {}
    }
  }
  return { st, det, changed };
}

function estimateBreakdown(d, usedTokens) {
  const { st, det, changed } = loadState(d);
  const allChars = st.system + st.tools + st.skills + st.mcp + st.agents + st.memory + st.msgChars;
  // Calibrate chars→tokens against the real current context size (falls back to a fixed ratio).
  const charsPerTok = usedTokens && allChars ? Math.max(2, Math.min(6, allChars / usedTokens)) : CHARS_PER_TOKEN;
  const cats = {};
  let fixed = 0;
  for (const k of ['system', 'tools', 'skills', 'mcp', 'agents', 'memory']) {
    cats[k] = Math.round(st[k] / charsPerTok);
    fixed += cats[k];
  }
  if (usedTokens != null && fixed > usedTokens) { // keep the sum honest
    const f = usedTokens / fixed;
    fixed = 0;
    for (const k in cats) { cats[k] = Math.round(cats[k] * f); fixed += cats[k]; }
  }
  cats.messages = usedTokens != null ? Math.max(0, usedTokens - fixed) : Math.round(st.msgChars / charsPerTok);
  const total = fixed + cats.messages;

  let pagesDir = null;
  if (d.session_id) {
    pagesDir = path.join(PAGES_DIR, d.session_id);
    try {
      if (det && changed) {
        const fresh = !fs.existsSync(pagesDir);
        writePages(pagesDir, det, {
          cats, used: total, size: d.context_window?.context_window_size || 200000, cpt: charsPerTok,
          meta: { session: d.session_name, sessionId: d.session_id, model: d.model?.display_name ?? '' },
        });
        if (fresh) cleanup([PAGES_DIR, CACHE_DIR]);
      }
      if (!fs.existsSync(pagesDir)) pagesDir = null;
    } catch { pagesDir = null; }
  }
  return { cats, total, estimated: true, pagesDir };
}
