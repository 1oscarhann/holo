// ---------- helpers
const gbp = n => n == null ? '—' : '£' + Number(n).toLocaleString('en-GB', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const pct = n => n == null ? '—' : (n > 0 ? '+' : '') + n.toFixed(2) + '%';
const dir = n => n == null || Math.abs(n) < 0.005 ? 'flat' : n > 0 ? 'up' : 'down';
let toastT;
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('show'), 2200);
}
async function api(path, method = 'GET', body) {
  const r = await fetch(path, {method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || 'Something went wrong');
  return j;
}

// ---------- holo sheen on the hero value: reacts to tilt (phone) or scroll/pointer (desktop)
(function () {
  const el = document.querySelector('.hero .value');
  if (!el || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const set = v => el.style.setProperty('--holo', (100 - Math.max(0, Math.min(1, v)) * 100) + '%');
  set(0.15);
  let tilting = false;
  if (window.DeviceOrientationEvent) {
    addEventListener('deviceorientation', e => {
      if (e.gamma == null) return; tilting = true;
      set((e.gamma + 30) / 60);
    }, {passive: true});
  }
  addEventListener('scroll', () => { if (!tilting) set(0.15 + Math.min(scrollY, 300) / 300 * .7); }, {passive: true});
  addEventListener('pointermove', e => { if (!tilting) set(e.clientX / innerWidth); }, {passive: true});
})();

// ---------- line chart (no libs)
function drawLine(canvas, points, opts = {}) {
  const dpr = devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  canvas.width = W * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  if (points.length < 2) return;
  const ys = points.map(p => p.y);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (hi === lo) { hi += 1; lo -= 1; }
  const padY = 10;
  // padX keeps the end dot from being clipped by the canvas edge. Off by
  // default so the sparklines elsewhere are unchanged.
  const padX = opts.padX || 0;
  const X = i => padX + (i / (points.length - 1)) * (W - padX * 2);
  const Y = v => H - padY - ((v - lo) / (hi - lo)) * (H - padY * 2);
  const up = ys[ys.length - 1] >= ys[0];
  // Read the palette rather than hardcoding: these defaults are what every
  // sparkline outside the portfolio tab uses, and a fixed hex would leave them
  // on one theme's colours while the rest of the page followed another.
  const cv = getComputedStyle(document.documentElement);
  const col = opts.color ||
    (cv.getPropertyValue(up ? '--up' : '--down').trim() || (up ? '#5fbf87' : '#ff5252'));

  // fill
  const g = ctx.createLinearGradient(0, 0, 0, H);
  g.addColorStop(0, col + '33'); g.addColorStop(1, col + '00');
  ctx.beginPath(); ctx.moveTo(X(0), H);
  points.forEach((p, i) => ctx.lineTo(X(i), Y(p.y)));
  ctx.lineTo(X(points.length - 1), H); ctx.closePath();
  ctx.fillStyle = g; ctx.fill();

  // line
  ctx.beginPath();
  points.forEach((p, i) => i ? ctx.lineTo(X(i), Y(p.y)) : ctx.moveTo(X(i), Y(p.y)));
  ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.stroke();

  // end dot
  const lx = X(points.length - 1), ly = Y(ys[ys.length - 1]);
  ctx.beginPath(); ctx.arc(lx, ly, 3.5, 0, Math.PI * 2); ctx.fillStyle = col; ctx.fill();
}

/* ---- card image fallback -------------------------------------------------
   Each <img> carries data-alts: a JSON list of backup URLs (pokemontcg.io,
   then a locally drawn placeholder). On load failure we step to the next one.
   Guarded so a broken placeholder can't loop forever.                       */
function holoImg(el) {
  let alts;
  try { alts = JSON.parse(el.dataset.alts || '[]'); } catch (e) { alts = []; }
  if (!alts.length) { el.onerror = null; el.classList.add('img-dead'); return; }
  const next = alts.shift();
  el.dataset.alts = JSON.stringify(alts);
  if (!alts.length) el.onerror = null;   // last resort: stop trying
  el.src = next;
}
window.holoImg = holoImg;
