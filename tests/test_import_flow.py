"""Bulk import — the two-step flow.

The old importer did every lookup inside the request: ~1.5s a row against a
120s gunicorn worker, so a 143-row paste could not finish. It also committed
everything before telling you what it had guessed.

Now the POST only parses and stages (no network, so it returns immediately),
resolution happens on a background thread, and nothing reaches holdings until
the review step is committed.
"""

import os

import pytest

import app as holo
from conftest import login, make_card, make_user

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "tests-fixture-collectr.csv")


@pytest.fixture
def oscar(client):
    uid = make_user("oscar")
    login(client, uid, "oscar")
    client.uid = uid
    return client


def test_posting_the_whole_export_returns_immediately_and_touches_no_network(oscar):
    """The timeout fix. `no_network` in conftest raises on any requests call, so
    this failing means the request is doing lookups again."""
    text = open(FIXTURE).read()
    r = oscar.post("/import", json={"text": text})
    assert r.status_code == 200
    body = r.get_json()
    assert body["total"] == 143 and body["headers"] is True
    assert body["job"]


def test_the_paste_is_staged_not_imported(oscar):
    oscar.post("/import", json={"text": open(FIXTURE).read()})
    with holo.raw_db() as c:
        assert c.execute("SELECT COUNT(*) n FROM import_rows").fetchone()["n"] == 143
        assert not c.execute("SELECT 1 FROM holdings").fetchone(), \
            "nothing may reach holdings before review"


def test_nothing_is_capped_at_300_rows(oscar):
    """The old importer silently dropped everything past row 300."""
    text = "name,set,number,qty\n" + "".join(
        f"Card {i},Obsidian Flames,{i:03d},1\n" for i in range(1, 401))
    r = oscar.post("/import", json={"text": text})
    assert r.get_json()["total"] == 400
    with holo.raw_db() as c:
        assert c.execute("SELECT COUNT(*) n FROM import_rows").fetchone()["n"] == 400


def test_an_empty_paste_is_refused_politely(oscar):
    r = oscar.post("/import", json={"text": "   \n\n"})
    assert r.status_code == 400 and "error" in r.get_json()


def test_progress_is_reported_while_resolving(oscar, monkeypatch):
    monkeypatch.setattr(holo, "tcgdex", lambda *a, **k: [])
    job = oscar.post("/import", json={"text": "name,set,number,qty\n"
                                              "A,Obsidian Flames,001,1\n"
                                              "B,Obsidian Flames,002,1\n"}).get_json()["job"]
    s = oscar.get(f"/api/import/{job}").get_json()
    assert s["state"] == "resolving" and s["total"] == 2 and s["done"] == 0
    assert "rows" not in s          # no table until it is finished

    holo.run_import_job(job, oscar.uid)
    s = oscar.get(f"/api/import/{job}").get_json()
    assert s["state"] == "review" and s["done"] == 2
    assert len(s["rows"]) == 2


def test_the_review_table_reports_confidence_per_row(oscar, monkeypatch):
    monkeypatch.setattr(holo, "SETS", {
        "sv03": {"name": "Obsidian Flames", "abbr": "OBF", "serie": "Scarlet & Violet",
                 "release": "2023-08-11", "total": 197}})
    monkeypatch.setattr(holo, "_SET_BY_NAME", None)
    monkeypatch.setattr(holo, "_SET_BY_ABBR", None)

    def fake(path, **params):
        if path.startswith("cards/"):
            return {"id": path.split("/", 1)[1], "name": "Pikachu", "set": {}}
        return [{"id": "sv03-063", "name": "Pikachu", "localId": "063"}] \
            if params.get("name") == "Pikachu" else []
    monkeypatch.setattr(holo, "tcgdex", fake)
    monkeypatch.setattr(holo, "refresh_price", lambda *a, **k: None)

    job = oscar.post("/import", json={
        "text": "name,set,number,qty\n"
                "Pikachu,Obsidian Flames,063,1\n"      # exact
                "Pikachu,Obsidian Flames,999,1\n"      # right set, wrong number
                "Nobody,Obsidian Flames,001,1\n"       # not found
    }).get_json()["job"]
    holo.run_import_job(job, oscar.uid)
    s = oscar.get(f"/api/import/{job}").get_json()
    assert [r["confidence"] for r in s["rows"]] == ["exact", "set", "unmatched"]
    assert s["counts"]["exact"] == 1 and s["counts"]["unmatched"] == 1


def test_a_row_that_blows_up_does_not_kill_the_job(oscar, monkeypatch):
    calls = {"n": 0}
    def boom(path, **params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("network wobble")
        return []
    monkeypatch.setattr(holo, "tcgdex", boom)
    job = oscar.post("/import", json={"text": "name,set,number,qty\n"
                                              "A,Obsidian Flames,001,1\n"
                                              "B,Obsidian Flames,002,1\n"}).get_json()["job"]
    holo.run_import_job(job, oscar.uid)
    s = oscar.get(f"/api/import/{job}").get_json()
    assert s["state"] == "review"
    assert s["rows"][0]["confidence"] == "unmatched"
    assert "failed" in (s["rows"][0]["note"] or "").lower()


def test_rows_can_be_dropped_before_committing(oscar):
    make_card("sv03-125")
    with holo.raw_db() as c:
        job = c.execute("""INSERT INTO import_jobs (user_id,state,total)
                           VALUES (%s,'review',2) RETURNING id""",
                        (oscar.uid,)).fetchone()["id"]
        for i in (1, 2):
            c.execute("""INSERT INTO import_rows (job_id,n,name,qty,card_id,confidence)
                         VALUES (%s,%s,'Card',1,'sv03-125','exact')""", (job, i))
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM import_rows WHERE job_id=%s ORDER BY n", (job,)).fetchall()]
        c.commit()

    oscar.patch(f"/api/import/{job}/rows", json={"rows": [{"id": ids[0], "skip": True}]})
    r = oscar.post(f"/api/import/{job}/commit").get_json()
    assert r["added"] == 1 and r["skipped"] == 1


def test_committing_before_resolution_finishes_is_refused(oscar):
    with holo.raw_db() as c:
        job = c.execute("""INSERT INTO import_jobs (user_id,state,total)
                           VALUES (%s,'resolving',5) RETURNING id""",
                        (oscar.uid,)).fetchone()["id"]
        c.commit()
    assert oscar.post(f"/api/import/{job}/commit").status_code == 409


def test_editing_cannot_reach_another_jobs_rows(oscar):
    other = make_user("someone")
    with holo.raw_db() as c:
        mine = c.execute("""INSERT INTO import_jobs (user_id,state,total)
                            VALUES (%s,'review',1) RETURNING id""",
                         (oscar.uid,)).fetchone()["id"]
        theirs = c.execute("""INSERT INTO import_jobs (user_id,state,total)
                              VALUES (%s,'review',1) RETURNING id""",
                           (other,)).fetchone()["id"]
        rid = c.execute("""INSERT INTO import_rows (job_id,n,name,qty)
                           VALUES (%s,1,'Theirs',1) RETURNING id""",
                        (theirs,)).fetchone()["id"]
        c.commit()
    oscar.patch(f"/api/import/{mine}/rows", json={"rows": [{"id": rid, "skip": True}]})
    with holo.raw_db() as c:
        assert c.execute("SELECT skip FROM import_rows WHERE id=%s", (rid,)).fetchone()["skip"] is False


def test_a_second_import_of_the_same_card_tops_up_rather_than_failing(oscar):
    """holdings is unique on (user_id, card_id, grade); the upsert has to add."""
    make_card("sv03-125")
    for _ in range(2):
        with holo.raw_db() as c:
            job = c.execute("""INSERT INTO import_jobs (user_id,state,total)
                               VALUES (%s,'review',1) RETURNING id""",
                            (oscar.uid,)).fetchone()["id"]
            c.execute("""INSERT INTO import_rows (job_id,n,name,qty,card_id,confidence)
                         VALUES (%s,1,'Card',2,'sv03-125','exact')""", (job,))
            c.commit()
        assert oscar.post(f"/api/import/{job}/commit").status_code == 200
    with holo.raw_db() as c:
        rows = c.execute("SELECT qty FROM holdings WHERE user_id=%s", (oscar.uid,)).fetchall()
    assert len(rows) == 1 and rows[0]["qty"] == 4


def test_an_existing_cost_basis_is_not_overwritten_by_a_later_import(oscar):
    """COALESCE keeps what you actually paid rather than the newest guess."""
    make_card("sv03-125")
    with holo.raw_db() as c:
        c.execute("""INSERT INTO holdings (user_id,card_id,qty,paid,grade)
                     VALUES (%s,'sv03-125',1,50.0,'')""", (oscar.uid,))
        job = c.execute("""INSERT INTO import_jobs (user_id,state,total)
                           VALUES (%s,'review',1) RETURNING id""",
                        (oscar.uid,)).fetchone()["id"]
        c.execute("""INSERT INTO import_rows (job_id,n,name,qty,paid,card_id,confidence)
                     VALUES (%s,1,'Card',1,9.99,'sv03-125','exact')""", (job,))
        c.commit()
    oscar.post(f"/api/import/{job}/commit")
    with holo.raw_db() as c:
        assert c.execute("SELECT paid FROM holdings WHERE user_id=%s",
                         (oscar.uid,)).fetchone()["paid"] == 50.0


def test_a_snapshot_is_taken_after_a_successful_commit(oscar):
    make_card("sv03-125")
    with holo.raw_db() as c:
        job = c.execute("""INSERT INTO import_jobs (user_id,state,total)
                           VALUES (%s,'review',1) RETURNING id""",
                        (oscar.uid,)).fetchone()["id"]
        c.execute("""INSERT INTO import_rows (job_id,n,name,qty,card_id,confidence)
                     VALUES (%s,1,'Card',1,'sv03-125','exact')""", (job,))
        c.commit()
    oscar.post(f"/api/import/{job}/commit")
    with holo.raw_db() as c:
        assert c.execute("SELECT 1 FROM snapshots WHERE user_id=%s",
                         (oscar.uid,)).fetchone()
