"""The mod page: markdown parsing, URL resolution, and counting content.

    py -m tests.test_modinfo

No network. The markdown cases are shapes real mod READMEs use, and the
localisation counter runs on a miniature of Cryptid's `en-us.lua`.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bml import modinfo  # noqa: E402

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def ok(label: str, condition: bool) -> None:
    if not condition:
        FAILURES.append(label)


def kinds(blocks) -> list[str]:
    return [b.kind for b in blocks]


def flat(block) -> str:
    return "".join(span.text for span in block.spans)


# -- Repo URLs -------------------------------------------------------------

def test_repo_urls() -> None:
    same = "https://github.com/MathIsFun0/Cryptid"
    for label, given in (
        ("plain", same),
        ("trailing slash", same + "/"),
        ("dot git", same + ".git"),
        ("ssh", "git@github.com:MathIsFun0/Cryptid.git"),
        ("no scheme", "github.com/MathIsFun0/Cryptid"),
        ("deep link", same + "/releases/tag/v0.5.16d"),
        ("tree link", same + "/tree/main/localization"),
    ):
        check(f"normalise {label}", modinfo.normalise_repo(given), same)

    check("non-github is left alone",
          modinfo.normalise_repo("https://gitlab.com/a/b"),
          "https://gitlab.com/a/b")
    check("empty", modinfo.normalise_repo(""), "")
    check("parts", modinfo.repo_parts(same), ("MathIsFun0", "Cryptid"))
    check("parts of nonsense", modinfo.repo_parts("not a url"), None)
    check("parts of a user page",
          modinfo.repo_parts("https://github.com/MathIsFun0"), None)


def test_image_urls() -> None:
    base = "https://raw.githubusercontent.com/o/r/main/"
    check("relative",
          modinfo.resolve_image_url("assets/shot.png", base),
          base + "assets/shot.png")
    check("dot-slash relative",
          modinfo.resolve_image_url("./assets/shot.png", base),
          base + "assets/shot.png")
    # A blob URL serves an HTML page. Rendering that as an image gives a
    # broken image; rewriting it to raw gives the screenshot.
    check("blob becomes raw",
          modinfo.resolve_image_url(
              "https://github.com/o/r/blob/main/a/b.png", base),
          "https://raw.githubusercontent.com/o/r/main/a/b.png")
    check("?raw=true is stripped",
          modinfo.resolve_image_url(
              "https://github.com/o/r/blob/main/b.png?raw=true", base),
          "https://raw.githubusercontent.com/o/r/main/b.png")
    check("absolute is kept",
          modinfo.resolve_image_url("https://i.imgur.com/x.png", base),
          "https://i.imgur.com/x.png")
    check("protocol-relative gets https",
          modinfo.resolve_image_url("//i.imgur.com/x.png", base),
          "https://i.imgur.com/x.png")
    check("relative with no base is dropped",
          modinfo.resolve_image_url("assets/shot.png", ""), "")
    check("data uri is dropped",
          modinfo.resolve_image_url("data:image/png;base64,AAAA", base), "")

    ok("shields is a badge",
       modinfo.is_badge("https://img.shields.io/badge/build-passing-green"))
    ok("a screenshot is not a badge",
       not modinfo.is_badge("https://raw.githubusercontent.com/o/r/main/s.png"))


# -- Markdown --------------------------------------------------------------

def test_headings_and_text() -> None:
    blocks = modinfo.parse("# Title\n\nSome words here.\n\n## Section\n")
    check("block kinds", kinds(blocks), ["head", "para", "head"])
    check("h1 level", blocks[0].level, 1)
    check("h1 text", flat(blocks[0]), "Title")
    check("h2 level", blocks[2].level, 2)
    check("paragraph", flat(blocks[1]), "Some words here.")


def test_paragraphs_join_wrapped_lines() -> None:
    blocks = modinfo.parse("one line\nand its continuation\n\nsecond para")
    check("two paragraphs", len(blocks), 2)
    check("joined", flat(blocks[0]), "one line and its continuation")


def test_lists_and_code() -> None:
    blocks = modinfo.parse(
        "- first\n- second\n  - nested\n\n1. one\n\n```lua\nprint('hi')\n```\n")
    check("kinds", kinds(blocks), ["item", "item", "item", "item", "code"])
    check("bullet marker", blocks[0].alt, "•")
    check("nested indent", blocks[2].level, 1)
    check("numbered marker", blocks[3].alt, "1.")
    check("code survives verbatim", blocks[4].text, "print('hi')")


def test_fenced_code_is_not_reparsed() -> None:
    # A fence full of markdown-looking text is a common README shape (an
    # example manifest, a folder tree) and every character of it must survive.
    blocks = modinfo.parse("```\n# not a heading\n- not a list\n```\n")
    check("one block", kinds(blocks), ["code"])
    check("contents", blocks[0].text, "# not a heading\n- not a list")


def test_images_become_their_own_blocks() -> None:
    base = "https://raw.githubusercontent.com/o/r/main/"
    blocks = modinfo.parse(
        "Look at this: ![a screenshot](shots/one.png) and text after.", base)
    check("image then paragraph", kinds(blocks), ["image", "para"])
    check("url", blocks[0].url, base + "shots/one.png")
    check("alt text", blocks[0].alt, "a screenshot")
    check("surrounding text kept", flat(blocks[1]),
          "Look at this: and text after.")


def test_html_images_are_found() -> None:
    # `<p align="center"><img src="..." width="400"></p>` is the single most
    # common way a Balatro README puts a banner at the top.
    base = "https://raw.githubusercontent.com/o/r/main/"
    blocks = modinfo.parse(
        '<p align="center">\n<img src="assets/banner.png" width="400">\n</p>\n',
        base)
    check("just the image", kinds(blocks), ["image"])
    check("src resolved", blocks[0].url, base + "assets/banner.png")


def test_badges_are_skipped() -> None:
    text = ("![build](https://img.shields.io/badge/build-passing-green)\n\n"
            "![real](https://raw.githubusercontent.com/o/r/main/s.png)\n")
    check("badge dropped", kinds(modinfo.parse(text)), ["image"])
    check("badge kept when asked",
          kinds(modinfo.parse(text, skip_badges=False)), ["image", "image"])


def test_links_and_emphasis() -> None:
    blocks = modinfo.parse(
        "See [the wiki](https://example.com/wiki) for **bold** and `code`.")
    spans = blocks[0].spans
    link = [s for s in spans if s.style == "link"]
    check("one link", len(link), 1)
    check("link text", link[0].text, "the wiki")
    check("link href", link[0].href, "https://example.com/wiki")
    check("bold", [s.text for s in spans if s.style == "bold"], ["bold"])
    check("code span", [s.text for s in spans if s.style == "code"], ["code"])
    check("nothing lost", flat(blocks[0]),
          "See the wiki for bold and code.")


def test_relative_links_resolve() -> None:
    base = "https://raw.githubusercontent.com/o/r/main/"
    blocks = modinfo.parse("[the guide](docs/guide.md)", base)
    check("resolved", blocks[0].spans[0].href, base + "docs/guide.md")
    # An anchor into the same page has nowhere to go in a Text widget.
    check("anchor dropped",
          modinfo.parse("[jump](#section)", base)[0].spans[0].href, "")


def test_bare_urls_are_clickable() -> None:
    blocks = modinfo.parse("Join at https://discord.gg/abc for help.")
    links = [s for s in blocks[0].spans if s.style == "link"]
    check("one bare link", [s.href for s in links], ["https://discord.gg/abc"])


def test_rules_quotes_and_tables() -> None:
    blocks = modinfo.parse("> a quote\n\n---\n\ntext\n")
    check("quote then rule then text", kinds(blocks), ["quote", "rule", "para"])
    check("quote marker stripped", flat(blocks[0]), "a quote")

    table = modinfo.parse("| Mod | Version |\n|---|---|\n| Cryptid | 0.5.16 |\n")
    check("separator row dropped", kinds(table), ["table", "table"])
    check("cells joined", flat(table[1]), "Cryptid · 0.5.16")


def test_html_comments_and_entities() -> None:
    blocks = modinfo.parse("<!-- hidden -->\nA &amp; B\n")
    check("comment gone, entity decoded", flat(blocks[0]), "A & B")


def test_leading_and_trailing_rules_are_dropped() -> None:
    check("tidied", kinds(modinfo.parse("---\n\ntext\n\n---\n")), ["para"])


def test_empty_input() -> None:
    check("empty markdown", modinfo.parse(""), [])
    check("whitespace only", modinfo.parse("\n\n   \n"), [])


# -- What a mod adds -------------------------------------------------------

_LOCALIZATION = '''return {
\tdescriptions = {
\t\tBack = {
\t\t\tb_cry_antimatter = {
\t\t\t\tname = "Antimatter Deck",
\t\t\t\ttext = {
\t\t\t\t\t"Applies the {C:legendary,E:1}upsides{}",
\t\t\t\t\t"of {C:attention}almost every{} deck",
\t\t\t\t},
\t\t\t},
\t\t\tb_cry_blank = {
\t\t\t\tname = "Blank Deck",
\t\t\t\ttext = { "{C:inactive}No effect{}" },
\t\t\t},
\t\t},
\t\tJoker = {
\t\t\tj_cry_one = { name = "One", text = { "does {C:red}something{}" } },
\t\t\tj_cry_two = { name = "Two", text = { "does another" } },
\t\t\tj_cry_three = { name = "Three", text = { "a third" } },
\t\t},
\t\tOther = {
\t\t\tignored_section = { name = "not a game object" },
\t\t},
\t},
\tmisc = {
\t\tdictionary = {
\t\t\tk_whatever = "Whatever",
\t\t},
\t},
}
'''


def test_localization_counts_real_objects() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mod = Path(tmp) / "mod"
        (mod / "localization").mkdir(parents=True)
        (mod / "localization" / "en-us.lua").write_text(_LOCALIZATION,
                                                        encoding="utf-8")
        counts = modinfo.scan_localization(mod)
    check("jokers", counts.get("Joker"), 3)
    check("decks", counts.get("Back"), 2)
    # The colour tags inside the description strings are not table braces. If
    # they were counted, the parser would still be inside `Back` at `Joker`.
    ok("no phantom sections from {C:...} tags",
       set(counts) <= {"Joker", "Back"})
    ok("misc.dictionary is not counted as content", "Other" not in counts)


def test_localization_missing_is_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        check("no file", modinfo.scan_localization(Path(tmp)), {})
    check("no folder", modinfo.scan_localization(Path("nowhere-at-all")), {})


def test_merge_takes_the_higher_count() -> None:
    # Not a sum. A mod that both calls SMODS.Joker and localises each joker
    # would otherwise claim twice as many as it has.
    merged = modinfo.merge_adds({"Joker": 3, "Atlas": 2}, {"Joker": 3, "Back": 1})
    check("joker not doubled", merged["Joker"], 3)
    check("higher wins",
          modinfo.merge_adds({"Joker": 1}, {"Joker": 9})["Joker"], 9)
    check("smods-only survives", merged["Atlas"], 2)
    check("localisation-only survives", merged["Back"], 1)


def test_adds_lines_read_as_english() -> None:
    check("plural and order",
          modinfo.adds_lines({"Back": 2, "Joker": 12}),
          ["12 jokers", "2 decks"])
    check("singular", modinfo.adds_lines({"Joker": 1}), ["1 joker"])
    check("zeroes are not mentioned", modinfo.adds_lines({"Joker": 0}), [])
    check("nothing", modinfo.adds_lines({}), [])
    # An object type we have no label for still gets reported rather than
    # silently dropped -- SMODS gains constructors from time to time.
    check("unknown type", modinfo.adds_lines({"Gizmo": 2}), ["2 Gizmos"])


def test_human_size() -> None:
    check("bytes", modinfo.human_size(512), "512 B")
    check("kilobytes", modinfo.human_size(2048), "2.0 KB")
    check("megabytes", modinfo.human_size(5 * 1024 * 1024), "5.0 MB")


def test_cached_image_never_downloads() -> None:
    """The lookup the cached-page render uses -- disk only, no requests."""
    original = modinfo.IMAGE_CACHE
    with tempfile.TemporaryDirectory() as tmp:
        modinfo.IMAGE_CACHE = Path(tmp)
        try:
            url = "https://raw.githubusercontent.com/o/r/main/shot.png"
            check("nothing cached yet", modinfo.cached_image_file(url), None)

            path = modinfo.image_path(url)
            path.write_bytes(b"not really a png, but non-empty")
            check("found once it is on disk",
                  modinfo.cached_image_file(url), path)

            # An empty file is how a refused download is remembered -- it must
            # read back as absent, not as a zero-byte image.
            path.write_bytes(b"")
            check("a remembered failure stays absent",
                  modinfo.cached_image_file(url), None)
            check("empty url", modinfo.cached_image_file(""), None)
        finally:
            modinfo.IMAGE_CACHE = original


def test_overrides_round_trip() -> None:
    original = modinfo.OVERRIDES
    with tempfile.TemporaryDirectory() as tmp:
        modinfo.OVERRIDES = Path(tmp) / "repo-overrides.json"
        try:
            check("empty to start", modinfo.overrides(), {})
            modinfo.set_override("some@Mod", "github.com/o/r/tree/main")
            check("normalised on the way in",
                  modinfo.overrides()["some@Mod"], "https://github.com/o/r")
            modinfo.set_override("some@Mod", "  ")
            check("blank clears it", modinfo.overrides(), {})
        finally:
            modinfo.OVERRIDES = original


def main() -> int:
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    if FAILURES:
        print(f"test_modinfo: {len(FAILURES)} failure(s)")
        for failure in FAILURES:
            print("  " + failure)
        return 1
    print("test_modinfo: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
