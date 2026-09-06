"""img_chain / ptcgio_url — the fallback ladder behind every card thumbnail.

The trap this guards: pokemontcg.io answers a miss with HTTP 404 *and* a valid
Pokemon card-back PNG in the body. A browser renders that happily, so an
onerror handler never fires and the placeholder below never gets reached. The
only defence is that alt_image is a URL something already verified server-side
— so an unverified guess must never enter the chain.
"""

import json

import app as holo

TCGDEX = "https://assets.tcgdex.net/en/sv/sv08.5/161"
PTCGIO = "https://images.pokemontcg.io/sv8pt5/161.png"


def test_chain_order_and_extensions():
    chain = holo.img_chain(TCGDEX, "sv08.5", "161", alt_image=PTCGIO)
    assert chain == [f"{TCGDEX}/low.webp", PTCGIO, "/ph/sv08.5/161.svg"]


def test_hi_swaps_both_sources_to_high_res():
    chain = holo.img_chain(TCGDEX, "sv08.5", "161", hi=True, alt_image=PTCGIO)
    assert chain == [f"{TCGDEX}/high.webp",
                     "https://images.pokemontcg.io/sv8pt5/161_hires.png",
                     "/ph/sv08.5/161.svg"]


def test_unverified_alt_never_enters_the_chain():
    """No alt_image means resolve_alt_image found nothing (or hasn't run).

    Guessing the URL here is exactly how users silently get a card back.
    """
    chain = holo.img_chain(None, "sv08.5", "161", alt_image=None)
    assert chain == ["/ph/sv08.5/161.svg"]
    assert not any("pokemontcg.io" in u for u in chain)


def test_placeholder_always_last_and_always_present():
    for args in ((TCGDEX, "sv08.5", "161"), (None, "sv08.5", "161"), (None, None, None)):
        chain = holo.img_chain(*args)
        assert chain[-1].startswith("/ph/")
        assert chain[-1].endswith(".svg")


def test_placeholder_falls_back_to_x_when_ids_missing():
    assert holo.img_chain(None, None, None) == ["/ph/x/x.svg"]


def test_placeholder_path_is_url_quoted():
    """Set ids contain dots; anything odder must not break out of the path."""
    chain = holo.img_chain(None, "a/b", "1 2")
    assert chain == ["/ph/a%2Fb/1%202.svg"]


def test_ptcgio_url_uses_the_setmap_not_the_tcgdex_id():
    """The two APIs disagree on set ids: sv08.5 vs sv8pt5, swsh12.5 vs swsh12pt5."""
    assert holo.SETMAP.get("sv08.5") == "sv8pt5"
    assert holo.ptcgio_url("sv08.5", "161") == PTCGIO
    assert holo.ptcgio_url("sv08.5", "161", hi=True) == \
        "https://images.pokemontcg.io/sv8pt5/161_hires.png"


def test_ptcgio_url_gives_up_on_an_unmapped_set():
    assert holo.ptcgio_url("not-a-real-set", "1") is None


def test_ptcgio_url_gives_up_without_a_local_id():
    assert holo.ptcgio_url("sv08.5", None) is None
    assert holo.ptcgio_url("sv08.5", "") is None


def test_ptcgio_numbers_are_not_zero_padded():
    """TCGdex needs /001/, pokemontcg.io needs /1.png. Padding one breaks it."""
    assert holo.ptcgio_url("sv08.5", "1") == "https://images.pokemontcg.io/sv8pt5/1.png"


def test_setmap_is_loaded_and_substantial():
    """A silently empty setmap.json would kill every pokemontcg.io fallback."""
    assert len(holo.SETMAP) > 150
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in holo.SETMAP.items())


def test_with_images_attaches_primary_and_remaining_alternates():
    d = holo.with_images({"image": TCGDEX, "set_id": "sv08.5",
                          "local_id": "161", "alt_image": PTCGIO})
    assert d["img"] == f"{TCGDEX}/low.webp"
    assert d["img_alts"] == [PTCGIO, "/ph/sv08.5/161.svg"]
    assert d["img_hi"] == f"{TCGDEX}/high.webp"
    assert d["img_hi_alts"][-1] == "/ph/sv08.5/161.svg"


def test_img_attrs_emits_a_json_alt_list_the_client_can_parse():
    d = holo.with_images({"image": TCGDEX, "set_id": "sv08.5",
                          "local_id": "161", "alt_image": None})
    with holo.app.test_request_context():
        html = str(holo.img_attrs(d))
    assert html.startswith(f'src="{TCGDEX}/low.webp"')
    assert 'onerror="holoImg(this)"' in html
    assert json.loads(html.split('data-alts="')[1].split('"')[0].replace("&#34;", '"')) \
        == ["/ph/sv08.5/161.svg"]


def test_img_attrs_escapes_a_hostile_url():
    """Card metadata is third-party. It must not be able to close the attribute."""
    d = holo.with_images({"image": '"><script>alert(1)</script>', "set_id": "s",
                          "local_id": "1", "alt_image": None})
    with holo.app.test_request_context():
        html = str(holo.img_attrs(d))
    assert "<script>" not in html


def test_placeholder_route_returns_svg_without_login():
    """Images are referenced from pages and must render before any session exists."""
    c = holo.app.test_client()
    r = c.get("/ph/sv08.5/161.svg")
    assert r.status_code == 200
    assert r.mimetype == "image/svg+xml"
    assert b"NO ART" in r.data
    assert "max-age=604800" in r.headers["Cache-Control"]


def test_placeholder_route_escapes_its_labels():
    c = holo.app.test_client()
    r = c.get("/ph/%3Cscript%3E/%3Cimg%3E.svg")
    assert r.status_code == 200
    assert b"<script>" not in r.data
    assert b"<img>" not in r.data
