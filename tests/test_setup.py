"""The setup wizard's plumbing: settings, finding the game, folder checks, Stop.

    py -m tests.test_setup

No window is opened. Everything the wizard decides is in plain functions
(`settings`, `steam`, `wizard.check_folders`, `pipeline.fetch`), and those are
what is tested here. Settings go to a temp file via BML_SETTINGS and the data
folder to a temp directory via `config.set_root`, so nothing touches the real
install.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bml import config, db, pipeline, settings, steam, vault, wizard  # noqa: E402

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def ok(label: str, condition: bool) -> None:
    if not condition:
        FAILURES.append(label)


class Sandbox:
    """A temp settings file and data folder, restored on exit."""

    def __enter__(self):
        self.root = Path(tempfile.mkdtemp(prefix="bml-setup-"))
        self._env = {k: os.environ.get(k) for k in
                     ("BML_SETTINGS", "BML_VAULT", "BML_GAME_MODS", "BML_GAME_DIR")}
        for k in self._env:
            os.environ.pop(k, None)
        os.environ["BML_SETTINGS"] = str(self.root / "settings.json")
        self._old_root = config.ROOT
        config.set_root(self.root / "app")
        return self

    def __exit__(self, *exc):
        config.set_root(self._old_root)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)


def test_settings_round_trip():
    with Sandbox() as box:
        check("empty settings", settings.load(), {})
        ok("not done before saving", not settings.setup_done())
        settings.save({"library": box.root / "lib"})
        settings.save({"setup_done": True})
        check("merge keeps earlier keys", settings.get("library"), box.root / "lib")
        ok("bool survives as bool", settings.load()["setup_done"] is True)
        check("config reads the saved library", config.vault(), box.root / "lib")


def test_damaged_settings_file_is_no_file():
    with Sandbox() as box:
        (box.root / "settings.json").write_text("{not json", encoding="utf-8")
        check("damaged file", settings.load(), {})


def test_env_beats_settings():
    with Sandbox() as box:
        settings.save({"library": box.root / "from-settings"})
        os.environ["BML_VAULT"] = str(box.root / "from-env")
        check("env wins", config.vault(), box.root / "from-env")


def test_library_defaults_inside_app_folder():
    with Sandbox() as box:
        check("default library", config.vault(), box.root / "app" / "Mod Library")


def test_wizard_needed():
    with Sandbox():
        ok("fresh install needs setup", wizard.needed())
        settings.save({"setup_done": True})
        ok("done means no wizard", not wizard.needed())
    with Sandbox():
        db.connect().close()   # a catalog from before the wizard existed
        ok("existing catalog skips the wizard", not wizard.needed())


def test_library_folders_vdf():
    with Sandbox() as box:
        steam_root = box.root / "Steam"
        (steam_root / "steamapps").mkdir(parents=True)
        (steam_root / "steamapps" / "libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"C:\\\\Program Files (x86)\\\\Steam"\n'
            '\t}\n\t"1"\n\t{\n\t\t"path"\t\t"E:\\\\SteamLibrary"\n\t\t"label"\t\t""\n\t}\n}\n',
            encoding="utf-8")
        folders = steam.library_folders(steam_root)
        check("first is the root", folders[0], steam_root)
        ok("second library found", Path(r"E:\SteamLibrary") in folders)
        check("backslashes unescaped", str(folders[-1]), r"E:\SteamLibrary")


def test_exe_in_accepts_file_or_folder():
    with Sandbox() as box:
        game = box.root / "Balatro"
        game.mkdir()
        exe = game / "Balatro.exe"
        exe.write_bytes(b"MZ")
        check("the exe", steam.exe_in(exe), exe)
        check("its folder", steam.exe_in(game), exe)
        check("wrong folder", steam.exe_in(box.root), None)
        other = game / "notes.txt"
        other.write_text("x", encoding="utf-8")
        check("wrong file", steam.exe_in(other), None)


def test_folder_checks():
    with Sandbox() as box:
        mods = box.root / "Roaming" / "Balatro" / "Mods"
        app = box.root / "BML"
        check("sensible choice", wizard.check_folders(app, app / "Mod Library", mods), [])
        problems = wizard.check_folders(app, mods / "Library", mods)
        ok("library inside Mods refused",
           any("Mods folder" in p for p in problems))
        problems = wizard.check_folders(mods / "BML", app / "lib", mods)
        ok("app folder inside Mods refused",
           any("app folder" in p for p in problems))
        problems = wizard.check_folders(app, app / "lib", app / "lib" / "Mods")
        ok("Mods inside library refused",
           any("can't be inside the mod library" in p for p in problems))
        problems = wizard.check_folders(Path("relative"), app / "lib", mods)
        ok("relative path refused", any("full folder" in p for p in problems))
        ok("network share refused",
           wizard._is_network_drive(Path(r"\\server\share\mods")))


def test_fetch_stops_between_mods():
    """Stop is honoured before the next mod, never halfway through one."""
    with Sandbox():
        conn = db.connect()
        with conn:
            for n in range(5):
                db.upsert_catalog(conn, {"key": f"repo:test/mod{n}", "source": "index",
                                         "title": f"Mod {n}", "categories": [],
                                         "raw": {}})
        conn.close()
        stop = threading.Event()
        fetched = []

        def fake_fetch_one(entry, log=print, force=False):
            fetched.append(entry["key"])
            if len(fetched) == 2:
                stop.set()
            return {"key": entry["key"], "mod_id": "", "category": "X",
                    "category_reason": "test", "vault_path": "", "version": "",
                    "sha256": "", "archive_name": "", "manifest": "{}",
                    "smods_objects": "{}", "status": "ok", "note": "",
                    "fetched_at": db.now()}

        seen = []
        real, pause = vault.fetch_one, config.PAUSE_BETWEEN_DOWNLOADS
        vault.fetch_one, config.PAUSE_BETWEEN_DOWNLOADS = fake_fetch_one, 0
        try:
            stats = pipeline.fetch(log=lambda _m: None, stop=stop,
                                   progress=lambda i, n, t: seen.append((i, n)))
        finally:
            vault.fetch_one, config.PAUSE_BETWEEN_DOWNLOADS = real, pause
        check("two downloaded", len(fetched), 2)
        ok("reported stopped", stats["stopped"])
        check("counted", stats["ok"], 2)
        check("progress", seen, [(1, 5), (2, 5)])
        conn = db.connect()
        check("both recorded", len(db.vaulted_keys(conn)), 2)
        conn.close()


def main() -> int:
    for test in (
        test_settings_round_trip,
        test_damaged_settings_file_is_no_file,
        test_env_beats_settings,
        test_library_defaults_inside_app_folder,
        test_wizard_needed,
        test_library_folders_vdf,
        test_exe_in_accepts_file_or_folder,
        test_folder_checks,
        test_fetch_stops_between_mods,
    ):
        try:
            test()
        except Exception as exc:
            FAILURES.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("test_setup: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
