"""Graded pricing.

price_from() returns one raw market price, so a PSA 10 was valued as if it were
a raw copy — a £44 Ditto V showed as roughly £1. There is no free graded feed
(TCG Price Lookup's free tier is TCGplayer raw only), so this prices from eBay
sold comps, where the grade sits in the listing title, and falls back to graded
sales our own users have recorded.

Two properties matter more than coverage, and most of these tests are about
them: a thin sample is not shown, and an unreachable source falls back to raw
and says so rather than inventing a number.
"""


import pytest

import app as holo
from conftest import login, make_card, make_holding, make_price, make_user


# ------------------------------------------------------ reading the title

@pytest.mark.parametrize("title,expected", [
    ("2023 Pokemon Japanese VMAX Climax Ditto V PSA 10 GEM MINT", "PSA 10"),
    ("Charizard Base Set PSA 9 MINT", "PSA 9"),
    ("Pikachu psa10 holo", "PSA 10"),
    ("Umbreon VMAX BGS 9.5 Black Label", "BGS 9.5"),
    ("Mewtwo CGC 8.5", "CGC 8.5"),
    ("Lugia SGC 10 gem", "SGC 10"),
    ("Rayquaza ACE 10", "ACE 10"),
    ("Snorlax TAG 9", "TAG 9"),
    ("Charizard PSA-10", "PSA 10"),
    ("Charizard Near Mint raw ungraded", None),
    ("Base Set booster box sealed", None),
    ("", None), (None, None),
])
def test_grade_is_read_out_of_a_listing_title(title, expected):
    assert holo.grade_from_title(title) == expected


def test_a_raw_listing_is_not_counted_toward_a_graded_price():
    """Sellers write 'PSA' in raw listings all the time ('will grade PSA 10'),
    but without a number there is no grade to attach."""
    assert holo.grade_from_title("Charizard raw, would grade PSA") is None


# ------------------------------------------------------- reading the feed

def comps(monkeypatch, payload, status=200):
    class Resp:
        status_code = status
        def json(self):
            if isinstance(payload, Exception):
                raise payload
            return payload
    monkeypatch.setattr(holo.requests, "get", lambda *a, **k: Resp())


CARD = {"id": "swsh12-070", "name": "Ditto V", "set_name": "VMAX Climax", "local_id": "154"}


def test_comps_are_read_from_a_plain_list(monkeypatch):
    comps(monkeypatch, [{"title": "Ditto V PSA 10", "price": 55.0, "currency": "USD"}])
    got = holo.fetch_comps(CARD)
    assert got == [{"title": "Ditto V PSA 10", "price": 55.0, "currency": "USD"}]


@pytest.mark.parametrize("wrapper", ["comps", "results", "sales", "data", "items"])
def test_comps_are_found_however_the_feed_wraps_them(monkeypatch, wrapper):
    """This is a third-party feed that has already changed hands once. A shape
    change should cost us the graded price, not the portfolio page."""
    comps(monkeypatch, {wrapper: [{"title": "Ditto V PSA 10", "price": 55.0}]})
    assert len(holo.fetch_comps(CARD)) == 1


@pytest.mark.parametrize("payload", [
    {}, {"unexpected": []}, [], "not json at all", 42,
    [{"title": "no price here"}], [{"price": 10.0}], [None, 7],
])
def test_an_unusable_response_yields_no_comps_rather_than_an_exception(monkeypatch, payload):
    comps(monkeypatch, payload)
    assert holo.fetch_comps(CARD) == []


def test_a_dead_feed_yields_no_comps(monkeypatch):
    comps(monkeypatch, [], status=503)
    assert holo.fetch_comps(CARD) == []
    monkeypatch.setattr(holo.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(holo.requests.RequestException()))
    assert holo.fetch_comps(CARD) == []


def test_prices_written_as_strings_or_nested_objects_are_read(monkeypatch):
    comps(monkeypatch, [{"title": "A PSA 10", "price": "$55.00"},
                        {"title": "B PSA 10", "sold_price": {"value": 61.0}}])
    assert [c["price"] for c in holo.fetch_comps(CARD)] == [55.0, 61.0]


# --------------------------------------------------------- the aggregate

@pytest.fixture
def ditto(client):
    make_card("swsh12-070", name="Ditto V")
    make_price("swsh12-070", 1.20)          # raw is about a pound
    return "swsh12-070"


def stub_comps(monkeypatch, rows):
    monkeypatch.setattr(holo, "fetch_comps", lambda card, **k: rows)


def test_a_graded_price_is_the_median_of_matching_comps(ditto, monkeypatch):
    stub_comps(monkeypatch, [
        {"title": "Ditto V PSA 10", "price": 50.0, "currency": "GBP"},
        {"title": "Ditto V PSA 10", "price": 56.0, "currency": "GBP"},
        {"title": "Ditto V PSA 10", "price": 300.0, "currency": "GBP"},   # outlier
    ])
    with holo.raw_db() as c:
        row = holo.refresh_graded_price(ditto, "PSA 10", force=True, conn=c)
    assert row["gbp"] == 56.0, "median, not mean — one silly listing must not move it"
    assert row["samples"] == 3 and row["source"] == "ebay-comps"


def test_comps_for_a_different_grade_are_ignored(ditto, monkeypatch):
    stub_comps(monkeypatch, [
        {"title": "Ditto V PSA 10", "price": 50.0, "currency": "GBP"},
        {"title": "Ditto V PSA 9", "price": 12.0, "currency": "GBP"},
        {"title": "Ditto V PSA 9", "price": 14.0, "currency": "GBP"},
        {"title": "Ditto V raw NM", "price": 1.10, "currency": "GBP"},
    ])
    with holo.raw_db() as c:
        assert holo.refresh_graded_price(ditto, "PSA 10", force=True, conn=c) is None, \
            "one matching comp is not enough to price from"


def test_usd_comps_are_converted_to_gbp(ditto, monkeypatch):
    stub_comps(monkeypatch, [{"title": f"Ditto V PSA 10", "price": 100.0, "currency": "USD"}] * 3)
    with holo.raw_db() as c:
        row = holo.refresh_graded_price(ditto, "PSA 10", force=True, conn=c)
    assert row["gbp"] == round(100.0 * holo.fx_rate("USD_GBP"), 2)


def test_a_thin_sample_is_not_priced_at_all(ditto, monkeypatch):
    """Two sales is a coincidence, not a market. Showing a number from it is
    the same failure as showing the raw price — confidently wrong."""
    stub_comps(monkeypatch, [{"title": "Ditto V PSA 10", "price": 50.0, "currency": "GBP"}] * 2)
    with holo.raw_db() as c:
        assert holo.refresh_graded_price(ditto, "PSA 10", force=True, conn=c) is None


def test_a_stored_price_below_the_sample_floor_is_never_read_back(ditto):
    with holo.raw_db() as c:
        c.execute("""INSERT INTO graded_prices (card_id,grade,day,gbp,samples,source)
                     VALUES (%s,'PSA 10',CURRENT_DATE,55.0,1,'ebay-comps')""", (ditto,))
        c.commit()
        assert holo.graded_price(ditto, "PSA 10", c) is None


def test_a_fresh_price_is_not_refetched(ditto, monkeypatch):
    calls = {"n": 0}
    def once(card, **k):
        calls["n"] += 1
        return [{"title": "Ditto V PSA 10", "price": 50.0, "currency": "GBP"}] * 3
    monkeypatch.setattr(holo, "fetch_comps", once)
    with holo.raw_db() as c:
        holo.refresh_graded_price(ditto, "PSA 10", force=True, conn=c)
        c.commit()
        holo.refresh_graded_price(ditto, "PSA 10", conn=c)
    assert calls["n"] == 1, "comps move slowly; weekly is the cadence"


# ------------------------------------------------ the sold log as a source

def test_our_own_graded_sales_price_the_card_when_comps_find_nothing(ditto, monkeypatch):
    """The crowdsourced route: over time the sold log is data nobody else has."""
    stub_comps(monkeypatch, [])
    uid = make_user("oscar")
    with holo.raw_db() as c:
        for v in (40.0, 44.0, 48.0):
            c.execute("""INSERT INTO sales (user_id,card_id,qty,sold,grade,sold_on)
                         VALUES (%s,%s,1,%s,'PSA 10',CURRENT_DATE)""", (uid, ditto, v))
        c.commit()
        row = holo.refresh_graded_price(ditto, "PSA 10", force=True, conn=c)
    assert row["gbp"] == 44.0 and row["source"] == "sold-log"


def test_selling_a_graded_holding_records_the_grade(client):
    """Without this the sold log cannot become a graded price source."""
    uid = make_user("oscar")
    make_card("swsh12-070")
    make_price("swsh12-070", 1.20)
    hid = make_holding(uid, card_id="swsh12-070", qty=1, grade="PSA 10")
    login(client, uid, "oscar")
    assert client.post("/api/sales", json={"hid": hid, "sold": 44.0}).status_code == 200
    with holo.raw_db() as c:
        assert c.execute("SELECT grade FROM sales").fetchone()["grade"] == "PSA 10"


# --------------------------------------------------- what the portfolio shows

def test_a_graded_holding_is_valued_at_its_graded_price(ditto):
    uid = make_user("oscar")
    make_holding(uid, card_id=ditto, qty=1, grade="PSA 10")
    with holo.raw_db() as c:
        c.execute("""INSERT INTO graded_prices (card_id,grade,day,gbp,low,high,samples,source)
                     VALUES (%s,'PSA 10',CURRENT_DATE,43.99,40,48,5,'ebay-comps')""", (ditto,))
        c.commit()
        h = holo.holdings_with_prices(uid, c)[0]
    assert h["price"] == pytest.approx(43.99)
    assert h["price_basis"] == "graded"
    assert h["graded"]["samples"] == 5 and h["graded"]["raw"] == pytest.approx(1.20)


def test_a_graded_holding_with_no_graded_price_falls_back_and_is_flagged(ditto):
    """Better a raw number labelled 'priced as raw' than a wrong number shown
    as if it were right."""
    uid = make_user("oscar")
    make_holding(uid, card_id=ditto, qty=1, grade="PSA 10")
    with holo.raw_db() as c:
        h = holo.holdings_with_prices(uid, c)[0]
    assert h["price"] == pytest.approx(1.20)
    assert h["price_basis"] == "raw-fallback"


def test_a_raw_holding_is_unaffected(ditto):
    uid = make_user("oscar")
    make_holding(uid, card_id=ditto, qty=1)
    with holo.raw_db() as c:
        h = holo.holdings_with_prices(uid, c)[0]
    assert h["price"] == pytest.approx(1.20) and h["price_basis"] == "raw"


def test_a_graded_price_does_not_leak_onto_the_raw_copy(ditto):
    """holdings is unique on (user_id, card_id, grade) so both can be held; they
    must be valued separately or the whole point of the split is lost."""
    uid = make_user("oscar")
    make_holding(uid, card_id=ditto, qty=1, grade="")
    make_holding(uid, card_id=ditto, qty=1, grade="PSA 10")
    with holo.raw_db() as c:
        c.execute("""INSERT INTO graded_prices (card_id,grade,day,gbp,samples,source)
                     VALUES (%s,'PSA 10',CURRENT_DATE,43.99,5,'ebay-comps')""", (ditto,))
        c.commit()
        hs = {h["grade"]: h for h in holo.holdings_with_prices(uid, c)}
    assert hs[""]["price"] == pytest.approx(1.20)
    assert hs["PSA 10"]["price"] == pytest.approx(43.99)


def test_a_graded_card_gets_no_percentage_change_from_the_raw_series(ditto):
    """The raw history is not this card's history. A borrowed change figure is
    worse than none."""
    uid = make_user("oscar")
    make_price(ditto, 0.60, day_offset=7)
    make_holding(uid, card_id=ditto, qty=1, grade="PSA 10")
    with holo.raw_db() as c:
        c.execute("""INSERT INTO graded_prices (card_id,grade,day,gbp,samples,source)
                     VALUES (%s,'PSA 10',CURRENT_DATE,43.99,5,'ebay-comps')""", (ditto,))
        c.commit()
        h = holo.holdings_with_prices(uid, c)[0]
    assert h["chg7"] is None and h["chg30"] is None


def test_a_manual_valuation_still_beats_everything(ditto):
    uid = make_user("oscar")
    with holo.raw_db() as c:
        c.execute("""INSERT INTO holdings (user_id,card_id,qty,manual_gbp,grade)
                     VALUES (%s,%s,1,999.0,'PSA 10')""", (uid, ditto))
        c.execute("""INSERT INTO graded_prices (card_id,grade,day,gbp,samples,source)
                     VALUES (%s,'PSA 10',CURRENT_DATE,43.99,5,'ebay-comps')""", (ditto,))
        c.commit()
        h = holo.holdings_with_prices(uid, c)[0]
    assert h["price"] == 999.0 and h["price_basis"] == "manual"


def test_graded_prices_are_shared_between_users(ditto):
    """Same architecture as `prices`: one lookup serves everyone holding it."""
    a, b = make_user("a"), make_user("b")
    make_holding(a, card_id=ditto, qty=1, grade="PSA 10")
    make_holding(b, card_id=ditto, qty=1, grade="PSA 10")
    with holo.raw_db() as c:
        c.execute("""INSERT INTO graded_prices (card_id,grade,day,gbp,samples,source)
                     VALUES (%s,'PSA 10',CURRENT_DATE,43.99,5,'ebay-comps')""", (ditto,))
        c.commit()
        assert c.execute("SELECT COUNT(*) n FROM graded_prices").fetchone()["n"] == 1
        assert holo.holdings_with_prices(a, c)[0]["price"] == pytest.approx(43.99)
        assert holo.holdings_with_prices(b, c)[0]["price"] == pytest.approx(43.99)


# --------------------------------------------------------------- the sweep

def test_the_sweep_only_looks_up_grades_someone_actually_holds(ditto, monkeypatch):
    seen = []
    monkeypatch.setattr(holo, "fetch_comps", lambda card, **k: seen.append(card["id"]) or [])
    monkeypatch.setattr(holo.time, "sleep", lambda *_: None)
    uid = make_user("oscar")
    make_holding(uid, card_id=ditto, qty=1, grade="PSA 10")
    make_holding(uid, card_id=ditto, qty=1, grade="")        # raw: not a graded lookup
    with holo.raw_db() as c:
        holo.refresh_graded_all(c)
    assert seen == [ditto]


def test_one_broken_card_does_not_stop_the_sweep(ditto, monkeypatch):
    calls = {"n": 0}
    def flaky(card, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("feed exploded")
        # comps for both grades, so the test does not depend on sweep order
        return ([{"title": "X PSA 9", "price": 20.0, "currency": "GBP"}] * 3 +
                [{"title": "X PSA 10", "price": 60.0, "currency": "GBP"}] * 3)
    monkeypatch.setattr(holo, "fetch_comps", flaky)
    monkeypatch.setattr(holo.time, "sleep", lambda *_: None)
    uid = make_user("oscar")
    make_card("sv03-125", name="Other")
    make_holding(uid, card_id=ditto, qty=1, grade="PSA 10")
    make_holding(uid, card_id="sv03-125", qty=1, grade="PSA 9")
    with holo.raw_db() as c:
        assert holo.refresh_graded_all(c) == 1
