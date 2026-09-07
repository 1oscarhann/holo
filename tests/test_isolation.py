"""Cross-user isolation.

Everything per-user — holdings, alerts, watchlist, sales, snapshots, custom
items, events — must be invisible and unwritable from another account. Cards
and prices are deliberately shared, so those are asserted *not* to be scoped.

Every test sets up two real users and drives the second one's session at the
first one's row ids.
"""

import pytest

import app as holo
from conftest import login, make_card, make_holding, make_price, make_user


@pytest.fixture
def two_users():
    """(victim_id, attacker_id). The victim owns one card worth £120."""
    victim = make_user("victim")
    attacker = make_user("attacker")
    make_card("sv08.5-161", name="Umbreon ex")
    make_price("sv08.5-161", 120.0)
    return victim, attacker


@pytest.fixture
def attacker(client, two_users):
    return login(client, two_users[1], "attacker")


# ------------------------------------------------------------------- holdings

def test_cannot_delete_another_users_holding(attacker, two_users):
    hid = make_holding(two_users[0])
    r = attacker.delete(f"/api/holdings/{hid}")
    assert r.status_code == 403
    with holo.raw_db() as c:
        assert c.execute("SELECT 1 FROM holdings WHERE id=%s", (hid,)).fetchone()


def test_cannot_edit_another_users_holding(attacker, two_users):
    hid = make_holding(two_users[0], qty=1, paid=10.0)
    r = attacker.patch(f"/api/holdings/{hid}", json={"qty": 999, "paid": 0})
    assert r.status_code == 403
    with holo.raw_db() as c:
        row = c.execute("SELECT qty, paid FROM holdings WHERE id=%s", (hid,)).fetchone()
    assert (row["qty"], row["paid"]) == (1, 10.0)


def test_a_missing_holding_is_refused_the_same_way_as_someone_elses(attacker):
    """Identical responses, or the endpoint enumerates which ids exist."""
    assert attacker.delete("/api/holdings/999999").status_code == 403


def test_holdings_lists_are_scoped_to_the_caller(client, two_users):
    victim, atk = two_users
    make_holding(victim, card_id="sv08.5-161", qty=3)
    make_card("sv03-125", name="Miraidon ex")
    make_holding(atk, card_id="sv03-125", qty=1)

    login(client, atk, "attacker")
    assert b"Miraidon" in client.get("/cards").data
    assert b"Umbreon" not in client.get("/cards").data
    with holo.raw_db() as c:
        assert [h["id"] for h in holo.holdings_with_prices(atk, c)] == ["sv03-125"]


def test_card_detail_page_404s_on_another_users_holding(attacker, two_users):
    hid = make_holding(two_users[0])
    assert attacker.get(f"/card/{hid}").status_code == 404


# --------------------------------------------------------------------- alerts

def test_cannot_delete_another_users_alert(attacker, two_users):
    with holo.raw_db() as c:
        aid = c.execute("""INSERT INTO alerts (user_id,card_id,kind,threshold)
                           VALUES (%s,'sv08.5-161','above',200) RETURNING id""",
                        (two_users[0],)).fetchone()["id"]
        c.commit()
    attacker.delete(f"/api/alerts/{aid}")
    with holo.raw_db() as c:
        assert c.execute("SELECT 1 FROM alerts WHERE id=%s", (aid,)).fetchone()


def test_user_alerts_only_returns_the_named_users_rows(two_users):
    victim, atk = two_users
    with holo.raw_db() as c:
        for u in (victim, atk):
            c.execute("""INSERT INTO alerts (user_id,card_id,kind,threshold)
                         VALUES (%s,'sv08.5-161','above',200)""", (u,))
        c.commit()
    with holo.app.test_request_context():
        assert len(holo.user_alerts(atk)) == 1
        assert holo.user_alerts(atk)[0]["user_id"] == atk


# ------------------------------------------------------------------ watchlist

def test_watchlist_delete_only_touches_your_own_row(attacker, two_users):
    victim, atk = two_users
    with holo.raw_db() as c:
        for u in (victim, atk):
            c.execute("INSERT INTO watchlist (user_id,card_id) VALUES (%s,'sv08.5-161')", (u,))
        c.commit()
    attacker.delete("/api/watchlist", json={"card_id": "sv08.5-161"})
    with holo.raw_db() as c:
        rows = c.execute("SELECT user_id FROM watchlist").fetchall()
    assert [r["user_id"] for r in rows] == [victim]


def test_watchlist_page_is_scoped(client, two_users):
    victim, atk = two_users
    with holo.raw_db() as c:
        c.execute("INSERT INTO watchlist (user_id,card_id) VALUES (%s,'sv08.5-161')", (victim,))
        c.commit()
    login(client, atk, "attacker")
    assert b"Umbreon" not in client.get("/watchlist").data


# ---------------------------------------------------------------------- sales

def test_cannot_sell_another_users_holding(attacker, two_users):
    hid = make_holding(two_users[0], qty=2)
    r = attacker.post("/api/sales", json={"hid": hid, "sold": 500})
    assert r.status_code == 404
    with holo.raw_db() as c:
        assert c.execute("SELECT qty FROM holdings WHERE id=%s", (hid,)).fetchone()["qty"] == 2
        assert not c.execute("SELECT 1 FROM sales").fetchone()


def test_sold_log_is_scoped(client, two_users):
    victim, atk = two_users
    with holo.raw_db() as c:
        c.execute("""INSERT INTO sales (user_id,card_id,qty,sold,paid,venue)
                     VALUES (%s,'sv08.5-161',1,300,10,'eBay')""", (victim,))
        c.commit()
    login(client, atk, "attacker")
    body = client.get("/sold").data
    assert b"Umbreon" not in body and b"eBay" not in body


def test_selling_your_own_holding_still_works(client, two_users):
    victim = two_users[0]
    hid = make_holding(victim, qty=2)
    login(client, victim, "victim")
    assert client.post("/api/sales", json={"hid": hid, "qty": 1, "sold": 150}).status_code == 200
    with holo.raw_db() as c:
        assert c.execute("SELECT qty FROM holdings WHERE id=%s", (hid,)).fetchone()["qty"] == 1
        assert c.execute("SELECT user_id FROM sales").fetchone()["user_id"] == victim


def test_sale_quantity_is_clamped_to_what_you_hold(client, two_users):
    """Otherwise you can log selling 99 of a card you own one of."""
    victim = two_users[0]
    hid = make_holding(victim, qty=2)
    login(client, victim, "victim")
    client.post("/api/sales", json={"hid": hid, "qty": 99, "sold": 10})
    with holo.raw_db() as c:
        assert c.execute("SELECT qty FROM sales").fetchone()["qty"] == 2
        assert not c.execute("SELECT 1 FROM holdings WHERE id=%s", (hid,)).fetchone()


# -------------------------------------------------------------- custom items

def test_cannot_delete_another_users_custom_item(attacker, two_users):
    with holo.raw_db() as c:
        cid = c.execute("""INSERT INTO custom_items (user_id,name) VALUES (%s,'ETB')
                           RETURNING id""", (two_users[0],)).fetchone()["id"]
        c.commit()
    attacker.delete("/api/custom", json={"id": cid})
    with holo.raw_db() as c:
        assert c.execute("SELECT 1 FROM custom_items WHERE id=%s", (cid,)).fetchone()


# -------------------------------------------------------------------- exports

def test_exports_contain_only_the_callers_rows(client, two_users):
    victim, atk = two_users
    make_holding(victim, card_id="sv08.5-161")
    make_card("sv03-125", name="Miraidon ex")
    make_price("sv03-125", 5.0)
    make_holding(atk, card_id="sv03-125")

    login(client, atk, "attacker")
    cards = client.get("/export/cards.csv").data
    assert b"Miraidon ex" in cards and b"Umbreon ex" not in cards

    prices = client.get("/export/prices.csv").data
    assert b"sv03-125" in prices and b"sv08.5-161" not in prices


def test_history_export_is_scoped(client, two_users):
    victim, atk = two_users
    with holo.raw_db() as c:
        c.execute("INSERT INTO snapshots (user_id,day,total,cards) VALUES (%s,CURRENT_DATE,9999,7)",
                  (victim,))
        c.commit()
    login(client, atk, "attacker")
    assert b"9999" not in client.get("/export/history.csv").data


# ------------------------------------------------------- aggregates and stats

def test_portfolio_totals_do_not_include_another_user(client, two_users):
    victim, atk = two_users
    make_holding(victim, card_id="sv08.5-161", qty=5)      # £600 of Umbreon
    login(client, atk, "attacker")
    assert client.get("/api/dashboard").get_json()["total"] == 0
    assert client.get("/api/stats").get_json()["cards"] == 0


def test_snapshots_are_per_user(two_users):
    victim, atk = two_users
    make_holding(victim, card_id="sv08.5-161", qty=1)
    with holo.raw_db() as c:
        holo.ensure_snapshot(victim, c, force=True)
        holo.ensure_snapshot(atk, c, force=True)
        rows = {r["user_id"]: r["total"] for r in
                c.execute("SELECT user_id,total FROM snapshots").fetchall()}
    assert rows[victim] == 120.0
    assert rows[atk] == 0.0


# ------------------------------------------------------ deliberately *shared*

def test_cards_and_prices_are_shared_on_purpose(two_users):
    """The single most important architectural decision here: one fetch per
    card per day serves everybody. If these ever became per-user the cost model
    breaks, so assert the sharing rather than leaving it to chance."""
    victim, atk = two_users
    make_holding(victim, card_id="sv08.5-161")
    make_holding(atk, card_id="sv08.5-161")
    with holo.raw_db() as c:
        assert c.execute("SELECT COUNT(*) n FROM cards WHERE id='sv08.5-161'").fetchone()["n"] == 1
        assert c.execute("SELECT COUNT(*) n FROM prices WHERE card_id='sv08.5-161'").fetchone()["n"] == 1
    with holo.raw_db() as c:
        assert holo.holdings_with_prices(victim, c)[0]["price"] == 120.0
        assert holo.holdings_with_prices(atk, c)[0]["price"] == 120.0


def test_deleting_a_user_takes_their_rows_but_leaves_shared_data(two_users):
    victim, atk = two_users
    make_holding(victim, card_id="sv08.5-161")
    with holo.raw_db() as c:
        c.execute("DELETE FROM users WHERE id=%s", (victim,))
        c.commit()
        assert not c.execute("SELECT 1 FROM holdings WHERE user_id=%s", (victim,)).fetchone()
        assert c.execute("SELECT 1 FROM cards WHERE id='sv08.5-161'").fetchone()
        assert c.execute("SELECT 1 FROM prices WHERE card_id='sv08.5-161'").fetchone()
