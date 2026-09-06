/* ---------------------------------------------------------------------------
   Interaction polish. Everything here is progressive: if the API is missing
   (older Safari, desktop, iOS for haptics) the feature is skipped and nothing
   breaks. No native wrapper required — this all works from the home screen.
--------------------------------------------------------------------------- */
(() => {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* --- haptics: Android supports vibrate, iOS Safari doesn't. Silent no-op --- */
  window.buzz = (ms = 12) => { try { navigator.vibrate?.(ms); } catch {} };

  /* --- count up the portfolio value on first paint ------------------------ */
  function countUp(el) {
    if (reduced || el.dataset.counted) return;
    el.dataset.counted = '1';
    const txt = el.textContent.trim();
    const target = parseFloat(txt.replace(/[^0-9.]/g, ''));
    if (!isFinite(target) || target <= 0) return;
    const dur = 620, t0 = performance.now();
    const fmt = n => n.toLocaleString('en-GB', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    (function tick(now) {
      const p = Math.min(1, (now - t0) / dur);
      const e = 1 - Math.pow(1 - p, 3);           // easeOutCubic
      el.textContent = fmt(target * e);
      if (p < 1) requestAnimationFrame(tick);
      else el.textContent = fmt(target);
    })(t0);
  }

  /* --- long press on a card row -> quick actions --------------------------- */
  function sheet(items) {
    const back = document.createElement('div');
    back.className = 'sheet-back';
    back.innerHTML = `<div class="sheet">${items.map((i, n) =>
      `<button data-n="${n}"${i.danger ? ' class="danger"' : ''}>${i.label}</button>`
    ).join('')}<button class="cancel">Cancel</button></div>`;
    document.body.appendChild(back);
    requestAnimationFrame(() => back.classList.add('on'));
    const close = () => { back.classList.remove('on'); setTimeout(() => back.remove(), 200); };
    back.onclick = e => {
      if (e.target === back || e.target.classList.contains('cancel')) return close();
      const n = e.target.dataset.n;
      if (n != null) { close(); items[+n].run(); }
    };
  }

  let timer = null;
  document.addEventListener('pointerdown', e => {
    const row = e.target.closest('[data-card]');
    if (!row) return;
    timer = setTimeout(() => {
      buzz(18);
      const id = row.dataset.card, hid = row.dataset.hid;
      const items = [];
      if (hid) items.push({label: 'Open card', run: () => location.href = '/card/' + hid});
      items.push({label: 'Watch this card', run: async () => {
        await api('/api/watchlist', 'POST', {card_id: id}); toast('Added to watchlist'); }});
      items.push({label: 'Compare with…', run: () => {
        sessionStorage.setItem('cmp', id); toast('Now long-press another card'); }});
      const first = sessionStorage.getItem('cmp');
      if (first && first !== id) items.push({label: 'Compare with picked card',
        run: () => { sessionStorage.removeItem('cmp');
                     location.href = `/compare?id=${first}&id=${id}`; }});
      sheet(items);
    }, 480);
  });
  ['pointerup', 'pointermove', 'pointercancel', 'scroll'].forEach(ev =>
    document.addEventListener(ev, () => { clearTimeout(timer); }, {passive: true}));

  /* --- pull to refresh ----------------------------------------------------- */
  let y0 = null, pulling = false;
  const ind = document.createElement('div');
  ind.className = 'ptr'; ind.textContent = '↓';
  document.body.appendChild(ind);

  addEventListener('touchstart', e => {
    y0 = scrollY <= 0 ? e.touches[0].clientY : null;
  }, {passive: true});

  addEventListener('touchmove', e => {
    if (y0 == null) return;
    const dy = e.touches[0].clientY - y0;
    if (dy > 0 && scrollY <= 0) {
      pulling = dy > 70;
      ind.style.transform = `translateX(-50%) translateY(${Math.min(dy * .45, 62)}px)`;
      ind.classList.toggle('ready', pulling);
    }
  }, {passive: true});

  addEventListener('touchend', () => {
    ind.style.transform = '';
    ind.classList.remove('ready');
    if (pulling) { buzz(14); location.reload(); }
    y0 = null; pulling = false;
  }, {passive: true});

  /* --- run on load and after every client-side page swap ------------------- */
  function enhance() {
    // target the inner span, not the whole .value — the £ lives outside it
    document.querySelectorAll('.hero .value [data-count]').forEach(countUp);
  }
  enhance();
  addEventListener('holo:navigated', enhance);
})();
