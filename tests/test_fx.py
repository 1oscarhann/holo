"""Live FX rates.

EUR_GBP and USD_GBP were hardcoded. That quietly skewed every TCGplayer
fallback, and once graded pricing landed it skewed every graded price too —
eBay comps are USD and were being multiplied by a constant somebody typed in by
hand. The constants are now fallbacks; the live rate is fetched daily.
"""

import time

import pytest

import app as holo


@pytest.fixture(autouse=True)
def clear_cache():
    holo._FX["at"], holo._FX["rates"] = 0.0, {}
    yield
    holo._FX["at"], holo._FX["rates"] = 0.0, {}


def feed(monkeypatch, payload, status=200):
    class Resp:
        status_code = status
        def json(self):
            if isinstance(payload, Exception):
                raise payload
            return payload
    monkeypatch.setattr(holo.requests, "get", lambda *a, **k: Resp())


def test_the_feed_is_stored_as_x_to_gbp(client, monkeypatch):
    """Frankfurter quotes GBP->X; we price X->GBP, so it has to be inverted."""
    feed(monkeypatch, {"base": "GBP", "rates": {"EUR": 1.25, "USD": 1.28}})
    with holo.raw_db() as c:
        assert holo.refresh_fx(c) == 2
        c.commit()
        rows = {r["pair"]: r["rate"] for r in
                c.execute("SELECT pair, rate FROM fx_rates").fetchall()}
    assert rows["EUR_GBP"] == pytest.approx(1 / 1.25, rel=1e-4)
    assert rows["USD_GBP"] == pytest.approx(1 / 1.28, rel=1e-4)


def test_a_stored_rate_is_used_instead_of_the_constant(client, monkeypatch):
    with holo.raw_db() as c:
        c.execute("""INSERT INTO fx_rates (pair,day,rate,source)
                     VALUES ('USD_GBP',CURRENT_DATE,0.5,'test')""")
        c.commit()
    assert holo.fx_rate("USD_GBP") == 0.5
    assert holo.fx_rate("USD_GBP") != holo.USD_GBP


def test_the_constant_is_used_when_nothing_has_been_fetched(client):
    assert holo.fx_rate("USD_GBP") == holo.USD_GBP
    assert holo.fx_rate("EUR_GBP") == holo.EUR_GBP


def test_an_unknown_pair_does_not_blow_up(client):
    assert holo.fx_rate("JPY_GBP") == 1.0


def test_a_stale_rate_is_ignored(client):
    """A fortnight-old rate is worse than the constant, which at least says
    plainly that it is an approximation."""
    with holo.raw_db() as c:
        c.execute("""INSERT INTO fx_rates (pair,day,rate,source)
                     VALUES ('USD_GBP',CURRENT_DATE - 30,0.5,'test')""")
        c.commit()
    assert holo.fx_rate("USD_GBP") == holo.USD_GBP


@pytest.mark.parametrize("payload,status", [
    ({}, 200), ({"rates": {}}, 200), ({"rates": {"EUR": 0}}, 200),
    ({"rates": {"EUR": "nonsense"}}, 200), ("not json", 200),
    ({"rates": {"EUR": 1.25}}, 503), (None, 200),
])
def test_a_bad_response_stores_nothing_and_raises_nothing(client, monkeypatch, payload, status):
    feed(monkeypatch, payload, status)
    with holo.raw_db() as c:
        holo.refresh_fx(c)
        c.commit()
        assert not c.execute("SELECT 1 FROM fx_rates").fetchone()
    assert holo.fx_rate("USD_GBP") == holo.USD_GBP


def test_a_dead_feed_leaves_pricing_working(client, monkeypatch):
    monkeypatch.setattr(holo.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(holo.requests.RequestException()))
    with holo.raw_db() as c:
        assert holo.refresh_fx(c) == 0
    gbp, src = holo.price_from({"pricing": {"cardmarket": {"trend": 100.0}}})
    assert gbp == round(100.0 * holo.EUR_GBP, 2)


def test_todays_rates_are_not_refetched(client, monkeypatch):
    calls = {"n": 0}
    def once(*a, **k):
        calls["n"] += 1
        class R:
            status_code = 200
            def json(self): return {"rates": {"EUR": 1.25, "USD": 1.28}}
        return R()
    monkeypatch.setattr(holo.requests, "get", once)
    with holo.raw_db() as c:
        holo.refresh_fx(c); c.commit()
        holo.refresh_fx(c); c.commit()
    assert calls["n"] == 1


def test_the_rate_is_cached_rather_than_read_per_card(client, monkeypatch):
    """price_from runs once per holding; a database round trip per card would
    turn a portfolio page into a query storm."""
    with holo.raw_db() as c:
        c.execute("""INSERT INTO fx_rates (pair,day,rate,source)
                     VALUES ('EUR_GBP',CURRENT_DATE,0.5,'test')""")
        c.commit()
    holo.fx_rate("EUR_GBP")
    reads = {"n": 0}
    real = holo.raw_db
    def counted():
        reads["n"] += 1
        return real()
    monkeypatch.setattr(holo, "raw_db", counted)
    for _ in range(50):
        holo.fx_rate("EUR_GBP")
    assert reads["n"] == 0


def test_a_graded_comp_is_converted_at_the_live_rate(client, monkeypatch):
    from conftest import make_card, make_price
    make_card("swsh12-070", name="Ditto V")
    make_price("swsh12-070", 1.20)
    with holo.raw_db() as c:
        c.execute("""INSERT INTO fx_rates (pair,day,rate,source)
                     VALUES ('USD_GBP',CURRENT_DATE,0.5,'test')""")
        c.commit()
    monkeypatch.setattr(holo, "fetch_comps", lambda card, **k: [
        {"title": "Ditto V PSA 10", "price": 100.0, "currency": "USD"}] * 3)
    with holo.raw_db() as c:
        row = holo.refresh_graded_price("swsh12-070", "PSA 10", force=True, conn=c)
    assert row["gbp"] == 50.0, "USD comps must use the live rate, not the constant"
