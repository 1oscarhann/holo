"""Bulk import — the paste-a-CSV path.

This crashed on any card you already owned, because holdings is unique on
(user_id, card_id, grade). The ON CONFLICT that fixed it is load-bearing.
"""

import pytest

import app as holo
from conftest import login, make_card, make_holding, make_user

SETS = {"sv08.5": {"name": "Prismatic Evolutions", "serie": "Scarlet & Violet",
                   "release": "2025-01-17", "total": 207},
        "A1": {"name": "Genetic Apex", "serie": "Pokémon TCG Pocket",
               "release": "2024-10-30", "total": 226}}

UMBREON = {"id": "sv08.5-161", "name": "Umbreon ex", "localId": "161",
           "image": "https://assets.tcgdex.net/en/sv/sv08.5/161",
           "set": {"id": "sv08.5", "name": "Prismatic Evolutions", "cardCount": {"official": 131}},
           "pricing": {"cardmarket": {"trend": 100.0}}}
POCKET = {"id": "A1-94", "name": "Umbreon", "localId": "94",
          "set": {"id": "A1", "name": "Genetic Apex"}}


@pytest.fixture
def importer(client, monkeypatch):
    monkeypatch.setattr(holo, "SETS", SETS)
    uid = make_user("alice")
    login(client, uid, "alice")
    client.uid = uid
    client.pool = [UMBREON]

    def fake_tcgdex(path, **params):
        if path.startswith("cards/"):
            cid = path.split("/", 1)[1]
            return next((c for c in client.pool if c["id"] == cid), None)
        return client.pool

    monkeypatch.setattr(holo, "tcgdex", fake_tcgdex)
    return client


def do(importer, text):
    return importer.post("/import", json={"text": text}).get_json()


def holdings(uid):
    with holo.raw_db() as c:
        return c.execute("SELECT card_id, qty, paid FROM holdings WHERE user_id=%s",
                         (uid,)).fetchall()


def test_a_plain_name_imports(importer):
    assert do(importer, "Umbreon ex")["added"] == 1
    assert holdings(importer.uid)[0]["card_id"] == "sv08.5-161"


def test_importing_a_card_you_already_own_adds_to_the_pile(importer):
    """The regression: this used to blow up on the unique constraint."""
    make_holding(importer.uid, card_id="sv08.5-161", qty=2, paid=50.0)
    r = do(importer, "Umbreon ex")
    assert r["added"] == 1 and r["failed"] == []
    rows = holdings(importer.uid)
    assert len(rows) == 1 and rows[0]["qty"] == 3


def test_importing_the_same_line_twice_accumulates(importer):
    do(importer, "Umbreon ex\nUmbreon ex")
    assert holdings(importer.uid)[0]["qty"] == 2


def test_an_existing_paid_price_is_not_overwritten_by_a_later_import(importer):
    """COALESCE keeps what you actually paid rather than the newest guess."""
    make_holding(importer.uid, card_id="sv08.5-161", qty=1, paid=50.0)
    do(importer, "1, Umbreon ex, 161, 9.99")
    assert holdings(importer.uid)[0]["paid"] == 50.0


@pytest.mark.parametrize("line,qty,paid", [
    ("Umbreon ex", 1, None),
    ("2, Umbreon ex", 2, None),
    ("2, Umbreon ex, 161", 2, None),
    ("2, Umbreon ex, 161, 12.50", 2, 12.50),
    ("Umbreon ex\t161\t£12.50", 1, 12.50),
    ("2, Umbreon ex, 161, $12.50", 2, 12.50),
])
def test_quantity_and_price_are_parsed_off_the_line(importer, line, qty, paid):
    do(importer, line)
    row = holdings(importer.uid)[0]
    assert (row["qty"], row["paid"]) == (qty, paid)


def test_quoted_fields_containing_commas_stay_one_field(importer):
    do(importer, '1,"Umbreon ex, Special Illustration Rare",161')
    assert len(holdings(importer.uid)) == 1


def test_header_rows_are_skipped(importer):
    r = do(importer, "Name,Quantity,Price\nUmbreon ex")
    assert r["added"] == 1 and r["total"] == 1


def test_blank_lines_are_ignored(importer):
    assert do(importer, "\n\nUmbreon ex\n\n  \n")["total"] == 1


def test_unmatched_names_are_reported_not_silently_dropped(importer):
    importer.pool = []
    r = do(importer, "Notapokemon\nAlsonot")
    assert r["added"] == 0
    assert set(r["failed"]) == {"Notapokemon", "Alsonot"}


def test_a_failure_does_not_abort_the_rest_of_the_batch(importer, monkeypatch):
    def fake_tcgdex(path, **params):
        if path.startswith("cards/"):
            return UMBREON
        return [] if params.get("name") == "Notapokemon" else [UMBREON]

    monkeypatch.setattr(holo, "tcgdex", fake_tcgdex)
    r = do(importer, "Notapokemon\nUmbreon ex")
    assert r["added"] == 1 and r["failed"] == ["Notapokemon"]


def test_digital_only_cards_are_never_imported(importer):
    importer.pool = [POCKET]
    r = do(importer, "Umbreon")
    assert r["added"] == 0 and r["failed"] == ["Umbreon"]
    assert holdings(importer.uid) == []


def test_the_batch_is_capped(importer):
    """300 rows a go, so one paste cannot pin the single Render worker."""
    r = do(importer, "\n".join(["Umbreon ex"] * 400))
    assert r["total"] == 400
    assert r["added"] == 300


def test_import_only_writes_to_the_calling_user(importer):
    bystander = make_user("bystander")
    do(importer, "Umbreon ex")
    assert holdings(bystander) == []


def test_a_snapshot_is_taken_after_a_successful_import(importer):
    do(importer, "Umbreon ex")
    with holo.raw_db() as c:
        assert c.execute("SELECT 1 FROM snapshots WHERE user_id=%s",
                         (importer.uid,)).fetchone()
