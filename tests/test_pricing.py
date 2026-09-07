"""price_from and the numbers built on top of it.

price_from is the deliberate seam: every price read in the app goes through it,
so a licensed provider can be swapped in without touching anything else. These
tests pin the contract that seam has to keep — (gbp, source) or (None, None).
"""

import pytest

import app as holo
from conftest import (login, make_card, make_holding, make_price, make_user)


def cm(**kw):
    return {"pricing": {"cardmarket": kw}}


def tp(**kw):
    return {"pricing": {"tcgplayer": kw}}


# ------------------------------------------------------------- source choice

def test_cardmarket_is_preferred_over_tcgplayer():
    """Cardmarket is the European market; TCGplayer is a USD fallback."""
    card = {"pricing": {"cardmarket": {"trend": 100.0},
                        "tcgplayer": {"holofoil": {"marketPrice": 999.0}}}}
    gbp, src = holo.price_from(card)
    assert src == "cardmarket.trend"
    assert gbp == round(100.0 * holo.fx_rate("EUR_GBP"), 2)


@pytest.mark.parametrize("key", ["trend", "avg7", "avg", "avg30", "low"])
def test_each_cardmarket_key_is_accepted(key):
    gbp, src = holo.price_from(cm(**{key: 50.0}))
    assert src == f"cardmarket.{key}"
    assert gbp == round(50.0 * holo.fx_rate("EUR_GBP"), 2)


def test_cardmarket_keys_are_tried_in_priority_order():
    gbp, src = holo.price_from(cm(low=1.0, avg30=2.0, avg=3.0, avg7=4.0, trend=5.0))
    assert src == "cardmarket.trend"


def test_falls_through_to_the_next_key_when_one_is_missing():
    gbp, src = holo.price_from(cm(avg=3.0, low=1.0))
    assert src == "cardmarket.avg"


@pytest.mark.parametrize("variant", ["holofoil", "reverse-holofoil", "normal",
                                     "1st-edition-holofoil", "1st-edition"])
def test_tcgplayer_variants_are_accepted_when_cardmarket_is_absent(variant):
    gbp, src = holo.price_from(tp(**{variant: {"marketPrice": 20.0}}))
    assert src == f"tcgplayer.{variant}"
    assert gbp == round(20.0 * holo.fx_rate("USD_GBP"), 2)


# ----------------------------------------------------------- currency and fx

def test_eur_and_usd_use_different_rates():
    """Both rates are hardcoded constants, not live FX — a known inaccuracy in
    every price shown. If they ever collapse to one number that is a bug."""
    assert holo.fx_rate("EUR_GBP") != holo.fx_rate("USD_GBP")
    eur, _ = holo.price_from(cm(trend=100.0))
    usd, _ = holo.price_from(tp(holofoil={"marketPrice": 100.0}))
    assert eur != usd


def test_prices_are_rounded_to_pence():
    gbp, _ = holo.price_from(cm(trend=1.0 / 3))
    assert gbp == round((1.0 / 3) * holo.fx_rate("EUR_GBP"), 2)
    assert len(str(gbp).split(".")[-1]) <= 2


# --------------------------------------------------------------- no price

@pytest.mark.parametrize("card", [
    None, {}, {"pricing": None}, {"pricing": {}},
    {"pricing": {"cardmarket": None, "tcgplayer": None}},
    {"pricing": {"cardmarket": {}}},
    {"pricing": {"cardmarket": {"trend": 0}}},          # zero is not a price
    {"pricing": {"tcgplayer": {"holofoil": {}}}},
    {"pricing": {"tcgplayer": {"unknown-variant": {"marketPrice": 5.0}}}},
])
def test_absent_or_useless_pricing_returns_a_clean_miss(card):
    assert holo.price_from(card) == (None, None)


# ------------------------------------------------------------ pct and totals

@pytest.mark.parametrize("now,then,expected", [
    (110.0, 100.0, 10.0),
    (90.0, 100.0, -10.0),
    (100.0, 100.0, 0.0),
    (None, 100.0, None),
    (100.0, None, None),
    (100.0, 0, None),          # would divide by zero
])
def test_pct_change(now, then, expected):
    assert holo.pct(now, then) == expected


def test_manual_price_overrides_the_market_and_suppresses_change(client):
    """A manually valued card has no price history, so a % move is meaningless."""
    uid = make_user("alice")
    make_card("sv08.5-161")
    make_price("sv08.5-161", 120.0)
    make_price("sv08.5-161", 100.0, day_offset=7)
    with holo.raw_db() as c:
        c.execute("""INSERT INTO holdings (user_id,card_id,qty,manual_gbp,grade)
                     VALUES (%s,'sv08.5-161',1,555.0,'PSA 10')""", (uid,))
        c.commit()
        h = holo.holdings_with_prices(uid, c)[0]
    assert h["price"] == 555.0
    assert h["value"] == 555.0
    assert h["chg7"] is None


def test_value_multiplies_by_quantity(client):
    uid = make_user("alice")
    make_card("sv08.5-161")
    make_price("sv08.5-161", 120.0)
    make_holding(uid, qty=3)
    with holo.raw_db() as c:
        assert holo.holdings_with_prices(uid, c)[0]["value"] == 360.0


def test_a_card_with_no_price_is_worth_zero_not_a_crash(client):
    uid = make_user("alice")
    make_holding(uid)                       # card exists, no prices row
    with holo.raw_db() as c:
        h = holo.holdings_with_prices(uid, c)[0]
    assert h["price"] is None and h["value"] == 0


def test_holdings_are_sorted_most_valuable_first(client):
    uid = make_user("alice")
    for cid, price in (("sv08.5-161", 120.0), ("sv03-125", 400.0), ("sv03-1", 5.0)):
        make_card(cid)
        make_price(cid, price)
        make_holding(uid, card_id=cid)
    with holo.raw_db() as c:
        assert [h["id"] for h in holo.holdings_with_prices(uid, c)] == \
            ["sv03-125", "sv08.5-161", "sv03-1"]


# -------------------------------------------------------- the shared cache

def test_one_price_row_per_card_per_day(client, monkeypatch):
    """The whole cost model: one fetch per card per day serves every user."""
    make_card("sv08.5-161")
    monkeypatch.setattr(holo, "tcgdex", lambda p, **k: {
        "id": "sv08.5-161", "name": "Umbreon ex", "pricing": {"cardmarket": {"trend": 100.0}}})
    with holo.raw_db() as c:
        for _ in range(3):
            holo.refresh_price("sv08.5-161", force=True, conn=c)
        c.commit()
        assert c.execute("SELECT COUNT(*) n FROM prices").fetchone()["n"] == 1


def test_a_failed_fetch_keeps_the_last_known_price(client, monkeypatch):
    """TCGdex going down must not zero out everybody's portfolio."""
    make_card("sv08.5-161")
    make_price("sv08.5-161", 120.0, day_offset=1)
    monkeypatch.setattr(holo, "tcgdex", lambda p, **k: None)
    with holo.raw_db() as c:
        holo.refresh_price("sv08.5-161", force=True, conn=c)
        rows = c.execute("SELECT gbp FROM prices ORDER BY day").fetchall()
    assert [r["gbp"] for r in rows] == [120.0]


def test_a_priceless_response_carries_the_last_price_forward(client, monkeypatch):
    make_card("sv08.5-161")
    make_price("sv08.5-161", 120.0, day_offset=1)
    monkeypatch.setattr(holo, "tcgdex", lambda p, **k: {"id": "sv08.5-161", "pricing": {}})
    with holo.raw_db() as c:
        holo.refresh_price("sv08.5-161", force=True, conn=c)
        c.commit()
        row = c.execute("SELECT gbp, source FROM prices ORDER BY day DESC LIMIT 1").fetchone()
    assert row["gbp"] == 120.0 and row["source"] == "carried"
