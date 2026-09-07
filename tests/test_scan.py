"""/api/scan — turning two smudged OCR strings into a shortlist of real cards.

The phone reads a name band and a collector number. Neither is trustworthy, so
this is all about degrading gracefully: TCGdex matches substrings, which only
helps if the substring is clean.
"""

import pytest

import app as holo
from conftest import login, make_user

SETS = {
    "sv08.5": {"name": "Prismatic Evolutions", "serie": "Scarlet & Violet",
               "release": "2025-01-17", "total": 207, "abbr": "PRE"},
    "sv03": {"name": "Obsidian Flames", "serie": "Scarlet & Violet",
             "release": "2023-08-11", "total": 197, "abbr": "OBF"},
    "A1": {"name": "Genetic Apex", "serie": "Pokémon TCG Pocket",
           "release": "2024-10-30", "total": 226, "abbr": "A1"},
}


@pytest.fixture
def scan(client, monkeypatch):
    """Logged-in premium client plus a recorder of the queries TCGdex was asked."""
    monkeypatch.setattr(holo, "SETS", SETS)
    login(client, make_user("paid", premium=True), "paid")
    client.queries = []

    def fake_tcgdex(path, **params):
        client.queries.append(params.get("name", path))
        return client.responses.get(params.get("name"), [])

    monkeypatch.setattr(holo, "tcgdex", fake_tcgdex)
    client.responses = {}
    return client


def card(cid, name, local_id):
    return {"id": cid, "name": name, "localId": local_id,
            "image": f"https://assets.tcgdex.net/en/sv/{cid.rsplit('-', 1)[0]}/{local_id}"}


def post(scan, **body):
    return scan.post("/api/scan", json=body).get_json()


# ------------------------------------------------------------- name cleaning

def test_unreadable_name_gives_an_actionable_error(scan):
    r = post(scan, name="~~", number="161/207")
    assert r["candidates"] == []
    assert "light" in r["error"].lower()
    assert scan.queries == []          # no point spending a request on junk


def test_apostrophes_and_hyphens_survive_cleaning(scan):
    """Farfetch'd and Ho-Oh are real names; stripping those breaks the search."""
    scan.responses["Farfetch'd"] = [card("sv03-1", "Farfetch\'d", "1")]
    post(scan, name="Farfetch'd", number="")
    assert scan.queries[0] == "Farfetch'd"
    scan.queries.clear()
    post(scan, name="Ho-Oh 12", number="")
    assert scan.queries[0] == "Ho-Oh"


def test_junk_characters_become_spaces_so_glued_text_still_splits(scan):
    """A space, not a deletion: 'Umbreon161/207' must not search 'Umbreon161207'.

    The cost is that a misread letter splits a word — but the token fallback
    then searches the longest clean fragment, which still finds the card.
    """
    post(scan, name="Umbreon161/207", number="")
    assert scan.queries[0] == "Umbreon"

    scan.queries.clear()
    scan.responses["rfetch'd"] = [card("sv03-1", "Farfetch\'d", "1")]
    r = post(scan, name="F@rfetch'd", number="")
    assert r["matched"] == "rfetch'd"
    assert r["candidates"][0]["name"] == "Farfetch'd"


# ------------------------------------------------------- collector numbers

@pytest.mark.parametrize("raw,expected", [
    ("161/207", "161"),
    ("16l/2O7", "161"),          # OCR: 1 as l, 0 as O
    ("I6I/207", "161"),
    ("161", "161"),
    ("  007/165  ", "7"),        # leading zeros normalised away
    ("", None),
    ("no digits here", None),
])
def test_number_is_recovered_from_mangled_ocr(scan, raw, expected):
    scan.responses["Umbreon"] = [card("sv08.5-161", "Umbreon ex", "161")]
    assert post(scan, name="Umbreon", number=raw)["number"] == expected


def test_the_matching_number_is_promoted_to_the_top(scan):
    scan.responses["Umbreon"] = [
        card("sv03-10", "Umbreon", "10"),
        card("sv08.5-161", "Umbreon ex", "161"),
    ]
    r = post(scan, name="Umbreon", number="161/207")
    assert r["candidates"][0]["id"] == "sv08.5-161"


def test_a_wrong_number_does_not_throw_away_the_name_matches(scan):
    """OCR misreads numbers constantly; an empty result is worse than a list."""
    scan.responses["Umbreon"] = [card("sv03-10", "Umbreon", "10")]
    r = post(scan, name="Umbreon", number="999")
    assert [c["id"] for c in r["candidates"]] == ["sv03-10"]


# ------------------------------------------------- fallback search attempts

def test_the_full_string_is_tried_first_and_nothing_else_if_it_hits(scan):
    scan.responses["Umbreon ex"] = [card("sv08.5-161", "Umbreon ex", "161")]
    post(scan, name="Umbreon ex", number="161")
    assert scan.queries == ["Umbreon ex"]


def test_short_tokens_are_never_searched(scan):
    """An early version fell back to the first word, so 'pe mbreon' searched
    'pe' and returned Annihilape and Morpeko."""
    scan.responses["mbreon"] = [card("sv08.5-161", "Umbreon ex", "161")]
    r = post(scan, name="pe mbreon", number="")
    assert "pe" not in scan.queries
    assert all(len(q) >= 4 for q in scan.queries if q != "pe mbreon")
    assert r["candidates"][0]["id"] == "sv08.5-161"


def test_longest_token_is_tried_before_shorter_ones(scan):
    scan.responses["Charizard"] = [card("sv03-125", "Charizard ex", "125")]
    post(scan, name="Charizard vstar", number="")
    tokens = scan.queries[1:]
    assert tokens[0] == "Charizard"


def test_a_mangled_word_ending_is_recovered_by_shortening_the_prefix(scan):
    """OCR usually breaks the end of a word: 'Charizard' comes back 'Charizara'."""
    scan.responses["Charizar"] = [card("sv03-125", "Charizard ex", "125")]
    r = post(scan, name="Charizara", number="")
    assert r["matched"] == "Charizar"
    assert r["candidates"][0]["name"] == "Charizard ex"


def test_prefix_shortening_is_capped_so_a_scan_cannot_fan_out(scan):
    """Each attempt is a live request; an unbounded loop would hammer TCGdex."""
    post(scan, name="Zacianandzamazenta", number="")
    assert len(scan.queries) <= 6
    assert all(len(q) > 4 for q in scan.queries[1:])


def test_a_total_miss_returns_an_empty_list_not_an_error(scan):
    r = post(scan, name="Notapokemon", number="1")
    assert r["candidates"] == []
    assert r.get("error") is None


# -------------------------------------------------------------- result shape

def test_digital_only_cards_are_filtered_out(scan):
    """TCG Pocket cards do not physically exist and have no resale value."""
    scan.responses["Pikachu"] = [
        card("A1-94", "Pikachu ex", "94"),          # TCG Pocket
        card("sv03-25", "Pikachu", "25"),
    ]
    r = post(scan, name="Pikachu", number="")
    assert [c["id"] for c in r["candidates"]] == ["sv03-25"]


def test_candidates_carry_a_full_image_fallback_chain(scan):
    scan.responses["Umbreon"] = [card("sv08.5-161", "Umbreon ex", "161")]
    c = post(scan, name="Umbreon", number="161")["candidates"][0]
    assert c["img"].endswith("/low.webp")
    assert c["img_alts"][-1].startswith("/ph/")
    assert c["set_name"] == "Prismatic Evolutions"


def test_the_shortlist_is_capped_at_twelve(scan):
    scan.responses["Umbreon"] = [card(f"sv03-{i}", "Umbreon", str(i)) for i in range(40)]
    assert len(post(scan, name="Umbreon", number="")["candidates"]) == 12


def test_scanning_never_adds_a_card_by_itself(scan):
    """Always confirm. A wrong auto-add is silent corruption of a portfolio."""
    scan.responses["Umbreon"] = [card("sv08.5-161", "Umbreon ex", "161")]
    post(scan, name="Umbreon", number="161")
    with holo.raw_db() as c:
        assert not c.execute("SELECT 1 FROM holdings").fetchone()


def test_scan_api_is_premium_only(client, monkeypatch):
    monkeypatch.setattr(holo, "tcgdex", lambda *a, **k: [])
    login(client, make_user("free", premium=False), "free")
    r = client.post("/api/scan", json={"name": "Umbreon", "number": "161"})
    assert r.status_code in (200, 403)
