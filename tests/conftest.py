"""Shared fixtures.

app.py reads its configuration into module-level constants and connects to
Postgres at import time, so the environment has to be complete *before* the
import below. Tests that need a different value for one of those constants
monkeypatch the attribute on the module — that is the real thing the request
handlers read.

Needs a live Postgres. Point TEST_DATABASE_URL at a throwaway database; the
tables are truncated between tests, so never aim it at anything real.
"""

import os

os.environ.setdefault("TEST_DATABASE_URL",
                      "postgresql://postgres@127.0.0.1:5433/holotest")
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENABLE_SCHEDULER"] = "0"
os.environ.setdefault("ADMIN_USER", "oscar")
os.environ.setdefault("PREMIUM_CODE", "letmein")
os.environ.pop("RENDER", None)          # keep session cookies non-Secure over http
os.environ.pop("CRON_SECRET", None)

import pytest                                                    # noqa: E402
from werkzeug.security import generate_password_hash             # noqa: E402

import app as holo                                               # noqa: E402

# Every table app.py writes to, children before parents.
TABLES = ("events", "custom_items", "alerts", "watchlist", "sales", "snapshots",
          "holdings", "prices", "set_cards", "cards", "users")


@pytest.fixture(autouse=True)
def clean_db():
    """Empty database around every test, so no test can see another's rows."""
    with holo.raw_db() as c:
        c.execute("TRUNCATE %s RESTART IDENTITY CASCADE" % ", ".join(TABLES))
        c.commit()
    yield


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly instead of hitting TCGdex, pokemontcg.io, ntfy or Discord.

    Tests that want a canned API response monkeypatch holo.tcgdex themselves.
    """
    def boom(*a, **k):
        raise AssertionError("test made a real HTTP request: %r" % (a,))

    for verb in ("get", "post", "head", "put", "delete", "request"):
        monkeypatch.setattr(holo.requests, verb, boom)


@pytest.fixture(autouse=True)
def no_background_threads(monkeypatch):
    """Never let a test spawn the import worker.

    POST /import hands resolution to a background thread with its own
    connection. In the suite that thread races the next test's TRUNCATE and
    deadlocks, and the failure surfaces somewhere unrelated. Tests that want
    the work done call run_import_job directly, which is synchronous.
    """
    started = []

    class Recorded:
        def __init__(self, target=None, args=(), kwargs=None, **kw):
            self.target, self.args = target, args or ()

        def start(self):
            started.append((self.target, self.args))

    monkeypatch.setattr(holo.threading, "Thread", Recorded)
    return started


@pytest.fixture
def client():
    holo.app.config["TESTING"] = True
    return holo.app.test_client()


def make_user(username, password="password123", premium=False):
    """Insert a user directly. Returns the new id."""
    with holo.raw_db() as c:
        row = c.execute(
            "INSERT INTO users (username, pw, premium) VALUES (%s,%s,%s) RETURNING id",
            (username, generate_password_hash(password), premium)).fetchone()
        c.commit()
        return row["id"]


def make_card(card_id="sv08.5-161", name="Umbreon ex", set_id=None,
              local_id="161", image="https://assets.tcgdex.net/en/sv/sv08.5/161",
              rarity="Special Illustration Rare", set_total=207):
    with holo.raw_db() as c:
        c.execute("""INSERT INTO cards (id,name,set_id,set_name,local_id,rarity,image,set_total)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                  (card_id, name, set_id or card_id.rsplit("-", 1)[0], "Prismatic Evolutions",
                   local_id, rarity, image, set_total))
        c.commit()
    return card_id


def make_holding(user_id, card_id="sv08.5-161", qty=1, paid=10.0, grade=""):
    make_card(card_id)
    with holo.raw_db() as c:
        row = c.execute("""INSERT INTO holdings (user_id,card_id,qty,paid,grade)
                           VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                        (user_id, card_id, qty, paid, grade)).fetchone()
        c.commit()
        return row["id"]


def make_price(card_id, gbp, day_offset=0, source="cardmarket.trend"):
    from datetime import date, timedelta
    with holo.raw_db() as c:
        c.execute("""INSERT INTO prices (card_id,day,gbp,source,fetched)
                     VALUES (%s,%s,%s,%s,now())
                     ON CONFLICT (card_id, day) DO UPDATE SET gbp=EXCLUDED.gbp""",
                  (card_id, date.today() - timedelta(days=day_offset), gbp, source))
        c.commit()


def login(client, user_id, username):
    """Set the session the way a successful POST /login would."""
    with client.session_transaction() as s:
        s["uid"] = user_id
        s["username"] = username
    return client
