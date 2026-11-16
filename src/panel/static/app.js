'use strict';
/* Murad Bot panel: one small app, no build step. Data from /api/*, actions go through the bot's command queue. */
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v))) ? '–' : Number(v).toLocaleString('az-AZ', { minimumFractionDigits: d, maximumFractionDigits: d });
const money = (v, signed = false) => {
  if (v === null || v === undefined) return '–';
  const n = Number(v); const s = '$' + num(Math.abs(n));
  return signed ? (n > 0 ? '▲ +' : n < 0 ? '▼ −' : '') + s : (n < 0 ? '−' : '') + s;
};
const tone = (v) => (Number(v) > 0 ? 'up' : Number(v) < 0 ? 'down' : '');
const R = (v) => (v === null || v === undefined) ? '–' : (Number(v) >= 0 ? '+' : '−') + Math.abs(Number(v)).toFixed(2) + 'R';
const ago = (iso) => {
  if (!iso) return '–'; const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return Math.max(1, Math.round(s)) + ' san əvvəl'; if (s < 3600) return Math.round(s / 60) + ' dəq əvvəl';
  if (s < 86400) return Math.round(s / 3600) + ' saat əvvəl'; return Math.round(s / 86400) + ' gün əvvəl';
};
const localTime = (iso, withDate) => iso ? new Date(iso).toLocaleString('az-AZ', withDate ? { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Baku' } : { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Baku' }) : '–';
const dirLabel = (d) => (d === 'buy' ? 'AL' : 'SAT');

const ICON = {
  home: '<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/>', pos: '<path d="M3 17l5-6 4 3 7-9"/>',
  opp: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/>', hist: '<path d="M12 7v5l3 2"/><circle cx="12" cy="12" r="9"/>',
  strat: '<path d="M4 19V9M10 19V5M16 19v-7M22 19H2"/>', ctrl: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  sys: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8"/>', pause: '<path d="M9 5v14M15 5v14"/>', play: '<path d="M7 5l12 7-12 7z"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>', refresh: '<path d="M20 12a8 8 0 11-3-6.2"/><path d="M20 4v5h-5"/>', out: '<path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10"/>', fan: '<path d="M3 12h5M8 12l13-7M8 12h13M8 12l13 7"/>'
};
const icon = (k, s = 18) => `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[k]}</svg>`;

const ROUTES = [
  ['merkez', 'Komanda mərkəzi', 'home', 'Mərkəz'], ['movqeler', 'Mövqelər', 'pos', 'Mövqe'], ['fursetler', 'Fürsətlər', 'opp', 'Fürsət'],
  ['simulyasiya', 'Simulyasiya', 'fan', null], ['jurnal', 'Jurnal', 'hist', null], ['strategiyalar', 'Strategiyalar', 'strat', null], ['idare', 'İdarəetmə', 'ctrl', 'İdarə'], ['saglamliq', 'Sağlamlıq', 'sys', null]
];
const state = { route: 'merkez', timer: null, period: '1A', overview: null, opp: { picked: null, risk: 1, managed: true, ack: false }, settingsDraft: {}, journalDays: 30, dayOffset: 0, check: null, sim: null, simPage: { symbol: null, side: 'sell', scale: 'Normal', minutes: 30, result: null, busy: false } };

// ------------------------------------------------------------- api, toast, modal
async function api(path, opts = {}) {
  const init = { credentials: 'same-origin', headers: {} };
  if (opts.body !== undefined) { init.method = 'POST'; init.headers = { 'Content-Type': 'application/json', 'X-Panel': '1' }; init.body = JSON.stringify(opts.body); }
  const res = await fetch(path, init);
  if (res.status === 401 && !path.startsWith('/api/login')) { renderLogin(); throw new Error('login'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || ('Xəta ' + res.status));
  return data;
}
function toast(text, bad = false) {
  const el = document.createElement('div'); el.textContent = text; if (bad) el.className = 'bad';
  $('#toast').appendChild(el); setTimeout(() => el.remove(), 4500);
}
function ask(title, text, ok = 'Bəli', danger = false) {
  return new Promise((resolve) => {
    const m = $('#modal');
    m.innerHTML = `<div class="box" role="dialog" aria-modal="true" aria-labelledby="mt"><h2 id="mt">${esc(title)}</h2><div class="muted" style="font-size:14px;line-height:1.5">${text}</div>
      <div class="row" style="justify-content:flex-end"><button class="btn" data-a="0">Ləğv et</button><button class="btn ${danger ? 'danger' : 'primary'}" data-a="1">${esc(ok)}</button></div></div>`;
    m.hidden = false; $('[data-a="1"]', m).focus();
    m.onclick = (e) => { const a = e.target.closest('[data-a]'); if (a || e.target === m) { m.hidden = true; m.innerHTML = ''; resolve(a ? a.dataset.a === '1' : false); } };
  });
}
async function command(cmd, doneText) {
  try {
    const { id } = await api('/api/command', { body: { command: cmd } });
    toast('Əmr bota göndərildi…');
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      const c = await api('/api/command/' + id);
      if (c.status === 'done') { toast(doneText || 'Hazırdır'); refresh(); return true; }
      if (c.status === 'failed' || c.status === 'expired') { toast('Alınmadı: ' + (c.message || c.status), true); return false; }
    }
    toast('Bot hələ cavab vermədi; bir az sonra yoxlayın.', true);
  } catch (e) { if (e.message !== 'login') toast(e.message, true); }
  return false;
}

// ------------------------------------------------------------- charts
function scale(values, lo, hi, h, pad) { const span = (hi - lo) || 1; return (v) => pad + (hi - v) / span * (h - 2 * pad); }
function lineSvg(values, { w = 760, h = 220, stroke = '#4338CA', fill = '#4338CA', fillOpacity = 0.08, label = 'Qrafik' } = {}) {
  const vals = values.filter((v) => v !== null && v !== undefined && !Number.isNaN(Number(v))).map(Number);
  if (vals.length < 2) return `<div class="empty">Qrafik üçün hələ məlumat azdır</div>`;
  const lo = Math.min(...vals), hi = Math.max(...vals), y = scale(vals, lo, hi, h, 8);
  const pts = vals.map((v, i) => [8 + i * (w - 16) / (vals.length - 1), y(v)]);
  const d = 'M' + pts.map((p) => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' L');
  return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(label)}" style="width:100%;height:auto;display:block">
    <path d="${d} L${pts[pts.length - 1][0].toFixed(1)},${h} L8,${h} Z" fill="${fill}" fill-opacity="${fillOpacity}"/>
    <path d="${d}" fill="none" stroke="${stroke}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}
const fmtP = (v, d) => (v === null || v === undefined) ? '–' : Number(v).toFixed(d ?? (Math.abs(v) >= 1000 ? 1 : 2));
// Right-edge price tags that never overlap: sorted by height, pushed apart by at least `gap` px.
function priceTags(tags, y, w, h, gap = 16) {
  const list = tags.filter((t) => t.v !== null && t.v !== undefined && !Number.isNaN(Number(t.v))).map((t) => ({ ...t, y: y(Number(t.v)) })).sort((a, b) => a.y - b.y);
  for (let i = 1; i < list.length; i++) if (list[i].y - list[i - 1].y < gap) list[i].y = list[i - 1].y + gap;
  const over = list.length ? list[list.length - 1].y - (h - 9) : 0;
  if (over > 0) list.forEach((t) => { t.y -= over; });
  for (let i = list.length - 2; i >= 0; i--) if (list[i + 1].y - list[i].y < gap) list[i].y = list[i + 1].y - gap;
  return list.map((t) => `<rect x="${w - 62}" y="${(t.y - 8).toFixed(1)}" width="62" height="16" rx="5" fill="${t.color}"/><text x="${w - 31}" y="${(t.y + 4).toFixed(1)}" font-size="10" text-anchor="middle" fill="#fff" font-family="JetBrains Mono, monospace">${esc(t.text)}</text>`).join('');
}
function candlesInto(bars, x0, x1, y) {
  if (!bars.length) return '';
  const step = (x1 - x0) / bars.length, bw = Math.max(1.5, step * 0.6);
  return bars.map((b, i) => { const x = x0 + i * step + step / 2; const up = b.c >= b.o; const col = up ? '#0F7A4B' : '#B4232A';
    const top = y(Math.max(b.o, b.c)), bot = y(Math.min(b.o, b.c));
    return `<line x1="${x.toFixed(1)}" y1="${y(b.h).toFixed(1)}" x2="${x.toFixed(1)}" y2="${y(b.l).toFixed(1)}" stroke="${col}" stroke-width="1"/><rect x="${(x - bw / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, bot - top).toFixed(1)}" fill="${col}" rx="1"/>`; }).join('');
}
function zone(x0, x1, y, a, b, color, opacity) {
  if (a === null || b === null || a === undefined || b === undefined) return '';
  const top = Math.min(y(a), y(b)), hgt = Math.abs(y(a) - y(b));
  return `<rect x="${x0.toFixed(1)}" y="${top.toFixed(1)}" width="${Math.max(0, x1 - x0).toFixed(1)}" height="${hgt.toFixed(1)}" fill="${color}" fill-opacity="${opacity}"/>`;
}
const hline = (x0, x1, y, color, dash, width = 1.2) => `<line x1="${x0}" y1="${y.toFixed(1)}" x2="${x1}" y2="${y.toFixed(1)}" stroke="${color}" stroke-width="${width}" ${dash ? 'stroke-dasharray="5 4"' : ''}/>`;
function positionChart(p) {
  const w = 360, h = 170, right = 66, plotW = w - right;
  const bars = (p.bars || []).filter((b) => b && b.h != null && b.l != null);
  const entry = Number(p.open_price), sl = p.sl ? Number(p.sl) : null, tp = p.tp ? Number(p.tp) : null;
  const cur = p.current != null ? Number(p.current) : (bars.length ? bars[bars.length - 1].c : entry);
  const vals = bars.flatMap((b) => [b.h, b.l]).concat([entry, cur, sl, tp].filter((v) => v !== null && !Number.isNaN(v)));
  if (vals.length < 2) return '';
  let lo = Math.min(...vals), hi = Math.max(...vals); const padv = (hi - lo || Math.abs(hi) * 0.001 || 1) * 0.06; lo -= padv; hi += padv;
  const y = scale(null, lo, hi, h, 6);
  const opened = p.open_time ? new Date(p.open_time).getTime() : null;
  let idx = opened && bars.length ? bars.findIndex((b) => new Date(b.t).getTime() >= opened - 30000) : -1; if (idx < 0) idx = 0;
  const step = plotW / Math.max(1, bars.length); const ex = idx * step + step / 2;
  const tags = [{ v: tp, text: 'Hədəf', color: '#0F7A4B' }, { v: sl, text: 'Stop', color: '#B4232A' }, { v: entry, text: 'Giriş', color: '#6B6F80' }, { v: cur, text: fmtP(cur), color: '#14141A' }];
  return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(p.symbol)}: şamlar, giriş, stop və hədəf zonaları" style="width:100%;height:auto;display:block">
    <rect width="${w}" height="${h}" rx="12" fill="#FBFAF7"/>
    ${zone(ex, plotW, y, entry, tp, '#0F7A4B', 0.09)}${zone(ex, plotW, y, entry, sl, '#B4232A', 0.08)}
    ${candlesInto(bars, 0, plotW, y)}
    ${tp !== null ? hline(0, plotW, y(tp), '#0F7A4B', true) : ''}${sl !== null ? hline(0, plotW, y(sl), '#B4232A', true) : ''}
    ${hline(0, plotW, y(entry), '#6B6F80', false, 1)}${hline(0, plotW, y(cur), '#14141A', true, 0.8)}
    <line x1="${ex.toFixed(1)}" y1="6" x2="${ex.toFixed(1)}" y2="${h - 6}" stroke="#6B6F80" stroke-width="1" stroke-dasharray="2 3"/>
    <circle cx="${ex.toFixed(1)}" cy="${y(entry).toFixed(1)}" r="4.5" fill="#fff" stroke="#14141A" stroke-width="2"/>
    ${priceTags(tags, y, w, h)}</svg>`;
}
function outlookHtml(o) {
  if (!o) return '';
  if (!o.used) return `<div class="muted" style="font-size:12px">Zaman maşını: ${esc(o.reason || 'hesablanmayıb')}</div>`;
  const worse = o.hold_r < o.now_r;
  return `<div class="outlook ${o.action === 'close' ? 'bad' : worse ? 'warn' : 'good'}"><b>Zaman maşını, indidən ${o.minutes_left} dəq:</b>
    <span class="up">hədəf ${pct(o.target)}</span> · <span class="down">stop ${pct(o.stop)}</span> · vaxt ${pct(o.time)}
    <div>Saxlamaq <b class="mono">${R(o.hold_r)}</b>, indi bağlamaq <b class="mono">${R(o.now_r)}</b> → ${o.action === 'close' ? (o.closed ? '<b>bot bağladı</b>' : '<b>bot bağlayır</b>') : esc(worse ? 'fərq kiçikdir, saxlanır' : 'saxlamaq daha yaxşıdır')}</div></div>`;
}
function positionProgress(p) {
  const entry = Number(p.open_price), sl = p.sl ? Number(p.sl) : null, tp = p.tp ? Number(p.tp) : null; const cur = p.current != null ? Number(p.current) : null;
  const sign = p.direction === 'buy' ? 1 : -1; const rd = p.risk_distance || (sl !== null ? Math.abs(entry - sl) : null);
  const r = cur !== null && rd ? (cur - entry) * sign / rd : null;
  const mins = p.open_time ? Math.max(0, Math.round((Date.now() - new Date(p.open_time).getTime()) / 60000)) : null;
  const time = mins === null ? '' : (p.max_hold_minutes ? `${mins}/${p.max_hold_minutes} dəq` : `${mins} dəq`);
  let bar = '';
  if (sl !== null && tp !== null && cur !== null && tp !== sl) {
    const clamp = (v) => Math.max(0, Math.min(1, v)); const f = clamp((cur - sl) / (tp - sl)), fe = clamp((entry - sl) / (tp - sl));
    bar = `<div class="prog" role="img" aria-label="Qiymət stop ilə hədəf arasında: ${Math.round(f * 100)}%">
      <span class="lbl down">Stop</span><div class="track"><i style="left:0;width:${(fe * 100).toFixed(1)}%;background:var(--down-soft)"></i><i style="left:${(fe * 100).toFixed(1)}%;width:${((1 - fe) * 100).toFixed(1)}%;background:var(--up-soft)"></i>
      <b class="entry" style="left:${(fe * 100).toFixed(1)}%"></b><b class="now" style="left:${(f * 100).toFixed(1)}%"></b></div><span class="lbl up">Hədəf</span></div>`;
  }
  return `${bar}<div class="row" style="justify-content:space-between;font-size:13px"><span class="mono ${tone(p.profit)}" style="font-weight:700">${money(p.profit, true)} · ${R(r)}</span><span class="muted mono">${esc(time)}</span></div>`;
}
// Time machine: candles until now, then the real paths that followed similar past moments, coloured by outcome.
function fanSvg(sim, candles, digits) {
  const w = 760, h = 300, right = 66, plotW = w - right; const split = candles && candles.length ? plotW * 0.3 : 0;
  const c = (candles || []).slice(-30); const fan = sim.fan || [];
  const all = []; fan.forEach((f) => f.p.forEach((v) => all.push(v))); all.sort((a, b) => a - b);
  const q = (k) => all.length ? all[Math.min(all.length - 1, Math.max(0, Math.round(k * (all.length - 1))))] : sim.entry;
  const vals = [q(0.03), q(0.97), sim.entry, sim.stop, sim.target].concat(c.flatMap((b) => [b.h, b.l]));
  let lo = Math.min(...vals), hi = Math.max(...vals); const padv = (hi - lo || 1) * 0.06; lo -= padv; hi += padv;
  const y = scale(null, lo, hi, h, 6); const cy = (v) => Math.max(2, Math.min(h - 2, y(v)));
  const n = fan.length ? fan[0].p.length - 1 : 1; const sx = (plotW - split) / n;
  const colour = { 1: ['#0F7A4B', 0.30], '-1': ['#B4232A', 0.26], 0: ['#6B6F80', 0.22] };
  const paths = fan.map((f) => { const [col, op] = colour[f.o] || colour[0];
    return `<path d="M${f.p.map((v, i) => (split + i * sx).toFixed(1) + ',' + cy(v).toFixed(1)).join(' L')}" fill="none" stroke="${col}" stroke-opacity="${op}" stroke-width="1"/>`; }).join('');
  const tags = [{ v: sim.target, text: 'Hədəf', color: '#0F7A4B' }, { v: sim.stop, text: 'Stop', color: '#B4232A' }, { v: sim.entry, text: fmtP(sim.entry, digits), color: '#14141A' }];
  return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Zaman maşını: ${fan.length} keçmiş yol, hədəfə, stopa və ya vaxt bitməsinə görə rənglənib" style="width:100%;height:auto;display:block">
    <rect width="${w}" height="${h}" rx="12" fill="#FBFAF7"/>
    ${zone(split, plotW, y, sim.entry, sim.target, '#0F7A4B', 0.07)}${zone(split, plotW, y, sim.entry, sim.stop, '#B4232A', 0.06)}
    ${candlesInto(c, 0, split, y)}${paths}
    ${hline(0, plotW, y(sim.target), '#0F7A4B', true)}${hline(0, plotW, y(sim.stop), '#B4232A', true)}${hline(0, plotW, y(sim.entry), '#14141A', false, 1)}
    <circle cx="${split.toFixed(1)}" cy="${y(sim.entry).toFixed(1)}" r="4.5" fill="#14141A"/>
    <text x="${(split + 6).toFixed(1)}" y="16" font-size="11" fill="#6B6F80" font-family="Manrope">indi → ${sim.minutes} dəq</text>
    ${priceTags(tags, y, w, h)}</svg>`;
}
const pct = (v) => (v === null || v === undefined) ? '–' : Math.round(v * 100) + '%';
function simResultHtml(sim, candles, opts = {}) {
  if (!sim) return '';
  if (sim.error) return `<div class="why">${esc(sim.error)}</div>`;
  const live = opts.liveAge != null ? `<span class="tag" style="background:var(--acc-soft);color:var(--acc)">canlı · ${Math.round(opts.liveAge)} san əvvəl</span> ` : '';
  const head = `<div class="row" style="justify-content:space-between;gap:8px"><div>${live}<b style="font-size:22px">${num(sim.analogs, 0)}</b> <span class="muted" style="font-size:13px">oxşar an · ${esc(sim.match || '')}${sim.years ? ` · ${sim.years[0]}–${sim.years[1]}` : ''}</span></div>
    <span class="tag" style="background:${sim.reliability === 'etibarlı' ? 'var(--up-soft)' : sim.reliability === 'etibarlı deyil' ? 'var(--down-soft)' : '#F2F0EB'};color:${sim.reliability === 'etibarlı' ? 'var(--up)' : sim.reliability === 'etibarlı deyil' ? 'var(--down)' : 'var(--muted)'}">${esc(sim.reliability || '')}</span></div>
    <div class="muted" style="font-size:12px">İndiki vəziyyət: saat ${sim.state?.hour_utc ?? '–'}:00 UTC · trend ${esc(sim.state?.trend || '–')} · volatillik ${esc(sim.state?.volatility || '–')}${opts.stale ? ' · <b style="color:var(--warn)">parametrlər dəyişib, yenidən simulyasiya edin</b>' : ''}</div>`;
  if (!sim.enough) return `${head}<div class="empty">Kifayət qədər oxşar an yoxdur (${sim.analogs}). Bu vəziyyətdə rəqəm göstərilmir.</div>`;
  const ch = sim.chosen;
  const cards = `<div class="simcards"><div class="up-box"><small>Hədəf əvvəl</small><b>${pct(ch.target)}</b></div><div class="down-box"><small>Stop əvvəl</small><b>${pct(ch.stop)}</b></div>
    <div><small>Vaxt bitdi</small><b>${pct(ch.time)}</b></div><div><small>Orta nəticə · uduş ${pct(ch.win)}</small><b class="${tone(ch.expectancy_r)}">${R(ch.expectancy_r)}</b></div></div>`;
  const best = sim.best || {};
  const grid = `<div class="tablebox"><table class="simgrid"><thead><tr><th>Stop \\ hədəf</th>${[1, 1.5, 2].map((r) => `<th class="num">${r}× stop</th>`).join('')}</tr></thead><tbody>
    ${sim.grid.map((row) => `<tr><th>${esc(row.name)} <small class="muted mono">${fmtP(row.stop_distance, opts.digits)}</small></th>${row.cells.map((cell) => {
      const e = cell.expectancy_r; const cls = e > 0.02 ? 'g' : e < -0.02 ? 'r' : 'n'; const isBest = best.stop === row.name && best.ratio === cell.ratio && best.expectancy_r > 0;
      return `<td><button class="cell ${cls}${isBest ? ' best' : ''}" data-simcell="${row.stop_distance}:${cell.target_distance}" title="Bu stop və hədəfi seç"><b>${R(e)}</b><small>uduş ${pct(cell.win)} · hədəf ${pct(cell.target)}</small></button></td>`; }).join('')}</tr>`).join('')}</tbody></table></div>
    <div class="muted" style="font-size:12px">R xərclərdən (spred, slippage) sonradır. Xanaya basın: həmin stop və hədəf əmr panelinə keçir.</div>`;
  return `${head}${fanSvg(sim, candles, opts.digits)}
    <div class="row" style="gap:14px;font-size:12px"><span><i class="lg" style="background:#0F7A4B"></i>əvvəl hədəfə çatdı</span><span><i class="lg" style="background:#B4232A"></i>əvvəl stopa düşdü</span><span><i class="lg" style="background:#6B6F80"></i>vaxt bitdi</span></div>
    ${cards}${grid}`;
}

// ------------------------------------------------------------- shell
function shell(content, opportunities = state.overview?.opportunities || 0, status = state.overview?.status) {
  const nav = ROUTES.map(([k, l, ic]) => `<a href="#/${k}" class="${state.route === k ? 'on' : ''}" ${state.route === k ? 'aria-current="page"' : ''}>${icon(ic)}${l}${k === 'fursetler' && opportunities ? `<span class="badge">${opportunities}</span>` : ''}</a>`).join('');
  const tabs = ROUTES.filter((r) => r[3]).map(([k, , ic, short]) => `<a href="#/${k}" class="${state.route === k ? 'on' : ''}">${icon(ic, 20)}${short}${k === 'fursetler' && opportunities ? `<span class="badge">${opportunities}</span>` : ''}</a>`).join('');
  const st = status || {};
  const alive = !st.stale && st.status === 'running';
  const real = st.account_type && st.account_type !== 'DEMO';
  return `<div class="shell"><aside class="rail" aria-label="Menyu">
      <div class="brand"><div class="avatar">M</div><div><b>Murad Bot</b><small>XM · ${esc(st.location || 'server')}</small></div></div>
      <nav class="nav" aria-label="Bölmələr">${nav}</nav>
      <div class="railcard"><div class="row" style="justify-content:space-between"><span class="muted" style="font-size:12px">Hesab</span>
        <span class="tag" style="background:${real ? 'var(--down-soft)' : 'var(--up-soft)'};color:${real ? 'var(--down)' : 'var(--up)'}">${esc(st.account_type || '–')}</span></div>
        <div class="row"><span class="dot" style="background:${alive ? '#16A34A' : '#F59E0B'}"></span><b>${alive ? (st.entries_paused ? 'İşləyir · girişlər saxlanıb' : 'Bot işləyir') : 'Bot cavab vermir'}</b></div>
        <div class="muted" style="font-size:12px">Ürək döyüntüsü ${st.heartbeat_seconds != null ? Math.round(st.heartbeat_seconds) + ' san əvvəl' : '–'}</div>
        <button class="btn" data-logout style="min-height:34px;font-size:12px">${icon('out', 14)}Çıxış</button></div>
    </aside><main class="main" id="main">${content}</main><nav class="tabbar" aria-label="Bölmələr">${tabs}</nav></div>`;
}
function paint(content, ov) {
  const scroll = window.scrollY; $('#app').innerHTML = shell(content, ov?.opportunities, ov?.status); window.scrollTo(0, scroll);
}

// ------------------------------------------------------------- pages
function tickerHtml(rows) {
  if (!rows || !rows.length) return '';
  return `<div class="ticker" role="region" aria-label="Canlı qiymətlər">${rows.map((t) => `<div><b>${esc(t.symbol)}</b><span class="mono" style="font-size:14px;font-weight:600">${num(t.price, t.price > 1000 ? 1 : 3)}</span>${t.change_pct != null ? `<span class="${tone(t.change_pct)}" style="font-size:12px;font-weight:700">${t.change_pct >= 0 ? '▲' : '▼'} ${Math.abs(t.change_pct).toFixed(2)}%</span>` : ''}</div>`).join('')}</div>`;
}
function positionCard(p) {
  const a = p.analysis || {}; const danger = Number((a.danger && a.danger.score) || 0);
  const worry = a.tone === 'bad' || a.tone === 'warn' || danger >= 3; const toneBg = worry ? 'var(--warn-soft)' : 'var(--acc-soft)'; const toneFg = worry ? '#7A3A06' : '#312E81';
  const think = a.plan || (p.managed ? 'Bot mövqeni izləyir.' : 'Əl ilə açılıb: yalnız stop və hədəf işləyir.');
  return `<article class="card pos"><div class="top"><div><div class="row"><span class="sym">${esc(p.symbol)}</span><span class="tag" style="background:${p.direction === 'buy' ? 'var(--acc-soft)' : 'var(--down-soft)'};color:${p.direction === 'buy' ? 'var(--acc)' : 'var(--down)'}">${dirLabel(p.direction)}</span>${p.manual ? '<span class="tag" style="background:#F2F0EB;color:var(--muted)">əl ilə</span>' : ''}</div>
      <div class="muted" style="font-size:12px;margin-top:2px">${esc(p.strategy)} · ${num(p.volume, 2)} lot · ${localTime(p.open_time)}-da açılıb</div></div>
      <div style="text-align:right"><div class="mono ${tone(p.profit)}" style="font-size:20px;font-weight:600;white-space:nowrap">${money(p.profit, true)}</div><div class="muted" style="font-size:11px">canlı</div></div></div>
    ${positionChart(p)}${positionProgress(p)}${outlookHtml(p.outlook)}
    <div class="row" style="flex-wrap:nowrap;align-items:stretch"><div class="think" style="background:${toneBg};color:${toneFg}">${esc(think)}</div>
      <div class="danger" role="img" aria-label="Təhlükə ${danger} / 5">${[0, 1, 2, 3, 4].map((i) => `<i style="${i < danger ? `background:${danger >= 3 ? 'var(--down)' : 'var(--acc)'}` : ''}"></i>`).join('')}</div></div>
    <div class="row"><button class="btn" style="flex:1" data-be="${p.ticket}" data-good="${p.current != null && (Number(p.current) - Number(p.open_price)) * (p.direction === 'buy' ? 1 : -1) > 0 ? 1 : 0}">Stopu girişə çək</button><button class="btn danger" style="flex:1" data-close="${p.ticket}" data-sym="${esc(p.symbol)}">Bağla</button></div></article>`;
}
function feedHtml(events) {
  if (!events?.length) return '<div class="empty">Hələ hadisə yoxdur</div>';
  return `<ul class="feed">${events.map((e) => `<li><span class="dot" style="background:${e.level === 'ERROR' ? 'var(--down)' : e.level === 'WARNING' ? 'var(--warn)' : 'var(--acc)'}"></span><div><div style="font-size:13px;line-height:1.45">${esc(e.text)}</div><small>${ago(e.time)}</small></div></li>`).join('')}</ul>`;
}
function dayParam() {
  if (!state.dayOffset) return '';
  const d = new Date(Date.now() + 4 * 3600000 + state.dayOffset * 86400000);      // Baku date
  return '?date=' + d.toISOString().slice(0, 10);
}
function todayHtml(t) {
  const label = state.dayOffset === 0 ? 'Bu günün əməliyyatları' : state.dayOffset === -1 ? 'Dünənki əməliyyatlar' : `${esc(t.date)} əməliyyatları`;
  const rows = (t.items || []).map((x) => `<tr><td class="mono">${esc(x.time || '–')}</td><td><b>${esc(x.symbol)}</b></td>
      <td><span class="tag" style="background:${x.direction === 'buy' ? 'var(--acc-soft)' : 'var(--down-soft)'};color:${x.direction === 'buy' ? 'var(--acc)' : 'var(--down)'}">${dirLabel(x.direction)}</span></td>
      <td>${esc(x.strategy)}</td><td>${x.manual ? '<span class="tag" style="background:#F2F0EB;color:var(--muted)">əl ilə</span>' : 'bot'}</td>
      <td class="num ${tone(x.net)}">${x.status === 'open' ? '<span class="muted">açıq</span>' : money(x.net, true)}</td><td class="num ${tone(x.r)}">${x.status === 'open' ? '' : R(x.r)}</td><td>${esc(x.close_reason || '')}</td></tr>`).join('');
  return `<section class="card" aria-label="${label}"><div class="row" style="justify-content:space-between"><h2>${label}</h2>
      <div class="seg light"><button data-dayshift="-1" aria-label="Əvvəlki gün">◀</button><button data-dayshift="0" class="${state.dayOffset === 0 ? 'on' : ''}">Bu gün</button><button data-dayshift="1" aria-label="Növbəti gün" ${state.dayOffset >= 0 ? 'hidden' : ''}>▶</button></div></div>
    <div class="minis" style="margin:10px 0;grid-template-columns:repeat(auto-fit,minmax(140px,1fr))"><div class="mini"><small>Bağlanan</small><b>${t.closed}</b></div><div class="mini"><small>Qazanan / uduzan</small><b><span class="up">${t.wins}</span> / <span class="down">${t.losses}</span></b></div>
      <div class="mini"><small>Nəticə</small><b class="${tone(t.net)}">${money(t.net, true)}</b></div><div class="mini"><small>Cəmi R</small><b class="${tone(t.r)}">${R(t.r)}</b></div></div>
    ${rows ? `<div class="tablebox" style="box-shadow:none;border:0;padding:0"><table><thead><tr><th>Saat</th><th>Alət</th><th></th><th>Strategiya</th><th>Kim</th><th class="num">Nəticə</th><th class="num">R</th><th>Səbəb</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="empty">Bu gün əməliyyat olmayıb</div>'}</section>`;
}
async function pageMerkez() {
  const [ov, today] = await Promise.all([api('/api/overview?period=' + encodeURIComponent(state.period)), api('/api/today' + dayParam()).catch(() => null)]); state.overview = ov;
  const st = ov.status; const lim = ov.limits;
  const head = `<div class="head"><h1>Komanda mərkəzi</h1><div class="row">
      <button class="btn" data-pause="${st.entries_paused ? 'resume' : 'pause'}">${icon(st.entries_paused ? 'play' : 'pause', 16)}<span class="label">${st.entries_paused ? 'Girişləri bərpa et' : 'Yeni girişləri saxla'}</span></button>
      <button class="btn" style="color:var(--down)" data-closeall>${icon('x', 16)}<span class="label">Hamısını bağla</span></button></div></div>`;
  const alert = st.stale ? `<div class="card" style="background:var(--down-soft);border-color:#F5C2C4;color:var(--down);font-weight:700">Bot ${st.heartbeat_seconds ? Math.round(st.heartbeat_seconds / 60) + ' dəqiqədir' : ''} cavab vermir. İlişmə keşikçisi onu avtomatik yenidən başladacaq.</div>` : '';
  const equity = `<section class="card dark span2" aria-label="Kapital" style="display:flex;flex-direction:column;gap:16px">
      <div class="row" style="justify-content:space-between;align-items:flex-start;gap:16px"><div><div style="font-size:13px;color:#B8B9C6">Kapital</div><div class="big-money">${money(ov.equity)}</div><div style="font-size:12px;color:#B8B9C6">Balans ${money(ov.balance)}</div></div>
      <div class="kpis"><div><small>Bu gün</small><span style="color:${ov.today.pnl >= 0 ? '#6EE7A8' : '#FF8A8F'}">${money(ov.today.pnl, true)}</span></div>
        <div><small>Bu həftə</small><span style="color:${ov.week.pnl >= 0 ? '#6EE7A8' : '#FF8A8F'}">${money(ov.week.pnl, true)}</span></div>
        <div><small>Uduş faizi (30 g)</small><span>${Math.round((ov.month.win_rate || 0) * 100)}%</span></div></div></div>
      ${lineSvg(ov.curve.map((c) => c.v), { stroke: '#A5ABFF', fill: '#8B93FF', fillOpacity: 0.14, label: 'Kapital əyrisi' })}
      <div class="seg">${['1G', '1H', '1A', 'Hamısı'].map((p) => `<button data-period="${p}" class="${state.period === p ? 'on' : ''}">${p}</button>`).join('')}</div></section>`;
  const limits = `<section class="card" aria-label="Bugünkü vəziyyət" style="display:flex;flex-direction:column;gap:16px"><h2>Bugünkü vəziyyət</h2>
      <div><div class="row" style="justify-content:space-between;font-size:13px"><span>Gündəlik itki limiti</span><span class="mono">${num(lim.daily_used_pct, 1)}% / ${num(lim.daily_pct, 0)}%</span></div><div class="bar"><i style="width:${Math.min(100, lim.daily_used_pct / (lim.daily_pct || 1) * 100)}%;background:var(--warn)"></i></div></div>
      <div><div class="row" style="justify-content:space-between;font-size:13px"><span>Həftəlik limit</span><span class="mono">${num(lim.weekly_used_pct, 1)}% / ${num(lim.weekly_pct, 0)}%</span></div><div class="bar"><i style="width:${Math.min(100, lim.weekly_used_pct / (lim.weekly_pct || 1) * 100)}%;background:var(--acc)"></i></div></div>
      <div class="minis"><div class="mini"><small>Ticarət bu gün</small><b>${ov.today.trades}</b></div><div class="mini"><small>Risk / ticarət</small><b>${num(ov.risk_pct, 1)}%</b></div></div>
      <a href="#/idare" style="font-size:13px;font-weight:700;text-decoration:none">Risk və qaydaları dəyiş →</a></section>`;
  const positions = ov.positions.length ? ov.positions.map(positionCard).join('') : `<div class="card"><h2>Açıq mövqelər</h2><div class="empty">Hazırda açıq mövqe yoxdur</div></div>`;
  const oppCard = `<section class="card" aria-label="Fürsətlər"><div class="row" style="justify-content:space-between"><h2>Fürsətlər</h2><a href="#/fursetler" style="font-size:13px;font-weight:700;text-decoration:none">Hamısı →</a></div>
      <p class="muted" style="font-size:13px;line-height:1.5;margin:8px 0 0">${ov.opportunities ? `Son 15 dəqiqədə botun açmadığı <b style="color:var(--ink)">${ov.opportunities}</b> siqnal var.` : 'Hazırda gözləyən fürsət yoxdur.'}</p>
      ${ov.opportunities ? '<a class="btn ink" href="#/fursetler" style="margin-top:12px;text-decoration:none;width:100%">Fürsətlərə bax</a>' : ''}</section>`;
  paint(`${head}${alert}${tickerHtml(ov.ticker)}<div class="grid">${equity}${limits}</div>
    <div class="grid">${positions}${oppCard}</div>
    ${today ? todayHtml(today) : ''}
    <section class="card" aria-label="Botun gündəliyi"><div class="row" style="justify-content:space-between"><h2>Botun gündəliyi</h2><span class="muted" style="font-size:12px">Vacib hadisələr Telegram-a da gedir</span></div>${feedHtml(ov.events)}</section>`, ov);
}
async function pageMovqeler() {
  const data = await api('/api/positions');
  const cards = data.positions.length ? `<div class="grid">${data.positions.map(positionCard).join('')}</div>` : '<div class="card"><div class="empty">Hazırda açıq mövqe yoxdur</div></div>';
  paint(`<div class="head"><div><h1>Mövqelər</h1><p class="lead">Qrafik və botun fikri hər 2 saniyə yenilənir.</p></div><button class="btn" style="color:var(--down)" data-closeall>${icon('x', 16)}Hamısını bağla</button></div>${cards}`, state.overview);
}
function candleSvg(candles, opts = {}) {
  const w = 760, h = 300, pad = 10; const c = (candles || []).slice(-90);
  if (c.length < 3) return '<div class="empty">Qrafik məlumatı gəlmir (bazar bağlı ola bilər)</div>';
  const levels = [opts.bid, opts.sl, opts.tp].filter((v) => v);
  const lo = Math.min(...c.map((b) => b.l), ...levels), hi = Math.max(...c.map((b) => b.h), ...levels); const y = scale(null, lo, hi, h, pad);
  const step = (w - 60) / c.length; const bw = Math.max(2, step * 0.62);
  const bars = c.map((b, i) => { const x = 4 + i * step + step / 2; const up = b.c >= b.o; const col = up ? '#0F7A4B' : '#B4232A';
    const top = y(Math.max(b.o, b.c)), bot = y(Math.min(b.o, b.c));
    return `<line x1="${x.toFixed(1)}" y1="${y(b.h).toFixed(1)}" x2="${x.toFixed(1)}" y2="${y(b.l).toFixed(1)}" stroke="${col}" stroke-width="1"/><rect x="${(x - bw / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, bot - top).toFixed(1)}" fill="${col}" rx="1"/>`; }).join('');
  const line = (v, color, text, dash) => v ? `<line x1="0" y1="${y(v).toFixed(1)}" x2="${w - 58}" y2="${y(v).toFixed(1)}" stroke="${color}" stroke-width="1.2" ${dash ? 'stroke-dasharray="5 4"' : ''}/><rect x="${w - 56}" y="${(y(v) - 9).toFixed(1)}" width="56" height="18" rx="5" fill="${color}"/><text x="${w - 28}" y="${(y(v) + 4).toFixed(1)}" font-size="10" text-anchor="middle" fill="#fff" font-family="JetBrains Mono, monospace">${esc(text)}</text>` : '';
  const d = opts.digits ?? 2;
  return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(opts.label || 'Şam qrafiki')}" style="width:100%;height:auto;display:block">${bars}
    ${line(opts.tp, '#0F7A4B', Number(opts.tp).toFixed(d), true)}${line(opts.sl, '#B4232A', Number(opts.sl).toFixed(d), true)}${line(opts.bid, '#14141A', Number(opts.bid).toFixed(d), false)}</svg>`;
}
function oppCards(items, o) {
  if (!items.length) return '<div class="card"><div class="empty">Son 15 dəqiqədə botun açmadığı siqnal yoxdur.</div></div>';
  return `<div class="oppgrid" style="flex:none;width:100%">${items.map((i) => {
    const h = i.history; const hist = h ? `<span class="${tone(h.expectancy_r)}" style="font-weight:800">${R(h.expectancy_r)} · ${h.trades} tic.</span>` : '<span class="muted">test yoxdur</span>';
    return `<div class="opp"><span class="row" style="justify-content:space-between;width:100%"><span class="sym">${esc(i.symbol)} <span style="font-size:12px;color:${i.direction === 'buy' ? 'var(--acc)' : 'var(--down)'}">${dirLabel(i.direction)}</span></span><span class="muted" style="font-size:12px">${i.age_minutes != null ? Math.round(i.age_minutes) + ' dəq əvvəl' : ''}</span></span>
      <span class="muted" style="font-size:13px">${esc(i.strategy_label)} · ${esc(i.timeframe || '')}</span><span class="why">Bot açmadı: ${esc(i.reason)}</span>
      <span class="row" style="justify-content:space-between;width:100%;font-size:12px"><span class="muted">Tarixdə</span>${hist}</span>
      <button class="btn ink" data-opensig="${i.id}" data-sym="${esc(i.symbol)}">Bu siqnalı aç (${o.risk}%)</button></div>`; }).join('')}</div>`;
}
const SCALE = { 'Qısa': 0.5, 'Normal': 1, 'Geniş': 1.5 };
// Entry, stop and target the order panel would send now (auto levels times the scale, or the user's own prices).
function orderLevels(sym, m) {
  const auto = (sym.auto || {})[m.side] || {}; const entry = m.side === 'buy' ? sym.ask : sym.bid; const sign = m.side === 'buy' ? 1 : -1;
  if (m.mode === 'manual' && m.sl !== '' && m.tp !== '') return { entry, sl: Number(m.sl), tp: Number(m.tp), own: true };
  const k = SCALE[m.scale] || 1;
  const stop = auto.stop_distance ? auto.stop_distance * k : null; const target = auto.tp != null && auto.entry != null ? Math.abs(auto.tp - auto.entry) * k : null;
  return { entry, sl: stop ? entry - sign * stop : null, tp: target ? entry + sign * target : null, own: false };
}
function simBody(sym, m) {
  const lv = orderLevels(sym, m);
  return lv.own ? { symbol: sym.symbol, direction: m.side, sl: lv.sl, tp: lv.tp, minutes: m.minutes || 30 } : { symbol: sym.symbol, direction: m.side, scale: m.scale || 'Normal', minutes: m.minutes || 30 };
}
function checkHtml(res) {
  if (!res) return '';
  const ic = { ok: ['✓', '#7EE2AE'], warn: ['!', '#FFC56E'], fail: ['✕', '#FF9EA2'] };
  return `<div class="checks" aria-label="Risk yoxlaması">${res.checks.map((c) => `<div><span style="color:${ic[c.state][1]}">${ic[c.state][0]}</span><span><b>${esc(c.name)}</b> <small>${esc(c.text)}</small></span></div>`).join('')}</div>`;
}
async function pageFursetler() {
  const [d, board] = await Promise.all([api('/api/market'), api('/api/simulations/live').catch(() => ({ symbols: [] }))]); const o = state.opp; state.lastMarket = d;
  const m = state.market || (state.market = { symbol: null, tf: 'M5', side: 'buy', mode: 'auto', scale: 'Normal', minutes: 30, sl: '', tp: '' });
  const list = d.symbols || [];
  if (!m.symbol || !list.find((x) => x.symbol === m.symbol)) m.symbol = list[0]?.symbol || null;
  const sym = list.find((x) => x.symbol === m.symbol);
  const real = !d.demo; const risks = real ? [0.1, 0.25].filter((r) => r <= (d.real_risk_cap || 0.25)) : [0.5, 1, 2, 3];
  if (!risks.includes(o.risk)) o.risk = risks[Math.min(1, risks.length - 1)];
  const tabs = list.map((x) => { const t = (state.overview?.ticker || []).find((r) => r.symbol === x.symbol);
    return `<button data-msym="${esc(x.symbol)}" class="${x.symbol === m.symbol ? 'on' : ''}" aria-pressed="${x.symbol === m.symbol}">${esc(x.symbol)} <span class="mono" style="font-weight:600">${num(x.bid, x.digits > 2 ? 3 : x.bid > 1000 ? 1 : 2)}</span>${t && t.change_pct != null ? ` <span class="${tone(t.change_pct)}" style="font-size:11px">${t.change_pct >= 0 ? '▲' : '▼'}${Math.abs(t.change_pct).toFixed(2)}%</span>` : ''}</button>`; }).join('');
  const botSwitch = `<div class="row" style="background:#fff;border:1px solid var(--line);border-radius:14px;padding:8px 12px;gap:12px"><span style="font-size:13px;font-weight:700">Bot özü də açsın</span>
    <button class="switch" role="switch" aria-checked="${!d.entries_paused}" aria-label="Bot özü də ticarət açsın" data-botself="${d.entries_paused ? 'resume' : 'pause'}"><span></span></button></div>`;
  let main = '<div class="card"><div class="empty">Bot hələ bazar məlumatı göndərməyib. Bir neçə saniyə gözləyin (bazar bağlı ola bilər).</div></div>';
  let side = '';
  if (sym) {
    const auto = (sym.auto || {})[m.side] || {}; const dg = sym.digits || 2;
    const lv = orderLevels(sym, m); const { entry, sl, tp } = lv;
    const stopDist = sl ? Math.abs(entry - sl) : null; const targetDist = tp ? Math.abs(tp - entry) : null;
    const loss = d.balance * o.risk / 100; const rr = stopDist && targetDist ? targetDist / stopDist : null;
    const wrongSide = sl && tp && (m.side === 'buy' ? !(sl < entry && tp > entry) : !(sl > entry && tp < entry));
    const body = { ...simBody(sym, m), risk: o.risk }; const key = JSON.stringify(body);
    if (!wrongSide && (!state.check || state.check.key !== key || Date.now() - state.check.at > 8000)) {
      try { state.check = { key, at: Date.now(), res: await api('/api/order_check', { body }) }; } catch (e) { state.check = { key, at: Date.now(), res: null, error: e.message }; }
    }
    const check = state.check && state.check.key === key ? state.check.res : null;
    const view = sym.view || {}; const vcol = view.trend === 'yuxarı' ? 'var(--up)' : view.trend === 'aşağı' ? 'var(--down)' : 'var(--muted)';
    const sigs = (sym.signals || []).map((g) => `<div class="row" style="justify-content:space-between;font-size:13px;border-top:1px solid var(--line);padding:8px 0;flex-wrap:nowrap;gap:10px">
        <span><b>${esc(g.strategy_label)}</b> <span style="color:${g.direction === 'buy' ? 'var(--acc)' : 'var(--down)'};font-weight:800;font-size:12px">${dirLabel(g.direction)}</span></span>
        <span style="text-align:right;${g.taken ? 'color:var(--up);font-weight:700' : 'color:var(--warn)'}">${g.taken ? 'bot açdı' : 'açmadı: ' + esc(g.reason)}</span></div>`).join('');
    const simKey = JSON.stringify(simBody(sym, m));
    const liveRow = (board.symbols || []).find((x) => x.symbol === sym.symbol); const liveSide = liveRow && liveRow.sides ? liveRow.sides[m.side] : null;
    const liveFits = m.mode === 'auto' && (m.scale || 'Normal') === 'Normal' && liveSide;     // same levels the bot simulates every minute
    const own = state.sim && state.sim.symbol === sym.symbol && state.sim.key === simKey ? state.sim : null;
    const sim = own || (liveFits ? { ...liveSide, symbol: sym.symbol, reliability: liveRow.reliability, key: simKey } : (state.sim && state.sim.symbol === sym.symbol ? state.sim : null));
    const simOpts = { digits: dg, stale: !!sim && !own && !liveFits && sim.key !== simKey, liveAge: !own && liveFits ? board.age_seconds : null };
    if (!own && !liveFits && !wrongSide && sl && tp && !state.simBusy && state.autoSimKey !== simKey) {
      state.autoSimKey = simKey;                                        // own stop/target: simulate at once, no button needed
      setTimeout(() => runSimulation(simBody(sym, m), (res) => { state.sim = res; }), 0);
    }
    const simLine = sim && sim.enough && sim.chosen ? `<div class="checks" style="margin-top:-6px"><div><span style="color:${sim.chosen.expectancy_r >= -0.1 ? '#7EE2AE' : '#FFC56E'}">${sim.chosen.expectancy_r >= -0.1 ? '✓' : '!'}</span><span><b>Simulyasiya</b> <small>${R(sim.chosen.expectancy_r)} · hədəf ${pct(sim.chosen.target)} · stop ${pct(sim.chosen.stop)}${simOpts.stale ? ' (köhnə)' : ''}</small></span></div></div>` : '';
    main = `<section class="card" style="display:flex;flex-direction:column;gap:12px">
        <div class="row" style="justify-content:space-between"><div><span class="sym">${esc(sym.symbol)}</span> <span class="muted" style="font-size:12px">${esc(sym.broker)} · spred ${num(sym.spread, dg)}</span></div>
          <div class="seg light">${['M1', 'M5', 'M15'].map((tf) => `<button data-tf="${tf}" class="${m.tf === tf ? 'on' : ''}">${tf}</button>`).join('')}</div></div>
        ${candleSvg((sym.candles || {})[m.tf], { bid: sym.bid, sl, tp, digits: dg, label: sym.symbol + ' ' + m.tf + ' şam qrafiki' })}
        <div style="background:var(--soft);border-radius:14px;padding:12px 14px"><div style="font-size:13px"><b>Botun fikri:</b> <span style="color:${vcol};font-weight:700">${esc(view.text || '–')}</span></div>
          ${sigs ? `<div style="margin-top:6px">${sigs}</div>` : '<div class="muted" style="font-size:12px;margin-top:6px">Son 30 dəqiqədə strategiyalar bu alətdə siqnal verməyib.</div>'}</div>
        <div class="muted" style="font-size:12px">${d.age_seconds != null ? `Yeniləndi ${Math.round(d.age_seconds)} san əvvəl` : ''}</div></section>
      <section class="card" aria-label="Zaman maşını" style="display:flex;flex-direction:column;gap:12px">
        <div class="row" style="justify-content:space-between"><h2>Zaman maşını</h2><div class="row">
          <div class="seg light">${[15, 30, 60].map((n) => `<button data-simmin="${n}" class="${(m.minutes || 30) === n ? 'on' : ''}">${n} dəq</button>`).join('')}</div>
          <button class="btn ink" data-simulate ${state.simBusy ? 'disabled' : ''}>${state.simBusy ? 'Hesablanır…' : (liveFits ? 'Yenidən hesabla' : `Simulyasiya et · ${dirLabel(m.side)}`)}</button></div></div>
        ${sim ? simResultHtml(sim, (sym.candles || {}).M1, simOpts) : '<p class="muted" style="margin:0;font-size:13px;line-height:1.5">Bot hər dəqiqə bütün alətləri simulyasiya edir; nəticə bir neçə saniyəyə burada görünəcək. Öz stop və hədəfiniz üçün "Simulyasiya et" basın.</p>'}</section>`;
    side = `<aside class="card dark side" aria-label="Əmr" style="display:flex;flex-direction:column;gap:14px">
        <div class="levels" style="grid-template-columns:repeat(2,minmax(0,1fr))"><div><small>SAT (bid)</small><b style="font-size:18px">${num(sym.bid, dg)}</b></div><div><small>AL (ask)</small><b style="font-size:18px">${num(sym.ask, dg)}</b></div></div>
        <div class="riskseg" role="group" aria-label="İstiqamət"><button data-side="buy" class="${m.side === 'buy' ? 'on' : ''}" aria-pressed="${m.side === 'buy'}" style="${m.side === 'buy' ? 'background:#4338CA;color:#fff' : ''}">AL</button><button data-side="sell" class="${m.side === 'sell' ? 'on' : ''}" aria-pressed="${m.side === 'sell'}" style="${m.side === 'sell' ? 'background:#B4232A;color:#fff' : ''}">SAT</button></div>
        <div role="group" aria-label="Risk" class="riskseg">${risks.map((r) => `<button data-risk="${r}" class="${o.risk === r ? 'on' : ''}" aria-pressed="${o.risk === r}">${r}%</button>`).join('')}</div>
        <div class="riskseg" role="group" aria-label="Stop və hədəf"><button data-mode="auto" class="${m.mode === 'auto' ? 'on' : ''}">Avtomatik</button><button data-mode="manual" class="${m.mode === 'manual' ? 'on' : ''}">Əl ilə</button></div>
        ${m.mode === 'auto' ? `<div class="riskseg" role="group" aria-label="Stop ölçüsü">${Object.keys(SCALE).map((k) => `<button data-scale="${k}" class="${(m.scale || 'Normal') === k ? 'on' : ''}" aria-pressed="${(m.scale || 'Normal') === k}">${k}</button>`).join('')}</div>` : ''}
        <div class="levels" style="grid-template-columns:repeat(2,minmax(0,1fr))">
          <label><small style="color:#FF9EA2">Stop</small>${m.mode === 'manual' ? `<input class="input" data-lvl="sl" value="${esc(m.sl !== '' ? m.sl : (sl ? Number(sl).toFixed(dg) : ''))}" inputmode="decimal" style="width:100%;margin-top:4px;background:#14141A;color:#fff;border-color:#3A3A45">` : `<b>${sl ? num(sl, dg) : '–'}</b>`}</label>
          <label><small style="color:#7EE2AE">Hədəf</small>${m.mode === 'manual' ? `<input class="input" data-lvl="tp" value="${esc(m.tp !== '' ? m.tp : (tp ? Number(tp).toFixed(dg) : ''))}" inputmode="decimal" style="width:100%;margin-top:4px;background:#14141A;color:#fff;border-color:#3A3A45">` : `<b>${tp ? num(tp, dg) : '–'}</b>`}</label></div>
        ${wrongSide ? '<div class="realbox" style="font-size:13px">Stop və hədəf istiqamətə uyğun deyil: AL üçün stop qiymətdən aşağı, hədəf yuxarı olmalıdır (SAT üçün əksinə).</div>' : ''}
        <div style="font-size:13px;color:#D4D5DE">Stopa dəysə itki <b style="color:#fff">${money(loss)}</b>${rr ? ` · hədəfə çatsa <b style="color:#7EE2AE">${money(loss * rr)}</b> (${rr.toFixed(2)}R)` : ''}</div>
        ${checkHtml(check)}${simLine}
        <label class="check" style="color:#D4D5DE"><input type="checkbox" data-managed ${o.managed ? 'checked' : ''}>Bot erkən çıxış və vaxt qaydalarını tətbiq etsin</label>
        <button class="btn big" data-market style="border:0;background:${m.side === 'buy' ? '#6366F1' : '#DC2626'};color:#fff">${m.side === 'buy' ? 'AL' : 'SAT'} · ${esc(sym.symbol)} · ${o.risk}%</button>
        <p style="margin:0;font-size:12px;color:#B8B9C6;line-height:1.5">${real ? '<b style="color:#FF9EA2">REAL hesab.</b> ' : ''}Bir kliklə açılır${real && d.confirm_real ? ' (REAL hesabda təsdiq soruşulur)' : ''}. Bot qiyməti, spredi və limitləri yenidən yoxlayır, lotu riskə görə özü hesablayır.</p></aside>`;
  }
  paint(`<div class="head"><div><h1>Fürsətlər</h1><p class="lead">Seçilmiş alətlərin canlı qrafiki və botun fikri. İstədiyiniz an özünüz AL/SAT edə bilərsiniz, bot da öz siqnalları ilə işləməyə davam edir.</p></div>${botSwitch}</div>
    <div class="chips" role="group" aria-label="Alətlər">${tabs}</div>
    <div class="row" style="align-items:flex-start;gap:20px"><div style="flex:999 1 460px;min-width:0;display:flex;flex-direction:column;gap:16px">${main}</div>${side}</div>
    <h2 style="margin-top:6px">Botun açmadığı siqnallar (son 15 dəqiqə)</h2>${oppCards(d.opportunities || [], o)}`, state.overview);
}
async function runSimulation(body, store) {
  state.simBusy = true; refresh();
  try { const res = await api('/api/simulate', { body }); store({ ...res, key: JSON.stringify(body) }); }
  catch (e) { if (e.message !== 'login') { store({ symbol: body.symbol, error: e.message, key: JSON.stringify(body) }); } }
  state.simBusy = false; refresh();
}
async function pageSimulyasiya() {
  const [d, hist, board] = await Promise.all([api('/api/market'), api('/api/simulations'), api('/api/simulations/live').catch(() => ({ symbols: [] }))]); const sp = state.simPage; state.lastMarket = d;
  state.liveBoard = board;
  const sideCell = (row, side) => {
    const x = (row.sides || {})[side]; if (!x) return `<div class="lside"><b>${dirLabel(side)}</b><div class="muted" style="font-size:12px">məlumat yoxdur</div></div>`;
    if (!x.enough) return `<div class="lside"><b>${dirLabel(side)}</b><div class="muted" style="font-size:12px">oxşar an azdır (${x.analogs})</div></div>`;
    const c = x.chosen; const b = x.best || {};
    return `<button class="lside" data-liveopen="${esc(row.symbol)}:${side}" aria-label="${esc(row.symbol)} ${dirLabel(side)} ətraflı">
      <div class="row" style="justify-content:space-between"><b style="color:${side === 'buy' ? 'var(--acc)' : 'var(--down)'}">${dirLabel(side)}</b><b class="mono ${tone(c.expectancy_r)}">${R(c.expectancy_r)}</b></div>
      ${fanSvg({ ...x, fan: (x.fan || []).slice(0, 25) }, null, 2)}
      <div style="font-size:12px"><span class="up">hədəf ${pct(c.target)}</span> · <span class="down">stop ${pct(c.stop)}</span> · vaxt ${pct(c.time)}</div>
      <div class="muted" style="font-size:12px">${b.expectancy_r > 0 ? `Ən yaxşı: <b class="up">${esc(b.stop)} · ${b.ratio}× (${R(b.expectancy_r)})</b>` : 'Bütün aralıqlar zərərli'}</div></button>`;
  };
  const liveCards = (board.symbols || []).map((row) => `<div class="card livecard"><div class="row" style="justify-content:space-between"><span class="sym">${esc(row.symbol)}</span>
      ${row.reliability ? `<span class="tag" style="background:${row.reliability === 'etibarlı' ? 'var(--up-soft)' : '#F2F0EB'};color:${row.reliability === 'etibarlı' ? 'var(--up)' : 'var(--muted)'}">${esc(row.reliability)}</span>` : ''}</div>
      ${row.error ? `<div class="why">${esc(row.error)}</div>` : `<div class="muted" style="font-size:12px">trend ${esc(row.state?.trend || '–')} · volatillik ${esc(row.state?.volatility || '–')} · ${num(row.analogs, 0)} oxşar an</div>
      <div class="lsides">${sideCell(row, 'buy')}${sideCell(row, 'sell')}</div>`}</div>`).join('');
  const liveHtml = `<section aria-label="Canlı simulyasiya"><div class="row" style="justify-content:space-between;margin-bottom:10px"><h2>Canlı simulyasiya · bütün alətlər</h2>
      <span class="muted" style="font-size:12px">${board.age_seconds != null ? `Bot hər dəqiqə yeniləyir · son ${Math.round(board.age_seconds)} san əvvəl` : 'Bot hələ canlı simulyasiya yazmayıb'}</span></div>
      ${liveCards ? `<div class="grid">${liveCards}</div>` : '<div class="card"><div class="empty">Bot yenilənəndən bir dəqiqə sonra burada bütün alətlər görünəcək</div></div>'}</section>`;
  const list = d.symbols || []; if (!sp.symbol || !list.find((x) => x.symbol === sp.symbol)) sp.symbol = list[0]?.symbol || null;
  const sym = list.find((x) => x.symbol === sp.symbol); const dg = sym?.digits || 2;
  const cal = (hist.calibration || {}).symbols || {};
  const calRows = Object.entries(cal).map(([k, v]) => `<span class="tag" style="background:${v.reliable ? 'var(--up-soft)' : 'var(--down-soft)'};color:${v.reliable ? 'var(--up)' : 'var(--down)'}">${esc(k)}: ${v.reliable ? 'etibarlı' : 'etibarlı deyil'} (fərq ${pct(v.worst_gap)})</span>`).join(' ');
  const who = (h) => h.source === 'bot' ? `bot · ${({ skip: '<span class="down">açmadı</span>', levels: '<span class="up">aralığı dəyişdi</span>', open: 'açdı' })[h.action] || ''}` : 'siz';
  const rows = (hist.items || []).map((h) => `<tr><td>${localTime(h.time, true)}</td><td>${who(h)}</td><td><b>${esc(h.symbol)}</b> ${dirLabel(h.direction)}</td><td class="num">${R(h.expectancy_r)}</td><td class="num">${pct(h.target)}</td>
      <td>${h.best ? (h.best.expectancy_r > 0 ? `${esc(h.best.stop)} · ${h.best.ratio}× (${R(h.best.expectancy_r)})` : '<span class="down">hamısı zərərli</span>') : '–'}</td>
      <td class="num ${typeof h.real === 'number' ? tone(h.real) : ''}">${typeof h.real === 'number' ? R(h.real) : esc(h.real)}</td></tr>`).join('');
  const controls = `<div class="row" style="gap:10px">
      <div class="chips" role="group" aria-label="Alət">${list.map((x) => `<button data-simsym="${esc(x.symbol)}" class="${x.symbol === sp.symbol ? 'on' : ''}">${esc(x.symbol)}</button>`).join('')}</div>
      <div class="seg light">${['buy', 'sell'].map((k) => `<button data-simside="${k}" class="${sp.side === k ? 'on' : ''}">${dirLabel(k)}</button>`).join('')}</div>
      <div class="seg light">${Object.keys(SCALE).map((k) => `<button data-simscale="${k}" class="${sp.scale === k ? 'on' : ''}">${k}</button>`).join('')}</div>
      <div class="seg light">${[15, 30, 60].map((n) => `<button data-simpmin="${n}" class="${sp.minutes === n ? 'on' : ''}">${n} dəq</button>`).join('')}</div>
      <button class="btn ink" data-simpage ${state.simBusy || !sym ? 'disabled' : ''}>${state.simBusy ? 'Hesablanır…' : 'Simulyasiya et'}</button></div>`;
  paint(`<div><h1>Simulyasiya</h1><p class="lead">Zaman maşını: indiki ana oxşayan keçmiş anlardan sonra qiymətin real yolları. Bot da hər öz girişindən əvvəl bunu özü işlədir: proqnozu pis olanı açmır, daha yaxşı aralıq varsa onu seçir (İdarəetmə → Qoruma və öyrənmə).</p></div>
    ${liveHtml}<h2 style="margin-top:6px">Öz parametrlərinizlə</h2>
    <section class="card" style="display:flex;flex-direction:column;gap:12px">${controls}
      ${sp.result ? simResultHtml(sp.result, (sym?.candles || {}).M1, { digits: dg }) : '<div class="empty">Alət, istiqamət və stop ölçüsünü seçib "Simulyasiya et" basın</div>'}</section>
    <section class="card"><h2>Etibarlılıq yoxlaması</h2><p class="muted" style="font-size:13px;line-height:1.5;margin:6px 0 10px">Keçmişdə təsadüfi anlar yalnız onlardan əvvəlki tarixçə ilə simulyasiya edilib və real nəticə ilə müqayisə olunub.</p>
      ${calRows || '<div class="empty">Yoxlama hələ işlədilməyib (scripts/check_time_machine.py)</div>'}</section>
    <div class="tablebox"><table><thead><tr><th>Vaxt</th><th>Kim</th><th>Alət</th><th class="num">Simulyasiya R</th><th class="num">Hədəf %</th><th>Ən yaxşı seçim</th><th class="num">Real nəticə</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="7" class="muted">Hələ simulyasiya yoxdur</td></tr>'}</tbody></table></div>`, state.overview);
}
async function pageJurnal() {
  const d = await api('/api/journal?days=' + state.journalDays); const s = d.summary || {};
  const rowsHtml = (d.trades || []).slice(0, 200).map((t) => `<tr><td>${localTime(t.close_time, true)}</td><td><b>${esc(t.symbol)}</b> <span class="${t.direction === 'buy' ? '' : 'down'}" style="font-size:11px;font-weight:800">${dirLabel(t.direction)}</span></td><td>${esc(t.strategy_label)}</td><td>${esc(t.close_reason_label)}</td><td class="num ${tone(t.net)}">${money(t.net, true)}</td></tr>`).join('');
  const group = (list, label) => `<div class="tablebox"><table><thead><tr><th>${label}</th><th class="num">Ticarət</th><th class="num">Uduş</th><th class="num">Nəticə</th></tr></thead><tbody>${(list || []).map((g) => `<tr><td>${esc(g.label || g.name)}</td><td class="num">${g.trades}</td><td class="num">${Math.round((g.win_rate || 0) * 100)}%</td><td class="num ${tone(g.total)}">${money(g.total, true)}</td></tr>`).join('')}</tbody></table></div>`;
  const hours = (d.by_hour || []).map((h) => `<tr><td>${String(h.hour).padStart(2, '0')}:00</td><td class="num">${h.trades}</td><td class="num">${Math.round(h.win_rate * 100)}%</td><td class="num ${tone(h.total)}">${money(h.total, true)}</td></tr>`).join('');
  const reviews = (d.exit_reviews || []).map((r) => `<tr><td>${esc(r.label || r.reason)}</td><td class="num">${r.trades}</td><td class="num">${r.helped}</td><td class="num">${r.hurt}</td><td class="num ${tone(r.saved)}">${money(r.saved, true)}</td></tr>`).join('');
  paint(`<div class="head"><div><h1>Jurnal</h1><p class="lead">Bağlanmış ticarətlər, strategiya və saatlara görə nəticə, botun öz bağlamalarının yoxlanması.</p></div>
      <div class="seg light">${[1, 7, 30, 90, 365].map((n) => `<button data-days="${n}" class="${state.journalDays === n ? 'on' : ''}">${n === 1 ? 'Bu gün' : n + ' gün'}</button>`).join('')}</div></div>
    <div class="grid"><div class="card"><div class="minis"><div class="mini"><small>Nəticə</small><b class="${tone(s.total)}">${money(s.total, true)}</b></div><div class="mini"><small>Ticarət</small><b>${s.trades ?? 0}</b></div>
      <div class="mini"><small>Uduş faizi</small><b>${s.win_rate != null ? Math.round(s.win_rate * 100) + '%' : '–'}</b></div><div class="mini"><small>Profit factor</small><b>${s.profit_factor != null ? num(s.profit_factor, 2) : '–'}</b></div></div></div>
      <div class="card"><h2>Bot nə öyrəndi</h2>${(d.insights || []).length ? `<ul style="margin:10px 0 0;padding-left:18px;font-size:13px;line-height:1.6">${d.insights.map((i) => `<li>${esc(i)}</li>`).join('')}</ul>` : '<div class="empty">Hələ kifayət qədər ticarət yoxdur</div>'}</div></div>
    <div class="grid">${group(d.by_strategy, 'Strategiya')}${group(d.by_symbol, 'Alət')}</div>
    <div class="grid"><div class="tablebox"><table><thead><tr><th>Saat (Bakı)</th><th class="num">Ticarət</th><th class="num">Uduş</th><th class="num">Nəticə</th></tr></thead><tbody>${hours}</tbody></table></div>
      <div class="tablebox"><table><thead><tr><th>Botun öz bağlamaları</th><th class="num">Say</th><th class="num">Düzgün</th><th class="num">Səhv</th><th class="num">Qənaət</th></tr></thead><tbody>${reviews || '<tr><td colspan="5" class="muted">Hələ yoxlanılmış bağlama yoxdur</td></tr>'}</tbody></table></div></div>
    <div class="tablebox"><table><thead><tr><th>Bağlandı</th><th>Alət</th><th>Strategiya</th><th>Səbəb</th><th class="num">Nəticə</th></tr></thead><tbody>${rowsHtml || '<tr><td colspan="5" class="muted">Bu dövrdə ticarət yoxdur</td></tr>'}</tbody></table></div>`, state.overview);
}
async function pageStrategiyalar() {
  const d = await api('/api/strategies');
  const cards = d.strategies.map((s) => {
    const t = s.test; const badge = !t ? ['test yoxdur', '#F2F0EB', 'var(--muted)'] : t.passed ? ['testdən keçdi', 'var(--up-soft)', 'var(--up)'] : ['testdən keçmədi', 'var(--down-soft)', 'var(--down)'];
    const guard = (s.guard || []).map((g) => `<div class="row" style="justify-content:space-between;font-size:12px;border-top:1px solid var(--line);padding-top:8px">
        <span><b>${esc(g.symbol)}</b> · ${g.state === 'paused' ? '<span class="down">dayandırılıb</span>' : 'aktiv'} · pay ×${num(g.weight, 2)} · ${g.trades} tic. ${g.live_r != null ? R(g.live_r) : ''}</span>
        ${g.state === 'paused' ? `<button class="btn" style="min-height:32px" data-reset="${esc(s.name)}:${esc(g.symbol)}">Yenidən başlat</button>` : ''}</div>${g.reason ? `<div class="muted" style="font-size:12px">${esc(g.reason)}</div>` : ''}`).join('');
    return `<div class="card" style="display:flex;flex-direction:column;gap:10px;background:${s.enabled ? '#fff' : 'var(--soft)'}">
      <div class="row" style="justify-content:space-between;flex-wrap:nowrap"><b style="font-size:15px">${esc(s.label)}</b>
        <button class="switch" role="switch" aria-checked="${s.enabled}" aria-label="${esc(s.label)}" data-strat="${esc(s.name)}"><span></span></button></div>
      <div class="row"><span class="tag" style="background:${badge[1]};color:${badge[2]}">${badge[0]}</span>${t ? `<span class="muted" style="font-size:12px">ən yaxşı: ${esc(t.symbol)} ${R(t.expectancy_r)}, ${t.trades} tic., PF ${t.profit_factor != null ? num(t.profit_factor, 2) : '–'}</span>` : ''}</div>
      ${t && t.passed_where?.length ? `<div class="muted" style="font-size:12px">Keçən: ${t.passed_where.map(esc).join('; ')}</div>` : ''}${guard}</div>`;
  }).join('');
  const filters = d.filters.length ? `<div class="tablebox"><table><thead><tr><th>Strategiya</th><th>Alət</th><th>Şərt</th><th>Vəziyyət</th><th>Sübut</th><th></th></tr></thead><tbody>${d.filters.map((f) => `<tr><td>${esc(f.strategy)}</td><td>${esc(f.symbol)}</td><td>${esc(f.label)}</td>
      <td>${f.status === 'active' ? '<span class="tag" style="background:var(--acc-soft);color:var(--acc)">tətbiq olunur</span>' : '<span class="tag" style="background:#F2F0EB;color:var(--muted)">təklif</span>'}</td><td style="white-space:normal;min-width:260px">${esc(f.evidence)}</td>
      <td><button class="btn" style="min-height:32px" data-unfilter="${f.id}">Sil</button></td></tr>`).join('')}</tbody></table></div>` : '<div class="card"><div class="empty">Uduzma yarılması hələ təkrarlanan zəif yer tapmayıb</div></div>';
  paint(`<div><h1>Strategiyalar</h1><p class="lead">Açar strategiyanı tam söndürür. Keşikçi hər ticarətdən sonra payları tədricən dəyişir və pis gedən cütü özü dayandırır. ${d.tested_at ? `Uzun tarix testi: ${localTime(d.tested_at, true)}, imtahan ${esc(d.test_period)}-dən.` : 'Uzun tarix testinin nəticəsi hələ serverə köçürülməyib.'}</p></div>
    <div class="grid">${cards}</div><h2 style="margin-top:8px">Uduzma yarılması — öyrənilmiş filtrlər</h2>${filters}`, state.overview);
}
async function pageIdare() {
  const [d, ov] = await Promise.all([api('/api/settings'), state.overview ? Promise.resolve(state.overview) : api('/api/overview')]);
  state.overview = ov; const st = ov.status; const draft = state.settingsDraft;
  const value = (f) => (f.path in draft ? draft[f.path] : f.value);
  const control = (f) => {
    const v = value(f); const id = 'f_' + f.path.replace(/\W/g, '_');
    if (f.kind === 'bool') return `<button class="switch" role="switch" aria-checked="${!!v}" aria-labelledby="${id}" data-set="${esc(f.path)}" data-kind="bool"><span></span></button>`;
    if (f.kind === 'choice') return `<select class="input" aria-labelledby="${id}" data-set="${esc(f.path)}" data-kind="choice">${f.choices.map((c) => `<option value="${esc(c)}" ${String(c) === String(v) ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select>`;
    if (f.kind === 'symbols') return `<div class="chips" role="group" aria-labelledby="${id}">${d.symbols.map((s) => `<button data-sym-toggle="${esc(s)}" data-set="${esc(f.path)}" class="${(v || []).includes(s) ? 'on' : ''}" aria-pressed="${(v || []).includes(s)}">${esc(s)}</button>`).join('')}</div>`;
    return `<input class="input" aria-labelledby="${id}" data-set="${esc(f.path)}" data-kind="${f.kind}" value="${esc(v ?? '')}" ${f.kind === 'time' ? 'placeholder="09:30"' : `type="number" ${f.min != null ? `min="${f.min}"` : ''} ${f.max != null ? `max="${f.max}"` : ''} ${f.step != null ? `step="${f.step}"` : ''}`}>`;
  };
  const sections = d.sections.map((sec) => {
    const fields = d.fields.filter((f) => f.section === sec); if (!fields.length) return '';
    return `<section class="card"><h2 style="margin-bottom:8px">${esc(sec)}</h2>${fields.map((f) => `<div class="set" style="${f.kind === 'symbols' ? 'flex-direction:column;align-items:flex-start' : ''}"><div class="txt"><b id="f_${f.path.replace(/\W/g, '_')}">${esc(f.label)}</b>${f.help ? `<small>${esc(f.help)}</small>` : ''}</div>${control(f)}</div>`).join('')}</section>`;
  }).join('');
  const dirty = Object.keys(draft).length;
  paint(`<div><h1>İdarəetmə</h1><p class="lead">Dəyişiklik yadda saxlananda ayarlar yoxlanır, köhnəsi ehtiyata qoyulur və bot özü yenidən başlayır. Açıq mövqelərə toxunulmur.</p></div>
    <section class="card dark" aria-label="Bot" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;align-items:center">
      <div><div style="font-size:12px;color:#B8B9C6">Vəziyyət</div><div style="font-size:18px;font-weight:800;margin-top:6px">${st.stale ? 'Cavab vermir' : st.entries_paused ? 'Girişlər saxlanıb' : 'İşləyir'} · ${esc(st.account_type || '')}</div></div>
      <button class="btn big" style="background:#fff;color:var(--ink);border:0" data-pause="${st.entries_paused ? 'resume' : 'pause'}">${st.entries_paused ? 'Girişləri bərpa et' : 'Yeni girişləri saxla'}</button>
      <button class="btn big ghost-dark" data-restart>${icon('refresh', 16)}Yenidən başlat</button>
      <button class="btn big ghost-dark" style="color:#FF9EA2" data-closeall>Hamısını bağla</button></section>
    <div class="grid">${sections}</div>
    <section class="card"><h2>Panel girişi</h2><form class="row" data-pw style="margin-top:10px"><input class="input" style="width:200px;font-family:inherit" type="password" name="old" placeholder="Köhnə parol" autocomplete="current-password" required><input class="input" style="width:200px;font-family:inherit" type="password" name="new" placeholder="Yeni parol (≥8)" autocomplete="new-password" minlength="8" required><button class="btn">Parolu dəyiş</button></form></section>
    ${dirty ? `<div class="savebar"><span>${dirty} yadda saxlanmamış dəyişiklik</span><div class="row"><button class="btn ghost-dark" data-discard>Ləğv et</button><button class="btn primary" data-save>Yadda saxla</button></div></div>` : ''}`, ov);
}
async function pageSaglamliq() {
  const d = await api('/api/health'); const s = d.status; const w = d.watchdog || {};
  const item = (label, ok, text) => `<div class="set"><div class="txt"><b>${label}</b><small>${esc(text)}</small></div><span class="pill" style="background:${ok ? 'var(--up-soft)' : 'var(--down-soft)'};color:${ok ? 'var(--up)' : 'var(--down)'}">${ok ? 'qaydasında' : 'diqqət'}</span></div>`;
  paint(`<div><h1>Sağlamlıq</h1><p class="lead">Botun, MT5-in və keşikçinin vəziyyəti.</p></div>
    <div class="grid"><section class="card">
      ${item('Ürək döyüntüsü', !s.stale, s.heartbeat_seconds != null ? Math.round(s.heartbeat_seconds) + ' saniyə əvvəl' : 'məlumat yoxdur')}
      ${item('MT5 bağlantısı', s.mt5_connected, s.mt5_connected ? 'bağlıdır' : 'bağlı deyil')}
      ${item('Avtomatik ticarət icazəsi', s.trade_allowed, s.trade_allowed ? 'MT5-də açıqdır' : 'MT5-də bağlıdır')}
      ${item('İlişmə keşikçisi', !w.last_error, w.checked_at ? `son yoxlama ${ago(w.checked_at)}${w.last_restart ? ', son yenidən başlatma ' + ago(w.last_restart) : ''}` : 'hələ işləməyib (serverdə quraşdırılmalıdır)')}
      ${item('Son 24 saatda yenidən başlamalar', d.restarts_24h < 5, d.restarts_24h + ' dəfə')}
      ${s.last_error ? `<div class="why" style="margin-top:10px">Son xəta: ${esc(s.last_error)}</div>` : ''}</section>
      <section class="card"><h2>Aktiv qoruma fasilələri</h2>${d.gates.length ? d.gates.map((g) => `<div class="set"><div class="txt"><b>${esc(g.scope)} · ${esc(g.name)}</b><small>${esc(g.reason || '')} · ${localTime(g.disabled_until, true)}-a qədər</small></div></div>`).join('') : '<div class="empty">Fasilə yoxdur</div>'}</section></div>
    <div class="tablebox"><table><thead><tr><th>Əmr</th><th>Vəziyyət</th><th>Cavab</th><th>Vaxt</th></tr></thead><tbody>${d.commands.map((c) => `<tr><td class="mono">${esc(c.command)}</td><td>${esc(c.status)}</td><td style="white-space:normal">${esc(c.message || '')}</td><td>${localTime(c.created_at, true)}</td></tr>`).join('')}</tbody></table></div>
    <section class="card"><h2>Son hadisələr</h2>${feedHtml(d.events)}</section>`, state.overview);
}

// ------------------------------------------------------------- routing & events
const PAGES = { merkez: [pageMerkez, 2000], movqeler: [pageMovqeler, 2000], fursetler: [pageFursetler, 2000], simulyasiya: [pageSimulyasiya, 5000], jurnal: [pageJurnal, 30000], strategiyalar: [pageStrategiyalar, 30000], idare: [pageIdare, 0], saglamliq: [pageSaglamliq, 5000] };
async function refresh() {
  const page = PAGES[state.route] || PAGES.merkez;
  try { await page[0](); } catch (e) { if (e.message !== 'login') toast(e.message, true); }
}
function route() {
  state.route = (location.hash.replace('#/', '') || 'merkez'); if (!PAGES[state.route]) state.route = 'merkez';
  clearInterval(state.timer); refresh();
  const every = PAGES[state.route][1];
  if (every) state.timer = setInterval(() => { if (!document.hidden && $('#modal').hidden && !document.activeElement?.closest?.('.side')) refresh(); }, every);
}
document.addEventListener('click', async (e) => {
  const t = e.target.closest('button, [data-logout]'); if (!t) return;
  const d = t.dataset;
  if (d.logout !== undefined) { await api('/api/logout', { body: {} }).catch(() => {}); renderLogin(); return; }
  if (d.period) { state.period = d.period; refresh(); return; }
  if (d.days) { state.journalDays = Number(d.days); refresh(); return; }
  if (d.pause) { await command(d.pause, d.pause === 'pause' ? 'Yeni girişlər saxlandı' : 'Girişlər bərpa olundu'); return; }
  if (d.closeall !== undefined) { if (await ask('Hamısını bağla', 'Botun bütün açıq mövqeləri cari qiymətlə bağlanacaq.', 'Bağla', true)) command('close_all', 'Mövqelər bağlandı'); return; }
  if (d.restart !== undefined) { if (await ask('Yenidən başlat', 'Bot dövrəsini tamamlayıb yenidən başlayacaq (təxminən 1 dəqiqə). Açıq mövqelər qalır.')) command('restart', 'Bot yenidən başladı'); return; }
  if (d.close) { if (await ask(`${d.sym} bağlansın?`, 'Mövqe cari qiymətlə bağlanacaq.', 'Bağla', true)) command('close_position:' + d.close, 'Mövqe bağlandı'); return; }
  if (d.be) {
    if (d.good !== '1') { toast('Mövqe hələ mənfəətdə deyil: stop girişə yalnız qiymət lehinizə gedəndə çəkilə bilər', true); return; }
    command('breakeven:' + d.be, 'Stop girişə çəkildi'); return;
  }
  if (d.dayshift !== undefined) { const v = Number(d.dayshift); state.dayOffset = v === 0 ? 0 : Math.min(0, state.dayOffset + v); refresh(); return; }
  if (d.scale) { state.market.scale = d.scale; refresh(); return; }
  if (d.simmin) { state.market.minutes = Number(d.simmin); refresh(); return; }
  if (d.simulate !== undefined) {
    const data = state.lastMarket || await api('/api/market'); const sym = (data.symbols || []).find((x) => x.symbol === state.market.symbol); if (!sym) return;
    runSimulation(simBody(sym, state.market), (res) => { state.sim = res; }); return;
  }
  if (d.liveopen) {
    const [symbol, side] = d.liveopen.split(':'); const row = ((state.liveBoard || {}).symbols || []).find((x) => x.symbol === symbol); const x = row && row.sides[side]; if (!x) return;
    const sp = state.simPage; sp.symbol = symbol; sp.side = side; sp.scale = 'Normal'; sp.result = { ...x, symbol, reliability: row.reliability };
    refresh(); setTimeout(() => document.querySelector('[data-simpage]')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 300); return;
  }
  if (d.simsym) { state.simPage.symbol = d.simsym; state.simPage.result = null; refresh(); return; }
  if (d.simside) { state.simPage.side = d.simside; refresh(); return; }
  if (d.simscale) { state.simPage.scale = d.simscale; refresh(); return; }
  if (d.simpmin) { state.simPage.minutes = Number(d.simpmin); refresh(); return; }
  if (d.simpage !== undefined) {
    const sp = state.simPage; runSimulation({ symbol: sp.symbol, direction: sp.side, scale: sp.scale, minutes: sp.minutes }, (res) => { sp.result = res; }); return;
  }
  if (d.simcell) {
    const [stop, target] = d.simcell.split(':').map(Number); const onSim = state.route === 'simulyasiya';
    const symbol = onSim ? state.simPage.symbol : state.market.symbol; const side = onSim ? state.simPage.side : state.market.side;
    const data = state.lastMarket || await api('/api/market'); const sym = (data.symbols || []).find((x) => x.symbol === symbol); if (!sym) return;
    const sign = side === 'buy' ? 1 : -1; const entry = side === 'buy' ? sym.ask : sym.bid; const dg = sym.digits || 2;
    state.market = { ...(state.market || { tf: 'M5', minutes: 30 }), symbol, side, mode: 'manual', scale: 'Normal', sl: (entry - sign * stop).toFixed(dg), tp: (entry + sign * target).toFixed(dg) };
    toast('Stop və hədəf əmr panelinə yazıldı');
    if (onSim) location.hash = '#/fursetler'; else refresh(); return;
  }
  if (d.msym) { state.market.symbol = d.msym; state.market.sl = ''; state.market.tp = ''; refresh(); return; }
  if (d.tf) { state.market.tf = d.tf; refresh(); return; }
  if (d.side) { state.market.side = d.side; state.market.sl = ''; state.market.tp = ''; refresh(); return; }
  if (d.mode) { state.market.mode = d.mode; if (d.mode === 'auto') { state.market.sl = ''; state.market.tp = ''; } refresh(); return; }
  if (d.botself) { await command(d.botself, d.botself === 'pause' ? 'Bot öz girişlərini saxladı (siz aça bilərsiniz)' : 'Bot yenidən özü də açır'); return; }
  if (d.opensig) { const o = state.opp; command(`open_signal:${d.opensig}:${o.risk}:${o.managed ? 1 : 0}`, 'Mövqe açıldı'); return; }
  if (d.market !== undefined) {
    const m = state.market, o = state.opp; const data = await api('/api/market'); const sym = (data.symbols || []).find((x) => x.symbol === m.symbol); if (!sym) return;
    const lv = orderLevels(sym, m); const dg = sym.digits || 2;
    if (!(lv.sl > 0) || !(lv.tp > 0)) { toast('Stop və hədəfi yazın', true); return; }
    const chk = state.check && state.check.res;
    if (chk && !chk.can_open) { const bad = chk.checks.find((c) => c.state === 'fail'); toast('Açılmır: ' + (bad ? `${bad.name}: ${bad.text}` : 'yoxlama keçmədi'), true); return; }
    const fixed = lv.own || (m.scale && m.scale !== 'Normal');          // Normal auto: the bot recomputes levels at the fill price
    const sl = fixed ? Number(lv.sl.toFixed(dg)) : 0; const tp = fixed ? Number(lv.tp.toFixed(dg)) : 0;
    if (!data.demo && data.confirm_real) {
      const text = `${dirLabel(m.side)} ${sym.symbol} · risk ${o.risk}% · ${fixed ? `stop ${sl}, hədəf ${tp}` : 'stop/hədəf avtomatik'}`;
      if (!(await ask('REAL hesab: bazar əmri', esc(text), dirLabel(m.side), m.side === 'sell'))) return;
    }
    command(`open_market:${sym.broker}:${m.side}:${o.risk}:${o.managed ? 1 : 0}:${sl}:${tp}`, 'Mövqe açıldı');
    return;
  }
  if (d.risk) { state.opp.risk = Number(d.risk); refresh(); return; }
  if (d.open !== undefined) {
    const o = state.opp; const ok = await ask('Ticarəti aç', `Risk ${o.risk}% · ${o.managed ? 'bot idarə edəcək' : 'yalnız stop və hədəf'}. Bot qiyməti yenidən yoxlayıb açacaq.`, 'Aç');
    if (ok) command(`open_signal:${o.picked}:${o.risk}:${o.managed ? 1 : 0}`, 'Mövqe açıldı'); return;
  }
  if (d.strat) {
    const on = t.getAttribute('aria-checked') !== 'true';
    if (await ask(on ? 'Strategiyanı aç' : 'Strategiyanı söndür', 'Ayar yadda saxlanacaq və bot yenidən başlayacaq.')) {
      try { await api('/api/strategies/toggle', { body: { name: d.strat, enabled: on } }); toast('Yadda saxlandı; bot yenidən başlayır'); refresh(); } catch (err) { toast(err.message, true); }
    } return;
  }
  if (d.reset) { command('guard_reset:' + d.reset, 'Cüt yenidən açıldı'); return; }
  if (d.unfilter) { if (await ask('Filtri sil', 'Bot bu şərtdə yenidən girə biləcək və bu filtri bir daha təklif etməyəcək.', 'Sil', true)) command('filter_remove:' + d.unfilter, 'Filtr silindi'); return; }
  if (d.symToggle) {
    const f = d.set; const cur = state.settingsDraft[f] ?? (await api('/api/settings')).fields.find((x) => x.path === f).value ?? [];
    const next = cur.includes(d.symToggle) ? cur.filter((x) => x !== d.symToggle) : cur.concat([d.symToggle]);
    state.settingsDraft[f] = next; refresh(); return;
  }
  if (d.set && d.kind === 'bool') { state.settingsDraft[d.set] = t.getAttribute('aria-checked') !== 'true'; refresh(); return; }
  if (d.discard !== undefined) { state.settingsDraft = {}; refresh(); return; }
  if (d.save !== undefined) {
    try { const r = await api('/api/settings', { body: { changes: state.settingsDraft } }); state.settingsDraft = {}; toast('Yadda saxlandı; bot yenidən başlayır'); (r.changes || []).slice(0, 3).forEach((c) => toast(c)); refresh(); }
    catch (err) { toast(err.message, true); } return;
  }
});
document.addEventListener('change', (e) => {
  const t = e.target;
  if (t.matches('[data-managed]')) { state.opp.managed = t.checked; return; }
  if (t.matches('[data-lvl]')) { state.market[t.dataset.lvl] = t.value.replace(',', '.'); refresh(); return; }
  if (t.matches('[data-ack]')) { state.opp.ack = t.checked; refresh(); return; }
  if (t.dataset.set && t.dataset.kind && t.dataset.kind !== 'bool') {
    const k = t.dataset.kind; let v = t.value;
    if (k === 'int') v = parseInt(v, 10); else if (k === 'float') v = parseFloat(v); else if (k === 'choice' && v !== '' && !Number.isNaN(Number(v)) && /^[\d.]+$/.test(v)) v = Number(v);
    state.settingsDraft[t.dataset.set] = v; refresh();
  }
});
document.addEventListener('submit', async (e) => {
  const f = e.target;
  if (f.matches('[data-pw]')) {
    e.preventDefault(); const fd = new FormData(f);
    try { await api('/api/password', { body: { old: fd.get('old'), new: fd.get('new') } }); toast('Parol dəyişdi'); f.reset(); } catch (err) { toast(err.message, true); }
  }
});

// ------------------------------------------------------------- login
async function renderLogin() {
  clearInterval(state.timer);
  const s = await fetch('/api/session', { credentials: 'same-origin' }).then((r) => r.json()).catch(() => ({}));
  if (s.authenticated) { route(); return; }
  const setup = !s.has_user;
  $('#app').innerHTML = `<div class="login"><form id="loginform"><div class="brand" style="padding:0"><div class="avatar">M</div><div><b>Murad Bot</b><small>${setup ? 'Panel girişini yaradın' : 'Panelə giriş'}</small></div></div>
    ${setup && !s.can_setup ? '<p class="err">Panel girişi hələ yaradılmayıb. Onu yalnız serverin özündən (SSH tuneli ilə, http://localhost:8000) yaratmaq olar.</p>' : `
    <label>İstifadəçi adı<input name="username" autocomplete="username" required></label>
    <label>Parol<input name="password" type="password" autocomplete="${setup ? 'new-password' : 'current-password'}" minlength="${setup ? 8 : 1}" required></label>
    <p class="err" id="lerr" role="alert"></p><button class="btn primary big">${setup ? 'Girişi yarat' : 'Daxil ol'}</button>`}</form></div>`;
  const form = $('#loginform');
  form.onsubmit = async (e) => {
    e.preventDefault(); const fd = new FormData(form);
    try { await api(setup ? '/api/setup' : '/api/login', { body: { username: fd.get('username'), password: fd.get('password') } }); route(); }
    catch (err) { $('#lerr').textContent = err.message; }
  };
}
window.addEventListener('hashchange', route);
fetch('/api/session', { credentials: 'same-origin' }).then((r) => r.json()).then((s) => (s.authenticated ? route() : renderLogin())).catch(() => renderLogin());
