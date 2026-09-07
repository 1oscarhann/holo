"""Bulk import — which printing a row actually attaches to.

This is where the real damage was. 37 of 143 rows landed on the right Pokemon
in the wrong set, and a GBP200 Chinese Cubone would have imported as a common
English one. The rule these tests exist to hold: a confident wrong match is
worse than an honest "not found".

TCGdex is stubbed throughout, so nothing here depends on the network.
"""

import pytest

import app as holo
from conftest import login, make_user

SETS = {
    "sv08.5": {"name": "Prismatic Evolutions", "abbr": "PRE", "serie": "Scarlet & Violet",
               "release": "2025-01-17", "total": 131},
    "sv03": {"name": "Obsidian Flames", "abbr": "OBF", "serie": "Scarlet & Violet",
             "release": "2023-08-11", "total": 197},
    "svp": {"name": "Scarlet & Violet Promos", "abbr": "PR-SV", "serie": "Scarlet & Violet",
            "release": "2023-01-01", "total": 200},
    "sv01": {"name": "Scarlet & Violet Base Set", "abbr": "SVI", "serie": "Scarlet & Violet",
             "release": "2023-03-31", "total": 198},
}


def card(cid, name, local_id):
    return {"id": cid, "name": name, "localId": local_id}


@pytest.fixture
def tcg(monkeypatch):
    """Stub both language endpoints. `en` and `ja` are answered separately so a
    test can prove an English answer is never used for a Japanese row."""
    state = {"en": {}, "ja": {}, "calls": []}

    def en(path, **params):
        state["calls"].append(("en", params.get("name", path)))
        if path.startswith("cards/"):
            return {"id": path.split("/", 1)[1], "name": "x", "set": {}}
        return state["en"].get(params.get("name"), [])

    def lang(l, path, **params):
        state["calls"].append((l, params.get("name", path)))
        return state[l].get(params.get("name"), []) if l in state else []

    monkeypatch.setattr(holo, "SETS", SETS)
    monkeypatch.setattr(holo, "_SET_BY_NAME", None)
    monkeypatch.setattr(holo, "_SET_BY_ABBR", None)
    monkeypatch.setattr(holo, "tcgdex", en)
    monkeypatch.setattr(holo, "tcgdex_lang", lang)
    return state


def row(**kw):
    base = {"name": None, "number": None, "set_name": None, "region": None}
    base.update(kw)
    return base


# --------------------------------------------------- the set column matters

def test_the_named_set_wins_over_a_better_ranked_reprint(tcg):
    """The 37-row bug: a Scarlet & Violet Base card resolving to a promo reprint
    because the search ranked the newer printing first."""
    tcg["en"]["Pikachu"] = [card("svp-140", "Pikachu", "140"),
                            card("sv01-063", "Pikachu", "063")]
    cid, sid, conf, _ = holo.resolve_row(
        row(name="Pikachu", number="063", set_name="Scarlet & Violet Base Set"))
    assert cid == "sv01-063" and sid == "sv01" and conf == "exact"


def test_set_matched_but_number_did_not_is_flagged_not_hidden(tcg):
    tcg["en"]["Pikachu"] = [card("sv01-063", "Pikachu", "063")]
    cid, _, conf, note = holo.resolve_row(
        row(name="Pikachu", number="999", set_name="Scarlet & Violet Base Set"))
    assert cid == "sv01-063" and conf == "set"
    assert "number" in note.lower()


def test_a_card_absent_from_the_named_set_is_not_silently_reattached(tcg):
    """If the row says Obsidian Flames and the card is not in Obsidian Flames,
    guessing another printing is exactly the bug being fixed."""
    tcg["en"]["Pikachu"] = [card("svp-140", "Pikachu", "140")]
    cid, _, conf, note = holo.resolve_row(
        row(name="Pikachu", number="140", set_name="Obsidian Flames"))
    assert cid is None and conf == "unmatched"
    assert "set" in note.lower()


def test_an_unrecognised_set_falls_back_but_says_so(tcg):
    tcg["en"]["Pikachu"] = [card("sv01-063", "Pikachu", "063")]
    cid, _, conf, note = holo.resolve_row(
        row(name="Pikachu", number="063", set_name="Some Set Nobody Has"))
    assert cid == "sv01-063" and conf == "number"
    assert "not recognised" in note.lower()


def test_name_only_matches_are_marked_as_a_guess(tcg):
    tcg["en"]["Pikachu"] = [card("sv01-063", "Pikachu", "063")]
    cid, _, conf, note = holo.resolve_row(row(name="Pikachu"))
    assert cid == "sv01-063" and conf == "name"
    assert "guess" in note.lower()


# ------------------------------------------- Chinese and Japanese never fall back

def test_a_chinese_row_is_never_matched_to_an_english_card(tcg):
    """The single most damaging failure in the old importer: Cubone (Full Art)
    (CN), the most valuable card in the collection at GBP199.84, importing as a
    random English Cubone worth pennies."""
    tcg["en"]["Cubone"] = [card("sv03-100", "Cubone", "100")]
    cid, _, conf, note = holo.resolve_row(
        row(name="Cubone", number="1904", set_name="Gem Pack Vol. 3", region="cn"))
    assert cid is None and conf == "unmatched"
    assert "chinese" in note.lower()


def test_a_chinese_row_does_not_even_ask_tcgdex(tcg):
    """There is no Chinese endpoint, so a lookup could only produce a wrong answer."""
    tcg["en"]["Meowth"] = [card("sv03-050", "Meowth", "050")]
    holo.resolve_row(row(name="Meowth", set_name="Gem Pack Vol. 3", region="cn"))
    assert tcg["calls"] == []


def test_meowth_master_ball_pattern_cn_does_not_resolve_to_an_english_meowth(tcg):
    tcg["en"]["Meowth"] = [card("sv03-050", "Meowth", "050")]
    name, variant, region = holo.split_variant("Meowth (Master Ball Pattern) (CN)")
    cid, _, conf, _ = holo.resolve_row(
        row(name=name, number="0205", set_name="Gem Pack Vol. 3", region=region))
    assert cid is None and conf == "unmatched"


def test_a_japanese_row_is_looked_up_in_japanese(tcg):
    """TCGdex is multilingual; /ja covers most of the JP rows."""
    tcg["ja"]["Espeon V"] = [card("s12a-184", "Espeon V", "184")]
    cid, _, conf, _ = holo.resolve_row(row(name="Espeon V", number="184", region="jp"))
    assert cid == "s12a-184"
    assert all(lang == "ja" for lang, _ in tcg["calls"] if _ == "Espeon V")


def test_a_japanese_row_never_falls_back_to_english(tcg):
    """An English Espeon V exists. It is still the wrong card."""
    tcg["en"]["Espeon V"] = [card("swsh7-064", "Espeon V", "064")]
    tcg["ja"] = {}
    cid, _, conf, note = holo.resolve_row(row(name="Espeon V", number="184", region="jp"))
    assert cid is None and conf == "unmatched"
    assert "japanese" in note.lower()
    assert ("en", "Espeon V") not in tcg["calls"]


# ---------------------------------------------------- honest "not found"

def test_a_card_tcgdex_does_not_have_is_reported_not_guessed(tcg):
    """Treasure Gadget (JP) and Darkrai Prism Star are genuinely absent."""
    cid, _, conf, note = holo.resolve_row(row(name="Darkrai Prism Star", number="88"))
    assert cid is None and conf == "unmatched" and note


def test_digital_only_cards_are_never_matched(tcg):
    tcg["en"]["Pikachu"] = [card("A1-094", "Pikachu ex", "094")]
    holo.SETS["A1"] = {"name": "Genetic Apex", "serie": "Pokémon TCG Pocket",
                       "release": "2024-10-30", "total": 226}
    cid, _, conf, _ = holo.resolve_row(row(name="Pikachu", number="094"))
    assert cid is None and conf == "unmatched"


# ------------------------------------------------------------ commit stage

def _stage(client, uid, rows):
    with holo.raw_db() as c:
        job = c.execute("""INSERT INTO import_jobs (user_id,state,total)
                           VALUES (%s,'review',%s) RETURNING id""",
                        (uid, len(rows))).fetchone()["id"]
        for i, r in enumerate(rows, 1):
            c.execute("""INSERT INTO import_rows
                (job_id,n,name,qty,paid,grade,condition,finish,region,variant,card_id,confidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'exact')""",
                (job, i, r.get("name"), r.get("qty", 1), r.get("paid"), r.get("grade"),
                 r.get("condition"), r.get("finish"), r.get("region"), r.get("variant"),
                 r.get("card_id")))
        c.commit()
    return job


def test_a_graded_card_does_not_collapse_into_the_raw_one(client):
    """holdings is unique on (user_id, card_id, grade) precisely so a PSA 10 and
    a raw copy are separate holdings with separate values. The old importer
    omitted grade from the INSERT, so both took the '' default and merged."""
    uid = make_user("oscar")
    from conftest import make_card
    make_card("swsh12-070", name="Chansey")
    login(client, uid, "oscar")
    job = _stage(client, uid, [
        {"name": "Chansey", "card_id": "swsh12-070", "grade": None, "qty": 1},
        {"name": "Chansey", "card_id": "swsh12-070", "grade": "PSA 9", "qty": 1},
    ])
    r = client.post(f"/api/import/{job}/commit")
    assert r.status_code == 200 and r.get_json()["added"] == 2
    with holo.raw_db() as c:
        rows = c.execute("SELECT grade, qty FROM holdings WHERE user_id=%s ORDER BY grade",
                         (uid,)).fetchall()
    assert [(x["grade"], x["qty"]) for x in rows] == [("", 1), ("PSA 9", 1)]


def test_commit_writes_condition_finish_region_and_variant(client):
    uid = make_user("oscar")
    from conftest import make_card
    make_card("sv03-125")
    login(client, uid, "oscar")
    job = _stage(client, uid, [{"name": "Cubone", "card_id": "sv03-125", "qty": 1,
                                "condition": "Near Mint", "finish": "Holofoil",
                                "region": "jp", "variant": "Full Art"}])
    client.post(f"/api/import/{job}/commit")
    with holo.raw_db() as c:
        h = c.execute("SELECT * FROM holdings WHERE user_id=%s", (uid,)).fetchone()
    assert (h["condition"], h["finish"], h["region"], h["variant"]) == \
        ("Near Mint", "Holofoil", "jp", "Full Art")


def test_unmatched_and_skipped_rows_are_not_committed(client):
    uid = make_user("oscar")
    from conftest import make_card
    make_card("sv03-125")
    login(client, uid, "oscar")
    job = _stage(client, uid, [{"name": "A", "card_id": "sv03-125"},
                               {"name": "B", "card_id": None}])
    with holo.raw_db() as c:
        c.execute("UPDATE import_rows SET skip=true WHERE name='A' AND job_id=%s", (job,))
        c.commit()
    r = client.post(f"/api/import/{job}/commit").get_json()
    assert r["added"] == 0 and r["skipped"] == 2
    with holo.raw_db() as c:
        assert not c.execute("SELECT 1 FROM holdings WHERE user_id=%s", (uid,)).fetchone()


def test_the_market_column_only_becomes_cost_when_asked(client):
    uid = make_user("oscar")
    from conftest import make_card
    make_card("sv03-125")
    login(client, uid, "oscar")
    with holo.raw_db() as c:
        job = c.execute("""INSERT INTO import_jobs (user_id,state,total)
                           VALUES (%s,'review',1) RETURNING id""", (uid,)).fetchone()["id"]
        c.execute("""INSERT INTO import_rows (job_id,n,name,qty,market,card_id,confidence)
                     VALUES (%s,1,'Cubone',1,199.84,'sv03-125','exact')""", (job,))
        c.commit()

    client.post(f"/api/import/{job}/commit")
    with holo.raw_db() as c:
        assert c.execute("SELECT paid FROM holdings WHERE user_id=%s",
                         (uid,)).fetchone()["paid"] is None

    with holo.raw_db() as c:
        c.execute("DELETE FROM holdings WHERE user_id=%s", (uid,))
        c.execute("UPDATE import_jobs SET state='review' WHERE id=%s", (job,))
        c.commit()
    client.post(f"/api/import/{job}/paid-column", json={"column": "market"})
    client.post(f"/api/import/{job}/commit")
    with holo.raw_db() as c:
        assert c.execute("SELECT paid FROM holdings WHERE user_id=%s",
                         (uid,)).fetchone()["paid"] == pytest.approx(199.84)


def test_another_user_cannot_read_or_commit_your_import(client):
    victim, attacker = make_user("victim"), make_user("attacker")
    job = _stage(client, victim, [{"name": "A", "card_id": None}])
    login(client, attacker, "attacker")
    assert client.get(f"/api/import/{job}").status_code == 404
    assert client.post(f"/api/import/{job}/commit").status_code == 404
    assert client.patch(f"/api/import/{job}/rows", json={"rows": []}).status_code == 404


def test_import_endpoints_require_login(client):
    assert client.get("/api/import/1").status_code == 401
    assert client.post("/api/import/1/commit").status_code == 401


def test_a_region_with_no_tcgdex_endpoint_is_never_looked_up_in_english(tcg):
    """Korean is mapped as a region but TCGdex serves no Korean data. Falling
    back to English there would reintroduce exactly the Cubone failure."""
    tcg["en"]["Lapras"] = [card("sv03-032", "Lapras", "032")]
    cid, _, conf, note = holo.resolve_row(row(name="Lapras", number="032", region="kr"))
    assert cid is None and conf == "unmatched"
    assert "korean" in note.lower()
    assert tcg["calls"] == []
