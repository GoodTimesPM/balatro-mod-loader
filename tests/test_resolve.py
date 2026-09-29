"""Dependency resolution over a synthetic vault.

    py -m tests.test_resolve

Synthetic, so the tests don't depend on what is in any real library.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bml import graph, resolve  # noqa: E402
from bml.graph import Findings, Node  # noqa: E402
from bml.version import parse_dependencies, parse_version  # noqa: E402

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def ok(label: str, condition: bool) -> None:
    if not condition:
        FAILURES.append(label)


def node(mod_id, version="1.0.0", deps=(), conflicts=(), provides=(), title=""):
    return Node(
        key=f"repo:test/{mod_id.lower()}",
        mod_id=mod_id,
        title=title or mod_id,
        version=parse_version(version),
        version_raw=version,
        category="JOKERS+",
        vault_path=f"C:/vault/JOKERS+/test@{mod_id}",
        dependencies=parse_dependencies(list(deps)),
        conflicts=parse_dependencies(list(conflicts)),
        provides=parse_dependencies(list(provides)),
    )


def findings(*nodes) -> Findings:
    f = Findings()
    for n in nodes:
        f.nodes[graph.norm_id(n.mod_id)] = n
        f.available[graph.norm_id(n.mod_id)] = n.version
    for n in nodes:
        for provided in n.provides:
            for alt in provided.alternatives:
                key = graph.norm_id(alt.mod_id)
                if key not in f.available:
                    f.available[key] = n.version
                    f.provider_of[key] = n.mod_id
    return f


def ids(resolution) -> set[str]:
    return {n.mod_id for n in resolution.include.values()}


# -- Tests -----------------------------------------------------------------


def test_dependencies_are_pulled_in_transitively() -> None:
    f = findings(
        node("Cryptid", "0.5.4", deps=["Talisman (>=2.0.9)", "Steamodded (>=1.0.0~BETA-1814a)"]),
        node("Talisman", "2.1.1", deps=["Steamodded (>=1.0.0~BETA-0827c)"]),
        node("Steamodded", "1.0.0~BETA-1925a"),
        node("Unrelated"),
    )
    r = resolve.resolve(f, ["Cryptid"])
    check("transitive closure", ids(r), {"Cryptid", "Talisman", "Steamodded"})
    ok("clean resolution", r.ok)
    check("only Cryptid was requested", r.requested, ["cryptid"])
    check("two were added", {n.mod_id for n in r.added}, {"Talisman", "Steamodded"})
    # Each added mod records why it was added.
    ok("reason names the requester",
       r.reasons["talisman"] == "needed by Cryptid")


def test_wrong_version_is_not_reported_as_missing() -> None:
    """A wrong version is not a missing mod: one needs an update, one a download."""
    f = findings(
        node("Cryptid", "0.5.4", deps=["Talisman (>=2.0.9)"]),
        node("Talisman", "2.0.0"),
    )
    r = resolve.resolve(f, ["Cryptid"])
    check("nothing missing", r.missing, [])
    check("one version problem", len(r.unsatisfiable), 1)
    ok("names the version we have", "2.0.0" in r.unsatisfiable[0][2])
    ok("not clean", not r.ok)


def test_absent_dependency_is_missing() -> None:
    f = findings(node("SomeMod", deps=["Spectrallib (>=1.0)"]))
    r = resolve.resolve(f, ["SomeMod"])
    check("one missing", len(r.missing), 1)
    check("nothing unsatisfiable", r.unsatisfiable, [])


def test_conflicts_only_count_inside_the_set() -> None:
    """A conflict against a mod that isn't enabled is not a problem."""
    f = findings(
        node("CardSleeves", "1.6.4", conflicts=["GRM (<=0.9.5)"]),
        node("GRM", "0.9.5"),
        node("Innocent"),
    )
    alone = resolve.resolve(f, ["CardSleeves", "Innocent"])
    check("no conflict without the other mod", alone.conflicts, [])
    ok("clean", alone.ok)

    together = resolve.resolve(f, ["CardSleeves", "GRM"])
    check("conflict once both are in", len(together.conflicts), 1)
    ok("not clean", not together.ok)


def test_conflict_reported_once_when_both_sides_declare_it() -> None:
    f = findings(
        node("A", conflicts=["B"]),
        node("B", conflicts=["A"]),
    )
    r = resolve.resolve(f, ["A", "B"])
    check("deduplicated", len(r.conflicts), 1)


def test_environment_requirements_do_not_block() -> None:
    """Lovely and the base game can never be in a vault."""
    f = findings(node("SomeMod", deps=["Lovely (>=0.7.1)", "Balatro (>=1.0.1)"]))
    r = resolve.resolve(f, ["SomeMod"])
    check("both recorded as environment", len(r.environment), 2)
    check("neither is missing", r.missing, [])
    ok("still clean", r.ok)


def test_provides_satisfies_a_dependency() -> None:
    f = findings(
        node("Needs", deps=["OldLib (>=1.0)"]),
        node("NewLib", "2.0.0", provides=["OldLib (1.5)"]),
    )
    r = resolve.resolve(f, ["Needs"])
    check("substitute pulled in", ids(r), {"Needs", "NewLib"})
    ok("clean", r.ok)


def test_alternation_picks_an_available_option() -> None:
    f = findings(
        node("Picky", deps=["Absent | Present (>=1.0)"]),
        node("Present", "1.2.0"),
    )
    r = resolve.resolve(f, ["Picky"])
    check("took the one that exists", ids(r), {"Picky", "Present"})
    ok("clean", r.ok)


def test_diamond_dependency_resolves_once() -> None:
    f = findings(
        node("Top", deps=["Left", "Right"]),
        node("Left", deps=["Base (>=1.0)"]),
        node("Right", deps=["Base (>=1.1)"]),
        node("Base", "1.2.0"),
    )
    r = resolve.resolve(f, ["Top"])
    check("one of each", ids(r), {"Top", "Left", "Right", "Base"})
    ok("clean", r.ok)


def test_cycle_terminates() -> None:
    """The vault has no cycles today, but the graph is third-party input."""
    f = findings(node("A", deps=["B"]), node("B", deps=["A"]))
    r = resolve.resolve(f, ["A"])
    check("both included, no hang", ids(r), {"A", "B"})


def test_unknown_name_is_reported_not_ignored() -> None:
    """Silently dropping a typo would enable fewer mods than asked for."""
    f = findings(node("Real"))
    r = resolve.resolve(f, ["Real", "Nonexistent"])
    check("one unknown", r.unknown, ["Nonexistent"])
    ok("blocks apply", not r.ok)


def test_lookup_by_title_key_and_substring() -> None:
    f = findings(node("cartomancer", title="Cartomancer"))
    ok("by id", resolve.find(f, "cartomancer") is not None)
    ok("by title", resolve.find(f, "Cartomancer") is not None)
    ok("by key", resolve.find(f, "repo:test/cartomancer") is not None)
    ok("by substring", resolve.find(f, "cartoman") is not None)
    ok("nonsense finds nothing", resolve.find(f, "zzzz") is None)

    # Ambiguity must not resolve.
    two = findings(node("JokerA", title="Joker Pack A"), node("JokerB", title="Joker Pack B"))
    ok("ambiguous substring refuses", resolve.find(two, "Joker Pack") is None)


def test_framework_upper_bound_is_honoured() -> None:
    """`bundlesoffun` is the ecosystem's only upper bound on Steamodded.

    The resolver must notice when the pinned version is too new.
    """
    too_new = findings(
        node("bundlesoffun", deps=["Steamodded (<<1.0.0~BETA-1827b)"]),
        node("Steamodded", "1.0.0~BETA-1925a"),
    )
    r = resolve.resolve(too_new, ["bundlesoffun"])
    check("too new is flagged", len(r.unsatisfiable), 1)

    old_enough = findings(
        node("bundlesoffun", deps=["Steamodded (<<1.0.0~BETA-1827b)"]),
        node("Steamodded", "1.0.0~BETA-1814a"),
    )
    ok("older Steamodded satisfies it", resolve.resolve(old_enough, ["bundlesoffun"]).ok)


def main() -> int:
    for test in (
        test_dependencies_are_pulled_in_transitively,
        test_wrong_version_is_not_reported_as_missing,
        test_absent_dependency_is_missing,
        test_conflicts_only_count_inside_the_set,
        test_conflict_reported_once_when_both_sides_declare_it,
        test_environment_requirements_do_not_block,
        test_provides_satisfies_a_dependency,
        test_alternation_picks_an_available_option,
        test_diamond_dependency_resolves_once,
        test_cycle_terminates,
        test_unknown_name_is_reported_not_ignored,
        test_lookup_by_title_key_and_substring,
        test_framework_upper_bound_is_honoured,
    ):
        test()
    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("test_resolve: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
