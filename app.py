#!/usr/bin/env python3
"""
Holo v2 — multi-user Pokémon card portfolio.

Stack: Flask + Neon (Postgres) on Render, behind Cloudflare.

    pip install -r requirements.txt
    DATABASE_URL=postgres://... SECRET_KEY=... python3 app.py

Prices: TCGdex (free, no key). Cardmarket EUR converted to GBP, TCGplayer USD as fallback.
Price cache is shared across all users; one fetch per card per day.
Snapshots: one row per user per day, taken at 06:00 (and on first login of the day).
"""

import csv
import json
import os
from urllib.parse import quote
import psycopg
from psycopg.types.json import Json
from psycopg.rows import dict_row
import threading
import time
import secrets
from datetime import date, datetime, timedelta
from functools import wraps

import re
import requests
from flask import (Flask, g, jsonify, redirect, render_template, request,
                   session, url_for, abort)
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

DATABASE_URL = os.environ["DATABASE_URL"]
CRON_SECRET = os.environ.get("CRON_SECRET")
TCGDEX = "https://api.tcgdex.net/v2/en"
EUR_GBP = float(os.environ.get("EUR_GBP", "0.85"))
USD_GBP = float(os.environ.get("USD_GBP", "0.78"))
PRICE_TTL_HOURS = 20
PTCGIO_IMG = "https://images.pokemontcg.io"
VERSION = os.environ.get("APP_VERSION", "0.16.0-beta")

# Cloudflare Web Analytics. Only needed for the manual setup (site not proxied,
# e.g. hitting the onrender.com host directly). If the domain is orange-clouded,
# Cloudflare injects the beacon itself and this can stay unset.
CF_ANALYTICS_TOKEN = os.environ.get("CF_ANALYTICS_TOKEN", "")

# No payments yet. Premium is unlocked with a code you hand out — set PREMIUM_CODE
# in the environment, share it, revoke it by changing it. Swap this for a real
# checkout later; nothing else needs to change.
PREMIUM_CODE = os.environ.get("PREMIUM_CODE", "")
ADMIN_USER = os.environ.get("ADMIN_USER", "")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # Render + Cloudflare in front
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")))

# ------------------------------------------------------------------ card images
#
# TCGdex is missing art for a fair number of cards (Paldean Fates, Stellar Crown
# secrets, most recent promos). pokemontcg.io's image CDN covers most of those,
# but its set IDs differ from TCGdex's ("sv08.5" vs "sv8pt5"), so we carry a
# generated lookup table. Order of preference per card:
#
#     TCGdex  ->  pokemontcg.io  ->  locally drawn SVG placeholder
#
# Nothing is downloaded or cached server-side; these are just URLs the browser
# fetches directly. The stepping between them happens client-side in app.js.

def _load(fn, default):
    try:
        with open(os.path.join(os.path.dirname(__file__), fn)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


SETMAP = _load("setmap.json", {})   # tcgdex set id -> pokemontcg.io set id
SETS = _load("sets.json", {})       # tcgdex set id -> name / abbr / release / total
MARKET = _load("market.json", [])   # curated chase-card watchlist, priced daily
MARKET_IDS = [c["id"] for c in MARKET]


def set_meta(set_id):
    return SETS.get(set_id) or {}


def ptcgio_url(set_id, local_id, hi=False):
    """Guess the pokemontcg.io URL for a card, or None if we can't map the set."""
    alt_set = SETMAP.get(set_id)
    if not alt_set or not local_id:
        return None
    return f"{PTCGIO_IMG}/{alt_set}/{local_id}{'_hires.png' if hi else '.png'}"


def img_chain(image, set_id, local_id, hi=False, alt_image=None):
    """Ordered list of candidate image URLs for one card, best first.

    `alt_image` is the pokemontcg.io URL we have already *verified* returns 200.
    That check has to happen server-side: on a miss pokemontcg.io returns 404 but
    with a Pokemon card-back PNG in the body, which a browser renders happily —
    so an onerror handler alone would never fall through to our placeholder.
    """
    size = "high" if hi else "low"
    urls = []
    if image:
        urls.append(f"{image}/{size}.webp")
    if alt_image:
        urls.append(alt_image.replace(".png", "_hires.png") if hi else alt_image)
    # literal path, not url_for(), so this is safe to call off the request thread
    urls.append(f"/ph/{quote(str(set_id or 'x'), safe='')}/{quote(str(local_id or 'x'), safe='')}.svg")
    return urls


def with_images(d):
    """Attach img/img_hi plus their fallback chains to a card-ish dict."""
    a = d.get("alt_image")
    lo = img_chain(d.get("image"), d.get("set_id"), d.get("local_id"), alt_image=a)
    hi = img_chain(d.get("image"), d.get("set_id"), d.get("local_id"), hi=True, alt_image=a)
    d["img"], d["img_alts"] = lo[0], lo[1:]
    d["img_hi"], d["img_hi_alts"] = hi[0], hi[1:]
    return d


def resolve_alt_image(card_id, set_id, local_id, conn=None):
    """One-time HEAD against pokemontcg.io; remember whether it has this card.

    Runs at most once per card ever (the cards table is shared by all users),
    so this costs a single request no matter how many people hold the card.
    Stores the URL on success, NULL on a miss; either way stamps alt_checked
    so we never ask twice.
    """
    url = ptcgio_url(set_id, local_id)
    found = None
    if url:
        try:
            r = requests.head(url, timeout=6, allow_redirects=True)
            if r.status_code == 200:
                found = url
        except requests.RequestException:
            return None  # transient: leave unchecked so we retry another day
    (conn or db()).execute(
        "UPDATE cards SET alt_image=%s, alt_checked=now() WHERE id=%s", (found, card_id))
    if conn is None:
        db().commit()
    return found


def backfill_alt_images(conn, limit=40):
    """Resolve pokemontcg.io art for held cards that TCGdex has no image for."""
    rows = conn.execute("""
        SELECT id, set_id, local_id FROM cards
        WHERE image IS NULL AND alt_checked IS NULL
          AND id IN (SELECT card_id FROM holdings)
        LIMIT %s""", (limit,)).fetchall()
    n = 0
    for r in rows:
        if resolve_alt_image(r["id"], r["set_id"], r["local_id"], conn):
            n += 1
    conn.commit()
    return n


@app.template_global()
def img_attrs(card, hi=False):
    """Renders src + fallback list + handler. Use as: <img {{ img_attrs(c) }}>"""
    src = card["img_hi"] if hi else card["img"]
    alts = card.get("img_hi_alts" if hi else "img_alts") or []
    return Markup('src="{}" data-alts="{}" onerror="holoImg(this)" loading="lazy"'.format(
        escape(src), escape(json.dumps(alts))))


@app.route("/ph/<set_id>/<local_id>.svg")
def placeholder(set_id, local_id):
    """Card-shaped tile for the handful of cards neither CDN has art for."""
    label = escape(str(local_id)[:6])
    sub = escape(str(set_id).upper()[:10])
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 245 342">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#2a211c"/><stop offset="1" stop-color="#1b1512"/>
</linearGradient></defs>
<rect width="245" height="342" rx="14" fill="url(#g)"/>
<rect x="6" y="6" width="233" height="330" rx="10" fill="none"
      stroke="#3b2f27" stroke-width="1.5"/>
<text x="122.5" y="168" text-anchor="middle" fill="#8a7264"
      font-family="system-ui,sans-serif" font-size="42" font-weight="700">{label}</text>
<text x="122.5" y="196" text-anchor="middle" fill="#77604f"
      font-family="system-ui,sans-serif" font-size="14" letter-spacing="2">{sub}</text>
<text x="122.5" y="300" text-anchor="middle" fill="#4e3f36"
      font-family="system-ui,sans-serif" font-size="11" letter-spacing="1">NO ART</text>
</svg>"""
    return app.response_class(svg, mimetype="image/svg+xml",
                              headers={"Cache-Control": "public, max-age=604800"})


# --------------------------------------------------------------------------- db

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  pw TEXT NOT NULL, created TIMESTAMPTZ NOT NULL DEFAULT now(),
  premium BOOLEAN NOT NULL DEFAULT false, premium_since TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY, name TEXT, set_id TEXT, set_name TEXT, local_id TEXT,
  rarity TEXT, image TEXT, set_total INTEGER,
  alt_image TEXT, alt_checked TIMESTAMPTZ, data JSONB);

-- beta usage log. Server-side, so ad blockers can't touch it and there's no
-- third-party script, no consent banner, and no data leaving your database.
CREATE TABLE IF NOT EXISTS events (
  id BIGSERIAL PRIMARY KEY,
  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, path TEXT, meta JSONB,
  at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS events_at ON events (at DESC);
CREATE INDEX IF NOT EXISTS events_user ON events (user_id, at DESC);
CREATE INDEX IF NOT EXISTS events_kind ON events (kind, at DESC);

-- sealed product and anything else without a TCGdex entry
CREATE TABLE IF NOT EXISTS custom_items (
  id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL, kind TEXT DEFAULT 'sealed', qty INTEGER DEFAULT 1,
  paid REAL, value REAL, note TEXT, added TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS prices (
  card_id TEXT, day DATE, gbp REAL, source TEXT, fetched TIMESTAMPTZ,
  PRIMARY KEY (card_id, day));
CREATE TABLE IF NOT EXISTS holdings (
  id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  card_id TEXT REFERENCES cards(id), qty INTEGER DEFAULT 1,
  paid REAL, manual_gbp REAL, grade TEXT NOT NULL DEFAULT '', added TIMESTAMPTZ DEFAULT now(),
  UNIQUE(user_id, card_id, grade));
CREATE TABLE IF NOT EXISTS snapshots (
  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, day DATE, total REAL, cards INTEGER,
  PRIMARY KEY (user_id, day));
CREATE INDEX IF NOT EXISTS prices_card_day ON prices (card_id, day DESC);
ALTER TABLE users ADD COLUMN IF NOT EXISTS ntfy_topic TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS discord_webhook TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS digest BOOLEAN NOT NULL DEFAULT TRUE;
CREATE TABLE IF NOT EXISTS alerts (
  id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  card_id TEXT REFERENCES cards(id), kind TEXT NOT NULL, threshold REAL NOT NULL,
  created TIMESTAMPTZ DEFAULT now(), fired TIMESTAMPTZ, fired_price REAL);

-- cards you don't own but want to watch. Alerts work on these too.
CREATE TABLE IF NOT EXISTS watchlist (
  id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  card_id TEXT REFERENCES cards(id), added TIMESTAMPTZ DEFAULT now(),
  UNIQUE (user_id, card_id));

-- disposals. Paper value is not the same as money made.
CREATE TABLE IF NOT EXISTS sales (
  id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  card_id TEXT REFERENCES cards(id), qty INTEGER NOT NULL DEFAULT 1,
  sold REAL NOT NULL, paid REAL, sold_on DATE NOT NULL DEFAULT CURRENT_DATE,
  venue TEXT, note TEXT, created TIMESTAMPTZ DEFAULT now());

-- every card in a set, so the completion grid works without a live API call
CREATE TABLE IF NOT EXISTS set_cards (
  set_id TEXT, card_id TEXT, local_id TEXT, name TEXT, image TEXT,
  sort_n INTEGER, fetched TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (set_id, card_id));
CREATE INDEX IF NOT EXISTS set_cards_set ON set_cards (set_id, sort_n);
CREATE INDEX IF NOT EXISTS watchlist_user ON watchlist (user_id);
CREATE INDEX IF NOT EXISTS sales_user ON sales (user_id, sold_on DESC);

-- Bulk import runs in two steps: parse (fast, no network) then resolve (one
-- TCGdex round trip per row, ~1.5s). A 143-row paste is 3-4 minutes, which no
-- request survives on a 120s worker, so rows land here first and a background
-- thread fills in the matches while the page polls.
CREATE TABLE IF NOT EXISTS import_jobs (
  id SERIAL PRIMARY KEY,
  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  state TEXT NOT NULL DEFAULT 'resolving',
  total INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
  columns JSONB, paid_column TEXT, error TEXT,
  created TIMESTAMPTZ DEFAULT now(), finished TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS import_jobs_user ON import_jobs (user_id, created DESC);
CREATE TABLE IF NOT EXISTS import_rows (
  id SERIAL PRIMARY KEY,
  job_id INTEGER REFERENCES import_jobs(id) ON DELETE CASCADE,
  n INTEGER NOT NULL,
  raw JSONB,
  name TEXT, variant TEXT, set_name TEXT, number TEXT, region TEXT,
  qty INTEGER NOT NULL DEFAULT 1, paid REAL, market REAL,
  condition TEXT, grade TEXT, finish TEXT,
  card_id TEXT, set_id TEXT, confidence TEXT, note TEXT,
  skip BOOLEAN NOT NULL DEFAULT false);
CREATE INDEX IF NOT EXISTS import_rows_job ON import_rows (job_id, n);

-- Graded prices, shared across users exactly like `prices`: one lookup per
-- (card, grade) per refresh serves everybody. Kept in its own table rather
-- than as rows in `prices` because the cadence, the source and the confidence
-- are all different — comps are sparse, so `samples` decides whether a number
-- is trustworthy enough to show.
CREATE TABLE IF NOT EXISTS graded_prices (
  card_id TEXT, grade TEXT, day DATE,
  gbp REAL, low REAL, high REAL, samples INTEGER NOT NULL DEFAULT 0,
  source TEXT, fetched TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (card_id, grade, day));
CREATE INDEX IF NOT EXISTS graded_card ON graded_prices (card_id, grade, day DESC);
"""


def raw_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def db():
    if "db" not in g:
        g.db = raw_db()
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


with raw_db() as c:
    c.execute(SCHEMA)
    # migrations for databases created before v2.2
    c.execute("ALTER TABLE cards ADD COLUMN IF NOT EXISTS alt_image TEXT")
    c.execute("ALTER TABLE cards ADD COLUMN IF NOT EXISTS alt_checked TIMESTAMPTZ")
    c.execute("ALTER TABLE cards ADD COLUMN IF NOT EXISTS data JSONB")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS premium BOOLEAN NOT NULL DEFAULT false")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_since TIMESTAMPTZ")
    # v0.18: the importer was dropping variant, condition, finish and region on
    # the floor. Grade already existed and is part of the holdings unique key.
    for col, typ in (("condition", "TEXT"), ("finish", "TEXT"),
                     ("region", "TEXT"), ("variant", "TEXT")):
        c.execute(f"ALTER TABLE holdings ADD COLUMN IF NOT EXISTS {col} {typ}")
    # A graded sale is the only price signal nobody else has. Recording the
    # grade on the sale turns the sold log into a second source.
    c.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS grade TEXT")

# ------------------------------------------------------------------------- auth


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("uid"):
            if request.path.startswith("/api/"):
                return jsonify(error="login required"), 401
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w


def uid():
    return session["uid"]


def safe_next(target):
    """Return ?next= only if it is a path on this site, else None.

    Unvalidated, this hands anyone a redirect off the domain that runs after a
    real login on the real login form — which is the whole of a phishing hop.
    Only a single-slash relative path is allowed: "//evil" is protocol-relative
    and browsers normalise a backslash to a slash, so both are rejected too.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return None
    if "\\" in target or any(ch in target for ch in "\r\n\t") or "\0" in target:
        return None
    return target


@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        u = request.form.get("username", "").strip().lower()
        p = request.form.get("password", "")
        row = db().execute("SELECT * FROM users WHERE username=%s", (u,)).fetchone()
        if row and check_password_hash(row["pw"], p):
            session["uid"] = row["id"]
            session["username"] = u
            ensure_snapshot(row["id"])
            return redirect(safe_next(request.args.get("next")) or url_for("home"))
        err = "Wrong username or password."
    return render_template("auth.html", mode="login", err=err)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    err = None
    if request.method == "POST":
        u = request.form.get("username", "").strip().lower()
        p = request.form.get("password", "")
        if not (3 <= len(u) <= 20) or not u.replace("_", "").isalnum():
            err = "Username: 3–20 letters, numbers or underscores."
        elif len(p) < 8:
            err = "Password needs at least 8 characters."
        elif db().execute("SELECT 1 FROM users WHERE username=%s", (u,)).fetchone():
            err = "That username's taken."
        else:
            new_id = db().execute("INSERT INTO users (username,pw) VALUES (%s,%s) RETURNING id",
                                  (u, generate_password_hash(p))).fetchone()["id"]
            db().commit()
            session["uid"] = new_id
            session["username"] = u
            log_event("signup", username=u)
            return redirect(url_for("home"))
    return render_template("auth.html", mode="signup", err=err)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ----------------------------------------------------------------------- tcgdex


def tcgdex(path, **params):
    try:
        r = requests.get(f"{TCGDEX}/{path}", params=params, timeout=12)
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        pass
    return None


def price_from(card):
    """Return (gbp, source) from a TCGdex card object, or (None, None)."""
    p = (card or {}).get("pricing") or {}
    cm = p.get("cardmarket") or {}
    for k in ("trend", "avg7", "avg", "avg30", "low"):
        v = cm.get(k)
        if v:
            return round(v * EUR_GBP, 2), f"cardmarket.{k}"
    tp = p.get("tcgplayer") or {}
    for variant in ("holofoil", "reverse-holofoil", "normal", "1st-edition-holofoil", "1st-edition"):
        v = (tp.get(variant) or {}).get("marketPrice")
        if v:
            return round(v * USD_GBP, 2), f"tcgplayer.{variant}"
    return None, None


def upsert_card(card, conn=None):
    s = card.get("set") or {}
    # keep the whole payload: HP, attacks, weaknesses, illustrator, legality etc.
    keep = {k: card.get(k) for k in (
        "category", "hp", "types", "stage", "suffix", "evolveFrom", "attacks",
        "abilities", "weaknesses", "resistances", "retreat", "regulationMark",
        "illustrator", "description", "dexId", "legal", "variants") if card.get(k) is not None}
    (conn or db()).execute("""INSERT INTO cards
        (id,name,set_id,set_name,local_id,rarity,image,set_total,data)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, set_id=EXCLUDED.set_id,
        set_name=EXCLUDED.set_name, local_id=EXCLUDED.local_id, rarity=EXCLUDED.rarity,
        image=EXCLUDED.image, set_total=EXCLUDED.set_total,
        data=COALESCE(EXCLUDED.data, cards.data)""",
                 (card["id"], card.get("name"), s.get("id"), s.get("name"), card.get("localId"),
                  card.get("rarity"), card.get("image"),
                  (s.get("cardCount") or {}).get("total"),
                  Json(keep) if keep else None))


def refresh_price(card_id, force=False, conn=None):
    """Fetch today's price if stale. Returns gbp or None."""
    c = conn or db()
    today = date.today().isoformat()
    row = c.execute("SELECT gbp, fetched FROM prices WHERE card_id=%s AND day=%s",
                    (card_id, today)).fetchone()
    if row and not force:
        age = datetime.now(row["fetched"].tzinfo) - row["fetched"]
        if age < timedelta(hours=PRICE_TTL_HOURS):
            return row["gbp"]
    card = tcgdex(f"cards/{card_id}")
    if not card:
        return row["gbp"] if row else None
    gbp, src = price_from(card)
    if gbp is None:
        # carry forward last known
        last = c.execute("SELECT gbp, source FROM prices WHERE card_id=%s ORDER BY day DESC LIMIT 1",
                         (card_id,)).fetchone()
        if last:
            gbp, src = last["gbp"], "carried"
    if gbp is not None:
        c.execute("""INSERT INTO prices (card_id,day,gbp,source,fetched) VALUES (%s,%s,%s,%s,now())
                     ON CONFLICT (card_id, day) DO UPDATE SET gbp=EXCLUDED.gbp, source=EXCLUDED.source,
                     fetched=EXCLUDED.fetched""", (card_id, today, gbp, src))
    upsert_card(card, c)
    if conn is None:
        c.commit()
    return gbp

# ------------------------------------------------------------ graded pricing
#
# price_from() returns one raw market price. A PSA 10 routinely trades at
# several multiples of raw, so a graded collection priced from it is
# systematically undervalued — a GBP44 Ditto V showed as roughly GBP1.
#
# There is no free graded feed. TCG Price Lookup's free tier is TCGplayer raw
# only; graded needs their paid Trader plan. The one genuinely free route is
# eBay *sold* comps, where the grade is in the listing title and can be parsed
# out. That is what this does.
#
# Two properties matter more than coverage here. Comps are sparse, so a median
# over two sales is noise and is not shown; and if the source is unreachable
# the card falls back to raw and says so, rather than inventing a number.

# The graders whose slabs actually turn up in a UK collection. Used both to
# read a condition field on import and to read a grade out of a listing title.
GRADERS = ("PSA", "BGS", "CGC", "SGC", "ACE", "TAG")
_GRADE_RE = re.compile(
    r"\b(" + "|".join(GRADERS) + r")\s*\.?\s*(10|[0-9](?:\.5)?)\b", re.I)

GRADED_API = os.environ.get("GRADED_API", "https://tcgapi.net/v1")
GRADED_API_KEY = os.environ.get("GRADED_API_KEY", "")
GRADED_MIN_SAMPLES = int(os.environ.get("GRADED_MIN_SAMPLES", "3"))
GRADED_TTL_DAYS = int(os.environ.get("GRADED_TTL_DAYS", "7"))

# Titles are free text written by sellers: "PSA 10 GEM MINT", "psa10",
# "BGS 9.5 (Black Label)", "CGC 8.5". Whitespace between company and number is
# optional and the qualifier that follows is ignored.
_TITLE_GRADE_RE = re.compile(
    r"\b(" + "|".join(GRADERS) + r")\s*-?\s*(10|[0-9](?:\.5)?)\b", re.I)


def grade_from_title(title):
    """Pull 'PSA 10' out of an eBay listing title, or None if it is raw."""
    if not title:
        return None
    m = _TITLE_GRADE_RE.search(str(title))
    if not m:
        return None
    num = m.group(2)
    if num.endswith(".0"):
        num = num[:-2]
    return f"{m.group(1).upper()} {num}"


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def fetch_comps(card, timeout=12):
    """Sold comps for one card. Returns a list of {title, price, currency}.

    The response shape is read defensively: this is a third-party feed that has
    already changed hands once, and a schema change should degrade to "no
    graded price" rather than a traceback on the portfolio page.
    """
    name = (card or {}).get("name")
    if not name or not GRADED_API:
        return []
    q = " ".join(x for x in (name, card.get("set_name"), card.get("local_id")) if x)
    params = {"q": q, "limit": 60}
    if GRADED_API_KEY:
        params["key"] = GRADED_API_KEY
    try:
        r = requests.get(f"{GRADED_API}/comps", params=params, timeout=timeout)
        if r.status_code != 200:
            return []
        data = r.json()
    except (requests.RequestException, ValueError):
        return []

    if isinstance(data, dict):
        for key in ("comps", "results", "sales", "data", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            return []
    if not isinstance(data, list):
        return []

    out = []
    for it in data:
        if not isinstance(it, dict):
            continue
        title = it.get("title") or it.get("name") or it.get("listing")
        price = None
        for key in ("price", "sold_price", "soldPrice", "amount", "value", "total"):
            v = it.get(key)
            if isinstance(v, dict):
                v = v.get("value") or v.get("amount")
            if isinstance(v, (int, float)) and v > 0:
                price = float(v); break
            if isinstance(v, str):
                try:
                    price = float(re.sub(r"[^\d.]", "", v)); break
                except ValueError:
                    pass
        if title and price:
            out.append({"title": title, "price": price,
                        "currency": (it.get("currency") or "USD").upper()})
    return out


def _to_gbp(amount, currency):
    if currency == "GBP":
        return amount
    if currency == "EUR":
        return amount * EUR_GBP
    return amount * USD_GBP          # comps are overwhelmingly USD


def crowd_graded_price(card_id, grade, conn=None):
    """Graded sales our own users recorded. Sparse at first, but nobody else has it."""
    c = conn or db()
    rows = c.execute("""SELECT sold, qty FROM sales
                        WHERE card_id=%s AND grade=%s AND sold > 0
                          AND sold_on > CURRENT_DATE - INTERVAL '180 days'""",
                     (card_id, grade)).fetchall()
    vals = [r["sold"] / max(1, r["qty"] or 1) for r in rows]
    if not vals:
        return None
    return {"gbp": round(_median(vals), 2), "samples": len(vals),
            "low": round(min(vals), 2), "high": round(max(vals), 2),
            "source": "sold-log"}


def refresh_graded_price(card_id, grade, force=False, conn=None):
    """Price one (card, grade) from comps, falling back to our own sold log.

    Returns the stored row, or None when there is nothing trustworthy. Shared
    across users: one refresh serves everyone holding that card at that grade.
    """
    c = conn or db()
    today = date.today()
    if not force:
        row = c.execute("""SELECT * FROM graded_prices WHERE card_id=%s AND grade=%s
                           ORDER BY day DESC LIMIT 1""", (card_id, grade)).fetchone()
        if row and (today - row["day"]).days < GRADED_TTL_DAYS:
            return row

    card = c.execute("SELECT id,name,set_name,local_id FROM cards WHERE id=%s",
                     (card_id,)).fetchone()
    agg = None
    if card:
        vals = [_to_gbp(x["price"], x["currency"]) for x in fetch_comps(dict(card))
                if grade_from_title(x["title"]) == grade]
        if len(vals) >= GRADED_MIN_SAMPLES:
            agg = {"gbp": round(_median(vals), 2), "samples": len(vals),
                   "low": round(min(vals), 2), "high": round(max(vals), 2),
                   "source": "ebay-comps"}
    if agg is None:
        agg = crowd_graded_price(card_id, grade, c)
    if agg is None:
        return None

    c.execute("""INSERT INTO graded_prices (card_id,grade,day,gbp,low,high,samples,source,fetched)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now())
                 ON CONFLICT (card_id,grade,day) DO UPDATE SET
                   gbp=EXCLUDED.gbp, low=EXCLUDED.low, high=EXCLUDED.high,
                   samples=EXCLUDED.samples, source=EXCLUDED.source, fetched=now()""",
              (card_id, grade, today, agg["gbp"], agg["low"], agg["high"],
               agg["samples"], agg["source"]))
    if conn is None:
        c.commit()
    return c.execute("""SELECT * FROM graded_prices WHERE card_id=%s AND grade=%s
                        AND day=%s""", (card_id, grade, today)).fetchone()


def graded_price(card_id, grade, conn=None):
    """Latest stored graded price, or None. Never fetches — read path only."""
    if not grade:
        return None
    c = conn or db()
    row = c.execute("""SELECT * FROM graded_prices WHERE card_id=%s AND grade=%s
                       AND samples >= %s ORDER BY day DESC LIMIT 1""",
                    (card_id, grade, GRADED_MIN_SAMPLES)).fetchone()
    return dict(row) if row else None


def refresh_graded_all(conn, limit=200):
    """Weekly sweep of every (card, grade) anyone actually holds.

    Weekly rather than daily: comps move slowly, the feed is rate-limited, and
    CLAUDE.md's plan is graded weekly for free accounts and daily for paid.
    """
    # Ordered so a truncated sweep resumes predictably rather than re-rolling
    # which cards get looked up.
    rows = conn.execute("""SELECT DISTINCT card_id, grade FROM holdings
                           WHERE grade <> '' AND grade IS NOT NULL
                           ORDER BY card_id, grade LIMIT %s""", (limit,)).fetchall()
    n = 0
    for r in rows:
        try:
            if refresh_graded_price(r["card_id"], r["grade"], conn=conn):
                n += 1
        except Exception:
            pass                     # one bad card must not stop the sweep
        conn.commit()
        time.sleep(0.25)
    return n


# -------------------------------------------------------------------- portfolio


def holdings_with_prices(user_id, conn=None):
    c = conn or db()
    rows = c.execute("""
        SELECT h.id hid, h.qty, h.paid, h.manual_gbp, h.grade, h.added,
               c.id, c.name, c.set_id, c.set_name, c.local_id, c.rarity, c.image, c.alt_image, c.set_total
        FROM holdings h JOIN cards c ON c.id=h.card_id WHERE h.user_id=%s""", (user_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        hist = c.execute("SELECT day, gbp FROM prices WHERE card_id=%s ORDER BY day DESC LIMIT 31",
                         (r["id"],)).fetchall()
        hist = [{"day": h["day"].isoformat(), "gbp": h["gbp"]} for h in hist]
        raw = hist[0]["gbp"] if hist else None

        # A graded card is not worth what a raw one is. price_basis says which
        # number this is, so the UI can stop claiming a PSA 10 is worth raw
        # money and can show when it is falling back.
        g = graded_price(d["id"], d["grade"], c) if d["grade"] else None
        if d["manual_gbp"] is not None:
            latest, d["price_basis"] = d["manual_gbp"], "manual"
        elif g:
            latest, d["price_basis"] = g["gbp"], "graded"
            d["graded"] = {"samples": g["samples"], "source": g["source"],
                           "low": g["low"], "high": g["high"], "raw": raw}
        elif d["grade"]:
            latest, d["price_basis"] = raw, "raw-fallback"
        else:
            latest, d["price_basis"] = raw, "raw"
        d["price"] = latest
        d["value"] = (latest or 0) * d["qty"]
        d["history"] = list(reversed(hist))
        d["added"] = d["added"].isoformat() if d["added"] else None

        def ago(n):
            target = (date.today() - timedelta(days=n)).isoformat()
            for h in hist:
                if h["day"] <= target:
                    return h["gbp"]
            return None
        # Only the raw series belongs to this number. A graded price moves on a
        # different clock and a manual one does not move at all, so neither gets
        # a change figure rather than being handed the raw card's.
        movable = d["price_basis"] == "raw"
        d["chg1"] = pct(latest, ago(1)) if movable else None
        d["chg7"] = pct(latest, ago(7)) if movable else None
        d["chg30"] = pct(latest, ago(30)) if movable else None
        with_images(d)
        out.append(d)
    out.sort(key=lambda x: -(x["value"] or 0))
    return out


def pct(now, then):
    if now is None or then in (None, 0):
        return None
    return round((now - then) / then * 100, 2)


def snapshot_value(user_id, day_offset=0, conn=None):
    c = conn or db()
    target = (date.today() - timedelta(days=day_offset)).isoformat()
    r = c.execute("SELECT total FROM snapshots WHERE user_id=%s AND day<=%s ORDER BY day DESC LIMIT 1",
                  (user_id, target)).fetchone()
    return r["total"] if r else None


def ensure_snapshot(user_id, conn=None, force=False):
    c = conn or db()
    today = date.today().isoformat()
    if not force and c.execute("SELECT 1 FROM snapshots WHERE user_id=%s AND day=%s",
                               (user_id, today)).fetchone():
        return
    hs = holdings_with_prices(user_id, c)
    total = round(sum(h["value"] for h in hs), 2)
    c.execute("""INSERT INTO snapshots (user_id,day,total,cards) VALUES (%s,%s,%s,%s)
                 ON CONFLICT (user_id, day) DO UPDATE SET total=EXCLUDED.total, cards=EXCLUDED.cards""",
              (user_id, today, total, sum(h["qty"] for h in hs)))
    c.commit()


def stats(user_id):
    hs = holdings_with_prices(user_id)
    total = round(sum(h["value"] for h in hs), 2)
    cost = round(sum((h["paid"] or 0) * h["qty"] for h in hs if h["paid"]), 2)
    snaps = db().execute("SELECT day,total FROM snapshots WHERE user_id=%s ORDER BY day DESC LIMIT 90",
                         (user_id,)).fetchall()
    series = [{"day": s["day"].isoformat(), "total": s["total"]} for s in reversed(snaps)]
    if not series or series[-1]["day"] != date.today().isoformat():
        series.append({"day": date.today().isoformat(), "total": total})

    def chg(n):
        then = snapshot_value(user_id, n)
        return {"pct": pct(total, then), "abs": round(total - then, 2) if then is not None else None}

    by_set = {}
    for h in hs:
        k = h["set_name"] or "Unknown set"
        e = by_set.setdefault(k, {"set": k, "set_id": h["set_id"], "value": 0, "qty": 0,
                                  "unique": set(), "total": h["set_total"]})
        e["value"] += h["value"]
        e["qty"] += h["qty"]
        e["unique"].add(h["id"])
    sets = []
    for e in by_set.values():
        e["owned"] = len(e["unique"])
        e["completion"] = round(e["owned"] / e["total"] * 100, 1) if e["total"] else None
        e["share"] = round(e["value"] / total * 100, 1) if total else 0
        e["value"] = round(e["value"], 2)
        del e["unique"]
        sets.append(e)
    sets.sort(key=lambda x: -x["value"])

    by_rarity = {}
    for h in hs:
        k = h["rarity"] or "Unknown"
        by_rarity[k] = round(by_rarity.get(k, 0) + h["value"], 2)
    rarity = sorted([{"rarity": k, "value": v} for k, v in by_rarity.items()],
                    key=lambda x: -x["value"])

    movers = [h for h in hs if h["chg7"] is not None and h["price"]]
    movers.sort(key=lambda h: -abs(h["chg7"] * h["value"]))
    gainers = sorted([h for h in movers if h["chg7"] > 0], key=lambda h: -h["chg7"])[:5]
    losers = sorted([h for h in movers if h["chg7"] < 0], key=lambda h: h["chg7"])[:5]

    ath = max(series, key=lambda p: p["total"]) if series else None
    atl = min(series, key=lambda p: p["total"]) if series else None

    return {
        "total": total, "cost": cost,
        "ath": ath, "atl": atl,
        "drawdown": pct(total, ath["total"]) if ath else None,
        "pnl": {"abs": round(total - cost, 2), "pct": pct(total, cost)} if cost else None,
        "cards": sum(h["qty"] for h in hs), "unique": len(hs),
        "chg1": chg(1), "chg7": chg(7), "chg30": chg(30),
        "series": series, "sets": sets, "rarity": rarity,
        "top": hs[:5], "gainers": gainers, "losers": losers,
        "avg_card": round(total / sum(h["qty"] for h in hs), 2) if hs else 0,
    }

# ------------------------------------------------------------------------ pages


@app.route("/")
def home():
    """Logged out: a landing page. Logged in: the portfolio."""
    if not session.get("uid"):
        return render_template("landing.html")
    return index()


def index():
    return render_template("index.html", s=stats(uid()),
                           activity=recent_activity(uid(), 8), page="portfolio")


@app.route("/cards")
@login_required
def cards_page():
    return render_template("cards.html", cards=holdings_with_prices(uid()), page="cards")


@app.route("/add")
@login_required
def add_page():
    return render_template("add.html", page="portfolio")


@app.route("/movers")
@login_required
def movers_page():
    return render_template("movers.html", s=stats(uid()), alerts=user_alerts(uid()), page="market")


@app.route("/profile")
@login_required
def profile_page():
    u = db().execute("SELECT username, ntfy_topic, discord_webhook, digest FROM users WHERE id=%s", (uid(),)).fetchone()
    return render_template("profile.html", s=stats(uid()), u=u, page="profile")


@app.route("/card/<hid>")
@login_required
def card_page(hid):
    h = next((x for x in holdings_with_prices(uid()) if str(x["hid"]) == hid), None)
    if not h:
        abort(404)
    u = db().execute("SELECT ntfy_topic, discord_webhook FROM users WHERE id=%s", (uid(),)).fetchone()
    h["detail"] = card_detail(h["id"])
    return render_template("card.html", c=h, alerts=user_alerts(uid(), h["id"]),
                           has_notify=bool(u["ntfy_topic"] or u["discord_webhook"]), page="cards")

# -------------------------------------------------------------------------- api


# Digital-only: TCG Pocket cards do not physically exist, so they have no place
# in a portfolio of real cardboard. Filtered out of search entirely.
DIGITAL_SERIES = {"Pok\u00e9mon TCG Pocket"}

# Real cards, but rarely what someone means when they search a Pokemon's name.
NOVELTY_SERIES = {"Trainer kits", "McDonald's Collection", "POP"}


def rank_key(card, q):
    """Sort key for search results. Lower sorts first.

    TCGdex returns matches in no useful order, so 'pika' led with a 2020 futsal
    promo and a trainer-kit Raichu. Rank on name match, demote novelty sets,
    then newest first — which is what people are usually pulling out of a pack.
    """
    name = (card.get("name") or "").lower()
    meta = set_meta(card.get("set_id", ""))
    # exact and prefix share a tier, so "Umbreon ex" isn't buried under every
    # plain Umbreon ever printed
    if name == q or name.startswith(q):
        tier = 0
    elif f" {q}" in name:
        tier = 1
    else:
        tier = 2
    if meta.get("serie") in NOVELTY_SERIES:
        tier += 3
    # newest first: negate the date so it sorts ascending with everything else
    rel = meta.get("release") or "0000-00-00"
    return (tier, [-int(x) for x in rel.replace("-", " ").split()], name)


@app.route("/api/search")
@login_required
def api_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    res = tcgdex("cards", name=q) or []

    cards = []
    for c in res:
        set_id = c["id"].rsplit("-", 1)[0]
        meta = set_meta(set_id)
        if meta.get("serie") in DIGITAL_SERIES:
            continue
        cards.append({
            "id": c["id"], "name": c.get("name"), "local_id": c.get("localId"),
            "image": c.get("image"), "set_id": set_id,
            "set_name": meta.get("name") or set_id,
            "set_abbr": meta.get("abbr"),
            "release": meta.get("release"),
            "set_total": meta.get("total"),
        })

    ql = q.lower()
    cards.sort(key=lambda c: rank_key(c, ql))
    cards = cards[:40]

    # Attach any price we already have cached. Deliberately cache-only: looking
    # up 40 live prices per keystroke would be slow and hammer the API.
    ids = [c["id"] for c in cards]
    prices = {}
    if ids:
        rows = db().execute("""
            SELECT DISTINCT ON (card_id) card_id, gbp
            FROM prices WHERE card_id = ANY(%s)
            ORDER BY card_id, day DESC""", (ids,)).fetchall()
        prices = {r["card_id"]: r["gbp"] for r in rows}

    out = []
    for c in cards:
        c["price"] = float(prices[c["id"]]) if prices.get(c["id"]) is not None else None
        out.append(with_images(c))
    return jsonify(out)


@app.route("/api/holdings", methods=["POST"])
@login_required
def api_add():
    d = request.get_json(force=True)
    card_id = d.get("card_id")
    card = tcgdex(f"cards/{card_id}")
    if not card:
        return jsonify(error="Card not found on TCGdex."), 404
    upsert_card(card)
    if not card.get("image"):
        # TCGdex has no art for this one — see if pokemontcg.io does. Once, ever.
        resolve_alt_image(card_id, card_id.rsplit("-", 1)[0], card.get("localId"))
    log_event("card_added", card=card_id, name=card.get("name"))
    refresh_price(card_id, force=True)
    grade = (d.get("grade") or "").strip()
    db().execute("""INSERT INTO holdings (user_id,card_id,qty,paid,manual_gbp,grade)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (user_id,card_id,grade) DO UPDATE SET qty=holdings.qty+EXCLUDED.qty""",
                 (uid(), card_id, int(d.get("qty") or 1), d.get("paid"), d.get("manual_gbp"), grade))
    db().commit()
    return jsonify(ok=True)


@app.route("/api/holdings/<int:hid>", methods=["PATCH", "DELETE"])
@login_required
def api_holding(hid):
    own = db().execute("SELECT 1 FROM holdings WHERE id=%s AND user_id=%s", (hid, uid())).fetchone()
    if not own:
        return jsonify(error="not yours"), 403
    if request.method == "DELETE":
        db().execute("DELETE FROM holdings WHERE id=%s", (hid,))
    else:
        d = request.get_json(force=True)
        if "grade" in d:
            d["grade"] = (d["grade"] or "").strip()
        for k in ("qty", "paid", "manual_gbp", "grade"):
            if k in d:
                db().execute(f"UPDATE holdings SET {k}=%s WHERE id=%s", (d[k], hid))
    db().commit()
    return jsonify(ok=True)


@app.route("/api/alerts", methods=["POST"])
@login_required
def api_alert_add():
    d = request.get_json(force=True)
    if d.get("kind") not in ("above", "below", "pct") or not d.get("card_id"):
        return jsonify(error="bad alert"), 400
    try:
        thr = float(d.get("threshold"))
    except (TypeError, ValueError):
        return jsonify(error="threshold must be a number"), 400
    db().execute("INSERT INTO alerts (user_id,card_id,kind,threshold) VALUES (%s,%s,%s,%s)",
                 (uid(), d["card_id"], d["kind"], thr))
    db().commit()
    return jsonify(ok=True)


@app.route("/api/alerts/<int:aid>", methods=["DELETE"])
@login_required
def api_alert_del(aid):
    db().execute("DELETE FROM alerts WHERE id=%s AND user_id=%s", (aid, uid()))
    db().commit()
    return jsonify(ok=True)


def user_alerts(user_id, card_id=None):
    q = """SELECT a.*, c.name, c.set_name, c.local_id, c.image, c.alt_image, c.set_id,
                  (SELECT MIN(id) FROM holdings h WHERE h.user_id=a.user_id AND h.card_id=a.card_id) AS hid
           FROM alerts a JOIN cards c ON c.id=a.card_id
           WHERE a.user_id=%s""" + (" AND a.card_id=%s" if card_id else "") + " ORDER BY a.fired NULLS FIRST, a.created DESC"
    rows = db().execute(q, (user_id, card_id) if card_id else (user_id,)).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        r["fired"] = r["fired"].isoformat() if r["fired"] else None
        r["created"] = r["created"].isoformat()
        with_images(r)
        r["label"] = {"above": f"above £{r['threshold']:,.2f}", "below": f"below £{r['threshold']:,.2f}",
                      "pct": f"moves {r['threshold']:g}% in a day"}[r["kind"]]
        out.append(r)
    return out


@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings():
    d = request.get_json(force=True)
    topic = (d.get("ntfy_topic") or "").strip() or None
    hook = (d.get("discord_webhook") or "").strip() or None
    if hook and not hook.startswith("https://discord.com/api/webhooks/"):
        return jsonify(error="That doesn't look like a Discord webhook URL"), 400
    db().execute("UPDATE users SET ntfy_topic=%s, discord_webhook=%s, digest=%s WHERE id=%s",
                 (topic, hook, bool(d.get("digest", True)), uid()))
    db().commit()
    return jsonify(ok=True)


@app.route("/api/settings/test", methods=["POST"])
@login_required
def api_settings_test():
    u = db().execute("SELECT * FROM users WHERE id=%s", (uid(),)).fetchone()
    ok = notify(u, "Holo test", "If you can read this, alerts will work.")
    return jsonify(ok=ok, error=None if ok else "Nothing configured or delivery failed")


@app.route("/api/digest/preview", methods=["POST"])
@login_required
def api_digest_preview():
    u = db().execute("SELECT * FROM users WHERE id=%s", (uid(),)).fetchone()
    send_digest(u, db())
    return jsonify(ok=True)


@app.route("/export/cards.csv")
@login_required
def export_cards():
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["card_id", "name", "set", "number", "rarity", "grade", "qty", "paid_each_gbp",
                "price_gbp", "value_gbp", "chg_7d_pct", "chg_30d_pct", "added"])
    for h in holdings_with_prices(uid()):
        w.writerow([h["id"], h["name"], h["set_name"], h["local_id"], h["rarity"], h["grade"], h["qty"],
                    h["paid"], h["price"], round(h["value"], 2), h["chg7"], h["chg30"], h["added"]])
    return buf.getvalue(), 200, {"Content-Type": "text/csv",
                                 "Content-Disposition": "attachment; filename=holo-cards.csv"}


@app.route("/export/history.csv")
@login_required
def export_history():
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["day", "total_gbp", "cards"])
    for r in db().execute("SELECT day,total,cards FROM snapshots WHERE user_id=%s ORDER BY day", (uid(),)):
        w.writerow([r["day"].isoformat(), r["total"], r["cards"]])
    return buf.getvalue(), 200, {"Content-Type": "text/csv",
                                 "Content-Disposition": "attachment; filename=holo-history.csv"}


@app.route("/export/prices.csv")
@login_required
def export_prices():
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["card_id", "name", "day", "price_gbp"])
    rows = db().execute("""SELECT p.card_id, c.name, p.day, p.gbp FROM prices p JOIN cards c ON c.id=p.card_id
        WHERE p.card_id IN (SELECT card_id FROM holdings WHERE user_id=%s) ORDER BY c.name, p.day""", (uid(),))
    for r in rows:
        w.writerow([r["card_id"], r["name"], r["day"].isoformat(), r["gbp"]])
    return buf.getvalue(), 200, {"Content-Type": "text/csv",
                                 "Content-Disposition": "attachment; filename=holo-prices.csv"}


@app.route("/api/refresh", methods=["POST"])
@login_required
def api_refresh():
    ids = [r["card_id"] for r in db().execute(
        "SELECT DISTINCT card_id FROM holdings WHERE user_id=%s", (uid(),))]
    n = 0
    for cid in ids:
        if refresh_price(cid, force=True) is not None:
            n += 1
        time.sleep(0.15)
    ensure_snapshot(uid(), force=True)
    return jsonify(ok=True, refreshed=n, of=len(ids))


@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify(stats(uid()))


@app.route("/api/dashboard")
@login_required
def api_dashboard():
    s = stats(uid())
    return jsonify(total=s["total"], chg7=s["chg7"], series=s["series"][-7:], cards=s["cards"])

# --------------------------------------------------------------------- scheduler


# ----------------------------------------------------------------- notify


def notify(user, title, body, conn=None):
    """Push to whatever the user has set up. Never raises."""
    sent = False
    if user.get("ntfy_topic"):
        try:
            requests.post(f"https://ntfy.sh/{user['ntfy_topic']}", data=body.encode(),
                          headers={"Title": title, "Tags": "flower_playing_cards"}, timeout=8)
            sent = True
        except requests.RequestException:
            pass
    if user.get("discord_webhook"):
        try:
            requests.post(user["discord_webhook"], json={"content": f"**{title}**\n{body}"}, timeout=8)
            sent = True
        except requests.RequestException:
            pass
    return sent


def fmt_gbp(n):
    return f"£{n:,.2f}"


def check_alerts(conn):
    """Fire any un-fired alerts whose condition now holds. Returns {user_id: [messages]}."""
    fired = {}
    rows = conn.execute("""
        SELECT a.*, c.name, u.ntfy_topic, u.discord_webhook FROM alerts a
        JOIN cards c ON c.id=a.card_id JOIN users u ON u.id=a.user_id WHERE a.fired IS NULL""").fetchall()
    for a in rows:
        hist = conn.execute("SELECT gbp FROM prices WHERE card_id=%s ORDER BY day DESC LIMIT 2",
                            (a["card_id"],)).fetchall()
        if not hist:
            continue
        now = hist[0]["gbp"]
        prev = hist[1]["gbp"] if len(hist) > 1 else None
        hit, msg = False, ""
        if a["kind"] == "above" and now >= a["threshold"]:
            hit, msg = True, f"{a['name']} is {fmt_gbp(now)} — above your {fmt_gbp(a['threshold'])} target"
        elif a["kind"] == "below" and now <= a["threshold"]:
            hit, msg = True, f"{a['name']} dropped to {fmt_gbp(now)} — below {fmt_gbp(a['threshold'])}"
        elif a["kind"] == "pct" and prev:
            ch = pct(now, prev)
            if ch is not None and abs(ch) >= a["threshold"]:
                hit, msg = True, f"{a['name']} moved {ch:+.1f}% today ({fmt_gbp(prev)} → {fmt_gbp(now)})"
        if hit:
            conn.execute("UPDATE alerts SET fired=now(), fired_price=%s WHERE id=%s", (now, a["id"]))
            fired.setdefault(a["user_id"], []).append(msg)
            notify(a, "Price alert", msg)
    conn.commit()
    return fired


def send_digest(user, conn, alert_msgs=()):
    hs = holdings_with_prices(user["id"], conn)
    if not hs:
        return
    total = round(sum(h["value"] for h in hs), 2)
    then = snapshot_value(user["id"], 1, conn)
    ch = pct(total, then)
    lines = [f"Collection: {fmt_gbp(total)}" +
             (f" ({ch:+.2f}%, {fmt_gbp(total - then) if total >= then else '-' + fmt_gbp(then - total)})" if ch is not None else "")]
    movers = [h for h in hs if h["chg1"] is not None]
    if movers:
        m = max(movers, key=lambda h: abs(h["chg1"]))
        if abs(m["chg1"]) >= 0.5:
            lines.append(f"Biggest move: {m['name']} {m['chg1']:+.1f}% → {fmt_gbp(m['price'])}")
    lines += list(alert_msgs)
    notify(user, "Morning, here's your binder", "\n".join(lines))


# ------------------------------------------------------------------ set pages


def set_catalogue(set_id, conn=None):
    """Every card in a set, cached locally so the grid needs no live API call."""
    c = conn or db()
    rows = c.execute(
        "SELECT * FROM set_cards WHERE set_id=%s ORDER BY sort_n, local_id", (set_id,)).fetchall()
    if rows:
        return rows
    data = tcgdex(f"sets/{set_id}")
    if not data:
        return []
    for card in data.get("cards") or []:
        try:
            n = int(str(card.get("localId")))
        except (TypeError, ValueError):
            n = 99999                      # promos, TG/GG subsets: sort last
        c.execute("""INSERT INTO set_cards (set_id,card_id,local_id,name,image,sort_n)
                     VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                  (set_id, card["id"], card.get("localId"), card.get("name"),
                   card.get("image"), n))
    c.commit()
    return c.execute(
        "SELECT * FROM set_cards WHERE set_id=%s ORDER BY sort_n, local_id", (set_id,)).fetchall()


@app.route("/set/<set_id>")
@login_required
def set_page(set_id):
    meta = set_meta(set_id)
    cards = set_catalogue(set_id)
    owned = {r["card_id"]: r for r in db().execute("""
        SELECT h.card_id, h.id AS hid, SUM(h.qty) AS qty
        FROM holdings h WHERE h.user_id=%s GROUP BY h.card_id, h.id""", (uid(),)).fetchall()}
    watched = {r["card_id"] for r in db().execute(
        "SELECT card_id FROM watchlist WHERE user_id=%s", (uid(),)).fetchall()}
    prices = {r["card_id"]: r["gbp"] for r in db().execute("""
        SELECT DISTINCT ON (card_id) card_id, gbp FROM prices
        WHERE card_id = ANY(%s) ORDER BY card_id, day DESC""",
        ([c["card_id"] for c in cards],)).fetchall()} if cards else {}

    grid = []
    for c in cards:
        d = dict(c, id=c["card_id"], set_id=set_id)
        d["owned"] = c["card_id"] in owned
        d["hid"] = owned.get(c["card_id"], {}).get("hid")
        d["watched"] = c["card_id"] in watched
        d["price"] = prices.get(c["card_id"])
        grid.append(with_images(d))

    n_owned = sum(1 for g in grid if g["owned"])
    return render_template("set.html", set_id=set_id, meta=meta, grid=grid,
                           n_owned=n_owned, n_total=len(grid),
                           pct=round(n_owned / len(grid) * 100) if grid else 0,
                           value=sum(g["price"] or 0 for g in grid if g["owned"]),
                           page="cards")


# ------------------------------------------------------------------- watchlist


@app.route("/watchlist")
@login_required
def watchlist_page():
    rows = db().execute("""
        SELECT w.card_id, w.added, c.name, c.set_id, c.local_id, c.image, c.alt_image,
               c.set_name,
               (SELECT gbp FROM prices p WHERE p.card_id=w.card_id ORDER BY day DESC LIMIT 1) AS price,
               (SELECT gbp FROM prices p WHERE p.card_id=w.card_id ORDER BY day DESC OFFSET 1 LIMIT 1) AS prev
        FROM watchlist w JOIN cards c ON c.id=w.card_id
        WHERE w.user_id=%s ORDER BY w.added DESC""", (uid(),)).fetchall()
    for r in rows:
        r["pct"] = ((r["price"] - r["prev"]) / r["prev"] * 100
                    if r["price"] and r["prev"] else None)
        with_images(r)
    alerts = user_alerts(uid())
    return render_template("watchlist.html", rows=rows, alerts=alerts, page="portfolio")


@app.route("/api/watchlist", methods=["POST", "DELETE"])
@login_required
def api_watchlist():
    card_id = (request.get_json(force=True) or {}).get("card_id")
    if not card_id:
        return jsonify(error="card_id required"), 400
    if request.method == "DELETE":
        db().execute("DELETE FROM watchlist WHERE user_id=%s AND card_id=%s", (uid(), card_id))
        db().commit()
        return jsonify(watched=False)
    if not db().execute("SELECT 1 FROM cards WHERE id=%s", (card_id,)).fetchone():
        card = tcgdex(f"cards/{card_id}")
        if not card:
            return jsonify(error="Card not found."), 404
        upsert_card(card)
    db().execute("""INSERT INTO watchlist (user_id, card_id) VALUES (%s,%s)
                    ON CONFLICT DO NOTHING""", (uid(), card_id))
    log_event("watchlist_added", card=card_id)
    db().commit()
    refresh_price(card_id)
    return jsonify(watched=True)


# -------------------------------------------------------------------- sold log


@app.route("/sold")
@login_required
def sold_page():
    rows = db().execute("""
        SELECT s.*, c.name, c.set_id, c.local_id, c.image, c.alt_image, c.set_name
        FROM sales s JOIN cards c ON c.id=s.card_id
        WHERE s.user_id=%s ORDER BY s.sold_on DESC, s.id DESC""", (uid(),)).fetchall()
    for r in rows:
        r["gross"] = (r["sold"] or 0) * (r["qty"] or 1)
        r["cost"] = (r["paid"] or 0) * (r["qty"] or 1)
        r["pnl"] = r["gross"] - r["cost"] if r["paid"] is not None else None
        with_images(r)
    realised = sum(r["pnl"] or 0 for r in rows)
    return render_template("sold.html", rows=rows,
                           gross=sum(r["gross"] for r in rows),
                           realised=realised, page="cards")


@app.route("/api/sales", methods=["POST"])
@login_required
def api_sales():
    d = request.get_json(force=True)
    hid = d.get("hid")
    row = db().execute("SELECT * FROM holdings WHERE id=%s AND user_id=%s",
                       (hid, uid())).fetchone() if hid else None
    if not row:
        return jsonify(error="Holding not found."), 404
    qty = max(1, min(int(d.get("qty") or 1), row["qty"]))
    db().execute("""INSERT INTO sales (user_id,card_id,qty,sold,paid,sold_on,venue,note,grade)
                    VALUES (%s,%s,%s,%s,%s,COALESCE(%s,CURRENT_DATE),%s,%s,%s)""",
                 (uid(), row["card_id"], qty, float(d.get("sold") or 0),
                  row["paid"], d.get("sold_on") or None, d.get("venue"), d.get("note"),
                  row["grade"] or None))
    if qty >= row["qty"]:
        db().execute("DELETE FROM holdings WHERE id=%s", (hid,))
    else:
        db().execute("UPDATE holdings SET qty=qty-%s WHERE id=%s", (qty, hid))
    log_event("card_sold", card=row["card_id"], qty=qty, sold=float(d.get("sold") or 0))
    db().commit()
    return jsonify(ok=True)


# --------------------------------------------------------------- activity feed


def recent_activity(user_id, limit=25):
    """Adds and sales interleaved, newest first."""
    adds = db().execute("""
        SELECT 'add' AS kind, h.added AS at, h.qty, h.paid, NULL::real AS sold,
               c.id AS card_id, c.name, c.set_name, c.set_id, c.local_id,
               c.image, c.alt_image, h.id AS hid
        FROM holdings h JOIN cards c ON c.id=h.card_id
        WHERE h.user_id=%s ORDER BY h.added DESC LIMIT %s""", (user_id, limit)).fetchall()
    sells = db().execute("""
        SELECT 'sold' AS kind, s.created AS at, s.qty, s.paid, s.sold,
               c.id AS card_id, c.name, c.set_name, c.set_id, c.local_id,
               c.image, c.alt_image, NULL::int AS hid
        FROM sales s JOIN cards c ON c.id=s.card_id
        WHERE s.user_id=%s ORDER BY s.created DESC LIMIT %s""", (user_id, limit)).fetchall()
    out = sorted(adds + sells, key=lambda r: r["at"], reverse=True)[:limit]
    for r in out:
        with_images(r)
    return out


# ----------------------------------------------------------- all-time extremes


def market_alltime(limit=10):
    """Biggest move from each card's own low/high, across all recorded history."""
    rows = db().execute("""
        WITH agg AS (
          SELECT card_id, MIN(gbp) AS lo, MAX(gbp) AS hi, COUNT(*) AS days
          FROM prices WHERE card_id = ANY(%s) AND gbp IS NOT NULL
          GROUP BY card_id HAVING COUNT(*) > 1),
        latest AS (
          SELECT DISTINCT ON (card_id) card_id, gbp FROM prices
          WHERE card_id = ANY(%s) ORDER BY card_id, day DESC)
        SELECT c.id, c.name, c.set_id, c.local_id, c.image, c.alt_image,
               l.gbp AS price, a.lo, a.hi, a.days,
               CASE WHEN a.lo > 0 THEN (l.gbp - a.lo) / a.lo * 100 END AS from_low,
               CASE WHEN a.hi > 0 THEN (l.gbp - a.hi) / a.hi * 100 END AS from_high
        FROM agg a JOIN latest l ON l.card_id=a.card_id JOIN cards c ON c.id=a.card_id
        WHERE l.gbp IS NOT NULL
    """, (MARKET_IDS, MARKET_IDS)).fetchall()
    for r in rows:
        r["set_name"] = set_meta(r["set_id"]).get("name") or r["set_id"]
        with_images(r)
    return {
        "risen": sorted([r for r in rows if r["from_low"]], key=lambda r: -r["from_low"])[:limit],
        "fallen": sorted([r for r in rows if r["from_high"]], key=lambda r: r["from_high"])[:limit],
    }


def sparklines(card_ids, days=30):
    """{card_id: [price, ...]} for tiny inline charts."""
    if not card_ids:
        return {}
    rows = db().execute("""
        SELECT card_id, day, gbp FROM prices
        WHERE card_id = ANY(%s) AND gbp IS NOT NULL AND day > CURRENT_DATE - %s
        ORDER BY card_id, day""", (card_ids, days)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["card_id"], []).append(round(float(r["gbp"]), 2))
    return out


def refresh_market(conn):
    """Price the curated watchlist so the market page works for everyone.

    These cards aren't necessarily owned by anyone. Prices land in the same
    shared `prices` table, so if a user later adds one of them it's already
    priced and their history starts populated rather than empty.
    """
    n = 0
    for c in MARKET:
        try:
            upsert_card({"id": c["id"], "name": c["name"], "localId": c["local_id"],
                         "image": c.get("image"), "rarity": None,
                         "set": {"id": c["set_id"],
                                 "name": set_meta(c["set_id"]).get("name"),
                                 "cardCount": {"total": set_meta(c["set_id"]).get("total")}}},
                        conn=conn)
            if refresh_price(c["id"], force=True, conn=conn) is not None:
                n += 1
        except Exception:
            continue
        time.sleep(0.15)
    conn.commit()
    return n


def market_movers(limit=12):
    """Biggest 24h movers across the watchlist, plus the most valuable."""
    rows = db().execute("""
        WITH latest AS (
          SELECT DISTINCT ON (card_id) card_id, day, gbp
          FROM prices WHERE card_id = ANY(%s) ORDER BY card_id, day DESC),
        prev AS (
          SELECT DISTINCT ON (p.card_id) p.card_id, p.gbp
          FROM prices p JOIN latest l ON l.card_id = p.card_id AND p.day < l.day
          ORDER BY p.card_id, p.day DESC)
        SELECT c.id, c.name, c.set_id, c.local_id, c.image, c.alt_image,
               l.gbp AS price, pr.gbp AS prev,
               CASE WHEN pr.gbp > 0 THEN (l.gbp - pr.gbp) / pr.gbp * 100 END AS pct
        FROM latest l
        JOIN cards c ON c.id = l.card_id
        LEFT JOIN prev pr ON pr.card_id = l.card_id
        WHERE l.gbp IS NOT NULL
    """, ([c["id"] for c in MARKET],)).fetchall()

    for r in rows:
        r["set_name"] = set_meta(r["set_id"]).get("name") or r["set_id"]
        with_images(r)

    moved = [r for r in rows if r["pct"] is not None]
    return {
        "gainers": sorted(moved, key=lambda r: -r["pct"])[:limit],
        "losers": sorted(moved, key=lambda r: r["pct"])[:limit],
        "top": sorted(rows, key=lambda r: -(r["price"] or 0))[:limit],
        "count": len(rows),
        "has_history": bool(moved),
    }


@app.route("/market")
@login_required
def market():
    m = market_movers()
    shown = {r["id"] for grp in ("gainers", "losers", "top") for r in m[grp]}
    return render_template("market.html", m=m, alltime=market_alltime(),
                           spark=sparklines(list(shown)), page="market")


# Navigation, rebuilt.
#
# The old design had two stacked layers — five bottom tabs plus a segmented
# control — which meant up to ten tap targets before any content, and a "+"
# button wedged into the tab bar as a third pattern. Now:
#
#   3 tabs        Portfolio · Browse · You      (the only persistent nav)
#   1 add button  floating, always reachable
#   everything else is a pushed page with a back arrow
#
# Not every destination deserves to be a tab. Most are things you visit, do
# something, and leave.
TABS = [
    ("portfolio", "Portfolio", "/",        "M3 17l5-6 4 3 5-8 4 5"),
    ("browse",    "Browse",    "/market",  "M4 14l6-6 4 4 6-6 M14 6h6v6"),
    ("you",       "You",       "/profile", "M12 4a4 4 0 110 8 4 4 0 010-8 M4 21c0-4 4-6 8-6s8 2 8 6"),
]

# child page -> (title, parent url). Anything listed here renders a back header
# instead of appearing in the tab bar.
PAGES = {
    "/cards":     ("Your cards",   "/"),
    "/watchlist": ("Watchlist",    "/"),
    "/insights":  ("Insights",     "/"),
    "/movers":    ("Your movers",  "/"),
    "/sets":      ("All sets",     "/market"),
    "/sold":      ("Sold",         "/profile"),
    "/import":    ("Import cards", "/profile"),
    "/stats":     ("Admin",        "/profile"),
    "/compare":   ("Compare",      "/market"),
    "/add":       ("Add a card",   "/"),
    "/scan":      ("Scan a card",  "/"),
}


@app.context_processor
def inject_nav():
    here = request.path
    crumb = PAGES.get(here)
    if crumb is None and here.startswith("/card/"):
        crumb = ("", "/cards")
    elif crumb is None and here.startswith("/set/"):
        crumb = ("", "/sets")
    # Which tab lights up is derived from the path, walking up parents until we
    # reach one. Routes don't have to declare it, so it can't drift.
    tab_for = {u: k for k, _, u, _ in TABS}
    node = here
    seen = 0
    while node not in tab_for and seen < 5:
        parent = PAGES.get(node, (None, None))[1]
        if parent is None:
            parent = ("/cards" if node.startswith("/card/")
                      else "/sets" if node.startswith("/set/") else "/")
        node = parent
        seen += 1
    return {"TABS": TABS, "HERE": here, "NAVTAB": tab_for.get(node, "portfolio"),
            "CRUMB_TITLE": crumb[0] if crumb else None,
            "CRUMB_BACK": crumb[1] if crumb else None}


@app.context_processor
def inject_version():
    return {"VERSION": VERSION, "sv": static_v, "CF_TOKEN": CF_ANALYTICS_TOKEN,
            "IS_ADMIN": is_admin()}


_SV_CACHE = {}


def static_v(filename):
    """Static URL with a content stamp: /static/app.js?v=<mtime>.

    Static files are cached hard (a day in the browser, longer at the edge), so
    without this a deploy would keep serving the previous build's JS and CSS and
    the new code would silently never run.
    """
    if filename not in _SV_CACHE or app.debug:
        try:
            m = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            m = 0
        _SV_CACHE[filename] = m
    return f"/static/{filename}?v={_SV_CACHE[filename]}"


@app.route("/api/feedback", methods=["POST"])
@login_required
def api_feedback():
    """Beta feedback -> the same Discord webhook alerts already use."""
    d = request.get_json(force=True) or {}
    msg = (d.get("message") or "").strip()
    if not msg:
        return jsonify(error="Say something first"), 400
    u = db().execute("SELECT username, discord_webhook FROM users WHERE id=%s", (uid(),)).fetchone()
    hook = os.environ.get("FEEDBACK_WEBHOOK") or u.get("discord_webhook")
    if not hook:
        return jsonify(error="No feedback webhook configured yet."), 400
    try:
        requests.post(hook, json={"content":
            f"**Holo {VERSION}** feedback from `{u['username']}`\n"
            f"page: `{d.get('page') or '?'}`\n{msg[:1500]}"}, timeout=8)
    except requests.RequestException:
        return jsonify(error="Couldn't send. Try again."), 502
    log_event("feedback", chars=len(msg))
    return jsonify(ok=True)


@app.route("/manifest.json")
def manifest():
    """Installable to the home screen: no browser chrome, own app switcher entry."""
    return jsonify({
        "name": "Holo", "short_name": "Holo",
        "description": "Pok\u00e9mon card portfolio tracker",
        "start_url": "/", "scope": "/",
        "display": "standalone", "orientation": "portrait",
        "background_color": "#0d0907", "theme_color": "#0d0907",
        "icons": [
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "maskable"},
        ],
    })


@app.route("/icon.svg")
def icon():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<defs><linearGradient id="h" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#ff6a3d"/><stop offset=".5" stop-color="#ffa14d"/>
<stop offset="1" stop-color="#ffd08a"/></linearGradient></defs>
<rect width="512" height="512" rx="112" fill="#0d0907"/>
<rect x="150" y="104" width="212" height="296" rx="20" fill="none"
      stroke="url(#h)" stroke-width="20"/>
<path d="M196 300 L242 236 L286 274 L330 196" fill="none" stroke="url(#h)"
      stroke-width="20" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""
    return app.response_class(svg, mimetype="image/svg+xml",
                              headers={"Cache-Control": "public, max-age=604800"})


@app.after_request
def cache_static(resp):
    """Static assets are versioned by deploy; let the browser and Cloudflare keep them.

    Everything else is per-user, so it must never be cached anywhere shared.
    """
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "public, max-age=86400"
    elif request.path.startswith(("/api/", "/export/")) or session.get("uid"):
        resp.headers["Cache-Control"] = "private, no-store"
    return resp


ENERGY = {"Grass": "#4dab5c", "Fire": "#e8603c", "Water": "#4aa8e0", "Lightning": "#e8c33c",
          "Psychic": "#b060c8", "Fighting": "#c0703c", "Darkness": "#4a5568",
          "Metal": "#8a97a8", "Fairy": "#e878b0", "Dragon": "#b8952c",
          "Colorless": "#b8b4ac"}


@app.template_global()
def energy_colour(t):
    return ENERGY.get(t, "#8a8f9c")


def card_detail(card_id):
    """Full TCGdex payload, fetched once and cached in cards.data."""
    row = db().execute("SELECT data FROM cards WHERE id=%s", (card_id,)).fetchone()
    if row and row["data"]:
        return row["data"]
    card = tcgdex(f"cards/{card_id}")
    if card:
        upsert_card(card)
        db().commit()
        r = db().execute("SELECT data FROM cards WHERE id=%s", (card_id,)).fetchone()
        return (r or {}).get("data") or {}
    return {}


# ------------------------------------------------------------------ all sets


@app.route("/sets")
@login_required
def sets_page():
    owned = {r["set_id"]: r for r in db().execute("""
        SELECT c.set_id, COUNT(DISTINCT c.id) AS n,
               SUM(h.qty * COALESCE(h.manual_gbp,
                   (SELECT gbp FROM prices p WHERE p.card_id=c.id ORDER BY day DESC LIMIT 1),0)) AS value
        FROM holdings h JOIN cards c ON c.id=h.card_id
        WHERE h.user_id=%s GROUP BY c.set_id""", (uid(),)).fetchall()}

    today = date.today().isoformat()
    series, upcoming = {}, []
    for sid, m in SETS.items():
        if m.get("serie") in {"Pok\u00e9mon TCG Pocket"}:
            continue
        e = dict(m, id=sid, logo=f"https://assets.tcgdex.net/en/{sid.split('.')[0][:2]}/{sid}/logo.webp",
                 owned=(owned.get(sid) or {}).get("n") or 0,
                 value=float((owned.get(sid) or {}).get("value") or 0))
        e["pct"] = round(e["owned"] / m["total"] * 100) if m.get("total") else 0
        if (m.get("release") or "") > today:
            upcoming.append(e)
        series.setdefault(m.get("serie") or "Other", []).append(e)

    for v in series.values():
        v.sort(key=lambda e: e.get("release") or "", reverse=True)
    ordered = sorted(series.items(),
                     key=lambda kv: max((e.get("release") or "") for e in kv[1]), reverse=True)
    upcoming.sort(key=lambda e: e["release"])
    return render_template("sets.html", series=ordered, upcoming=upcoming[:6],
                           today=today, page="browse")


# -------------------------------------------------------------------- insights


@app.route("/insights")
@login_required
def insights_page():
    held = holdings_with_prices(uid())
    for h in held:
        h["gain"] = ((h["value"] or 0) - (h["paid"] or 0) * (h["qty"] or 1)
                     if h.get("paid") else None)
        h["gain_pct"] = (h["gain"] / ((h["paid"] or 1) * (h["qty"] or 1)) * 100
                         if h.get("paid") else None)
    scored = [h for h in held if h["gain_pct"] is not None]

    by_rarity = {}
    for h in held:
        k = h.get("rarity") or "Unknown"
        e = by_rarity.setdefault(k, {"rarity": k, "value": 0, "n": 0})
        e["value"] += h["value"] or 0
        e["n"] += h["qty"] or 1
    total = sum(e["value"] for e in by_rarity.values()) or 1
    rarities = sorted(by_rarity.values(), key=lambda e: -e["value"])
    for e in rarities:
        e["share"] = round(e["value"] / total * 100)

    sales = db().execute("""
        SELECT s.*, c.name FROM sales s JOIN cards c ON c.id=s.card_id
        WHERE s.user_id=%s""", (uid(),)).fetchall()
    for s in sales:
        s["pnl"] = ((s["sold"] or 0) - (s["paid"] or 0)) * (s["qty"] or 1)

    return render_template(
        "insights.html",
        best=sorted(scored, key=lambda h: -h["gain_pct"])[:5],
        worst=sorted(scored, key=lambda h: h["gain_pct"])[:5],
        rarities=rarities,
        best_flip=max(sales, key=lambda s: s["pnl"], default=None),
        worst_flip=min(sales, key=lambda s: s["pnl"], default=None),
        n_sales=len(sales), page="portfolio")


# --------------------------------------------------------------------- compare


@app.route("/compare")
@login_required
def compare_page():
    ids = [i for i in request.args.getlist("id") if i][:2]
    cards = []
    for cid in ids:
        row = db().execute("SELECT * FROM cards WHERE id=%s", (cid,)).fetchone()
        if not row:
            continue
        row["detail"] = card_detail(cid)
        row["price"] = (db().execute(
            "SELECT gbp FROM prices WHERE card_id=%s ORDER BY day DESC LIMIT 1",
            (cid,)).fetchone() or {}).get("gbp")
        row["spark"] = sparklines([cid], 60).get(cid, [])
        cards.append(with_images(row))
    return render_template("compare.html", cards=cards, page="browse")


# ---------------------------------------------------------------- bulk import
#
# Rewritten after a real 143-row Collectr export matched the right *printing*
# for only 104 of them. Almost every miss came from throwing information away:
# the set column was ignored, the collector number was dropped whenever it had
# a slash or a letter, and a Chinese card with no English release was quietly
# attached to whatever English card shared its name.

# Collectr appends variant labels TCGdex does not index. "Meowth (Master Ball
# Pattern) (CN)" finds nothing; "Meowth" finds it. The labels are still real
# information, so they are kept rather than discarded.
REGION_TAGS = {"jp": "jp", "japanese": "jp", "cn": "cn", "chinese": "cn",
               "kr": "kr", "korean": "kr"}

# Regions TCGdex actually serves. Anything tagged with a region outside this
# map is reported unmatched rather than looked up in English.
TCGDEX_LANGS = {"jp": "ja"}
NO_DATA_FOR = {
    "cn": "Chinese card — TCGdex has no Chinese data",
    "kr": "Korean card — TCGdex has no Korean data",
}

# Everything before the slash, verbatim. Zero padding is significant: TCGdex
# uses "072" for SV-era sets and "44" for older ones, so normalising either way
# breaks the match. Letter prefixes (TG/GG/SV/SWSH) and trailing letters (84a)
# are part of the number.
_NUM_RE = re.compile(r"^\s*([A-Za-z]{0,4}\d{1,4}[A-Za-z]?)\s*$")


def split_variant(raw_name):
    """'Meowth (Master Ball Pattern) (CN)' -> ('Meowth', 'Master Ball Pattern', 'cn').

    Returns (lookup_name, variant_label, region). The lookup name is what goes
    to TCGdex; the label is kept so the holding can show what it actually is.
    """
    name = (raw_name or "").strip()
    parens = re.findall(r"\(([^()]*)\)", name)
    region = None
    labels = []
    for p in parens:
        tag = p.strip().lower()
        if tag in REGION_TAGS:
            region = REGION_TAGS[tag]
        elif p.strip():
            labels.append(p.strip())
    plain = re.sub(r"\s*\([^()]*\)", "", name).strip()
    plain = re.sub(r"\s{2,}", " ", plain)
    return (plain or name), (" · ".join(labels) or None), region


def parse_number(raw):
    """'072/080' -> '072'. 'TG06/TG30' -> 'TG06'. 'SWSH153' -> 'SWSH153'.

    None when the field is not a collector number at all — Gem Pack's '0205/07'
    parses to '0205', which is deliberate: it is preserved for display even
    though no English set uses it.
    """
    if raw is None:
        return None
    head = str(raw).split("/")[0].strip()
    head = head.lstrip("#").strip()
    m = _NUM_RE.match(head)
    return m.group(1) if m else None


def parse_grade(condition):
    """'PSA 10 (GEM-MT)' -> ('PSA 10', None). 'Near Mint' -> (None, 'Near Mint').

    A graded card is a different holding from a raw one — holdings is unique on
    (user_id, card_id, grade) precisely so both can be held — so the grade has
    to come out of the free-text condition field or the two silently merge.
    """
    c = (condition or "").strip()
    if not c:
        return None, None
    m = _GRADE_RE.search(c)
    if not m:
        return None, c
    num = m.group(2)
    if num.endswith(".0"):
        num = num[:-2]
    return f"{m.group(1).upper()} {num}", None


# Column names that unambiguously mean "what I paid". Collectr's price column is
# collectr_price_gbp — current market value, not cost — and importing that as
# cost basis makes every card show zero gain forever, so anything that only
# means "price" is treated as market value instead.
PAID_HEADERS = {"paid", "cost", "purchase price", "purchase_price", "price paid",
                "price_paid", "paid_gbp", "paid gbp", "cost basis", "cost_basis",
                "bought for", "acquired for", "buy price", "buy_price"}
MARKET_HEADERS = {"price", "value", "market", "market price", "market_price",
                  "collectr_price_gbp", "collectr price gbp", "current price",
                  "current_value", "market value", "market_value", "price_gbp",
                  "price gbp", "tcg price", "tcgplayer price"}
HEADER_ALIASES = {
    "name": "name", "card": "name", "card name": "name", "cardname": "name",
    "set": "set", "set name": "set", "expansion": "set", "series": "set",
    "number": "number", "card number": "number", "collector number": "number",
    "no": "number", "num": "number", "#": "number",
    "qty": "qty", "quantity": "qty", "count": "qty", "amount": "qty",
    "condition": "condition", "grade": "condition", "cond": "condition",
    "finish": "finish", "printing": "finish", "variant": "finish",
    "foil": "finish", "rarity": "rarity", "notes": "notes", "note": "notes",
    "language": "region", "lang": "region", "region": "region",
}


def map_headers(cells):
    """Map a header row to canonical field names, or return None if it isn't one.

    A row is a header when enough of its cells are recognisable field names and
    none of them look like data.
    """
    mapped, hits = {}, 0
    for i, cell in enumerate(cells):
        key = re.sub(r"\s+", " ", (cell or "").strip().lower()).strip(" _-")
        if not key:
            continue
        if key in HEADER_ALIASES:
            mapped[i] = HEADER_ALIASES[key]; hits += 1
        elif key in PAID_HEADERS:
            mapped[i] = "paid"; hits += 1
        elif key in MARKET_HEADERS:
            mapped[i] = "market"; hits += 1
        else:
            mapped[i] = None
    # "name" alone is not enough — a card literally called "Set" would qualify.
    return mapped if hits >= 3 and "name" in mapped.values() else None


def sniff_rows(text):
    """Split a paste into cells. Handles comma and tab, and quoted fields.

    The old hand-rolled regex could not see a quoted comma inside a card name;
    csv can, and it is in the standard library.
    """
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return []
    sample = "\n".join(lines[:20])
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    out = []
    for row in csv.reader(lines, delimiter=delim, skipinitialspace=True):
        cells = [c.strip() for c in row]
        if any(cells):
            out.append(cells)
    return out


def parse_import(text):
    """Text in, staged rows out. No network, so this is safe inside a request.

    Returns (rows, columns) where columns is the resolved header map (or None
    for a headerless paste, which falls back to positional parsing).
    """
    grid = sniff_rows(text)
    if not grid:
        return [], None
    cols = map_headers(grid[0])
    body = grid[1:] if cols else grid
    named = {v: k for k, v in (cols or {}).items() if v}

    rows = []
    for n, cells in enumerate(body, start=1):
        def col(field):
            i = named.get(field)
            return cells[i].strip() if i is not None and i < len(cells) and cells[i] else None

        if cols:
            raw_name = col("name")
            set_name, number = col("set"), col("number")
            condition, finish = col("condition"), col("finish")
            qty_s, paid_s, market_s = col("qty"), col("paid"), col("market")
            region_s = col("region")
        else:
            # Headerless: "2, Umbreon ex, PRE, 161". A leading integer is a
            # quantity; everything else is positional and deliberately cautious.
            parts = [c for c in cells if c]
            if not parts:
                continue
            qty_s = None
            if parts[0].isdigit() and len(parts) > 1:
                qty_s, parts = parts[0], parts[1:]
            raw_name = parts[0] if parts else None
            rest = parts[1:]
            number = next((p for p in rest if parse_number(p)), None)
            set_name = next((p for p in rest
                             if p != number and not parse_number(p)
                             and not re.fullmatch(r"[£$]?[\d.,]+", p)), None)
            condition = finish = region_s = None
            # A bare number in a headerless paste is NOT assumed to be cost.
            paid_s, market_s = None, None
        if not raw_name:
            continue

        name, variant, region = split_variant(raw_name)
        grade, condition_out = parse_grade(condition)
        if not region and region_s:
            region = REGION_TAGS.get(region_s.strip().lower())

        def money(v):
            if not v:
                return None
            try:
                return round(float(re.sub(r"[^\d.\-]", "", v)), 2)
            except ValueError:
                return None

        try:
            qty = max(1, int(re.sub(r"[^\d]", "", qty_s or "1") or 1))
        except ValueError:
            qty = 1

        rows.append({
            "n": n, "raw": cells, "name": name, "variant": variant,
            "set_name": set_name, "number": parse_number(number),
            "region": region, "qty": qty,
            "paid": money(paid_s), "market": money(market_s),
            "condition": condition_out, "grade": grade, "finish": finish,
        })
    return rows, cols




# ---- resolution ----------------------------------------------------------

def _norm_set(v):
    return re.sub(r"[^a-z0-9]", "", (v or "").lower())


_SET_BY_NAME = None
_SET_BY_ABBR = None


def _set_index():
    """name -> set_id and abbr -> set_id, built once from sets.json."""
    global _SET_BY_NAME, _SET_BY_ABBR
    if _SET_BY_NAME is None:
        _SET_BY_NAME, _SET_BY_ABBR = {}, {}
        for sid, meta in SETS.items():
            n = _norm_set(meta.get("name"))
            if n:
                _SET_BY_NAME.setdefault(n, sid)
            a = _norm_set(meta.get("abbr"))
            if a:
                _SET_BY_ABBR.setdefault(a, sid)
    return _SET_BY_NAME, _SET_BY_ABBR


def resolve_set(set_name):
    """Set name or abbreviation -> TCGdex set id, or None.

    Ignoring this column is why 37 rows landed on the right Pokemon in the
    wrong set: a Scarlet & Violet Base card resolving to a Prize Pack reprint.
    """
    if not set_name:
        return None
    key = _norm_set(set_name)
    if not key:
        return None
    by_name, by_abbr = _set_index()
    if key in by_name:
        return by_name[key]
    if key in by_abbr:
        return by_abbr[key]
    # "Scarlet & Violet Base Set" vs "Scarlet & Violet"; prefer the longest
    # unambiguous prefix match so a short name cannot swallow a longer one.
    cands = [sid for n, sid in by_name.items() if key.startswith(n) or n.startswith(key)]
    if len(set(cands)) == 1:
        return cands[0]
    return None


def tcgdex_lang(lang, path, **params):
    """TCGdex in a specific language. Japanese cards genuinely live under /ja."""
    try:
        r = requests.get(f"https://api.tcgdex.net/v2/{lang}/{path}",
                         params=params, timeout=12)
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        pass
    return None


def _candidates(name, lang):
    res = (tcgdex("cards", name=name) if lang == "en"
           else tcgdex_lang(lang, "cards", name=name)) or []
    return [c for c in res
            if set_meta(c["id"].rsplit("-", 1)[0]).get("serie") not in DIGITAL_SERIES]


def resolve_row(row):
    """Fill in card_id / set_id / confidence for one staged row.

    confidence is one of:
      exact      set and number both matched
      set        set matched, number did not
      number     number matched inside an unresolved set
      name       name only — the printing is a guess
      unmatched  nothing safe to attach

    The hard rule is at the top: a row tagged JP or CN never falls back to an
    English card. Silently pricing a GBP200 Chinese Cubone as a common English
    one is worse than reporting it unmatched, because it looks confident.
    """
    name, number = row.get("name"), row.get("number")
    region = row.get("region")
    want_set = resolve_set(row.get("set_name"))

    # A region we have no endpoint for can only produce a wrong answer, so it
    # stops here. Falling back to English for a JP/CN row is the failure this
    # rewrite exists to remove: a confident wrong match is worse than none.
    if region and region not in TCGDEX_LANGS:
        return None, want_set, "unmatched", NO_DATA_FOR.get(
            region, f"No TCGdex data for {region.upper()} cards")
    lang = TCGDEX_LANGS.get(region, "en")

    pool = _candidates(name, lang)
    if not pool and lang == "ja":
        return None, want_set, "unmatched", "Not found in TCGdex Japanese data"
    if not pool:
        return None, want_set, "unmatched", "Not found in TCGdex"

    in_set = [c for c in pool if c["id"].rsplit("-", 1)[0] == want_set] if want_set else []
    scope = in_set or pool

    exact_n = [c for c in scope if number and str(c.get("localId")) == str(number)]

    if in_set and exact_n:
        return exact_n[0]["id"], want_set, "exact", None
    if in_set:
        note = "Set matched, collector number did not" if number else None
        return in_set[0]["id"], want_set, "set", note
    if want_set:
        # The set resolved but holds no card of that name — trusting the name
        # here is exactly the bug that produced the Prize Pack reprints.
        return None, want_set, "unmatched", "Not in the set named on the row"
    if exact_n:
        c = exact_n[0]
        return c["id"], c["id"].rsplit("-", 1)[0], "number", "Set not recognised; matched on number"
    pool.sort(key=lambda c: rank_key({"name": c.get("name"),
                                      "set_id": c["id"].rsplit("-", 1)[0]}, name.lower()))
    c = pool[0]
    return c["id"], c["id"].rsplit("-", 1)[0], "name", "Printing is a guess — set not recognised"


def run_import_job(job_id, user_id):
    """Resolve every staged row. Runs on its own thread with its own connection.

    Never touches flask.g: that is request-scoped and this outlives the request.
    """
    try:
        with raw_db() as c:
            rows = c.execute(
                "SELECT * FROM import_rows WHERE job_id=%s ORDER BY n", (job_id,)).fetchall()
            for i, r in enumerate(rows, start=1):
                try:
                    cid, sid, conf, note = resolve_row(dict(r))
                except Exception as e:                      # one bad row must not kill the job
                    cid, sid, conf, note = None, None, "unmatched", f"Lookup failed: {e}"
                if cid:
                    full = tcgdex(f"cards/{cid}")
                    if full:
                        upsert_card(full, c)
                        refresh_price(cid, conn=c)
                c.execute("""UPDATE import_rows SET card_id=%s, set_id=%s,
                             confidence=%s, note=%s WHERE id=%s""",
                          (cid, sid, conf, note, r["id"]))
                c.execute("UPDATE import_jobs SET done=%s WHERE id=%s", (i, job_id))
                c.commit()
            c.execute("UPDATE import_jobs SET state='review', finished=now() WHERE id=%s",
                      (job_id,))
            c.commit()
    except Exception as e:
        try:
            with raw_db() as c:
                c.execute("UPDATE import_jobs SET state='failed', error=%s WHERE id=%s",
                          (str(e)[:500], job_id))
                c.commit()
        except Exception:
            pass


@app.route("/import", methods=["GET", "POST"])
@login_required
def import_page():
    """Step one: parse the paste and stage it. Returns as soon as it is stored.

    Parsing is pure string work, so it finishes inside the request. Resolution
    is one network round trip per row and moves to a background thread; the
    page polls /api/import/<id> for progress and then shows the review table.
    """
    if request.method == "GET":
        return render_template("import.html", page="you")

    text = (request.get_json(force=True) or {}).get("text", "")
    rows, cols = parse_import(text)
    if not rows:
        return jsonify(error="Nothing to import — check the paste."), 400
    if len(rows) > 2000:
        return jsonify(error=f"{len(rows):,} rows is too many for one go. "
                             "Split it into batches of 2,000."), 400

    job = db().execute("""INSERT INTO import_jobs (user_id, state, total, columns)
                          VALUES (%s,'resolving',%s,%s) RETURNING id""",
                       (uid(), len(rows),
                        Json({str(k): v for k, v in (cols or {}).items()}))).fetchone()["id"]
    for r in rows:
        db().execute("""INSERT INTO import_rows
            (job_id,n,raw,name,variant,set_name,number,region,qty,paid,market,
             condition,grade,finish)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (job, r["n"], Json(r["raw"]), r["name"], r["variant"], r["set_name"],
             r["number"], r["region"], r["qty"], r["paid"], r["market"],
             r["condition"], r["grade"], r["finish"]))
    db().commit()
    log_event("import_staged", job=job, rows=len(rows), headers=bool(cols))

    threading.Thread(target=run_import_job, args=(job, uid()), daemon=True).start()
    return jsonify(job=job, total=len(rows), headers=bool(cols))


def _job_or_404(job_id):
    j = db().execute("SELECT * FROM import_jobs WHERE id=%s AND user_id=%s",
                     (job_id, uid())).fetchone()
    if not j:
        abort(404)
    return j


@app.route("/api/import/<int:job_id>")
@login_required
def api_import_status(job_id):
    """Progress while resolving, and the whole review table once it is done."""
    j = _job_or_404(job_id)
    out = {"state": j["state"], "done": j["done"], "total": j["total"],
           "error": j["error"], "paid_column": j["paid_column"]}
    if j["state"] not in ("review", "done"):
        return jsonify(out)

    rows = db().execute("""
        SELECT r.*, c.name AS matched_name, c.set_name AS matched_set,
               c.local_id AS matched_number,
               (SELECT gbp FROM prices p WHERE p.card_id=r.card_id
                 ORDER BY day DESC LIMIT 1) AS price
        FROM import_rows r LEFT JOIN cards c ON c.id=r.card_id
        WHERE r.job_id=%s ORDER BY r.n""", (job_id,)).fetchall()
    out["rows"] = [{
        "id": r["id"], "n": r["n"], "name": r["name"], "variant": r["variant"],
        "set_name": r["set_name"], "number": r["number"], "region": r["region"],
        "qty": r["qty"], "paid": r["paid"], "market": r["market"],
        "condition": r["condition"], "grade": r["grade"], "finish": r["finish"],
        "card_id": r["card_id"], "confidence": r["confidence"], "note": r["note"],
        "skip": r["skip"], "matched_name": r["matched_name"],
        "matched_set": r["matched_set"], "matched_number": r["matched_number"],
        "price": r["price"],
    } for r in rows]
    out["counts"] = {k: sum(1 for r in rows if r["confidence"] == k)
                     for k in ("exact", "set", "number", "name", "unmatched")}
    out["graded"] = sum(1 for r in rows if r["grade"])
    return jsonify(out)


@app.route("/api/import/<int:job_id>/rows", methods=["PATCH"])
@login_required
def api_import_edit(job_id):
    """Edit the review table before committing: drop rows, or fix what is wrong."""
    _job_or_404(job_id)
    d = request.get_json(force=True) or {}
    edits = d.get("rows") or []
    for e in edits:
        rid = e.get("id")
        if not rid:
            continue
        own = db().execute("SELECT 1 FROM import_rows WHERE id=%s AND job_id=%s",
                           (rid, job_id)).fetchone()
        if not own:
            continue
        for field in ("skip", "qty", "paid", "grade", "condition", "finish"):
            if field in e:
                v = e[field]
                if field == "skip":
                    v = bool(v)
                elif field == "qty":
                    try:
                        v = max(1, int(v))
                    except (TypeError, ValueError):
                        continue
                elif field == "paid":
                    try:
                        v = None if v in (None, "") else round(float(v), 2)
                    except (TypeError, ValueError):
                        continue
                db().execute(f"UPDATE import_rows SET {field}=%s WHERE id=%s", (v, rid))
    db().commit()
    return jsonify(ok=True)


@app.route("/api/import/<int:job_id>/paid-column", methods=["POST"])
@login_required
def api_import_paid_column(job_id):
    """Say which column is cost. Nothing is treated as cost unless asked.

    Collectr's price column is current market value; importing it as cost basis
    makes every card show zero gain forever, so it lands in `market` and only
    moves to `paid` when someone explicitly says that column is what they paid.
    """
    _job_or_404(job_id)
    which = (request.get_json(force=True) or {}).get("column")
    if which not in ("market", "none"):
        return jsonify(error="Unknown column"), 400
    if which == "market":
        db().execute("UPDATE import_rows SET paid=market WHERE job_id=%s AND market IS NOT NULL",
                     (job_id,))
    else:
        db().execute("UPDATE import_rows SET paid=NULL WHERE job_id=%s", (job_id,))
    db().execute("UPDATE import_jobs SET paid_column=%s WHERE id=%s", (which, job_id))
    db().commit()
    return jsonify(ok=True)


@app.route("/api/import/<int:job_id>/commit", methods=["POST"])
@login_required
def api_import_commit(job_id):
    """Step two: write the reviewed rows into holdings.

    Only rows with a card_id and no skip flag are written. grade is passed
    explicitly: holdings is unique on (user_id, card_id, grade), so leaving it
    to the column default would collapse a PSA 10 into the raw copy.
    """
    j = _job_or_404(job_id)
    if j["state"] not in ("review", "done"):
        return jsonify(error="Still resolving — give it a moment."), 409

    rows = db().execute("""SELECT * FROM import_rows WHERE job_id=%s
                           AND skip=false AND card_id IS NOT NULL ORDER BY n""",
                        (job_id,)).fetchall()
    added = 0
    for r in rows:
        db().execute("""INSERT INTO holdings
                (user_id,card_id,qty,paid,grade,condition,finish,region,variant)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (user_id,card_id,grade) DO UPDATE
                SET qty = holdings.qty + EXCLUDED.qty,
                    paid = COALESCE(holdings.paid, EXCLUDED.paid),
                    condition = COALESCE(holdings.condition, EXCLUDED.condition),
                    finish = COALESCE(holdings.finish, EXCLUDED.finish),
                    region = COALESCE(holdings.region, EXCLUDED.region),
                    variant = COALESCE(holdings.variant, EXCLUDED.variant)""",
                     (uid(), r["card_id"], r["qty"], r["paid"], r["grade"] or "",
                      r["condition"], r["finish"], r["region"], r["variant"]))
        added += 1
    skipped = db().execute("""SELECT COUNT(*) n FROM import_rows WHERE job_id=%s
                              AND (skip=true OR card_id IS NULL)""",
                           (job_id,)).fetchone()["n"]
    db().execute("UPDATE import_jobs SET state='done' WHERE id=%s", (job_id,))
    db().commit()
    if added:
        ensure_snapshot(uid(), db(), force=True)
        db().commit()
    log_event("import_committed", job=job_id, added=added, skipped=skipped)
    return jsonify(ok=True, added=added, skipped=skipped)


# ------------------------------------------------------------------ card scan


@app.route("/scan")
@login_required
def scan_page():
    if not is_premium():
        return render_template("locked.html", feature="Camera scan",
                               blurb="Point your camera at a card and Holo reads "
                                     "the name and set number, then finds it for you.",
                               page="portfolio")
    return render_template("scan.html", page="portfolio")


@app.route("/api/scan", methods=["POST"])
@login_required
def api_scan():
    """Match OCR output to real cards.

    The phone reads two strings off the card: the name across the top and the
    collector number at the bottom. Neither is reliable on its own — OCR turns
    'Umbreon ex' into 'Urnbreon ex' and '161/180' into '16l/l80' — so we search
    on the name, then use the number to narrow, and always return candidates
    for the person to confirm rather than adding anything automatically.
    """
    d = request.get_json(force=True) or {}
    raw_name = re.sub(r"[^A-Za-z' \-]", " ", (d.get("name") or "")).strip()
    raw_name = re.sub(r"\s+", " ", raw_name)
    # "161/180" is the usual form, but promos and some subsets print a bare
    # number, so fall back to any 1-3 digit run. OCR reads 1 as l or I and 0 as O.
    digits = (d.get("number") or "").replace("l", "1").replace("I", "1").replace("O", "0")
    m = re.search(r"(\d{1,3})\s*/\s*\d{1,3}", digits) or re.search(r"\b(\d{1,3})\b", digits)
    number = str(int(m.group(1))) if m else None

    if len(raw_name) < 3:
        return jsonify(candidates=[], name=raw_name, number=number,
                       error="Couldn't read the card name. Try again with more light.")

    # TCGdex matches substrings, so a partly-misread name still works: "mbreon"
    # finds Umbreon. But it needs a *clean* substring — "pe mbreon" finds nothing.
    # Try the whole string, then the longest tokens, longest first. Short tokens
    # are skipped: "pe" matches Annihilape and Morpeko and buries the real card.
    attempts = [raw_name]
    toks = sorted({t for t in raw_name.split() if len(t) >= 4}, key=len, reverse=True)
    attempts += toks
    # OCR usually mangles the end of a word ("Charizard" -> "Charizara"), so also
    # try shortening prefixes of the longest token. Capped: each try is a request.
    if toks:
        longest = toks[0]
        attempts += [longest[:k] for k in range(len(longest) - 1, 4, -1)][:4]
    res, used = [], raw_name
    for a in attempts:
        res = tcgdex("cards", name=a) or []
        if res:
            used = a
            break

    cards = []
    for c in res:
        set_id = c["id"].rsplit("-", 1)[0]
        meta = set_meta(set_id)
        if meta.get("serie") in DIGITAL_SERIES:
            continue
        cards.append({"id": c["id"], "name": c.get("name"), "local_id": c.get("localId"),
                      "image": c.get("image"), "set_id": set_id,
                      "set_name": meta.get("name") or set_id,
                      "set_abbr": meta.get("abbr"), "release": meta.get("release"),
                      "set_total": meta.get("total")})

    if number:
        exact = [c for c in cards if str(c["local_id"]) == number]
        if exact:
            cards = exact + [c for c in cards if c not in exact]

    ql = used.lower()
    cards.sort(key=lambda c: (0 if str(c["local_id"]) == number else 1, rank_key(c, ql)))
    out = [with_images(c) for c in cards[:12]]
    log_event("scan", name=raw_name, number=number, hits=len(out))
    return jsonify(candidates=out, name=raw_name, number=number, matched=used)


# --------------------------------------------------------------- sealed / misc


@app.route("/api/custom", methods=["POST", "DELETE"])
@login_required
def api_custom():
    d = request.get_json(force=True) or {}
    if request.method == "DELETE":
        db().execute("DELETE FROM custom_items WHERE id=%s AND user_id=%s",
                     (d.get("id"), uid()))
        db().commit()
        return jsonify(ok=True)
    if not (d.get("name") or "").strip():
        return jsonify(error="Name required"), 400
    db().execute("""INSERT INTO custom_items (user_id,name,kind,qty,paid,value,note)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                 (uid(), d["name"].strip(), d.get("kind") or "sealed",
                  int(d.get("qty") or 1), d.get("paid"), d.get("value"), d.get("note")))
    db().commit()
    return jsonify(ok=True)


def log_event(kind, path=None, conn=None, **meta):
    """Record a usage event. Never allowed to break the request that triggered it."""
    try:
        (conn or db()).execute(
            "INSERT INTO events (user_id, kind, path, meta) VALUES (%s,%s,%s,%s)",
            (session.get("uid"), kind, path or request.path,
             Json(meta) if meta else None))
        if conn is None:
            db().commit()
    except Exception:
        pass


# Pages worth counting. Static files, API calls and the health check would
# drown the signal.
_SKIP_PATHS = ("/static/", "/api/", "/ph/", "/icon.svg", "/manifest.json", "/export/")


@app.after_request
def log_pageview(resp):
    if (request.method == "GET" and resp.status_code == 200
            and session.get("uid")
            and not request.path.startswith(_SKIP_PATHS)
            and "text/html" in (resp.content_type or "")):
        log_event("pageview")
    return resp


def is_admin():
    """Fails closed. An earlier version returned True when ADMIN_USER was unset,
    which meant every logged-in beta tester was an admin on a fresh deploy."""
    if not session.get("uid") or not ADMIN_USER:
        return False
    return session.get("username") == ADMIN_USER


@app.template_global()
def is_premium():
    if not session.get("uid"):
        return False
    r = db().execute("SELECT premium FROM users WHERE id=%s", (uid(),)).fetchone()
    return bool(r and r["premium"])


@app.route("/api/premium/redeem", methods=["POST"])
@login_required
def redeem_premium():
    code = ((request.get_json(force=True) or {}).get("code") or "").strip()
    active = PREMIUM_CODE
    if not active:
        return jsonify(error="No code is active right now."), 400
    if code.lower() != active.lower():
        log_event("premium_code_failed")
        return jsonify(error="That code isn't right."), 400
    db().execute("UPDATE users SET premium=true, premium_since=now() WHERE id=%s", (uid(),))
    db().commit()
    log_event("premium_unlocked", via="code")
    return jsonify(ok=True)




@app.route("/stats")
@login_required
def stats_page():
    """Beta dashboard. Only the account named in ADMIN_USER can see it."""
    if not is_admin():
        abort(403)

    q = lambda sql, *a: db().execute(sql, a).fetchall()
    totals = db().execute("""
        SELECT (SELECT COUNT(*) FROM users) AS users,
               (SELECT COUNT(*) FROM users WHERE created > now() - interval '7 days') AS new_users,
               (SELECT COUNT(DISTINCT user_id) FROM events WHERE at > now() - interval '1 day') AS dau,
               (SELECT COUNT(DISTINCT user_id) FROM events WHERE at > now() - interval '7 days') AS wau,
               (SELECT COUNT(*) FROM holdings) AS holdings,
               (SELECT COUNT(*) FROM events WHERE at > now() - interval '7 days') AS events
    """).fetchone()

    return render_template(
        "stats.html",
        t=totals,
        people=q("""
            SELECT u.username, u.created, u.premium,
                   (SELECT COUNT(*) FROM holdings h WHERE h.user_id=u.id) AS cards,
                   (SELECT COUNT(*) FROM events e WHERE e.user_id=u.id) AS events,
                   (SELECT MAX(at) FROM events e WHERE e.user_id=u.id) AS last_seen
            FROM users u ORDER BY last_seen DESC NULLS LAST"""),
        actions=q("""
            SELECT kind, COUNT(*) AS n FROM events
            WHERE kind <> 'pageview' AND at > now() - interval '30 days'
            GROUP BY kind ORDER BY n DESC"""),
        pages=q("""
            SELECT path, COUNT(*) AS n, COUNT(DISTINCT user_id) AS people
            FROM events WHERE kind='pageview' AND at > now() - interval '7 days'
            GROUP BY path ORDER BY n DESC LIMIT 12"""),
        daily=q("""
            SELECT at::date AS day, COUNT(DISTINCT user_id) AS people, COUNT(*) AS n
            FROM events WHERE at > now() - interval '14 days'
            GROUP BY day ORDER BY day"""),
        recent=q("""
            SELECT e.kind, e.path, e.meta, e.at, u.username
            FROM events e LEFT JOIN users u ON u.id=e.user_id
            WHERE e.kind <> 'pageview' ORDER BY e.at DESC LIMIT 25"""),
        page="profile")


def refresh_all():
    """Refresh every held card once, then snapshot every user. Returns cards refreshed."""
    n = 0
    with raw_db() as c:
        ids = [r["card_id"] for r in c.execute("SELECT DISTINCT card_id FROM holdings").fetchall()]
        for cid in ids:
            if refresh_price(cid, force=True, conn=c) is not None:
                n += 1
            time.sleep(0.2)
        c.commit()
        refresh_market(c)
        refresh_graded_all(c)
        backfill_alt_images(c)
        fired = check_alerts(c)
        for u in c.execute("SELECT * FROM users").fetchall():
            ensure_snapshot(u["id"], c, force=True)
            if u["digest"]:
                send_digest(u, c, fired.get(u["id"], ()))
    return n


@app.route("/api/cron/refresh", methods=["POST", "GET"])
def api_cron():
    """Hit this from a Render cron job or a Cloudflare Worker cron trigger: ?key=CRON_SECRET"""
    if not CRON_SECRET or request.args.get("key") != CRON_SECRET:
        abort(403)
    return jsonify(ok=True, refreshed=refresh_all())


def nightly():
    while True:
        now = datetime.now()
        nxt = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        time.sleep((nxt - now).total_seconds())
        try:
            refresh_all()
        except Exception as e:  # keep the loop alive
            print("nightly failed:", e)


if os.environ.get("ENABLE_SCHEDULER", "1") == "1":
    threading.Thread(target=nightly, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8420)), debug=False)
