/* ---------------------------------------------------------------------------
   Client-side navigation.

   The app is server-rendered, which meant every tab tap was a full page load:
   white flash, re-parse, re-run. This intercepts same-origin navigations,
   fetches the page, and swaps <main> in place — so switching tabs feels
   instant and the bottom nav never blinks.

   Falls back to normal navigation on any error, so a bug here can't strand
   anyone on a broken page.
--------------------------------------------------------------------------- */
(() => {
  const main = () => document.querySelector('main.shell');
  const bar = document.getElementById('nprog');
  let busy = false;

  const showBar = on => bar && bar.classList.toggle('on', on);

  function runScripts(container) {
    // innerHTML doesn't execute <script>, so re-create them
    container.querySelectorAll('script').forEach(old => {
      const s = document.createElement('script');
      if (old.src) {
        s.src = old.src;
      } else {
        // Scope it: page scripts declare things like `const series`, and on a
        // second visit a top-level re-declaration throws. No template relies on
        // these being global (checked), so an IIFE is safe.
        s.textContent = '(()=>{' + old.textContent + '\n})();';
      }
      document.body.appendChild(s);
      s.remove();
    });
  }

  function setActive(path) {
    document.querySelectorAll('nav.tabs a').forEach(a => {
      const href = a.getAttribute('href');
      const on = href === '/' ? path === '/' : path.startsWith(href);
      a.classList.toggle('on', on);
    });
  }

  async function go(url, push = true) {
    if (busy) return;
    busy = true;
    showBar(true);
    try {
      const res = await fetch(url, {headers: {'X-Partial': '1'}});
      if (!res.ok || res.redirected) { location.href = url; return; }
      const html = await res.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const fresh = doc.querySelector('main.shell');
      if (!fresh) { location.href = url; return; }

      const swap = () => main().replaceWith(fresh);
      // real cross-fade where supported; instant swap everywhere else
      if (document.startViewTransition && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
        await document.startViewTransition(swap).updateCallbackDone;
      } else {
        swap();
      }
      // page-specific <script> blocks live after </main>
      const holder = document.createElement('div');
      doc.querySelectorAll('body > script:not([src])').forEach(s => holder.appendChild(s));
      runScripts(holder);

      document.title = doc.title;
      if (push) history.pushState({}, '', url);
      setActive(new URL(url, location.origin).pathname);
      window.scrollTo(0, 0);
      dispatchEvent(new CustomEvent('holo:navigated'));
      window.buzz?.(8);
    } catch (e) {
      location.href = url;
    } finally {
      busy = false;
      showBar(false);
    }
  }

  document.addEventListener('click', e => {
    const a = e.target.closest('a');
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || a.target || a.hasAttribute('download')) return;
    const href = a.getAttribute('href');
    if (!href || href.startsWith('#') || href.startsWith('http') || href.startsWith('mailto')) return;
    if (href.startsWith('/export/') || href.startsWith('/logout')) return;  // real navigations
    e.preventDefault();
    if (new URL(href, location.origin).pathname !== location.pathname) go(href);
  });

  addEventListener('popstate', () => go(location.pathname, false));
})();
