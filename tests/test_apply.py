"""Planning what to change in the mods folder.

    py -m tests.test_apply

The mods folder and the vault are redirected to temp directories via
BML_GAME_MODS / BML_VAULT, so real junctions are used without touching the game.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bml import apply as apply_mod  # noqa: E402
from bml import config, links  # noqa: E402
from bml.graph import Node  # noqa: E402
from bml.profiles import Profile  # noqa: E402
from bml.resolve import Resolution  # noqa: E402
from bml.version import parse_version  # noqa: E402

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def ok(label: str, condition: bool) -> None:
    if not condition:
        FAILURES.append(label)


class Sandbox:
    """A temp vault plus a temp mods folder, wired up through the environment."""

    def __enter__(self):
        self.root = Path(tempfile.mkdtemp(prefix="bml-apply-"))
        self.vault = self.root / "vault"
        self.mods = self.root / "mods"
        self.mods.mkdir(parents=True)
        self._saved = (os.environ.get("BML_VAULT"), os.environ.get("BML_GAME_MODS"))
        os.environ["BML_VAULT"] = str(self.vault)
        os.environ["BML_GAME_MODS"] = str(self.mods)
        return self

    def __exit__(self, *exc):
        for name, value in zip(("BML_VAULT", "BML_GAME_MODS"), self._saved):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.root, ignore_errors=True)

    def add(self, mod_id, category="JOKERS+", folder_name="", title="") -> Node:
        path = self.vault / category / f"test@{mod_id}"
        (path / "mod").mkdir(parents=True)
        (path / "mod" / "main.lua").write_text("--", encoding="utf-8")
        return Node(
            key=f"repo:test/{mod_id.lower()}",
            mod_id=mod_id,
            title=title or mod_id,
            version=parse_version("1.0.0"),
            version_raw="1.0.0",
            category=category,
            vault_path=str(path),
            folder_name=folder_name,
        )


def resolution_of(*nodes) -> Resolution:
    r = Resolution()
    for node in nodes:
        key = node.mod_id.lower()
        r.include[key] = node
        r.requested.append(key)
        r.reasons[key] = "requested"
    return r


def kinds(plan) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for action in plan.actions:
        out.setdefault(action.kind, set()).add(action.folder)
    if plan.blocked:
        out["blocked"] = {a.folder for a in plan.blocked}
    return out


# -- Tests -----------------------------------------------------------------


def test_folder_name_falls_back_through_three_sources() -> None:
    """Most catalog entries carry no folder name."""
    check("index folder name wins",
          apply_mod.folder_for(Node("k", "cryptid", "Cryptid", None, "", "",
                                    folder_name="Cryptid")), "Cryptid")
    check("mod id next",
          apply_mod.folder_for(Node("k", "cartomancer", "Cartomancer", None, "", "")),
          "cartomancer")
    check("title last",
          apply_mod.folder_for(Node("k", "", "Some Mod", None, "", "")), "Some Mod")
    check("path separators stripped",
          apply_mod.folder_for(Node("k", "a/b:c", "x", None, "", "")), "a-b-c")


def test_plan_links_what_is_missing_and_keeps_what_matches() -> None:
    with Sandbox() as box:
        node = box.add("Cryptid")
        plan = apply_mod.build_plan(resolution_of(node), Profile("p"))
        check("first run links it", kinds(plan), {"link": {"Cryptid"}})

        apply_mod.execute(plan, log=lambda m: None)
        again = apply_mod.build_plan(resolution_of(node), Profile("p"))
        check("second run is a no-op", kinds(again), {"keep": {"Cryptid"}})
        check("no changes to make", again.changes, [])


def test_the_folder_a_mod_is_already_in_beats_the_derived_name() -> None:
    """Otherwise adopting installs the mod twice, under two names.

    e.g. `ACT` installed by hand in a folder called `Actions`.
    """
    from bml import db

    with Sandbox() as box:
        saved = config.DB_PATH
        config.DB_PATH = box.root / "catalog.db"
        try:
            conn = db.connect()
            with conn:
                conn.execute(
                    "INSERT INTO installed (folder, mod_id) VALUES (?, ?)",
                    ("Actions", "ACT"))
            conn.close()

            node = box.add("ACT")
            (box.mods / "Actions").mkdir()          # the hand-installed copy
            (box.mods / "Actions" / "main.lua").write_text("--", encoding="utf-8")

            plan = apply_mod.build_plan(resolution_of(node), Profile("p"), adopt=True)
            check("adopts the folder that is really there",
                  kinds(plan), {"adopt": {"Actions"}})
            ok("no second folder invented",
               not any(a.folder == "ACT" for a in plan.actions))
        finally:
            config.DB_PATH = saved


def test_a_folder_is_never_both_adopted_and_left_loading() -> None:
    """Windows filenames are case-insensitive; the plan's comparison was not.

    `cartomancer` in the catalog and `Cartomancer` on disk are one folder.
    """
    with Sandbox() as box:
        node = box.add("cartomancer")
        (box.mods / "Cartomancer").mkdir()
        plan = apply_mod.build_plan(resolution_of(node), Profile("p"), adopt=True)
        folders = [a.folder.lower() for a in plan.actions]
        check("named once", folders.count("cartomancer"), 1)
        check("and it is an adopt", kinds(plan), {"adopt": {"Cartomancer"}})


def test_plan_unlinks_what_left_the_profile() -> None:
    with Sandbox() as box:
        a, b = box.add("ModA"), box.add("ModB")
        apply_mod.execute(apply_mod.build_plan(resolution_of(a, b), Profile("p")),
                          log=lambda m: None)
        plan = apply_mod.build_plan(resolution_of(a), Profile("p"))
        check("B is unlinked", kinds(plan), {"keep": {"ModA"}, "unlink": {"ModB"}})

        apply_mod.execute(plan, log=lambda m: None)
        ok("link gone", not (box.mods / "ModB").exists())
        ok("vault copy survives", (Path(b.vault_path) / "mod" / "main.lua").is_file())


def test_hand_installed_folder_blocks_without_adopt() -> None:
    """A real directory of files is never deleted to make room for a link."""
    with Sandbox() as box:
        node = box.add("Cryptid")
        real = box.mods / "Cryptid"
        real.mkdir()
        (real / "mine.lua").write_text("precious", encoding="utf-8")

        plan = apply_mod.build_plan(resolution_of(node), Profile("p"))
        check("blocked", kinds(plan), {"blocked": {"Cryptid"}})
        apply_mod.execute(plan, log=lambda m: None)
        ok("untouched", (real / "mine.lua").read_text(encoding="utf-8") == "precious")

        adopting = apply_mod.build_plan(resolution_of(node), Profile("p"), adopt=True)
        check("adopt planned", kinds(adopting), {"adopt": {"Cryptid"}})
        apply_mod.execute(adopting, log=lambda m: None)
        ok("now a junction", links.is_junction(box.mods / "Cryptid"))
        backups = list((box.mods.parent / config.BACKUP_DIR_NAME).rglob("mine.lua"))
        check("original preserved in the backup", len(backups), 1)
        ok("backup is outside the mods folder Steamodded scans",
           config.BACKUP_DIR_NAME not in [p.name for p in box.mods.iterdir()])


def test_protected_folders_are_never_planned_against() -> None:
    """`lovely` is the injector's dump directory, not a mod."""
    with Sandbox() as box:
        (box.mods / "lovely").mkdir()
        plan = apply_mod.build_plan(Resolution(), Profile("p"))
        check("nothing proposed", plan.actions, [])
        check("nothing blocked", plan.blocked, [])


def test_empty_steamodded_slot_leaves_the_loader_alone() -> None:
    """Switching profiles must not silently disable the mod loader.

    A profile that names no Steamodded means "leave the loader as it is".
    """
    with Sandbox() as box:
        loader = box.add("Steamodded", category="MODLOADERS")
        other = box.add("ModA")
        apply_mod.execute(
            apply_mod.build_plan(resolution_of(loader, other),
                                 Profile("p", steamodded="Steamodded")),
            log=lambda m: None)

        plan = apply_mod.build_plan(resolution_of(other), Profile("p"))
        check("loader kept, nothing else changed", kinds(plan),
              {"keep": {"ModA", "Steamodded"}})

        # But a profile that *does* name a Steamodded still gets to swap it.
        newer = box.add("Steamodded2", category="MODLOADERS",
                        folder_name="Steamodded")
        swapped = apply_mod.build_plan(
            resolution_of(other, newer), Profile("p", steamodded="Steamodded2"))
        check("repointed to the named version", kinds(swapped).get("relink"),
              {"Steamodded"})


def test_slot_can_name_one_of_several_steamodded_builds() -> None:
    """Several Steamodded versions in the vault, one chosen per profile.

    The graph collapses them into one node, so the slot names the folder.
    """
    with Sandbox() as box:
        box.add("Steamodded", category="MODLOADERS")
        newer = box.vault / "MODLOADERS" / "Steamodded@Steamodded BETA-1925a"
        (newer / "mod").mkdir(parents=True)
        (newer / "mod" / "version.lua").write_text("1925a", encoding="utf-8")
        other = box.add("ModA")

        plan = apply_mod.build_plan(
            resolution_of(other), Profile("p", steamodded="Steamodded BETA-1925a"))
        target = next(a.target for a in plan.actions if a.folder == "Steamodded")
        check("linked the named build", target, newer / "mod")

        # And naming the ordinary one still works, without the specific build
        # shadowing it.
        plain = apply_mod.build_plan(
            resolution_of(other), Profile("p", steamodded="test@Steamodded"))
        plain_target = next(a.target for a in plain.actions if a.folder == "Steamodded")
        check("linked the plain build", plain_target,
              box.vault / "MODLOADERS" / "test@Steamodded" / "mod")


def test_two_mods_claiming_one_folder_is_blocked_not_silently_dropped() -> None:
    with Sandbox() as box:
        a = box.add("ModA", folder_name="Shared")
        b = box.add("ModB", folder_name="Shared")
        plan = apply_mod.build_plan(resolution_of(a, b), Profile("p"))
        check("one blocked", len(plan.blocked), 1)
        check("the other still links", kinds(plan).get("link"), {"Shared"})


def test_missing_vault_directory_blocks() -> None:
    """A catalog row whose download failed has no mod/ to point at."""
    with Sandbox() as box:
        node = box.add("Broken")
        shutil.rmtree(Path(node.vault_path) / "mod")
        plan = apply_mod.build_plan(resolution_of(node), Profile("p"))
        check("blocked, not linked", kinds(plan), {"blocked": {"Broken"}})


def test_foreign_junction_is_left_alone() -> None:
    with Sandbox() as box:
        elsewhere = box.root / "elsewhere"
        elsewhere.mkdir()
        links.create(box.mods / "Theirs", elsewhere)
        plan = apply_mod.build_plan(Resolution(), Profile("p"))
        # Not desired and not ours, so it appears in neither list.
        check("no action proposed", plan.actions, [])
        ok("still there", links.is_junction(box.mods / "Theirs"))


def main() -> int:
    if sys.platform != "win32":
        print("test_apply: skipped (junctions are Windows-only)")
        return 0
    for test in (
        test_folder_name_falls_back_through_three_sources,
        test_plan_links_what_is_missing_and_keeps_what_matches,
        test_the_folder_a_mod_is_already_in_beats_the_derived_name,
        test_a_folder_is_never_both_adopted_and_left_loading,
        test_plan_unlinks_what_left_the_profile,
        test_hand_installed_folder_blocks_without_adopt,
        test_protected_folders_are_never_planned_against,
        test_empty_steamodded_slot_leaves_the_loader_alone,
        test_slot_can_name_one_of_several_steamodded_builds,
        test_two_mods_claiming_one_folder_is_blocked_not_silently_dropped,
        test_missing_vault_directory_blocks,
        test_foreign_junction_is_left_alone,
    ):
        test()
    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("test_apply: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
