"""Category rules, checked against object counts seen in the real fetch.

    py -m tests.test_categorize
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bml.categorize import categorise  # noqa: E402
from bml.config import UNSORTED  # noqa: E402

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def ok(label: str, condition: bool) -> None:
    if not condition:
        FAILURES.append(label)


class _Manifest:
    def __init__(self, mod_id: str = "", description: str = "") -> None:
        self.mod_id = mod_id
        self.description = description


def cat(entry=None, objects=None, mod_id="", manifest_count=1) -> str:
    category, _ = categorise(
        entry or {"title": "Some Mod"},
        manifest=_Manifest(mod_id),
        smods_objects=objects or {},
        manifest_count=manifest_count,
    )
    return category


def test_joker_plurality_wins() -> None:
    """The folder is JOKERS+, so the "+" content must not cause a tie.

    Regression: Batrocities' real counts sent it to _UNSORTED.
    """
    check(
        "Batrocities",
        cat(objects={"Atlas": 161, "Joker": 81, "Voucher": 16, "Consumable": 14,
                     "Sound": 13, "Back": 11, "Enhancement": 9, "Seal": 6}),
        "JOKERS+",
    )
    check("a plain joker mod", cat(objects={"Joker": 12, "Atlas": 3}), "JOKERS+")
    check("one joker and nothing else", cat(objects={"Joker": 1}), "JOKERS+")


def test_non_joker_ties_stay_unsorted() -> None:
    """Cards vs decks has no "+" folder to absorb the ambiguity."""
    check("evenly split cards and decks",
          cat(objects={"Voucher": 10, "Back": 9}), UNSORTED)
    check("decks dominant", cat(objects={"Back": 20, "Voucher": 2}), "DECKS")
    check("cards dominant",
          cat(objects={"Consumable": 30, "Back": 2}),
          "SUITS-CARDS-VOUCHERS-SEALS-CONSUMABLES")


def test_cosmetics_and_loaders() -> None:
    check("atlases only", cat(objects={"Atlas": 40, "Sound": 2}), "SKINS")
    check("Steamodded", cat(mod_id="Steamodded"), "MODLOADERS")
    check("Lovely", cat(mod_id="lovely"), "MODLOADERS")
    # Talisman is a library, so UTILITY+QOL, not MODLOADERS.
    check("Talisman is not a loader",
          cat({"title": "Talisman", "categories": ["Technical"]},
              mod_id="Talisman"),
          "UTILITY+QOL")
    # One token joker must not make it a joker mod.
    check("a library registering one token joker",
          cat({"title": "Talisman", "categories": ["Technical"]},
              objects={"Atlas": 1, "Sound": 7, "Joker": 1}, mod_id="Talisman"),
          "UTILITY+QOL")
    # But a small mod with no label to fall back on still trusts its objects.
    check("small joker mod with no registry label",
          cat({"title": "Tiny Jokers"}, objects={"Joker": 2}), "JOKERS+")


def test_precedence() -> None:
    check("forced category beats everything",
          cat({"title": "Whatever", "forced_category": "MODPACKS"},
              objects={"Joker": 99}),
          "MODPACKS")
    check("registered objects beat a registry label",
          cat({"title": "Whatever", "categories": ["Quality of Life"]},
              objects={"Joker": 20}),
          "JOKERS+")
    check("registry label beats a name match",
          cat({"title": "Deck of Jokers", "categories": ["Resource Packs"]}),
          "SKINS")
    check("multi-manifest download is a collection",
          cat(manifest_count=4), "COLLECTIONS")
    check("multi-manifest download described as a modpack",
          cat({"title": "My Modpack"}, manifest_count=4), "MODPACKS")
    check("nothing to go on", cat({"title": "Xyzzy"}), UNSORTED)


def test_big_joker_mod_absorbed_despite_card_lead() -> None:
    """Hot Potato's real counts: 141 jokers, narrowly more card objects.

    A mod with 141 jokers belongs in JOKERS+.
    """
    category, reason = categorise(
        {"title": "Hot Potato", "categories": ["Content"]},
        smods_objects={"Joker": 141, "Consumable": 107, "Voucher": 25,
                       "Edition": 12, "Atlas": 147, "Sound": 44},
    )
    check("big joker mod absorbed", category, "JOKERS+")
    ok("absorption is explained", "none dominant" in reason)

    # The floor keeps small mods out: Six Suits has 7 jokers and belongs with
    # the suits, which its name says and its object counts do not.
    category, _ = categorise(
        {"title": "Six Suits", "categories": ["Content"]},
        smods_objects={"Joker": 7, "Consumable": 7, "Suit": 2, "Atlas": 10},
    )
    ok("small joker count is not absorbed", category != "JOKERS+")


def test_inconclusive_objects_fall_through_to_name() -> None:
    """`Too Many Decks`: 42 backs against 26 assorted card objects.

    Inconclusive by count, so the name rule has to decide.
    """
    category, _ = categorise(
        {"title": "Too Many Decks", "categories": ["Content"]},
        smods_objects={"Back": 42, "Consumable": 9, "Edition": 8, "Rank": 5,
                       "Suit": 4, "Atlas": 13},
    )
    check("inconclusive counts fall through to the name", category, "DECKS")

    # With nothing weaker to fall back on, it still lands in _UNSORTED and the
    # mixed-content reasoning is what gets recorded.
    category, reason = categorise(
        {"title": "Whatsit", "categories": ["Content"]},
        smods_objects={"Consumable": 9, "Back": 6},
    )
    check("no fallback still unsorted", category, UNSORTED)
    ok("mixed reason preserved", "no dominant type" in reason)


def main() -> int:
    for fn in (test_joker_plurality_wins, test_non_joker_ties_stay_unsorted,
               test_cosmetics_and_loaders, test_precedence,
               test_big_joker_mod_absorbed_despite_card_lead,
               test_inconclusive_objects_fall_through_to_name):
        fn()
    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        for failure in FAILURES:
            print("  -", failure)
        return 1
    print("test_categorize: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
