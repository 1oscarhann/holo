"""Portfolio tab structure.

The tab has a deliberate order: the value, then the graph as the main object,
then the top five cards, and only then the numbers. Layout is easy to shuffle
by accident during an unrelated edit, so the order is asserted rather than
left to whoever reads the template next.
"""

import re

import pytest

import app as holo
from conftest import login, make_card, make_holding, make_price, make_user


@pytest.fixture
def page(client):
    """A portfolio with eight holdings, so top-five actually has to cut."""
    uid = make_user("oscar")
    for i, (cid, price) in enumerate([
            ("me05-120", 780.0), ("me04-122", 415.5), ("me05-117", 288.0),
            ("me05-114", 174.25), ("me05-118", 96.4), ("me05-115", 62.0),
            ("me05-111", 41.3), ("me05-113", 28.75)]):
        make_card(cid, name=f"Card {i}", local_id=cid.split("-")[1])
        make_price(cid, price)
        make_holding(uid, card_id=cid, qty=1)
    login(client, uid, "oscar")
    return client.get("/").data.decode()


def order(html, *needles):
    """Positions of each needle, asserting every one is present."""
    at = []
    for n in needles:
        i = html.find(n)
        assert i != -1, f"missing from the page: {n}"
        at.append(i)
    return at


def test_graph_is_above_the_top_cards_which_are_above_the_numbers(page):
    """The order the tab is designed around."""
    g, t, figs = order(page, 'class="graph"', 'class="topfive"', 'class="figs"')
    assert g < t < figs


def test_the_hero_total_still_leads_the_page(page):
    hero, graph = order(page, 'class="hero"', 'class="graph"')
    assert hero < graph


def test_exactly_five_cards_are_showcased(page):
    block = page.split('class="topfive"')[1].split("</section>")[0]
    assert block.count('class="tf"') == 5


def test_the_showcase_links_every_card_to_its_own_page(page):
    block = page.split('class="topfive"')[1].split("</section>")[0]
    assert len(re.findall(r'href="/card/\d+"', block)) == 5


def test_the_showcase_carries_value_and_seven_day_move(page):
    block = page.split('class="topfive"')[1].split("</section>")[0]
    assert block.count('class="num"') == 5          # a price on each
    assert block.count('class="mv') == 5            # and a 7d change


def test_the_old_duplicate_holdings_list_is_gone(page):
    """The strip above the chart and the 'Biggest holdings' rows below it were
    rendering the identical five cards twice. The showcase replaced both."""
    assert "Biggest holdings" not in page
    assert 'class="holdstrip"' not in page


def test_the_range_tabs_are_their_own_control(page):
    """Four ranges in one group, with Share outside it rather than a fifth
    button sharing the row."""
    tabs = page.split('id="ranges"')[1].split("</div>")[0]
    assert [m for m in re.findall(r'data-n="(\d+)"', tabs)] == ["7", "30", "90", "0"]
    assert "share" not in tabs.lower()
    assert 'id="share"' in page


def test_thirty_days_is_the_default_range(page):
    tabs = page.split('id="ranges"')[1].split("</div>")[0]
    on = re.search(r'data-n="(\d+)"[^>]*class="on"', tabs)
    assert on and on.group(1) == "30"


def test_the_chart_readout_element_exists_for_scrubbing(page):
    assert 'id="read"' in page
    assert 'class="chart-read"' in page


def test_a_portfolio_with_fewer_than_five_cards_still_renders(client):
    uid = make_user("newbie")
    make_card("me05-120", name="Mega Darkrai ex")
    make_price("me05-120", 12.0)
    make_holding(uid, card_id="me05-120")
    login(client, uid, "newbie")
    html = client.get("/").data.decode()
    block = html.split('class="topfive"')[1].split("</section>")[0]
    assert block.count('class="tf"') == 1


def test_an_empty_portfolio_shows_onboarding_and_no_graph(client):
    """No holdings means no series to draw; the tab must not render a dead axis."""
    login(client, make_user("empty"), "empty")
    html = client.get("/").data.decode()
    assert "Add your first card" in html
    assert 'class="graph"' not in html
    assert 'class="topfive"' not in html
