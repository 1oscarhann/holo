"""Manual entries — sealed product, and cards no API can price.

custom_items existed with an API and no UI, and the importer's honest "not
found" was indistinguishable from deletion: the fixture's fourteen Chinese
cards and its most valuable card, a £199.84 Cubone, were reported unmatched
and then dropped on commit. This is where they go instead.
"""

import pytest

import app as holo
from conftest import login, make_card, make_holding, make_price, make_user


@pytest.fixture
def oscar(client):
    uid = make_user("oscar")
    login(client, uid, "oscar")
    client.uid = uid
    return client


def add(client, **kw):
    body = {"name": "Cubone (Full Art)", "kind": "card", "qty": 1, "value": 199.84}
    body.update(kw)
    return client.post("/api/custom", json=body)


# ------------------------------------------------------------------ the basics

def test_a_manual_item_can_be_added_and_listed(oscar):
    assert add(oscar).status_code == 200
    html = oscar.get("/manual").data.decode()
    assert "Cubone" in html


def test_a_manual_item_needs_a_name(oscar):
    assert oscar.post("/api/custom", json={"value": 10}).status_code == 400


def test_region_set_and_number_are_kept(oscar):
    add(oscar, set_name="Gem Pack Vol. 3", number="1904/04", region="cn")
    with holo.raw_db() as c:
        r = c.execute("SELECT * FROM custom_items").fetchone()
    assert (r["set_name"], r["number"], r["region"]) == ("Gem Pack Vol. 3", "1904/04", "cn")


def test_an_item_can_be_edited_and_removed(oscar):
    iid = add(oscar).get_json()["id"]
    assert oscar.patch("/api/custom", json={"id": iid, "value": 250.0}).status_code == 200
    with holo.raw_db() as c:
        assert c.execute("SELECT value FROM custom_items").fetchone()["value"] == 250.0
    oscar.delete("/api/custom", json={"id": iid})
    with holo.raw_db() as c:
        assert not c.execute("SELECT 1 FROM custom_items").fetchone()


def test_you_cannot_edit_or_delete_someone_elses_item(oscar):
    other = make_user("someone")
    with holo.raw_db() as c:
        iid = c.execute("""INSERT INTO custom_items (user_id,name,value)
                           VALUES (%s,'Theirs',99) RETURNING id""", (other,)).fetchone()["id"]
        c.commit()
    assert oscar.patch("/api/custom", json={"id": iid, "value": 1}).status_code == 403
    oscar.delete("/api/custom", json={"id": iid})
    with holo.raw_db() as c:
        assert c.execute("SELECT value FROM custom_items WHERE id=%s", (iid,)).fetchone()["value"] == 99


def test_the_page_and_api_require_login(client):
    assert client.get("/manual").status_code == 302
    assert client.post("/api/custom", json={"name": "x"}).status_code == 401


# ------------------------------------------------- they have to actually count

def test_a_manual_item_counts_toward_the_portfolio_total(oscar):
    """Leaving these out is how a £200 card TCGdex cannot price reads as nothing."""
    make_card("sv03-125"); make_price("sv03-125", 10.0)
    make_holding(oscar.uid, card_id="sv03-125", qty=1)
    add(oscar, value=199.84)
    with holo.app.test_request_context():
        from flask import session
        session["uid"] = oscar.uid
        s = holo.stats(oscar.uid)
    assert s["total"] == pytest.approx(209.84)
    assert s["cards"] == 2 and s["unique"] == 2


def test_quantity_multiplies_a_manual_value(oscar):
    add(oscar, value=10.0, qty=3)
    with holo.raw_db() as c:
        assert custom_total(c) == pytest.approx(30.0)


def custom_total(c):
    return sum(x["value"] for x in holo.custom_with_values(
        c.execute("SELECT id FROM users LIMIT 1").fetchone()["id"], c))


def test_cost_basis_is_counted_when_given(oscar):
    add(oscar, value=200.0, paid=150.0)
    with holo.app.test_request_context():
        from flask import session
        session["uid"] = oscar.uid
        assert holo.stats(oscar.uid)["cost"] == pytest.approx(150.0)


def test_a_manual_item_never_pretends_to_have_a_market_move(oscar):
    add(oscar)
    with holo.raw_db() as c:
        item = holo.custom_with_values(oscar.uid, c)[0]
    assert item["price_basis"] == "manual"
    assert item["chg1"] is None and item["chg7"] is None and item["chg30"] is None


def test_the_snapshot_matches_the_hero_number(oscar):
    """A total that disagrees with its own chart is worse than either alone."""
    add(oscar, value=199.84)
    with holo.raw_db() as c:
        holo.ensure_snapshot(oscar.uid, c, force=True)
        total = c.execute("SELECT total FROM snapshots WHERE user_id=%s",
                          (oscar.uid,)).fetchone()["total"]
    assert total == pytest.approx(199.84)


def test_manual_items_are_scoped_to_their_owner(oscar):
    other = make_user("someone")
    add(oscar, value=199.84)
    with holo.raw_db() as c:
        assert holo.custom_with_values(other, c) == []


# ------------------------------------------ rescuing unmatched import rows

def _job_with_unmatched(uid, rows):
    with holo.raw_db() as c:
        job = c.execute("""INSERT INTO import_jobs (user_id,state,total)
                           VALUES (%s,'review',%s) RETURNING id""",
                        (uid, len(rows))).fetchone()["id"]
        for i, r in enumerate(rows, 1):
            c.execute("""INSERT INTO import_rows
                (job_id,n,name,qty,market,set_name,number,region,variant,grade,
                 card_id,confidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (job, i, r.get("name"), r.get("qty", 1), r.get("market"),
                 r.get("set_name"), r.get("number"), r.get("region"),
                 r.get("variant"), r.get("grade"), r.get("card_id"),
                 r.get("confidence", "unmatched")))
        c.commit()
    return job


def test_unmatched_rows_can_be_kept_rather_than_lost(oscar):
    job = _job_with_unmatched(oscar.uid, [
        {"name": "Cubone", "market": 199.84, "set_name": "Gem Pack Vol. 3",
         "number": "1904", "region": "cn", "variant": "Full Art"},
        {"name": "Meowth", "market": 12.40, "set_name": "Gem Pack Vol. 3",
         "number": "0205", "region": "cn", "variant": "Master Ball Pattern"},
    ])
    r = oscar.post(f"/api/import/{job}/keep", json={}).get_json()
    assert r["kept"] == 2
    with holo.raw_db() as c:
        items = {x["name"]: x for x in c.execute("SELECT * FROM custom_items").fetchall()}
    assert items["Cubone"]["value"] == pytest.approx(199.84)
    assert items["Cubone"]["region"] == "cn"
    assert items["Cubone"]["variant"] == "Full Art"
    assert items["Cubone"]["number"] == "1904"


def test_the_kept_value_comes_from_the_market_column_not_cost(oscar):
    """The export's price is market value. Keeping it as the manual valuation is
    right; recording it as cost basis is not."""
    job = _job_with_unmatched(oscar.uid, [{"name": "Cubone", "market": 199.84}])
    oscar.post(f"/api/import/{job}/keep", json={})
    with holo.raw_db() as c:
        row = c.execute("SELECT value, paid FROM custom_items").fetchone()
    assert row["value"] == pytest.approx(199.84) and row["paid"] is None


def test_kept_rows_are_not_committed_twice(oscar):
    job = _job_with_unmatched(oscar.uid, [{"name": "Cubone", "market": 199.84}])
    oscar.post(f"/api/import/{job}/keep", json={})
    oscar.post(f"/api/import/{job}/keep", json={})
    with holo.raw_db() as c:
        assert c.execute("SELECT COUNT(*) n FROM custom_items").fetchone()["n"] == 1


def test_matched_rows_are_never_swept_into_manual_entries(oscar):
    make_card("sv03-125")
    job = _job_with_unmatched(oscar.uid, [
        {"name": "Real", "card_id": "sv03-125", "confidence": "exact"},
        {"name": "Cubone", "market": 199.84},
    ])
    assert oscar.post(f"/api/import/{job}/keep", json={}).get_json()["kept"] == 1
    with holo.raw_db() as c:
        assert c.execute("SELECT name FROM custom_items").fetchone()["name"] == "Cubone"


def test_only_the_named_rows_are_kept_when_ids_are_given(oscar):
    job = _job_with_unmatched(oscar.uid, [{"name": "A", "market": 1.0},
                                          {"name": "B", "market": 2.0}])
    with holo.raw_db() as c:
        rid = c.execute("SELECT id FROM import_rows ORDER BY n LIMIT 1").fetchone()["id"]
    assert oscar.post(f"/api/import/{job}/keep", json={"ids": [rid]}).get_json()["kept"] == 1
    with holo.raw_db() as c:
        assert c.execute("SELECT name FROM custom_items").fetchone()["name"] == "A"


def test_you_cannot_keep_rows_from_someone_elses_import(oscar):
    other = make_user("someone")
    job = _job_with_unmatched(other, [{"name": "Theirs", "market": 5.0}])
    assert oscar.post(f"/api/import/{job}/keep", json={}).status_code == 404
    with holo.raw_db() as c:
        assert not c.execute("SELECT 1 FROM custom_items").fetchone()
