"""Version parsing and constraint evaluation.

Every literal is a real string from a mod manifest or the Steamodded spec.

    py -m tests.test_version
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bml.version import (  # noqa: E402
    parse_constraint,
    parse_dependencies,
    parse_dependency,
    parse_version,
)

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def ok(label: str, condition: bool) -> None:
    if not condition:
        FAILURES.append(label)


def test_parsing() -> None:
    v = parse_version("1.6.4")
    check("1.6.4 parts", v.parts, (1, 6, 4))
    check("1.6.4 pre", v.pre, None)

    v = parse_version("2.1.1~dev")
    check("2.1.1~dev parts", v.parts, (2, 1, 1))
    check("2.1.1~dev pre", v.pre, "dev")

    v = parse_version("1.0.0~ALPHA-1206c")
    check("alpha parts", v.parts, (1, 0, 0))
    check("alpha pre", v.pre, "ALPHA-1206c")

    # Galdur ships a two-component version.
    check("1.2 parts", parse_version("1.2").parts, (1, 2, 0))

    # TatteredDecks.
    check("0.0.5~BETA pre", parse_version("0.0.5~BETA").pre, "BETA")

    # Vocalatro's index entry puts a git SHA in the version field. That is
    # not orderable and must not be pretended into version 0.
    check("git sha is not a version", parse_version("0cf7086"), None)
    check("empty", parse_version(""), None)
    check("none", parse_version(None), None)


def test_ordering() -> None:
    ok("prerelease sorts below its release",
       parse_version("1.0.0~ALPHA-1206c") < parse_version("1.0.0"))
    ok("dev sorts below release",
       parse_version("2.1.1~dev") < parse_version("2.1.1"))
    ok("natural ordering inside prerelease",
       parse_version("1.0.0~ALPHA-1206c") < parse_version("1.0.0~ALPHA-1312c"))
    # The case a plain string compare gets wrong: "999" > "1206" lexically.
    ok("prerelease compares numerically, not lexically",
       parse_version("1.0.0~ALPHA-999c") < parse_version("1.0.0~ALPHA-1206c"))
    ok("patch ordering", parse_version("1.6.4") > parse_version("1.6.3"))
    ok("minor beats patch", parse_version("1.7.0") > parse_version("1.6.99"))
    ok("1 == 1.0.0", parse_version("1") == parse_version("1.0.0"))
    ok("v prefix tolerated", parse_version("v1.2.3") == parse_version("1.2.3"))


def test_constraints() -> None:
    def matches(constraint: str, version: str) -> bool:
        return parse_constraint(constraint).matches(parse_version(version))

    ok(">= inclusive", matches(">=2.0.9", "2.0.9"))
    ok(">= above", matches(">=2.0.9", "2.1.1"))
    ok(">= below rejected", not matches(">=2.0.9", "2.0.8"))

    ok("<= inclusive", matches("<=0.9.5", "0.9.5"))
    ok("<= above rejected", not matches("<=0.9.5", "0.9.6"))

    ok(">> strict rejects equal", not matches(">>1.0.0", "1.0.0"))
    ok(">> strict accepts above", matches(">>1.0.0", "1.0.1"))
    ok("<< strict rejects equal", not matches("<<1.0.0", "1.0.0"))
    ok("<< strict accepts below", matches("<<1.0.0", "0.9.9"))

    ok("== exact", matches("==1.6.4", "1.6.4"))
    ok("== rejects other", not matches("==1.6.4", "1.6.5"))

    ok("wildcard matches family", matches("==1.0.*", "1.0.7"))
    ok("wildcard rejects sibling", not matches("==1.0.*", "1.1.0"))
    ok("wildcard on minor", matches("==1.*", "1.9.9"))

    # A real constraint against a real installed version.
    ok("CardSleeves' Steamodded floor",
       matches(">=1.0.0~ALPHA-1206c", "1.0.0~ALPHA-1312c"))
    ok("a prerelease does not satisfy a release floor",
       not matches(">=1.0.0", "1.0.0~ALPHA-1312c"))

    ok("unknown version satisfies nothing",
       not parse_constraint(">=1.0.0").matches(None))


def test_dependency_syntaxes() -> None:
    # JSON form, from CardSleeves.json
    dep = parse_dependency("Steamodded (>=1.0.0~ALPHA-1206c)")
    check("json dep id", dep.alternatives[0].mod_id, "Steamodded")
    check("json dep op", dep.alternatives[0].constraints[0].op, ">=")
    ok("json dep satisfied",
       dep.satisfied_by({"steamodded": parse_version("1.0.0~ALPHA-1312c")}) is not None)

    # Legacy form, from Cryptid.lua
    dep = parse_dependency("Talisman>=2.0.9")
    check("legacy dep id", dep.alternatives[0].mod_id, "Talisman")
    ok("legacy dep satisfied",
       dep.satisfied_by({"talisman": parse_version("2.1.1")}) is not None)
    ok("legacy dep unsatisfied by older",
       dep.satisfied_by({"talisman": parse_version("2.0.8")}) is None)

    # Conflict form, from CardSleeves.json
    dep = parse_dependency("GRM (<=0.9.5)")
    check("conflict id", dep.alternatives[0].mod_id, "GRM")

    # No constraint at all.
    dep = parse_dependency("Cryptid")
    check("bare dep id", dep.alternatives[0].mod_id, "Cryptid")
    check("bare dep has no constraints", dep.alternatives[0].constraints, [])
    ok("bare dep satisfied by unknown version",
       dep.satisfied_by({"cryptid": None}) is not None)

    # Multiple constraints.
    dep = parse_dependency("Talisman (>=2.0.9, <<3.0.0)")
    check("two constraints", len(dep.alternatives[0].constraints), 2)
    ok("in range", dep.satisfied_by({"talisman": parse_version("2.1.1")}) is not None)
    ok("out of range", dep.satisfied_by({"talisman": parse_version("3.1.0")}) is None)

    # Alternatives.
    dep = parse_dependency("ModA | ModB (>=1.0)")
    ok("is alternation", dep.is_alternation)
    ok("second alternative wins",
       dep.satisfied_by({"modb": parse_version("1.2")}).mod_id == "ModB")
    ok("neither present", dep.satisfied_by({"modc": parse_version("9.9")}) is None)

    # Ids compare case- and separator-insensitively (TatteredDecks depends on
    # "galdur", whose manifest id is also "galdur" but indexes vary).
    dep = parse_dependency("galdur (>=1.1.4)")
    ok("galdur 1.2 satisfies >=1.1.4",
       dep.satisfied_by({"galdur": parse_version("1.2")}) is not None)


def test_wildcard_with_prerelease_marker() -> None:
    """`1.*~` means "any 1.x, pre-releases included".

    Regression: the trailing `~` was parsed as part of a version component.
    """
    smods = parse_version("1.0.0~BETA-1814a-STEAMODDED")
    ok("1.*~ matches a 1.x pre-release",
       parse_constraint(">=1.*~").matches(smods))
    ok("1.* matches it too", parse_constraint(">=1.*").matches(smods))
    ok("2.*~ does not match a 1.x", not parse_constraint(">=2.*~").matches(smods))
    # A pre-release still sorts below the plain release it precedes, so a mod
    # demanding the never-released 1.0.0 final is genuinely unsatisfied.
    ok("plain >=1.0.0 is not satisfied by a 1.0.0 pre-release",
       not parse_constraint(">=1.0.0").matches(smods))


def test_malformed_declarations() -> None:
    """Three real ways authors write a dependency wrong.

    Each one meant Steamodded but read as a dependency on a nonexistent mod.
    """
    smods = parse_version("1.0.0~BETA-1814a-STEAMODDED")

    # greener-jokers: the list separator left inside the string. Verbatim.
    dep = parse_dependency("Steamodded (>=1.0.0~BETA-0827c),")
    check("trailing-comma id", dep.alternatives[0].mod_id, "Steamodded")
    check("trailing-comma op", dep.alternatives[0].constraints[0].op, ">=")
    ok("trailing-comma satisfied", dep.satisfied_by({"steamodded": smods}) is not None)
    ok("trailing-comma still checks the floor",
       dep.satisfied_by({"steamodded": parse_version("0.9.8")}) is None)

    # And the mismatched-bracket variants of the same mistake.
    for text in ("Steamodded ( (>=1.0.0~BETA-0827c))",
                 "Steamodded (>=1.0.0~BETA-0827c"):
        dep = parse_dependency(text)
        check(f"bracket id {text!r}", dep.alternatives[0].mod_id, "Steamodded")
        ok(f"bracket floor holds {text!r}",
           dep.satisfied_by({"steamodded": parse_version("0.9.8")}) is None)

    # ZokersModMenu: a Thunderstore triplet inside a Steamodded manifest.
    dep = parse_dependency("Steamodded-Steamodded-0.9.8")
    check("triplet id", dep.alternatives[0].mod_id, "Steamodded")
    ok("triplet satisfied by a newer build",
       dep.satisfied_by({"steamodded": smods}) is not None)
    # A hyphenated id with a two-part version is not a triplet.
    check("not a triplet", parse_dependency("Some-Mod-2.0").alternatives[0].mod_id,
          "Some-Mod-2.0")

    # SnoresvilleTurbulentJokers: the object form.
    deps = parse_dependencies([{"id": "Steamodded", "min_version": "1.0.0~BETA-0224a"}])
    check("object form parsed", len(deps), 1)
    check("object form id", deps[0].alternatives[0].mod_id, "Steamodded")
    ok("object form satisfied", deps[0].satisfied_by({"steamodded": smods}) is not None)
    ok("object form floor holds",
       deps[0].satisfied_by({"steamodded": parse_version("0.9.8")}) is None)
    # The rendered string is what gets stored and re-parsed by the graph.
    ok("object form round-trips",
       parse_dependency(deps[0].raw).alternatives[0].mod_id == "Steamodded")


def main() -> int:
    for fn in (test_parsing, test_ordering, test_constraints,
               test_wildcard_with_prerelease_marker, test_dependency_syntaxes,
               test_malformed_declarations):
        fn()
    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        for failure in FAILURES:
            print("  -", failure)
        return 1
    print("test_version: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
