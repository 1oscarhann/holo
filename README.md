# Holo v2

Multi-user Pokémon card portfolio. Flask, Neon (Postgres), Render, Cloudflare. Prices from TCGdex.

## Deploy

1. **Neon** — create a project, copy the *pooled* connection string
   (`postgresql://...-pooler.../neondb?sslmode=require`).
2. **Render** — new Blueprint from this repo (it reads `render.yaml`). Paste the Neon URL into
   `DATABASE_URL`. `SECRET_KEY` and `CRON_SECRET` are generated for you. Tables are created on first boot.
3. **Cloudflare** — CNAME your subdomain to the `onrender.com` host, orange cloud on.
   SSL/TLS → Full (strict). Add a cache rule: bypass cache for `/api/*` and for any request with a
   `session` cookie, or logged-in pages will get cached and shown to the wrong person.
4. **Uptime Robot** — ping `/login` every 5 min so the instance never sleeps (keeps the 6am refresh thread alive).
5. **Backup cron (do this)** — a Render redeploy restarts the process and resets the scheduler's timer,
   so also hit `https://<your-domain>/api/cron/refresh?key=<CRON_SECRET>` from a Render cron job or a
   Cloudflare Worker cron trigger at 06:05 daily. Running it twice is harmless.

## Local

    pip install -r requirements.txt
    DATABASE_URL=postgresql://... SECRET_KEY=dev ENABLE_SCHEDULER=0 python3 app.py

## Tests

    pip install -r requirements-dev.txt
    eval "$(scripts/pgtest.sh start)"     # throwaway Postgres, prints TEST_DATABASE_URL
    pytest
    scripts/pgtest.sh stop                # deletes the cluster

The suite runs against a real Postgres rather than a stub, because most of what
is worth testing here is a `WHERE user_id = %s` that either scopes a query or
doesn't. `conftest.py` truncates every table between tests, so point
`TEST_DATABASE_URL` at a throwaway database and never at anything real. It
defaults to `postgresql://postgres@127.0.0.1:5433/holotest`, which is what
`scripts/pgtest.sh` sets up.

No test is allowed to reach the network. `conftest.py` replaces every
`requests` verb with a function that raises, so a test that would have hit
TCGdex, pokemontcg.io, ntfy or a Discord webhook fails loudly instead of
passing slowly. Tests that need an API response monkeypatch `app.tcgdex`.

`app.py` reads its configuration into module-level constants at import time, so
`conftest.py` sets the environment before importing it. A test that needs a
different `ADMIN_USER` or `PREMIUM_CODE` monkeypatches the attribute on the
module — that is the value the request handlers actually read.

What's covered, roughly in order of how much damage the bug would do:

| File | What it pins down |
| --- | --- |
| `test_isolation.py` | Every per-user surface probed from a second account: holdings, alerts, watchlist, sales, custom items, exports, snapshots, totals. Plus the reverse — cards and prices are asserted to stay *shared*, because that is the cost model. |
| `test_gates.py` | Logged-out redirects, 401 JSON on `/api/*`, the admin gate failing closed when `ADMIN_USER` is unset, premium read from the database rather than the session, code redemption, signup validation, and the `?next=` open redirect. |
| `test_scan.py` | OCR matching: name cleaning, `l`/`I`→`1` and `O`→`0`, the short-token rule, prefix shortening, the request cap, and that a scan never adds a card by itself. |
| `test_ranking.py` | `rank_key` — exact and prefix sharing a tier, novelty series demoted below every normal tier, newest set first, unknown sets not crashing. |
| `test_images.py` | The `TCGdex → pokemontcg.io → placeholder` chain, `setmap.json` translation, and that an *unverified* pokemontcg.io URL never enters the chain. |
| `test_pricing.py` | `price_from`'s contract — the seam a licensed provider gets swapped in at — plus one price row per card per day and carrying the last price forward when a fetch fails. |
| `test_import.py` | Bulk import: quantity/price parsing, quoted commas, the 300-row cap, and re-importing a card you already own topping up the pile instead of hitting the unique constraint. |

## What's in it

- Accounts (hashed passwords). Every user only sees their own collection.
- Shared price cache: one TCGdex fetch per card per day regardless of how many users hold it.
- Portfolio value, 24h/7d/30d change, value chart, cards/unique/avg, gain vs paid, biggest holdings,
  by-set breakdown with completion %. Movers tab, value by rarity. Card detail with sparkline and edits.
- `/api/dashboard` for the Kindle (needs the login cookie).
- Price alerts (above / below / daily % move) per card, checked after every refresh.
- Morning digest after the 6am refresh: value, day change, biggest mover, any alerts that fired.
- Notifications via ntfy (free push app, user picks a topic) or a Discord webhook. Set on the You tab.
- Shareable portfolio image: drawn on-device, sent to the phone's share sheet.
- All-time high + drawdown from peak on the portfolio page.
- CSV exports: cards, value history, per-card price history.

## Not done yet

- Rate limiting on `/login` and `/signup` (Cloudflare Rate Limiting rule on those paths is the zero-code fix).
- Snapshots start on each user's first login; charts need a few days to mean anything.
- Graded cards use manual prices only. Market data is for raw copies.

## Card images (v2.2)

Images are fetched by the browser directly from a CDN — nothing is downloaded,
cached or stored server-side, so this costs nothing and needs no account.

Order of preference per card:

1. **TCGdex** — `assets.tcgdex.net/.../low.webp` (or `high.webp` on detail pages)
2. **pokemontcg.io** — `images.pokemontcg.io/<set>/<number>.png`
3. **Local placeholder** — `/ph/<set>/<number>.svg`, drawn by Flask

`setmap.json` maps TCGdex set IDs to pokemontcg.io ones (`sv08.5` -> `sv8pt5`),
173 sets, generated by matching set names across both APIs.

### Why the pokemontcg.io check is server-side

On a miss, pokemontcg.io returns **HTTP 404 with a valid Pokemon card-back PNG
in the body**. Browsers render that fine, so an `onerror` handler never fires and
you'd silently show their card back instead of the placeholder. The status code
is a real 404 though, so `resolve_alt_image()` does one `HEAD` per card and
stores the result in `cards.alt_image` / `cards.alt_checked`.

The cards table is shared by all users, so that's one request per card *ever*,
regardless of how many people hold it. New cards resolve on add; anything missed
is picked up by `backfill_alt_images()` during the daily 06:00 refresh.

Existing databases migrate automatically on boot (`ADD COLUMN IF NOT EXISTS`).

## Bulk import

Rewritten after a real 143-row Collectr export resolved 141 names but matched
the right *printing* for only 104 of them, and could not finish inside a
request.

**Two steps.** `POST /import` parses and stages the paste — pure string work, so
it returns immediately — then a background thread resolves each row while the
page polls `/api/import/<id>`. Nothing reaches `holdings` until you have seen
the review table and pressed commit. There is no 300-row cap any more; the
limit is 2,000 and it is reported rather than silent.

**The set column decides the printing.** Ignoring it is what put 37 rows on the
right Pokémon in the wrong set — a Scarlet & Violet Base card landing on a Prize
Pack reprint. Set names and abbreviations resolve through `sets.json`, the
search is constrained to that set, and a row whose card is *not* in the named
set is reported unmatched rather than reattached somewhere plausible.

Each row carries a confidence: `exact` (set and number), `set` (number did not
match), `number` (set unrecognised), `name` (the printing is a guess), or
`unmatched`.

**Collector numbers are preserved verbatim.** `072/080` → `072`, `TG06/TG30` →
`TG06`, `SWSH153` → `SWSH153`, `84a/111` → `84a`. Zero padding is significant:
TCGdex uses `072` for SV-era sets and `44` for older ones, so normalising either
way breaks the match. The old code ran `re.fullmatch(r"\d+")` and dropped
anything with a slash or a letter.

**No column becomes cost basis unless you say so.** Collectr's column is
`collectr_price_gbp` — current market value, not what you paid — and importing
it as cost makes every card show zero gain forever. Unlabelled numbers are never
treated as cost, market values land in their own field, and the review step asks
which it is.

**Variant, condition, finish, grade and region are kept.** Collectr appends
`(Master Ball Pattern)`, `(Full Art)`, `(JP)`, `(CN)`; TCGdex indexes the plain
name, so those are stripped for the lookup and stored alongside the holding.
Grades are parsed out of the condition field (PSA, BGS, CGC, SGC, ACE, TAG) into
`holdings.grade`, which is part of the holdings unique key — the old importer
omitted it from the INSERT, so a PSA 10 collapsed into the raw copy.

**Japanese and Chinese rows never fall back to English.** TCGdex is multilingual
and JP rows are looked up under `/ja`. There is no Chinese endpoint, so CN rows
are reported unmatched with their name, set, number and value preserved. This is
the most important rule in the importer: silently pricing a £200 Chinese Cubone
as a common English one looks confident and is completely wrong, which is worse
than saying "not found". Any region without a TCGdex endpoint stops the same way.

**Graded cards are still priced as raw.** `price_from()` returns one raw market
price and there is no free source of graded prices — TCG Price Lookup's free tier
is TCGplayer raw only, and `tcgapi.net`'s `/v1/comps` (eBay sold comps, grade
parseable from the listing title) is the only genuinely free route. Until that
exists, graded rows import with the grade recorded and the review step says
plainly that they are priced as raw, rather than showing a wrong number quietly.

The test fixture is `tests/fixtures/tests-fixture-collectr.csv`.

## Analytics

Usage is logged server-side into the `events` table and shown at `/stats`.
Set `ADMIN_USER` to your username or anyone logged in can read it.

Because it runs on the server there is no third-party script, nothing for an ad
blocker to block, no cookie banner, and no data leaving your own database.
Tracked: pageviews, signups, cards added, cards sold, watchlist adds, imports
and feedback.

### Optional: Cloudflare Web Analytics — free, cookieless, no consent banner needed.

**If the domain is proxied through Cloudflare (orange cloud):** turn Web Analytics
on in the dashboard and it injects the beacon itself. Leave `CF_ANALYTICS_TOKEN`
unset.

**If it is not proxied** (hitting the `onrender.com` host directly, or DNS-only):
add a site in Web Analytics, copy the site token, and set it as
`CF_ANALYTICS_TOKEN` in Render. The beacon renders only when that variable is
present.

Client-side navigation is handled automatically: the beacon patches the History
API, so tab switches register as separate page views rather than one long visit.

## Premium (beta)

There is no checkout yet. Premium is unlocked with a code:

    PREMIUM_CODE=whatever-you-like

Set it in the environment only. Share it with whoever should have premium;
change the variable and redeploy to revoke.

There are deliberately **no admin write endpoints**. An earlier build let an
admin edit the premium code, delete users and reset passwords over HTTP, behind
a check that returned true when `ADMIN_USER` was unset — so on a fresh deploy
every logged-in user was an admin, and the password-reset route was an account
takeover waiting to happen. All of it was removed.

`/stats` is now read-only and `is_admin()` fails closed: with no `ADMIN_USER`,
nobody is an admin. Granting premium by hand is a SQL statement:

    UPDATE users SET premium = true, premium_since = now() WHERE username = 'x';

One more that the tests caught: `?next=` on the login form was passed straight
to `redirect()`, so `/login?next=https://evil.example` logged you in on the real
site and then bounced you off it — a complete phishing hop wearing the real
domain and a real login. `safe_next()` now accepts only a single-slash relative
path, rejecting protocol-relative `//host`, absolute URLs, and backslashes
(browsers normalise `\` to `/`).

## Landing page

`/` serves a public landing page when logged out and the portfolio when logged
in, so a shared link no longer drops strangers straight into a signup form.
Screenshots live in `static/img/` and are regenerated from real screens.

## Camera scan (premium)

`/scan` reads two regions off a card photo with Tesseract.js **in the browser**,
so scans cost nothing per use and no image ever leaves the phone. It OCRs the
name band at the top and the collector number at the bottom, then posts those
two strings to `/api/scan`.

Matching is deliberately forgiving, because OCR is not:

- TCGdex matches substrings, so a partly-misread name still resolves
  ("mbreon" finds Umbreon).
- Junk tokens are skipped. An early version fell back to the *first* word, so
  "pe mbreon" searched "pe" and returned Annihilape and Morpeko.
- OCR mangles word endings, so shortened prefixes are tried too
  ("Charizara" -> "Charizar" -> Charizard).
- 1 reads as l or I, 0 as O; digits are normalised before parsing.
- The collector number narrows the results but never decides alone.

Nothing is ever added automatically — the person picks from the candidates.

## Design

v0.16 rebuilt the visual system. The previous look — warm-grey dark palette,
glow headers, blurred art backdrops, a rainbow holo overlay, sixteen different
corner radii — read as the default "premium" template every generated app ships
with. It was removed wholesale.

What replaced it:

- Two radii, `--r` (4px) and `--r2` (6px). Nothing else.
- Hairline dividers instead of card-in-card surfaces.
- Card art displayed large and plain. Nothing sits on top of it.
- Uppercase 12px section labels, so the hierarchy reads at a glance.
- Numerals in JetBrains Mono. Prices, values and stats share one voice.

v0.17 kept all of that and changed the two things that carried the most
personality: the colour and the typeface.

**Cool charcoal and steel, not black and acid lime.** `--bg` is `#0b0d10` and
the accent is a desaturated slate blue, `#8fb3d9`. The neutrals carry a blue
cast so the accent sits in the same family rather than on top of it. The point
is low chroma: a page of Pokémon card art is already saturated, and an acid
accent competed with every card on the screen instead of framing it. Green
(`--up`) and clay red (`--down`) are now separate from the accent and mean only
one thing — direction.

The portfolio chart draws a rising line in the *accent*, not in the up-green.
Green everywhere would put the brand colour nowhere on the screen that matters
most, and an instrument draws its own line in its own colour and saves the
semantic hues for the deltas. A falling line still goes red: that is the one
state worth interrupting for.

**One typeface, not three.** Hierarchy comes from size and weight rather than
from switching family. An earlier v0.17 pass used a display serif for headlines;
it read as decoration rather than structure and was dropped.

**The figure face is no longer monospace.** Numerals were JetBrains Mono — a
*coding* face, with terminal letter-spacing and slashed zeros, which made a
portfolio total read like stdout rather than money. `--fig` now follows
`--sans`, so the biggest number on the screen is set in whatever face you
chose. Columns still line up because `body` already sets
`font-feature-settings: "tnum"`; alignment never needed a monospace face, only
tabular figures. Weights and tracking on the big figures were retuned at the
same time — 500 and `-.02em` were tuned for a face with fixed advance widths.

### Themes

Palette and typeface are chosen by the person using the app, on the You tab.
Nine palettes (including two light ones — every other card tracker is dark) and
eight faces, each a complete token set so adding one is additive and cannot
half-apply. The default is **Ember + Bricolage Grotesque**: a near-black with a
warm orange accent, and a grotesque with enough character in its figures to
carry the portfolio total.

Nothing paints a colour it hasn't read from the tokens. `drawLine`'s defaults
come from `--up`/`--down` rather than a fixed hex, so every sparkline in the app
follows the theme; and the share card, which draws to a canvas and so gets no
cascade, reads the palette and the selected family out of the computed style and
passes them into the drawing code. Both were previously stamped with one theme's
colours regardless of what was on screen.

`static/theme.js` is loaded **synchronously in `<head>`, before `app.css`**.
That ordering is the whole feature: stamped after the stylesheet, every page
load flashes the default theme first. `tests/test_theme.py` asserts it, along
with the script carrying no `defer`/`async`.

The choice lives in `localStorage`, not on the user row: it is a per-device
display preference, it has to resolve before the first paint (a round trip
cannot), and it needs no migration. It does not follow you to a second device —
the right trade for a setting you change once. Every read is wrapped, because
`localStorage` throws outright in some private modes.

Only the selected font family is fetched; loading all eight would cost more
than the feature is worth.

The set completion grid also had a real bug: missing cards were dimmed to
`brightness(.22)`, which is indistinguishable from black on a phone. They are
now greyscale at `.62`, so the whole set reads as a checklist.

### Portfolio tab

The graph is the reason the tab exists, so it is now the main object on it. It
used to be 150px tall and wedged between a scrolling card strip above it and
its own range buttons below. Now:

- The chart is 232px (280px from 560px up) and sits directly under the value.
- Its range tabs are one segmented control, with Share moved out of that row so
  the tabs read as a single thing rather than five loose buttons.
- A reserved strip above the canvas — not an overlay, which would put text on
  top of the line exactly where the line is interesting — shows what the
  *selected* window moved by in pounds. The hero already carries the total and
  its chips are fixed windows in percent, so this says something new. Drag
  across the chart and it becomes that day's value and date.
- `drawLine` gained an optional `padX` (default 0, so every other chart in the
  app is untouched) to stop the end dot being clipped by the canvas edge, and
  the portfolio chart now takes `--up`/`--down` from the palette instead of
  `drawLine`'s pre-0.16 mint and salmon.

Below the graph, above the numbers, the top five holdings are shown as art: the
biggest one large, the other four beside it. The art keeps its true aspect and
nothing is laid over it — the featured caption is pushed to the bottom of its
column so it lines up with the captions next to it.

That showcase replaced two sections that were rendering the *same five cards*
twice: the old horizontal `.holdstrip` above the chart, and the "Biggest
holdings" rows below it (`s.top` is already `hs[:5]`, so they were identical).
`tests/test_portfolio_layout.py` asserts the order, since it is the kind of
thing an unrelated edit shuffles by accident.
