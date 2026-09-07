"""Bulk import — parsing and resolution.

The old importer matched the right *printing* for 104 of 143 real rows. Almost
every miss came from discarding information: the set column was ignored, the
collector number was dropped whenever it had a slash or a letter, and a card
with no English release was attached to whatever English card shared its name.
These tests pin each of those open.

Nothing here touches the network: `resolve_row` is driven through a stubbed
`app.tcgdex` / `app.tcgdex_lang`, so a failure is a logic failure.
"""

import csv
import os

import pytest

import app as holo

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "tests-fixture-collectr.csv")


@pytest.fixture(scope="module")
def collectr():
    return open(FIXTURE).read()


# ------------------------------------------------------------ the whole file

def test_the_export_parses_to_143_rows_and_148_cards(collectr):
    rows, cols = holo.parse_import(collectr)
    assert len(rows) == 143
    assert sum(r["qty"] for r in rows) == 148


def test_the_header_row_is_recognised_and_not_imported_as_a_card(collectr):
    rows, cols = holo.parse_import(collectr)
    assert cols is not None, "Collectr's header row was not detected"
    assert all(r["name"] != "name" for r in rows)


def test_no_row_imports_a_price_as_cost_basis(collectr):
    """collectr_price_gbp is current market value. Importing it as cost makes
    every card show zero gain forever, so it must land in `market`, not `paid`."""
    rows, _ = holo.parse_import(collectr)
    assert all(r["paid"] is None for r in rows)
    assert sum(1 for r in rows if r["market"] is not None) == 143


def test_every_row_keeps_its_quantity(collectr):
    rows, _ = holo.parse_import(collectr)
    assert {r["qty"] for r in rows} == {1, 2}


# ------------------------------------------------------- collector numbers

@pytest.mark.parametrize("raw,expected", [
    ("072/080", "072"),          # zero padding is significant in SV-era sets
    ("004", "004"),
    ("63", "63"),                # and absent in older ones
    ("2/102", "2"),
    ("TG06/TG30", "TG06"),       # Trainer Gallery
    ("GG04/GG70", "GG04"),       # Galarian Gallery
    ("SV11/SV94", "SV11"),       # Shiny Vault
    ("SWSH153", "SWSH153"),      # promo, no slash at all
    ("84a/111", "84a"),          # trailing letter is part of the number
    ("0205/07", "0205"),         # Gem Pack: preserved even though no set uses it
    ("#12/45", "12"),
    ("  201/165  ", "201"),
    ("", None), (None, None), ("Near Mint", None), ("Holofoil", None),
])
def test_number_parsing(raw, expected):
    assert holo.parse_number(raw) == expected


def test_zero_padding_is_never_normalised():
    """TCGdex uses 072 for SV-era and 44 for older sets; changing either breaks
    the match, so the parser must not 'tidy' them."""
    assert holo.parse_number("072/080") == "072"
    assert holo.parse_number("072/080") != "72"
    assert holo.parse_number("44/102") == "44"
    assert holo.parse_number("44/102") != "044"


# ------------------------------------------------------------ variant labels

@pytest.mark.parametrize("raw,name,variant,region", [
    ("Meowth (Master Ball Pattern) (CN)", "Meowth", "Master Ball Pattern", "cn"),
    ("Cubone (Full Art) (CN)", "Cubone", "Full Art", "cn"),
    ("Espeon V (JP)", "Espeon V", None, "jp"),
    ("Pikachu (Shiny Pattern) (CN)", "Pikachu", "Shiny Pattern", "cn"),
    ("Lugia (Cosmos Holo)", "Lugia", "Cosmos Holo", None),
    ("Pikachu (Holiday Calendar)", "Pikachu", "Holiday Calendar", None),
    ("Umbreon ex", "Umbreon ex", None, None),
    ("Mr. Mime", "Mr. Mime", None, None),
])
def test_variant_and_region_split_off_the_name(raw, name, variant, region):
    assert holo.split_variant(raw) == (name, variant, region)


def test_the_variant_label_is_kept_not_discarded(collectr):
    """The label is real information about which printing it is."""
    rows, _ = holo.parse_import(collectr)
    meowth = next(r for r in rows if r["name"] == "Meowth")
    assert meowth["variant"] == "Master Ball Pattern"
    assert meowth["region"] == "cn"


# ------------------------------------------------------------------- grading

@pytest.mark.parametrize("cond,grade,left", [
    ("PSA 10 (GEM-MT)", "PSA 10", None),
    ("PSA 9 (MINT)", "PSA 9", None),
    ("BGS 9.5", "BGS 9.5", None),
    ("CGC 8", "CGC 8", None),
    ("SGC 10", "SGC 10", None),
    ("ACE 10", "ACE 10", None),
    ("TAG 9", "TAG 9", None),
    ("psa 10", "PSA 10", None),
    ("Near Mint", None, "Near Mint"),
    ("Lightly Played", None, "Lightly Played"),
    ("", None, None), (None, None, None),
])
def test_grade_is_parsed_out_of_the_condition_field(cond, grade, left):
    assert holo.parse_grade(cond) == (grade, left)


def test_the_two_graded_cards_in_the_export_are_found(collectr):
    rows, _ = holo.parse_import(collectr)
    graded = {r["name"]: r["grade"] for r in rows if r["grade"]}
    assert graded == {"Ditto V": "PSA 10", "Chansey": "PSA 9"}


def test_an_ungraded_condition_does_not_become_a_grade(collectr):
    rows, _ = holo.parse_import(collectr)
    nm = [r for r in rows if r["condition"] == "Near Mint"]
    assert nm and all(r["grade"] is None for r in nm)


def test_finish_is_stored(collectr):
    rows, _ = holo.parse_import(collectr)
    assert {r["finish"] for r in rows} >= {"Holofoil", "Normal", "1st Edition", "Unlimited"}


# ---------------------------------------------------------- set resolution

@pytest.mark.parametrize("name,expected_known", [
    ("Prismatic Evolutions", True), ("Obsidian Flames", True),
    ("prismatic evolutions", True),        # case and spacing are noise
    ("Gem Pack Vol. 3", False),            # Chinese-only, not in the English data
    ("Definitely Not A Set", False),
    ("", False), (None, False),
])
def test_set_names_resolve_or_honestly_fail(name, expected_known):
    assert (holo.resolve_set(name) is not None) is expected_known


def test_a_set_abbreviation_resolves():
    known = {m.get("abbr") for m in holo.SETS.values() if m.get("abbr")}
    abbr = sorted(known)[0]
    assert holo.resolve_set(abbr) is not None


# ------------------------------------------------------------ file handling

def test_tab_separated_input_works():
    rows, cols = holo.parse_import(
        "name\tset\tnumber\tqty\tcondition\tfinish\n"
        "Umbreon ex\tPrismatic Evolutions\t161/131\t1\tNear Mint\tHolofoil\n")
    assert cols and len(rows) == 1
    assert rows[0]["name"] == "Umbreon ex" and rows[0]["number"] == "161"


def test_quoted_commas_inside_a_field_survive():
    """The old hand-rolled regex could not see these; csv can."""
    rows, _ = holo.parse_import(
        'name,set,number,qty\n"Iris\'s Fighting Spirit, Full Art",Chaos Rising,120/100,1\n')
    assert len(rows) == 1
    assert rows[0]["name"].startswith("Iris's Fighting Spirit")


def test_a_headerless_paste_still_works_and_still_refuses_to_guess_cost():
    rows, cols = holo.parse_import("2, Umbreon ex, Prismatic Evolutions, 161\n"
                                   "Charizard ex, Obsidian Flames, 125\n")
    assert cols is None
    assert [r["qty"] for r in rows] == [2, 1]
    assert [r["number"] for r in rows] == ["161", "125"]
    assert all(r["paid"] is None for r in rows), "a bare number is not cost basis"


def test_blank_lines_are_ignored():
    rows, _ = holo.parse_import("\n\nUmbreon ex, Prismatic Evolutions, 161\n\n   \n")
    assert len(rows) == 1


def test_a_row_called_set_is_not_mistaken_for_a_header():
    """Three recognisable field names are required, so a card cannot trip it."""
    rows, cols = holo.parse_import("Set, Prismatic Evolutions, 161\n")
    assert cols is None and len(rows) == 1
