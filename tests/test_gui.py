"""The desktop window, driven without a person in front of it.

    py -m tests.test_gui

A real Tk window is created and withdrawn, so the real widgets are tested.
Profiles and the mods folder are redirected to temp directories. The catalog
is read from the real library, so this skips on a fresh clone.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def ok(label: str, condition: bool) -> None:
    if not condition:
        FAILURES.append(label)


class Harness:
    """A withdrawn window with its profiles and mods folder pointed at temp."""

    def __enter__(self):
        import tkinter as tk

        from bml import config

        self.root_dir = Path(tempfile.mkdtemp(prefix="bml-gui-"))
        self.mods = self.root_dir / "mods"
        self.mods.mkdir()
        self.profiles_dir = self.root_dir / "profiles"
        self.profiles_dir.mkdir()

        self._saved_profiles = config.PROFILES
        self._saved_env = os.environ.get("BML_GAME_MODS")
        config.PROFILES = self.profiles_dir
        os.environ["BML_GAME_MODS"] = str(self.mods)

        from bml import gui

        self.tk_root = tk.Tk()
        self.tk_root.withdraw()
        self.app = gui.App(self.tk_root)
        return self

    def __exit__(self, *exc):
        from bml import config

        try:
            self.tk_root.destroy()
        except Exception:
            pass
        config.PROFILES = self._saved_profiles
        if self._saved_env is None:
            os.environ.pop("BML_GAME_MODS", None)
        else:
            os.environ["BML_GAME_MODS"] = self._saved_env
        shutil.rmtree(self.root_dir, ignore_errors=True)

    def rows(self) -> int:
        return len(self.app._row_of)

    def select(self, node) -> None:
        """Select the row for a node, the way clicking it would."""
        for item, candidate in self.app._row_of.items():
            if candidate.mod_id == node.mod_id:
                self.app.tree.selection_set(item)
                return
        raise AssertionError(f"{node.mod_id} is not on screen")


def a_node_with_a_resolvable_dependency(app):
    """Find a real mod that pulls something else in, whatever the vault holds.

    Chosen by property, not by name, so the test survives catalog changes.
    """
    from bml import resolve

    for node in app.nodes:
        if not node.dependencies:
            continue
        result = resolve.resolve(app.findings, [node.mod_id])
        if result.added:
            return node, result
    return None, None


# -- Tests -----------------------------------------------------------------


def test_window_loads_the_real_vault() -> None:
    with Harness() as h:
        ok("nodes loaded", len(h.app.nodes) > 100)
        ok("table populated", h.rows() > 100)
        ok("categories offered", len(h.app.category_box["values"]) > 3)


def test_search_and_category_filters_narrow_the_table() -> None:
    with Harness() as h:
        everything = h.rows()
        h.app.search_var.set("joker")
        narrowed = h.rows()
        ok("search narrows", 0 < narrowed < everything)
        for node in h.app._row_of.values():
            ok(f"{node.title} matches the search term",
               "joker" in f"{node.title}{node.mod_id}{node.key}".lower())

        h.app.search_var.set("")
        category = h.app.nodes[0].category
        h.app.category_var.set(category)
        h.app.refresh_table()
        ok("category filter narrows", h.rows() < everything)
        ok("only that category", all(n.category == category
                                     for n in h.app._row_of.values()))


def test_toggling_a_mod_pulls_in_its_dependencies() -> None:
    """The set re-resolves as you click."""
    with Harness() as h:
        h.app.new_profile_named("scratch")
        node, expected = a_node_with_a_resolvable_dependency(h.app)
        if node is None:
            return  # a vault with no resolvable dependencies proves nothing

        h.app.search_var.set(node.title.lower())
        h.select(node)
        h.app.on_toggle()

        check("chosen", h.app.members, {h.app.norm(node.mod_id)})
        ok("dependencies came along", len(h.app.resolution.added) > 0)
        check("same set the resolver gives",
              set(h.app.resolution.include), set(expected.include))

        h.select(node)
        h.app.on_toggle()
        check("nothing chosen", h.app.members, set())
        check("and the dependencies left too", len(h.app.resolution.include), 0)


def test_unresolvable_entries_survive_a_save() -> None:
    """Profile entries that match no mod must survive a save."""
    with Harness() as h:
        from bml import profiles

        seeded = profiles.Profile(name="legacy",
                                  mods=["NotAModAnyoneHas", h.app.nodes[0].mod_id])
        seeded.save()
        h.app.refresh_profile_list()
        h.app.load_profile("legacy")

        check("the unknown one is held aside", h.app.unmatched, ["NotAModAnyoneHas"])
        check("the known one is a member", len(h.app.members), 1)

        h.app.save_profile()
        reloaded = profiles.load("legacy")
        ok("still written to the file", "NotAModAnyoneHas" in reloaded.mods)
        check("and nothing was lost", len(reloaded.mods), 2)


def test_preview_writes_nothing_to_the_mods_folder() -> None:
    with Harness() as h:
        h.app.new_profile_named("scratch")
        for node in h.app.nodes[:5]:
            h.app.members.add(h.app.norm(node.mod_id))
        h.app.reresolve()
        h.app.preview()
        check("mods folder untouched", list(h.mods.iterdir()), [])


def test_a_new_profile_starts_with_nothing_enabled() -> None:
    """New means empty, not "a copy of the one I'm looking at".

    The Steamodded slot is the one thing that carries over.
    """
    from bml import profiles

    with Harness() as h:
        seeded = profiles.Profile(name="full",
                                  mods=[n.mod_id for n in h.app.nodes[:6]])
        seeded.steamodded = "Steamodded@Steamodded"
        seeded.save()
        h.app.refresh_profile_list()
        h.app.load_profile("full")
        check("the seed profile is populated", len(h.app.members), 6)

        h.app.new_profile_named("clean")
        check("nothing carried over", h.app.members, set())
        check("and nothing resolves in", len(h.app.resolution.include), 0)
        check("but the loader slot did", h.app.steamodded_var.get(),
              "Steamodded@Steamodded")

        h.app.save_profile()
        check("and that is what lands on disk", profiles.load("clean").mods, [])
        check("the profile it came from is untouched",
              len(profiles.load("full").mods), 6)


def test_a_folder_sourced_entry_reaches_the_vault_like_any_other() -> None:
    """`capture` writes localSource entries; fetch must treat them as normal.

    A mod vaulted from a local folder gets a hash, archive and manifest too.
    """
    import json

    from bml import config, vault

    with Harness() as h:
        source = h.root_dir / "HandMadeMod"
        source.mkdir()
        (source / "HandMadeMod.json").write_text(json.dumps({
            "id": "HandMadeMod", "name": "Hand Made Mod",
            "author": ["Nobody"], "version": "1.2.3",
            "main_file": "HandMadeMod.lua",
        }), encoding="utf-8")
        (source / "HandMadeMod.lua").write_text("-- nothing\n", encoding="utf-8")

        saved_vault = os.environ.get("BML_VAULT")
        os.environ["BML_VAULT"] = str(h.root_dir / "vault")
        try:
            row = vault.fetch_one({
                "key": "local:handmade", "title": "Hand Made Mod",
                "author": "Nobody", "local_source": str(source),
                "categories": [], "declared_version": "1.2.3",
            }, log=lambda _m: None)
        finally:
            if saved_vault is None:
                os.environ.pop("BML_VAULT", None)
            else:
                os.environ["BML_VAULT"] = saved_vault

        check("it vaulted", row["status"], "ok")
        check("its manifest was read", row["mod_id"], "HandMadeMod")
        check("at the version the manifest gives", row["version"], "1.2.3")
        ok("it has a real hash", len(row["sha256"]) == 64)
        placed = Path(row["vault_path"])
        ok("the mod is on disk", (placed / "mod" / "HandMadeMod.json").is_file())
        ok("with an archive to roll back to",
           (placed / "_archive" / row["archive_name"]).is_file())


def test_steamodded_beta_satisfies_a_bare_1_0_0_requirement() -> None:
    """Steamodded has never shipped a final 1.0.0, so `>=1.0.0` must not fail.

    A `~BETA` build sorts below the plain release, but mods mean the 1.0.0 line.
    """
    from bml import resolve
    from bml.version import Version, parse_dependency

    available = {"steamodded": Version(
        raw="1.0.0~BETA-1814a-STEAMODDED", parts=(1, 0, 0),
        pre="BETA-1814a-STEAMODDED")}

    ok("a bare >=1.0.0 is treated as met",
       resolve._is_steamodded_1_0_0_wording(
           parse_dependency("Steamodded (>=1.0.0)"), available))
    ok("but a real newer build is still not met",
       not resolve._is_steamodded_1_0_0_wording(
           parse_dependency("Steamodded (>=1.0.0~BETA-1925a)"), available))
    ok("and a different release line is still not met",
       not resolve._is_steamodded_1_0_0_wording(
           parse_dependency("Steamodded (>=1.1.0)"), available))
    ok("the rule is Steamodded-only",
       not resolve._is_steamodded_1_0_0_wording(
           parse_dependency("Talisman (>=1.0.0)"), {"talisman": available["steamodded"]}))


def test_the_window_is_dark_and_its_fields_are_outlined() -> None:
    """The palette has to reach the widgets, not just exist in theme.py.

    Only `clam` can be recoloured; vista drops colours silently.
    """
    from tkinter import ttk

    from bml import theme

    with Harness() as h:
        style = ttk.Style(h.tk_root)
        check("the one themeable engine", style.theme_use(), "clam")
        check("window background", str(h.tk_root.cget("background")), theme.BASE)
        for widget in ("Treeview", "TEntry", "TCombobox"):
            check(f"{widget} is outlined in green",
                  style.lookup(widget, "bordercolor"), theme.GREEN_DIM)
        check("the table is dark", style.lookup("Treeview", "fieldbackground"),
              theme.BASE)
        check("and so are the panes", str(h.app.details.cget("background")),
              theme.BASE)
        check("which are outlined too",
              str(h.app.details.cget("highlightbackground")), theme.GREEN_DIM)


def test_a_second_look_at_a_page_still_has_its_screenshots() -> None:
    """Click a mod, click away, click back -- the images have to come back too.

    No network: the page and its image are put in the cache by hand.
    """
    from bml import modinfo

    with Harness() as h:
        saved_pages, saved_images = modinfo.PAGE_CACHE, modinfo.IMAGE_CACHE
        saved_facts = modinfo.facts_for
        cache = h.root_dir / "pages"
        try:
            modinfo.PAGE_CACHE = cache
            modinfo.IMAGE_CACHE = cache / "img"
            modinfo.IMAGE_CACHE.mkdir(parents=True)

            repo = "https://github.com/ExampleAuthor/ExampleMod"
            base = "https://raw.githubusercontent.com/ExampleAuthor/ExampleMod/main/"
            modinfo._save_cached(
                modinfo._cache_path("ExampleAuthor", "ExampleMod"),
                modinfo.Page(repo=repo, html_url=repo, base_url=base,
                             markdown="# Example\n\n![a screenshot](shot.png)\n",
                             fetched_at=time.time()))
            shot = modinfo.image_path(base + "shot.png")
            try:
                from PIL import Image

                Image.new("RGB", (64, 32), (20, 120, 60)).save(shot)
            except ImportError:
                return  # Tk alone can't be relied on to decode what we'd write

            node = h.app.nodes[0]
            modinfo.facts_for = lambda key, path: modinfo.Facts(
                key=key, mod_id=node.mod_id, title=node.title, repo=repo,
                repo_source="override")

            h.app.show_page(node)
            first = len(h.app._page_images)
            check("the first look draws the screenshot", first, 1)

            h.app.show_page(node)   # click away and back
            check("and so does the second", len(h.app._page_images), first)
            ok("no placeholder text",
               "[image]" not in h.app.page.get("1.0", "end"))
        finally:
            modinfo.PAGE_CACHE, modinfo.IMAGE_CACHE = saved_pages, saved_images
            modinfo.facts_for = saved_facts


def test_steamodded_slot_lists_vault_folders_not_mod_ids() -> None:
    """Several builds of the loader are only distinguishable by folder."""
    with Harness() as h:
        choices = list(h.app.steamodded_box["values"])
        check("empty slot offered first", choices[0], "(leave installed alone)")
        ok("every choice is a real vault folder",
           all((h.app.vault_root() / "MODLOADERS" / c / "mod").is_dir()
               for c in choices[1:]))


def main() -> int:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("test_gui: skipped (no tkinter)")
        return 0
    try:
        tkinter.Tk().destroy()
    except Exception as exc:
        print(f"test_gui: skipped (no display: {exc})")
        return 0
    # These need a real, fetched library; a fresh clone has none.
    from bml import db

    conn = db.connect()
    vaulted = len(db.vaulted_keys(conn))
    conn.close()
    if vaulted == 0:
        print("test_gui: skipped (the mod library is empty; run "
              "`py -m bml.main sync` and `fetch --all` first)")
        return 0

    for test in (
        test_window_loads_the_real_vault,
        test_search_and_category_filters_narrow_the_table,
        test_toggling_a_mod_pulls_in_its_dependencies,
        test_unresolvable_entries_survive_a_save,
        test_preview_writes_nothing_to_the_mods_folder,
        test_steamodded_slot_lists_vault_folders_not_mod_ids,
        test_a_new_profile_starts_with_nothing_enabled,
        test_a_folder_sourced_entry_reaches_the_vault_like_any_other,
        test_steamodded_beta_satisfies_a_bare_1_0_0_requirement,
        test_the_window_is_dark_and_its_fields_are_outlined,
    ):
        test()
    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("test_gui: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
