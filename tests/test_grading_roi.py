"""Grading ROI — is this raw card worth sending off?

Unanswerable while every graded card was priced as raw. With comps in the
database it is arithmetic, but arithmetic with a real unknown in it: you cannot
order a grade. So this never quotes one number, and it never computes an ROI
from a graded price it does not actually have.
"""

import pytest

import app as holo
from conftest import login, make_card, make_holding, make_price, make_user


@pytest.fixture
def chansey(client):
    make_card("xy12-070", name="Chansey")
    make_price("xy12-070", 2.10)
    return "xy12-070"


def graded(card_id, grade, gbp, samples=5, conn=None):
    def _ins(c):
        c.execute("""INSERT INTO graded_prices (card_id,grade,day,gbp,low,high,samples,source)
                     VALUES (%s,%s,CURRENT_DATE,%s,%s,%s,%s,'ebay-comps')
                     ON CONFLICT (card_id,grade,day) DO UPDATE SET gbp=EXCLUDED.gbp""",
                  (card_id, grade, gbp, gbp * 0.9, gbp * 1.1, samples))
    if conn:
        _ins(conn)
    else:
        with holo.raw_db() as c:
            _ins(c); c.commit()


# ------------------------------------------------------------------ the sums

def test_the_fee_tier_follows_the_declared_value():
    assert holo.grading_fee(50.0)[0] == "Value"
    assert holo.grading_fee(300.0)[0] == "Regular"
    assert holo.grading_fee(1000.0)[0] == "Express"
    assert holo.grading_fee(99999.0)[0] == "Premium", "an absurd value must still price"


def test_postage_is_split_across_a_submission():
    """Nobody posts one card. Charging a whole round trip to each would make
    every card look not worth grading."""
    _, cost = holo.grading_fee(50.0)
    assert cost == pytest.approx(20.0 + holo.GRADING_POSTAGE / holo.GRADING_BATCH)


def test_net_is_measured_against_selling_it_raw_today(chansey):
    """The choice is not 'grade or do nothing', it is 'grade or sell as it is',
    so the raw price is an opportunity cost and comes off the top."""
    graded(chansey, "PSA 9", 18.60)
    with holo.raw_db() as c:
        roi = holo.grading_roi(chansey, 2.10, c)
    row = next(r for r in roi if r["grade"] == "PSA 9")
    assert row["net"] == pytest.approx(18.60 - 2.10 - row["cost"], abs=0.01)
    assert row["multiple"] == pytest.approx(8.9, abs=0.1)


def test_every_grade_with_comps_is_shown_not_just_the_best(chansey):
    """You cannot order a PSA 10. Showing only the best case would be selling a
    gamble as a certainty."""
    graded(chansey, "PSA 10", 60.0)
    graded(chansey, "PSA 9", 18.60)
    graded(chansey, "PSA 8", 4.00)
    with holo.raw_db() as c:
        roi = holo.grading_roi(chansey, 2.10, c)
    assert {r["grade"] for r in roi} == {"PSA 10", "PSA 9", "PSA 8"}
    assert [r["grade"] for r in roi][0] == "PSA 10", "best first"


def test_a_grade_that_would_lose_money_is_marked_as_such(chansey):
    graded(chansey, "PSA 8", 4.00)
    with holo.raw_db() as c:
        roi = holo.grading_roi(chansey, 2.10, c)
    assert roi[0]["net"] < 0 and roi[0]["worth_it"] is False


def test_nothing_is_computed_without_real_comps(chansey):
    """An ROI from a guessed graded price is the same confident-and-wrong
    failure the rest of the codebase refuses to make."""
    with holo.raw_db() as c:
        assert holo.grading_roi(chansey, 2.10, c) == []


def test_a_thin_sample_does_not_produce_an_roi(chansey):
    graded(chansey, "PSA 10", 60.0, samples=1)
    with holo.raw_db() as c:
        assert holo.grading_roi(chansey, 2.10, c) == []


def test_a_card_with_no_raw_price_has_no_roi(chansey):
    graded(chansey, "PSA 10", 60.0)
    with holo.raw_db() as c:
        assert holo.grading_roi(chansey, None, c) == []
        assert holo.grading_roi(chansey, 0, c) == []


# --------------------------------------------------------- the candidate list

def test_candidates_are_ranked_by_what_they_would_net(client):
    uid = make_user("oscar")
    for cid, raw, top in (("a-1", 2.10, 60.0), ("b-1", 5.00, 200.0), ("c-1", 1.00, 9.0)):
        make_card(cid, name=cid); make_price(cid, raw)
        make_holding(uid, card_id=cid, qty=1)
        graded(cid, "PSA 10", top)
    with holo.raw_db() as c:
        cands = holo.grading_candidates(uid, c)
    assert [x["id"] for x in cands] == ["b-1", "a-1"], "c-1 does not clear the bar"


def test_a_card_already_in_a_slab_is_not_a_candidate(chansey):
    uid = make_user("oscar")
    make_holding(uid, card_id=chansey, qty=1, grade="PSA 9")
    graded(chansey, "PSA 10", 60.0)
    with holo.raw_db() as c:
        assert holo.grading_candidates(uid, c) == []


def test_the_raw_copy_of_a_card_you_also_hold_graded_is_still_a_candidate(chansey):
    """holdings is unique on (user_id, card_id, grade), so both rows exist and
    the raw one is still worth considering."""
    uid = make_user("oscar")
    make_holding(uid, card_id=chansey, qty=1, grade="")
    make_holding(uid, card_id=chansey, qty=1, grade="PSA 9")
    graded(chansey, "PSA 10", 60.0)
    with holo.raw_db() as c:
        cands = holo.grading_candidates(uid, c)
    assert len(cands) == 1 and cands[0]["grade"] == ""


def test_a_manually_valued_card_is_not_a_candidate(chansey):
    """A price somebody typed in is not a market price to arbitrage against."""
    uid = make_user("oscar")
    graded(chansey, "PSA 10", 60.0)
    with holo.raw_db() as c:
        c.execute("""INSERT INTO holdings (user_id,card_id,qty,manual_gbp,grade)
                     VALUES (%s,%s,1,2.10,'')""", (uid, chansey))
        c.commit()
        assert holo.grading_candidates(uid, c) == []


def test_candidates_are_scoped_to_their_owner(chansey):
    a, b = make_user("a"), make_user("b")
    make_holding(a, card_id=chansey, qty=1)
    graded(chansey, "PSA 10", 60.0)
    with holo.raw_db() as c:
        assert len(holo.grading_candidates(a, c)) == 1
        assert holo.grading_candidates(b, c) == []


# -------------------------------------------------------------------- the page

def test_the_page_renders_with_candidates(client, chansey):
    uid = make_user("oscar")
    make_holding(uid, card_id=chansey, qty=1)
    graded(chansey, "PSA 10", 60.0)
    graded(chansey, "PSA 8", 4.00)
    login(client, uid, "oscar")
    html = client.get("/grading").data.decode()
    assert "Chansey" in html
    assert "PSA 10" in html and "PSA 8" in html, "the downside is shown too"


def test_the_page_says_why_it_is_empty_rather_than_showing_nothing(client):
    login(client, make_user("oscar"), "oscar")
    html = client.get("/grading").data.decode()
    assert "sold slabs" in html or "graded comps" in html


def test_the_page_publishes_its_assumptions(client):
    """The fees are estimates with no feed behind them. A number that looks
    authoritative and is not is exactly what this codebase avoids elsewhere."""
    login(client, make_user("oscar"), "oscar")
    html = client.get("/grading").data.decode()
    assert "estimates, not quotes" in html
    assert "GRADING_POSTAGE" in html


def test_the_page_requires_login(client):
    assert client.get("/grading").status_code == 302


def test_the_card_page_offers_it_only_for_raw_cards(client, chansey, monkeypatch):
    monkeypatch.setattr(holo, "card_detail", lambda cid: {})   # the page fetches art detail
    uid = make_user("oscar")
    hid = make_holding(uid, card_id=chansey, qty=1)
    graded(chansey, "PSA 10", 60.0)
    login(client, uid, "oscar")
    assert b"Worth grading?" in client.get(f"/card/{hid}").data

    slab = make_holding(uid, card_id=chansey, qty=1, grade="PSA 10")
    assert b"Worth grading?" not in client.get(f"/card/{slab}").data
