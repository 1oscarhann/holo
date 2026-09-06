"""Theme and typeface selection.

The one rule that actually matters here is ordering: theme.js has to run
before app.css paints, or every load flashes the default theme first.
"""

import re

import app as holo
from conftest import login, make_user


def head_of(html):
    return html.split("</head>")[0]


def test_theme_script_runs_before_the_stylesheet_paints(client):
    """Stamped after the CSS loads, every page flashes the wrong theme."""
    for path, needs_login in (("/", False), ("/profile", True)):
        c = holo.app.test_client()
        if needs_login:
            login(c, make_user("oscar"), "oscar")
        head = head_of(c.get(path).data.decode())
        js, css = head.find("theme.js"), head.find("app.css")
        assert js != -1, f"theme.js missing from {path}"
        assert css != -1, f"app.css missing from {path}"
        assert js < css, f"theme.js must precede app.css on {path}"


def test_theme_script_is_not_deferred_or_async(client):
    """Either attribute lets the stylesheet win the race it must lose."""
    head = head_of(client.get("/").data.decode())
    tag = re.search(r"<script[^>]*theme\.js[^>]*>", head).group(0)
    assert "defer" not in tag and "async" not in tag


def test_the_theme_script_is_cache_busted(client):
    """Static files are cached for a day; an un-stamped theme.js would pin
    everyone to the previous build's theme list after a deploy."""
    head = head_of(client.get("/").data.decode())
    assert re.search(r"theme\.js\?v=\d+", head)


def test_every_palette_in_the_picker_exists_in_the_stylesheet(client):
    """A chip with no matching token block silently does nothing when tapped."""
    css = open("static/app.css").read()
    js = open("static/theme.js").read()
    listed = re.search(r'var THEMES = \[(.*?)\];', js, re.S).group(1)
    ids = re.findall(r'"([a-z]+)"', listed)
    assert len(ids) >= 8
    for tid in ids:
        assert f'[data-theme="{tid}"]' in css or tid == "ultramarine", tid


def test_every_face_in_the_picker_has_a_font_stack(client):
    css = open("static/app.css").read()
    js = open("static/theme.js").read()
    ids = re.findall(r'^\s{4}([a-z]+):\s+"', js, re.M)
    assert len(ids) >= 8
    for fid in ids:
        assert f'[data-font="{fid}"]' in css or fid == "familjen", fid


def test_the_default_theme_and_face_are_defined_on_bare_root(client):
    """A first-time visitor has nothing in localStorage, so the tokens have to
    resolve with no data-attribute stamped at all."""
    css = open("static/app.css").read()
    assert re.search(r':root,\s*\[data-theme="ultramarine"\]\{', css)
    assert re.search(r':root,\[data-font="familjen"\]\{', css)


def test_no_monospace_face_is_fetched_any_more(client):
    """The figure face follows --sans now; a mono webfont would be dead weight."""
    head = head_of(client.get("/").data.decode())
    assert "JetBrains" not in head
    assert "--fig:var(--sans)" in open("static/app.css").read()


def test_figures_still_align_in_columns(client):
    """Dropping the monospace face is only safe because tabular figures are on."""
    css = open("static/app.css").read().replace(" ", "")
    assert 'font-feature-settings:"tnum"1' in css
    assert "font-variant-numeric:tabular-nums" in css


def test_the_picker_is_on_the_profile_page(client):
    login(client, make_user("oscar"), "oscar")
    html = client.get("/profile").data.decode()
    assert 'id="appearance"' in html
    assert 'id="sw"' in html and 'id="fc"' in html


def test_the_picker_needs_no_login_endpoint(client):
    """It is a device preference, so nothing is written server-side and no new
    write endpoint is exposed."""
    writes = {r.rule for r in holo.app.url_map.iter_rules()
              if r.methods & {"POST", "PATCH", "PUT", "DELETE"}}
    assert not [r for r in writes if "theme" in r or "appearance" in r]
