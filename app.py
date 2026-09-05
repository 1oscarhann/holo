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

import json
import os
from urllib.parse import quote
import psycopg
from psycopg.rows import dict_row
import threading
import time
import secrets
from datetime import date, datetime, timedelta
from functools import wraps

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
<stop offset="0" stop-color="#1b2030"/><stop offset="1" stop-color="#12151f"/>
</linearGradient></defs>
<rect width="245" height="342" rx="14" fill="url(#g)"/>
<rect x="6" y="6" width="233" height="330" rx="10" fill="none"
      stroke="#2c3348" stroke-width="1.5"/>
<text x="122.5" y="168" text-anchor="middle" fill="#5a6480"
      font-family="system-ui,sans-serif" font-size="42" font-weight="700">{label}</text>
<text x="122.5" y="196" text-anchor="middle" fill="#4a5270"
      font-family="system-ui,sans-serif" font-size="14" letter-spacing="2">{sub}</text>
<text x="122.5" y="300" text-anchor="middle" fill="#39405a"
      font-family="system-ui,sans-serif" font-size="11" letter-spacing="1">NO ART</text>
</svg>"""
    return app.response_class(svg, mimetype="image/svg+xml",
                              headers={"Cache-Control": "public, max-age=604800"})


# --------------------------------------------------------------------------- db

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  pw TEXT NOT NULL, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY, name TEXT, set_id TEXT, set_name TEXT, local_id TEXT,
  rarity TEXT, image TEXT, set_total INTEGER,
  alt_image TEXT, alt_checked TIMESTAMPTZ);
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
            return redirect(request.args.get("next") or url_for("index"))
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
            return redirect(url_for("index"))
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
    (conn or db()).execute("""INSERT INTO cards
        (id,name,set_id,set_name,local_id,rarity,image,set_total) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, set_id=EXCLUDED.set_id,
        set_name=EXCLUDED.set_name, local_id=EXCLUDED.local_id, rarity=EXCLUDED.rarity,
        image=EXCLUDED.image, set_total=EXCLUDED.set_total""",
                 (card["id"], card.get("name"), s.get("id"), s.get("name"), card.get("localId"),
                  card.get("rarity"), card.get("image"),
                  (s.get("cardCount") or {}).get("total")))


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
        latest = d["manual_gbp"] if d["manual_gbp"] is not None else (hist[0]["gbp"] if hist else None)
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
        d["chg1"] = pct(latest, ago(1)) if d["manual_gbp"] is None else None
        d["chg7"] = pct(latest, ago(7)) if d["manual_gbp"] is None else None
        d["chg30"] = pct(latest, ago(30)) if d["manual_gbp"] is None else None
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
@login_required
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
    return render_template("add.html", page="add")


@app.route("/movers")
@login_required
def movers_page():
    return render_template("movers.html", s=stats(uid()), alerts=user_alerts(uid()), page="movers")


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
    return render_template("watchlist.html", rows=rows, alerts=alerts, page="movers")


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
                           realised=realised, page="profile")


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
    db().execute("""INSERT INTO sales (user_id,card_id,qty,sold,paid,sold_on,venue,note)
                    VALUES (%s,%s,%s,%s,%s,COALESCE(%s,CURRENT_DATE),%s,%s)""",
                 (uid(), row["card_id"], qty, float(d.get("sold") or 0),
                  row["paid"], d.get("sold_on") or None, d.get("venue"), d.get("note")))
    if qty >= row["qty"]:
        db().execute("DELETE FROM holdings WHERE id=%s", (hid,))
    else:
        db().execute("UPDATE holdings SET qty=qty-%s WHERE id=%s", (qty, hid))
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
                           spark=sparklines(list(shown)), page="movers")


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
