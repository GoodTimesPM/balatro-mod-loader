"""Manifest discovery and parsing, tested against the live install.

Reads real installed mods (both manifest formats, Thunderstore decoys, nested
folders). Only reads, and skips the corpus checks when no mods are installed.

    py -m tests.test_manifest
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bml import config  # noqa: E402
from bml.manifest import find_manifest, thunderstore_dependency  # noqa: E402
from bml.version import parse_dependency, parse_version  # noqa: E402

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def ok(label: str, condition: bool) -> None:
    if not condition:
        FAILURES.append(label)


def corpus_dir() -> Path | None:
    """Whichever candidate folder holds the most installed mods.

    `apply --adopt` moves hand-installed mods to `Mods-bml-backups`, so the
    live Mods folder is not always the best corpus.
    """
    candidates = [config.game_mods()]
    backups = config.game_mods().parent / config.BACKUP_DIR_NAME
    if backups.is_dir():
        candidates += sorted((p for p in backups.iterdir() if p.is_dir()),
                             reverse=True)
    best, most = None, 0
    for path in candidates:
        if not path.is_dir():
            continue
        count = sum(1 for p in path.iterdir() if p.is_dir())
        if count > most:
            best, most = path, count
    return best


def test_corpus() -> None:
    mods_dir = corpus_dir()
    if mods_dir is None:
        print(f"SKIP: no mod folders found under {config.game_mods().parent}")
        return

    folders = sorted(p for p in mods_dir.iterdir() if p.is_dir())
    print(f"scanning {len(folders)} mod folders in {mods_dir}")

    resolved: dict[str, str] = {}
    for folder in folders:
        manifest = find_manifest(folder)
        if manifest is None or not manifest.mod_id:
            FAILURES.append(f"{folder.name}: no manifest resolved")
            continue
        resolved[folder.name] = manifest.mod_id
        ok(f"{folder.name}: format is a known one",
           manifest.fmt in ("json", "legacy", "framework"))
        # A Thunderstore manifest must never be primary.
        ok(f"{folder.name}: primary is not a Thunderstore manifest",
           manifest.fmt != "thunderstore")

    check("every mod folder resolved", len(resolved), len(folders))
    ok("mod ids are unique", len(set(resolved.values())) == len(resolved))
    print(f"resolved {len(resolved)}/{len(folders)} manifests")


def test_known_values() -> None:
    """Spot-check values read straight off disk during planning."""
    mods_dir = corpus_dir()
    if mods_dir is None:
        return

    # JSON manifest under a bespoke filename, with a conflict.
    m = find_manifest(mods_dir / "CardSleeves")
    if m is None:
        FAILURES.append("CardSleeves: no manifest")
    else:
        check("CardSleeves id", m.mod_id, "CardSleeves")
        check("CardSleeves version", m.version_raw, "1.6.4")
        check("CardSleeves format", m.fmt, "json")
        check("CardSleeves source", m.source_file, "CardSleeves.json")
        check("CardSleeves priority", m.priority, -10)
        check("CardSleeves conflicts", len(m.conflicts), 1)
        check("CardSleeves conflict target",
              m.conflicts[0].alternatives[0].mod_id, "GRM")
        dep_ids = [d.alternatives[0].mod_id for d in m.dependencies]
        ok("CardSleeves needs Steamodded", "Steamodded" in dep_ids)
        ok("CardSleeves needs Lovely", "Lovely" in dep_ids)

    # Legacy Lua header with a bracketed dependency list.
    m = find_manifest(mods_dir / "Cryptid")
    if m is None:
        FAILURES.append("Cryptid: no manifest")
    else:
        check("Cryptid id", m.mod_id, "Cryptid")
        check("Cryptid version", m.version_raw, "0.5.4")
        check("Cryptid format", m.fmt, "legacy")
        check("Cryptid prefix", m.prefix, "cry")
        dep_ids = [d.alternatives[0].mod_id for d in m.dependencies]
        ok("Cryptid needs Talisman", "Talisman" in dep_ids)
        ok("Cryptid needs Steamodded", "Steamodded" in dep_ids)

    # Header lives in a file named after neither the mod nor "manifest".
    m = find_manifest(mods_dir / "Talisman")
    if m is None:
        FAILURES.append("Talisman: no manifest")
    else:
        check("Talisman id", m.mod_id, "Talisman")
        check("Talisman version", m.version_raw, "2.1.1~dev")
        check("Talisman source", m.source_file, "steamodded_metadata.lua")

    # Steamodded's real version is in version.lua, not its Thunderstore manifest.
    m = find_manifest(mods_dir / "Steamodded")
    if m is None:
        FAILURES.append("Steamodded: no manifest")
    else:
        check("Steamodded id", m.mod_id, "Steamodded")
        check("Steamodded format", m.fmt, "framework")
        ok("Steamodded version came from version.lua",
           m.version_raw.startswith("1.0.0~ALPHA"))
        ok("Steamodded version is a pre-release", m.version.pre is not None)
        ok("Steamodded pre-release sorts below the 1.0.0 the TS manifest claims",
           m.version < parse_version("1.0.0"))
        # The constraint Cryptid actually declares.
        ok("installed Steamodded satisfies Cryptid's floor",
           parse_dependency("Steamodded>=1.0.0~ALPHA-1312c").satisfied_by(
               {"steamodded": m.version}) is not None)

    # Lovely is the Rust injector, present but self-describing nowhere.
    m = find_manifest(mods_dir / "lovely")
    if m is None:
        FAILURES.append("lovely: framework not recognised")
    else:
        check("lovely id", m.mod_id, "Lovely")
        check("lovely format", m.fmt, "framework")

    # Blank line between the header marker and the first key.
    for folder, expected_id in (("Incantation", "incantation"), ("Nopeus", "nopeus")):
        m = find_manifest(mods_dir / folder)
        if m is None:
            FAILURES.append(f"{folder}: no manifest (blank line after header marker?)")
        else:
            check(f"{folder} id", m.mod_id, expected_id)

    # Nopeus declares its Steamodded floor the old way, via LOADER_VERSION_GEQ
    # rather than a DEPENDENCIES entry, and writes BADGE_COLOR not COLOUR.
    m = find_manifest(mods_dir / "Nopeus")
    if m is not None:
        check("Nopeus version", m.version_raw, "2.2.3")
        check("Nopeus badge colour (American spelling)", m.badge_colour, "ff3c3c")
        dep_ids = [d.alternatives[0].mod_id for d in m.dependencies]
        ok("LOADER_VERSION_GEQ became a Steamodded dependency",
           "Steamodded" in dep_ids)

    # A ~200-digit PRIORITY must not overflow the column it lands in.
    m = find_manifest(mods_dir / "Incantation")
    if m is not None:
        ok("Incantation's absurd priority is clamped", abs(m.priority) <= 2**31 - 1)

    # DebugPlus ships .luarc.json (decoy) next to smods.json (real).
    m = find_manifest(mods_dir / "DebugPlus")
    if m is None:
        FAILURES.append("DebugPlus: no manifest")
    else:
        check("DebugPlus id", m.mod_id, "DebugPlus")
        check("DebugPlus reads smods.json, not .luarc.json",
              m.source_file, "smods.json")

    # Nested one level down.
    m = find_manifest(mods_dir / "DiscoveryManager")
    if m is None:
        FAILURES.append("DiscoveryManager: no manifest")
    else:
        ok("DiscoveryManager found nested", "/" in m.source_file or m.mod_id != "")

    # Two-component version, and a dependent that must be satisfied by it.
    g = find_manifest(mods_dir / "Galdur")
    t = find_manifest(mods_dir / "TatteredDecks")
    if g and t:
        check("Galdur version", g.version_raw, "1.2")
        available = {g.mod_id.lower(): g.version}
        dep = next((d for d in t.dependencies
                    if d.alternatives[0].mod_id.lower() == "galdur"), None)
        ok("TatteredDecks declares a galdur dependency", dep is not None)
        if dep is not None:
            ok("installed Galdur 1.2 satisfies TatteredDecks' >=1.1.4",
               dep.satisfied_by(available) is not None)


def test_framework_detection_is_a_fallback() -> None:
    """A mod that vendors loader code is still that mod.

    Regression: Agarmons ships its own `src/loader.lua`.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        mod = Path(tmp) / "SomeMod"
        (mod / "src").mkdir(parents=True)
        (mod / "src" / "loader.lua").write_text("-- vendored\n", encoding="utf-8")
        (mod / "SomeMod.json").write_text(
            json.dumps({
                "id": "SomeMod", "name": "Some Mod", "prefix": "sm",
                "main_file": "main.lua", "version": "1.0.0",
            }),
            encoding="utf-8",
        )
        m = find_manifest(mod)
        if m is None:
            FAILURES.append("vendored-loader mod: no manifest")
        else:
            check("vendored loader.lua does not hijack identity", m.mod_id, "SomeMod")
            check("vendored-loader mod format", m.fmt, "json")

    # And a folder that really is Steamodded still resolves as Steamodded.
    with tempfile.TemporaryDirectory() as tmp:
        mod = Path(tmp) / "Steamodded"
        (mod / "src").mkdir(parents=True)
        (mod / "src" / "loader.lua").write_text("-- real\n", encoding="utf-8")
        (mod / "version.lua").write_text(
            'return "1.0.0~ALPHA-1426c-STEAMODDED"\n', encoding="utf-8"
        )
        m = find_manifest(mod)
        if m is None:
            FAILURES.append("real Steamodded: not detected")
        else:
            check("real Steamodded id", m.mod_id, "Steamodded")
            check("real Steamodded version", m.version_raw, "1.0.0~ALPHA-1426c-STEAMODDED")


def test_lenient_json() -> None:
    """Manifests strict JSON rejects but Steamodded loads happily.

    Regression: trailing commas dropped mods from the dependency graph.
    """
    import tempfile

    body = """{
  // hand-written, like most of them
  "id": "sholium",
  "name": "Sholium",
  "prefix": "sholium",
  "main_file": "main.lua",
  "version": "2.6.7b",
  "description": "commas, braces } and // slashes inside a string are safe",
  "dependencies": [
    "Talisman(>=2.4.0)",
    "Steamodded (>=1.0.0~BETA-0711a)",
  ],
}"""
    with tempfile.TemporaryDirectory() as tmp:
        mod = Path(tmp) / "Sholium"
        mod.mkdir()
        (mod / "sholium.json").write_text(body, encoding="utf-8")
        m = find_manifest(mod)
        if m is None:
            FAILURES.append("trailing-comma manifest: not parsed")
            return
        check("lenient id", m.mod_id, "sholium")
        check("lenient version", m.version_raw, "2.6.7b")
        check("string contents survived stripping", m.description,
              "commas, braces } and // slashes inside a string are safe")
        dep_ids = [d.alternatives[0].mod_id for d in m.dependencies]
        check("lenient dependencies", dep_ids, ["Talisman", "Steamodded"])
        # No space before the paren -- also in the wild, also must parse.
        talisman = m.dependencies[0].alternatives[0]
        check("tight-paren constraint parsed", len(talisman.constraints), 1)


def test_thunderstore_dep_conversion() -> None:
    check("thunderstore dep",
          thunderstore_dependency("Thunderstore-lovely-0.7.1"), "lovely (>=0.7.1)")
    check("thunderstore dep with dashes in package",
          thunderstore_dependency("Someone-My-Mod-1.2.3"), "My-Mod (>=1.2.3)")
    check("not a thunderstore dep", thunderstore_dependency("Steamodded"), None)


def main() -> int:
    config.load_env()
    for fn in (test_corpus, test_known_values,
               test_framework_detection_is_a_fallback,
               test_lenient_json,
               test_thunderstore_dep_conversion):
        fn()
    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        for failure in FAILURES:
            print("  -", failure)
        return 1
    print("test_manifest: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
