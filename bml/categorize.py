"""Sorting a mod into one of the nine vault folders.

The folders and what goes in each:

  CHALLENGES      challenge definitions
  COLLECTIONS     one author's multi-mod repo (`Betmma Mods`,
                  `my_balatro_mods-main`) -- several separate mods in one
                  download, which is exactly what "more than one manifest in
                  subfolders" detects
  DECKS           decks / backs
  JOKERS+         joker content, the biggest folder by far
  MODLOADERS      Steamodded and Lovely only (libraries like Talisman go
                  to UTILITY+QOL)
  MODPACKS        curated packs of other people's mods
  SKINS           texture packs and visual overhauls
  SUITS-CARDS-... suits, ranks, enhancements, seals, vouchers, consumables
  UTILITY+QOL     tools, QoL, libraries

Evidence is ranked: what the mod's code registers, then its registry label,
then its name. Anything still unclear goes to _UNSORTED with the reason.
"""

from __future__ import annotations

import re

from .config import UNSORTED

# The SMODS constructors that identify each kind of content.
_DECK_OBJECTS = ("Back", "Deck")
_CARD_OBJECTS = (
    "Suit", "Rank", "Enhancement", "Seal", "Voucher",
    "Consumable", "ConsumableType", "Edition", "Sticker",
)
_COSMETIC_OBJECTS = ("Atlas", "Sound")

# Registry labels -> our folders. Covers both the community index's vocabulary
# (Content, Joker, Quality of Life, Technical, Miscellaneous, Resource Packs,
# API, Extension) and Thunderstore's.
_LABEL_MAP = {
    "joker": "JOKERS+",
    "jokers": "JOKERS+",
    "resource packs": "SKINS",
    "resource pack": "SKINS",
    "textures": "SKINS",
    "texture pack": "SKINS",
    "skins": "SKINS",
    "quality of life": "UTILITY+QOL",
    "tools": "UTILITY+QOL",
    "libraries": "UTILITY+QOL",
    "library": "UTILITY+QOL",
    "api": "UTILITY+QOL",
    "technical": "UTILITY+QOL",
    "decks": "DECKS",
    "deck": "DECKS",
    "challenges": "CHALLENGES",
    "challenge": "CHALLENGES",
    "modpacks": "MODPACKS",
    "modpack": "MODPACKS",
}

_NAME_RULES = (
    (r"\bmod\s*pack\b|\bmodpack\b", "MODPACKS"),
    (r"\bchallenges?\b", "CHALLENGES"),
    (r"\bdecks?\b|\bcard\s*backs?\b", "DECKS"),
    (r"\bjokers?\b|\bjonklers?\b", "JOKERS+"),
    (r"\bsuits?\b|\btarots?\b|\bvouchers?\b|\bseals?\b|\bspectrals?\b"
     r"|\bconsumables?\b|\benhancements?\b|\bplanets?\b", "SUITS-CARDS-VOUCHERS-SEALS-CONSUMABLES"),
    (r"\btexture\s*pack\b|\bretexture\b|\bskins?\b|\bpaint\s*job\b|\bpixel\b"
     r"|\bdark\s*mode\b|\btheme\b", "SKINS"),
    (r"\bqol\b|\bquality of life\b|\bhotkeys?\b|\bpreview\b|\bdisplay\b"
     r"|\bmanager\b|\bdebug\b|\bcheat\b|\btimer\b|\bclock\b", "UTILITY+QOL"),
)

# Only these two count as loaders.
_LOADER_IDS = {"steamodded", "lovely", "smods"}

# How many jokers a mod needs before JOKERS+ absorbs it despite another object
# type narrowly outnumbering them. See the note in `_from_objects`.
_JOKER_ABSORB_FLOOR = 10


def categorise(entry: dict, manifest=None, smods_objects=None,
               manifest_count: int = 1) -> tuple[str, str]:
    """Return (category, reason).

    `entry` is the merged catalog record, `manifest` the parsed ModManifest
    (may be None), `smods_objects` the constructor counts from
    manifest.scan_smods_objects, and `manifest_count` how many distinct
    manifests the download contained.
    """
    forced = (entry.get("forced_category") or "").strip()
    if forced:
        return forced, "forced by local-index entry"

    mod_id = (manifest.mod_id if manifest else "") or ""
    if mod_id.lower() in _LOADER_IDS:
        return "MODLOADERS", f"{mod_id} is the mod loader itself"

    title = entry.get("title") or ""
    haystack = " ".join(
        [title, entry.get("description") or "",
         manifest.description if manifest else ""]
    ).lower()

    # Several mods in one download: a modpack or a collection, told apart by name.
    if manifest_count > 1:
        if re.search(r"\bmod\s*pack\b|\bmodpack\b", haystack):
            return "MODPACKS", f"{manifest_count} bundled mods, described as a modpack"
        return "COLLECTIONS", f"{manifest_count} separate mods in one download"

    labels = [str(c).strip().lower() for c in (entry.get("categories") or [])]
    labelled = next(
        (( _LABEL_MAP[label], label) for label in labels if label in _LABEL_MAP),
        None,
    )

    counts = dict(smods_objects or {})
    inconclusive = ""
    if counts:
        category, reason = _from_objects(counts)
        if not category and reason:
            # Inconclusive; let the weaker signals below decide.
            inconclusive = reason
        # A library that registers one token joker (Talisman) is not a joker
        # mod. With this little object evidence, the registry label wins.
        if category and labelled and _gameplay_total(counts) < 3:
            mapped, label = labelled
            if mapped != category:
                return mapped, (
                    f"registry category {label!r} -- registers too little "
                    f"content to call it {category} ({_fmt(counts, None)})"
                )
        if category:
            return category, reason

    if labelled:
        mapped, label = labelled
        return mapped, f"registry category {label!r}"

    for pattern, category in _NAME_RULES:
        if re.search(pattern, haystack):
            return category, f"name/description matched /{pattern}/"

    if inconclusive:
        return UNSORTED, inconclusive

    # "Content" and "Extension" don't name a folder, so they come last.
    if "content" in labels or "extension" in labels:
        return UNSORTED, "registry says 'Content' but the mod registers no identifiable objects"

    return UNSORTED, "no manifest evidence, registry label, or name match"


def _gameplay_total(counts: dict[str, int]) -> int:
    return (
        counts.get("Joker", 0)
        + sum(counts.get(k, 0) for k in _DECK_OBJECTS)
        + sum(counts.get(k, 0) for k in _CARD_OBJECTS)
        + counts.get("Challenge", 0)
    )


def _from_objects(counts: dict[str, int]) -> tuple[str, str]:
    """Decide from what the mod's Lua actually registers."""
    jokers = counts.get("Joker", 0)
    decks = sum(counts.get(k, 0) for k in _DECK_OBJECTS)
    cards = sum(counts.get(k, 0) for k in _CARD_OBJECTS)
    challenges = counts.get("Challenge", 0)
    cosmetic = sum(counts.get(k, 0) for k in _COSMETIC_OBJECTS)
    gameplay = jokers + decks + cards + challenges

    if gameplay == 0:
        if cosmetic:
            return "SKINS", f"registers only cosmetics ({_fmt(counts, _COSMETIC_OBJECTS)})"
        return "", ""

    # Challenges win when there is little else.
    if challenges and challenges >= max(jokers, decks, cards):
        return "CHALLENGES", f"registers {challenges} challenge(s)"

    ranked = [
        (jokers, "JOKERS+", f"registers {jokers} joker(s)"),
        (cards, "SUITS-CARDS-VOUCHERS-SEALS-CONSUMABLES",
         f"registers {cards} card object(s) ({_fmt(counts, _CARD_OBJECTS)})"),
        (decks, "DECKS", f"registers {decks} deck/back(s)"),
    ]
    ranked.sort(key=lambda item: item[0], reverse=True)
    top_count, category, reason = ranked[0]
    if top_count == 0:
        return "", ""

    runner_up = ranked[1][0]
    # The "+" in JOKERS+ covers the vouchers and consumables that joker mods
    # bring along, so a joker plurality is enough.
    if category == "JOKERS+":
        return category, (
            f"{reason}, the plurality of its content ({_fmt(counts, None)})"
            if runner_up else reason
        )

    # A big joker mod still belongs in JOKERS+ when another type narrowly
    # outnumbers its jokers (Hot Potato has 141).
    if jokers >= _JOKER_ABSORB_FLOOR and top_count < jokers * 2:
        return "JOKERS+", (
            f"registers {jokers} joker(s) alongside {top_count} "
            f"{category.split('-')[0].lower()} object(s), none dominant "
            f"({_fmt(counts, None)})"
        )

    if runner_up and top_count < runner_up * 2:
        # No clear winner. "" lets the caller fall through to label and name.
        return "", f"mixed content, no dominant type ({_fmt(counts, None)})"
    return category, reason


def _fmt(counts: dict[str, int], keys) -> str:
    items = [(k, v) for k, v in counts.items() if v and (keys is None or k in keys)]
    items.sort(key=lambda kv: kv[1], reverse=True)
    return ", ".join(f"{k}x{v}" for k, v in items[:6]) or "none"
