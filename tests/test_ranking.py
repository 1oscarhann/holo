"""rank_key — the sort that decides what a search actually shows first."""

import app as holo


def card(name, set_id):
    return {"name": name, "set_id": set_id}


def sorted_names(cards, q):
    return [c["name"] for c in sorted(cards, key=lambda c: holo.rank_key(c, q))]


def fake_sets(monkeypatch, mapping):
    monkeypatch.setattr(holo, "SETS", mapping)


def test_exact_and_prefix_share_a_tier(monkeypatch):
    """'Umbreon ex' must not be buried under every plain Umbreon ever printed.

    Both are tier 0, so release date breaks the tie and the newest wins.
    """
    fake_sets(monkeypatch, {
        "sv08.5": {"serie": "Scarlet & Violet", "release": "2025-01-17"},
        "neo1": {"serie": "Neo", "release": "2000-12-16"},
    })
    cards = [card("Umbreon", "neo1"), card("Umbreon ex", "sv08.5")]
    assert sorted_names(cards, "umbreon") == ["Umbreon ex", "Umbreon"]


def test_prefix_beats_mid_word_match(monkeypatch):
    fake_sets(monkeypatch, {"s": {"serie": "Scarlet & Violet", "release": "2024-01-01"}})
    cards = [card("Dark Umbreon", "s"), card("Umbreon VMAX", "s")]
    assert sorted_names(cards, "umbreon") == ["Umbreon VMAX", "Dark Umbreon"]


def test_word_boundary_beats_substring_only(monkeypatch):
    """' q' in the name is tier 1; a name that merely contains q is tier 2."""
    fake_sets(monkeypatch, {"s": {"serie": "Scarlet & Violet", "release": "2024-01-01"}})
    assert holo.rank_key(card("Dark Umbreon", "s"), "umbreon")[0] == 1
    assert holo.rank_key(card("Numbreonic", "s"), "umbreon")[0] == 2


def test_novelty_series_demoted_below_every_normal_tier(monkeypatch):
    """A tier-0 trainer-kit card must still lose to a tier-2 real card.

    The +3 bump is what keeps 'pika' from leading with a trainer-kit Raichu.
    """
    fake_sets(monkeypatch, {
        "tk": {"serie": "Trainer kits", "release": "2024-06-01"},
        "mcd": {"serie": "McDonald's Collection", "release": "2024-06-01"},
        "pop": {"serie": "POP", "release": "2024-06-01"},
        "sv": {"serie": "Scarlet & Violet", "release": "2000-01-01"},
    })
    for novelty in ("tk", "mcd", "pop"):
        assert holo.rank_key(card("Pikachu", novelty), "pikachu")[0] == 3
    # no space before the query, so this is the worst normal tier there is
    assert holo.rank_key(card("Superpikachu", "sv"), "pikachu")[0] == 2
    assert sorted_names([card("Pikachu", "tk"), card("Superpikachu", "sv")],
                        "pikachu") == ["Superpikachu", "Pikachu"]


def test_newest_set_first_within_a_tier(monkeypatch):
    fake_sets(monkeypatch, {
        "old": {"serie": "Base", "release": "1999-01-09"},
        "mid": {"serie": "Sword & Shield", "release": "2021-06-18"},
        "new": {"serie": "Scarlet & Violet", "release": "2025-03-28"},
    })
    cards = [card("Charizard", "mid"), card("Charizard", "old"), card("Charizard", "new")]
    keys = [holo.rank_key(c, "charizard") for c in cards]
    assert all(k[0] == 0 for k in keys)
    order = sorted(cards, key=lambda c: holo.rank_key(c, "charizard"))
    assert [c["set_id"] for c in order] == ["new", "mid", "old"]


def test_unknown_set_sorts_last_not_crash(monkeypatch):
    """Cards whose set isn't in sets.json still have to sort — no KeyError."""
    fake_sets(monkeypatch, {"known": {"serie": "Scarlet & Violet", "release": "2025-01-01"}})
    cards = [card("Umbreon", "nowhere"), card("Umbreon", "known")]
    assert sorted_names(cards, "umbreon") == ["Umbreon", "Umbreon"]
    assert holo.rank_key(card("Umbreon", "nowhere"), "umbreon")[1] == [0, 0, 0]


def test_key_is_totally_orderable(monkeypatch):
    """Every component must be mutually comparable or sort() raises TypeError."""
    fake_sets(monkeypatch, {"a": {"serie": "X", "release": "2024-01-01"}})
    cards = [card("B", "a"), card("A", "a"), card("A", "missing")]
    sorted(cards, key=lambda c: holo.rank_key(c, "a"))  # must not raise
