"""The first-run setup wizard.

Five pages: welcome, find the game, choose folders, optional downloads, done.
Every field has a Browse button, a sentence saying what it is for, and a
plain-English check. Nothing downloads unless the user presses a button that
says so. Runs as the whole window on first launch, or from the main window's
*Setup…* button, to change a folder or download something skipped earlier.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from . import apply as apply_mod
from . import config, db, modinfo, pipeline, profiles, settings, steam, theme
from .log import log as file_log

APP_NAME = "Balatro Mod Loader"
EXE_NAME = f"{APP_NAME}.exe"

#: Measured on a full library: ~470 mods, 8.1 GB, under an hour.
LIBRARY_SIZE_GB = 8
LIBRARY_MINUTES = 60

FONT_TITLE = ("Segoe UI", 16, "bold")
FONT_BODY = ("Segoe UI", 10)
FONT_LABEL = ("Segoe UI", 10, "bold")

WRAP = 600


def needed() -> bool:
    """First launch: nothing saved and no catalog from before the wizard existed.

    The catalog check skips the wizard for installs that predate it.
    """
    return not settings.setup_done() and not config.DB_PATH.is_file()


# -- Checks ------------------------------------------------------------------


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _is_network_drive(path: Path) -> bool:
    """Junctions cannot point at a network share, so the library must be local."""
    if str(path).startswith("\\\\"):
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes

        drive = os.path.splitdrive(str(path))[0] + "\\"
        return ctypes.windll.kernel32.GetDriveTypeW(drive) == 4  # DRIVE_REMOTE
    except Exception:
        return False


def _writable(path: Path) -> str | None:
    """None if we can make and write this folder, else a sentence saying why not."""
    if not path.is_absolute():
        return "Pick a full folder location, such as C:\\Games\\Balatro Mods."
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".bml-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return ("Windows won't let this app save files there. "
                "Pick a folder inside your user folder or on another drive.")
    return None


def _free_gb(path: Path) -> float | None:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free / 1024 ** 3
    except OSError:
        return None


def check_folders(app: Path, library: Path, mods: Path) -> list[str]:
    """Every problem with a folder choice, in plain words. Empty means fine."""
    problems = []
    for label, path in (("App folder", app), ("Mod library", library)):
        if _inside(path, mods):
            # Steamodded scans the mods folder recursively, so a library inside
            # it would switch on every mod ever downloaded.
            problems.append(
                f"The {label.lower()} can't be inside the game's Mods folder, "
                "or Balatro would try to load every mod in it at once.")
    if _is_network_drive(library):
        problems.append("The mod library has to be on this computer, "
                        "not a network drive.")
    if _inside(mods, library):
        problems.append("The game's Mods folder can't be inside the mod library.")
    for label, path in (("App folder", app), ("Mod library", library),
                        ("Game Mods folder", mods)):
        why = _writable(path)
        if why:
            problems.append(f"{label}: {why}")
    return problems


# -- Installing the app and its shortcuts ------------------------------------


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_exe(app_folder: Path) -> Path | None:
    """Copy the running exe into the app folder. Returns the installed exe.

    Only when running as the exe. From source there is nothing to copy; the
    shortcut points at `bml-gui.pyw` where it is.
    """
    if not _frozen():
        return None
    current = Path(sys.executable).resolve()
    target = app_folder / EXE_NAME
    if target.exists() and target.resolve() == current:
        return target
    try:
        shutil.copy2(current, target)
    except OSError as exc:
        file_log(f"setup: could not copy the exe to {target} ({exc})")
        return None
    file_log(f"setup: installed {target}")
    return target


def make_shortcut(folder_kind: str, exe: Path | None) -> Path | None:
    """A `.lnk` on the Desktop (`Desktop`) or in the Start menu (`Programs`).

    Made with WScript.Shell. Paths go through environment variables so an
    apostrophe in a folder name can't break the script.
    """
    if os.name != "nt":
        return None
    if exe is not None:
        target, arguments, workdir = exe, "", exe.parent
        icon = f"{exe},0"
    else:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        entry = config.BUNDLE / "bml-gui.pyw"
        target, arguments, workdir = pyw, f'"{entry}"', entry.parent
        icon = str(config.BUNDLE / "assets" / "bml.ico")
    env = {**os.environ,
           "BML_LNK_FOLDER": folder_kind, "BML_LNK_NAME": f"{APP_NAME}.lnk",
           "BML_LNK_TARGET": str(target), "BML_LNK_ARGS": arguments,
           "BML_LNK_DIR": str(workdir), "BML_LNK_ICON": icon}
    script = (
        "$dir = [Environment]::GetFolderPath($env:BML_LNK_FOLDER);"
        "$link = Join-Path $dir $env:BML_LNK_NAME;"
        "$sc = (New-Object -ComObject WScript.Shell).CreateShortcut($link);"
        "$sc.TargetPath = $env:BML_LNK_TARGET;"
        "$sc.Arguments = $env:BML_LNK_ARGS;"
        "$sc.WorkingDirectory = $env:BML_LNK_DIR;"
        "$sc.IconLocation = $env:BML_LNK_ICON;"
        "$sc.Description = 'Balatro Mod Loader';"
        "$sc.Save();"
        "Write-Output $link"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, env=env, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        file_log(f"setup: shortcut failed ({exc})")
        return None
    if done.returncode != 0:
        file_log(f"setup: shortcut failed ({done.stderr.strip()})")
        return None
    return Path(done.stdout.strip())


def _remove_if_empty(root: Path) -> None:
    """Tidy up the folders `config` made at the default location before the
    user chose a different one. Only ever removes empty directories."""
    if not root.is_dir():
        return
    for folder in sorted((p for p in root.rglob("*") if p.is_dir()),
                         key=lambda p: len(p.parts), reverse=True):
        try:
            folder.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def _has_data(root: Path) -> bool:
    return (root / "data" / "catalog.db").is_file() or any(
        (root / "profiles").glob("*.json"))


# -- The window --------------------------------------------------------------


class PathField(ttk.Frame):
    """A titled folder or file picker: label, explanation, entry, Browse, status."""

    def __init__(self, master, title: str, help_text: str, *, pick: str = "folder",
                 on_change: Callable[[], None] | None = None,
                 extra: tuple[str, Callable[[], None]] | None = None) -> None:
        super().__init__(master)
        self.pick = pick
        self.on_change = on_change
        ttk.Label(self, text=title, font=FONT_LABEL).pack(anchor="w")
        ttk.Label(self, text=help_text, style="Muted.TLabel", font=FONT_BODY,
                  wraplength=WRAP, justify="left").pack(anchor="w", pady=(1, 4))
        row = ttk.Frame(self)
        row.pack(fill="x")
        self.var = tk.StringVar()
        self.entry = ttk.Entry(row, textvariable=self.var, font=FONT_BODY)
        self.entry.pack(side="left", fill="x", expand=True)
        self.browse = ttk.Button(row, text="Browse…", command=self._browse)
        self.browse.pack(side="left", padx=(6, 0))
        if extra:
            ttk.Button(row, text=extra[0], command=extra[1]).pack(side="left", padx=(6, 0))
        self.status = ttk.Label(self, text="", font=FONT_BODY, wraplength=WRAP,
                                justify="left")
        self.status.pack(anchor="w", pady=(3, 0))
        self.var.trace_add("write", lambda *_: on_change and on_change())

    def get(self) -> Path | None:
        text = self.var.get().strip().strip('"')
        return Path(os.path.expandvars(text)) if text else None

    def set(self, path: Path | str | None) -> None:
        self.var.set(str(path) if path else "")

    def say(self, text: str, tone: str = "muted") -> None:
        colour = {"good": theme.GREEN_BRIGHT, "bad": theme.RED,
                  "warn": theme.AMBER, "muted": theme.MUTED}[tone]
        self.status.configure(text=text, foreground=colour)

    def lock(self, reason: str) -> None:
        self.entry.configure(state="disabled")
        self.browse.configure(state="disabled")
        self.say(reason, "muted")

    def _browse(self) -> None:
        current = self.get()
        start = current if current and current.exists() else (
            current.parent if current and current.parent.exists() else Path.home())
        if self.pick == "exe":
            chosen = filedialog.askopenfilename(
                parent=self, title="Find Balatro.exe",
                initialdir=str(start if start.is_dir() else start.parent),
                filetypes=[("Balatro", "Balatro.exe"), ("Programs", "*.exe")])
        else:
            chosen = filedialog.askdirectory(parent=self, title="Choose a folder",
                                             initialdir=str(start), mustexist=False)
        if chosen:
            self.set(Path(chosen))


class Wizard(ttk.Frame):
    PAGES = ("Welcome", "Find Balatro", "Folders", "Mods", "Done")

    def __init__(self, master: tk.Misc, on_done: Callable[[bool], None],
                 first_run: bool = True) -> None:
        super().__init__(master, padding=(28, 20, 28, 16))
        self.on_done = on_done
        self.first_run = first_run
        self.page = 0
        self._events: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._finishing = False
        saved = settings.load()
        self._start_root = config.ROOT
        self._had_data = not first_run and _has_data(config.ROOT)

        self.steps = ttk.Label(self, style="Muted.TLabel", font=FONT_BODY)
        self.steps.pack(anchor="w")
        self.title_label = ttk.Label(self, font=FONT_TITLE)
        self.title_label.pack(anchor="w", pady=(2, 12))

        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)

        nav = ttk.Frame(self)
        nav.pack(fill="x", side="bottom", pady=(14, 0))
        self.cancel_button = ttk.Button(
            nav, text="Quit setup" if first_run else "Close", command=self.cancel)
        self.cancel_button.pack(side="left")
        self.next_button = ttk.Button(nav, text="Next  >", style="Accent.TButton",
                                      command=self.next)
        self.next_button.pack(side="right")
        self.back_button = ttk.Button(nav, text="<  Back", command=self.back)
        self.back_button.pack(side="right", padx=6)

        self.frames = [self._welcome(), self._game(saved), self._folders(saved),
                       self._mods(), self._done()]
        self.show(0 if first_run else 1)
        self.after(120, self._drain)

    # -- Pages ---------------------------------------------------------------

    def _text(self, parent, text: str, muted: bool = False, pady=(0, 10)) -> ttk.Label:
        label = ttk.Label(parent, text=text, font=FONT_BODY, wraplength=WRAP,
                          justify="left", style="Muted.TLabel" if muted else "TLabel")
        label.pack(anchor="w", pady=pady)
        return label

    def _welcome(self) -> ttk.Frame:
        f = ttk.Frame(self.body)
        self._text(f, "This app keeps your Balatro mods in one place and lets you "
                      "switch them on and off without dragging folders around.")
        self._text(f, "Setup asks three things:")
        for line in ("1.  Where Balatro is installed (usually found for you)",
                     "2.  Where to keep this app and your downloaded mods",
                     "3.  Whether to download anything now (you can skip this)"):
            self._text(f, line, pady=(0, 4))
        self._text(f, "Nothing is downloaded and nothing in your game changes "
                      "until you press a button that says so.", muted=True,
                   pady=(12, 0))
        return f

    def _game(self, saved: dict) -> ttk.Frame:
        f = ttk.Frame(self.body)
        self.game_field = PathField(
            f, "Balatro.exe",
            "The game itself. If it wasn't found, click Browse and pick "
            "Balatro.exe. In Steam you can find the folder by right-clicking "
            "Balatro, then Manage, then Browse local files.",
            pick="exe", on_change=self._check_game,
            extra=("Search again", self._search_game))
        self.game_field.pack(fill="x")
        start = saved.get("game_dir")
        if start and steam.exe_in(Path(start)):
            self.game_field.set(steam.exe_in(Path(start)))
        return f

    def _folders(self, saved: dict) -> ttk.Frame:
        f = ttk.Frame(self.body)
        self.app_field = PathField(
            f, "App folder",
            "Where this app keeps its settings, your mod lists, and a copy of "
            "itself. The suggested folder is fine for almost everyone.",
            on_change=self._app_changed)
        self.app_field.pack(fill="x", pady=(0, 10))
        self.library_field = PathField(
            f, "Mod library",
            "Where downloaded mods are stored. Mods you switch on are linked "
            "into the game from here, not copied, so each one is stored once. "
            "Pick a drive with room to spare if you plan to download a lot.",
            on_change=self._library_changed)
        self.library_field.pack(fill="x", pady=(0, 10))
        self.mods_field = PathField(
            f, "Balatro's Mods folder",
            "Where Balatro loads mods from. Leave this alone unless you know "
            "you've moved it.",
            extra=("Use the usual one", lambda: self.mods_field.set(config.default_game_mods())))
        self.mods_field.pack(fill="x", pady=(0, 8))

        self.desktop_var = tk.BooleanVar(value=self.first_run)
        self.startmenu_var = tk.BooleanVar(value=self.first_run)
        checks = ttk.Frame(f)
        checks.pack(fill="x")
        ttk.Checkbutton(checks, text="Put a shortcut on my Desktop",
                        variable=self.desktop_var).pack(side="left")
        ttk.Checkbutton(checks, text="Add it to the Start menu",
                        variable=self.startmenu_var).pack(side="left", padx=(16, 0))

        self._library_touched = True   # until the fields are filled in below
        self._following = False
        self.app_field.set(saved.get("app_folder") or
                           (config.ROOT if not self.first_run or not _frozen()
                            else config.default_app_folder()))
        self.library_field.set(saved.get("library") or config.vault())
        self._library_touched = bool(saved.get("library")) or not self.first_run
        self.mods_field.set(saved.get("game_mods") or config.game_mods())

        if self._had_data:
            why = ("Your mod lists and library are already here. Moving them "
                   "isn't supported from setup yet, so these stay put.")
            self.app_field.lock(why)
            self.library_field.lock(why)
        return f

    def _mods(self) -> ttk.Frame:
        f = ttk.Frame(self.body)
        self._text(f, "Both of these are optional. Skip them and you can come "
                      "back any time with the Setup button in the main window. "
                      "Downloading a mod does not turn it on in your game; "
                      "you choose which mods to use afterwards.", muted=True)

        ttk.Label(f, text="Mod support (Lovely and Steamodded)",
                  font=FONT_LABEL).pack(anchor="w")
        self._text(f, "Balatro needs these two small tools before it will load "
                      "any mod. Lovely goes into the game folder (any older copy "
                      "is kept as a backup). Steamodded goes into your mod library.",
                   pady=(1, 4))
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(0, 14))
        self.support_button = ttk.Button(row, text="Install mod support",
                                         command=self.install_support)
        self.support_button.pack(side="left")
        self.support_status = ttk.Label(row, font=FONT_BODY, style="Muted.TLabel")
        self.support_status.pack(side="left", padx=10)

        ttk.Label(f, text="The community mod collection",
                  font=FONT_LABEL).pack(anchor="w")
        self.collection_text = self._text(f, "", pady=(1, 4))
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.collection_button = ttk.Button(row, text="Download all community mods",
                                            command=self.download_collection)
        self.collection_button.pack(side="left")
        self.stop_button = ttk.Button(row, text="Stop", command=self.stop_download,
                                      state="disabled")
        self.stop_button.pack(side="left", padx=6)

        self.progress = ttk.Progressbar(f, mode="determinate", maximum=1)
        self.progress.pack(fill="x", pady=(10, 4))
        self.activity = ttk.Label(f, font=FONT_BODY, style="Muted.TLabel",
                                  wraplength=WRAP, justify="left")
        self.activity.pack(anchor="w")
        return f

    def _done(self) -> ttk.Frame:
        f = ttk.Frame(self.body)
        self.summary = self._text(f, "")
        self._text(f, "In the main window: double-click a mod to add it to your "
                      "list, then press Apply to game. Launch Balatro starts the "
                      "game through Steam.", muted=True)
        return f

    # -- Navigation ----------------------------------------------------------

    def show(self, index: int) -> None:
        for frame in self.frames:
            frame.pack_forget()
        self.page = index
        self.frames[index].pack(fill="both", expand=True)
        self.title_label.configure(text=self.PAGES[index] if index else
                                   "Welcome to Balatro Mod Loader")
        self.steps.configure(text=f"Step {index + 1} of {len(self.PAGES)}")
        first_page = 0 if self.first_run else 1
        self.back_button.configure(
            state="normal" if index > first_page and index < 4 else "disabled")
        last = index == len(self.PAGES) - 1
        # Settings are saved by now, so quitting here would only skip the
        # default profile. One button on the last page, not two.
        if last:
            self.cancel_button.pack_forget()
        self.next_button.configure(
            text=("Open Balatro Mod Loader" if self.first_run else "Close")
            if last else "Next  >")
        if index == 1 and not self.game_field.get():
            self.after(50, self._search_game)
        elif index == 1:
            self._check_game()
        elif index == 2:
            self._app_changed()
        elif index == 3:
            self._refresh_mods_page()
        elif index == 4:
            self._write_summary()

    def next(self) -> None:
        if self.page == 1 and not self._leave_game():
            return
        if self.page == 2 and not self._leave_folders():
            return
        if self.page == 3 and self._busy() and not self._stop_before_leaving():
            return
        if self.page == len(self.PAGES) - 1:
            self.finish()
            return
        self.show(self.page + 1)

    def back(self) -> None:
        if self._busy():
            return
        self.show(max(self.page - 1, 0))

    def cancel(self) -> None:
        if self._busy():
            if not messagebox.askyesno(
                    "Stop downloading?",
                    "A download is still running. Stop it and close setup? "
                    "Mods already downloaded are kept.", parent=self):
                return
            self._stop.set()
        if self.first_run and not messagebox.askyesno(
                "Quit setup?",
                "Quit without finishing setup? It will start again next time "
                "you open the app.", parent=self):
            return
        self.on_done(False)

    def finish(self) -> None:
        settings.save({"setup_done": True})
        self._default_profile()
        file_log("setup: finished")
        self.on_done(True)

    # -- Page 2: the game ----------------------------------------------------

    def _search_game(self) -> None:
        self.game_field.say("Looking for Balatro…")
        self.update_idletasks()
        found = steam.find_balatro()
        if found:
            self.game_field.set(found)
        else:
            self.game_field.say(
                "Couldn't find Balatro on its own. Click Browse and pick "
                "Balatro.exe.", "warn")

    def _check_game(self) -> bool:
        path = self.game_field.get()
        if not path:
            return False
        exe = steam.exe_in(path)
        if exe is None:
            self.game_field.say("That isn't Balatro.exe. Pick the file named "
                                "Balatro.exe, or the folder it's in.", "bad")
            return False
        self.game_field.say(f"Found Balatro in {exe.parent}", "good")
        return True

    def _leave_game(self) -> bool:
        if not self._check_game():
            if not self.game_field.get():
                self.game_field.say("Pick Balatro.exe to continue.", "bad")
            return False
        settings.save({"game_dir": steam.exe_in(self.game_field.get()).parent})
        return True

    # -- Page 3: folders -----------------------------------------------------

    def _app_changed(self) -> None:
        """The library follows the app folder until the user picks it themselves."""
        app = self.app_field.get()
        if app and not self._library_touched:
            self._following = True
            self.library_field.set(app / "Mod Library")
            self._following = False

    def _library_changed(self) -> None:
        if not self._following:
            self._library_touched = True
        library = self.library_field.get()
        if library and not self._had_data:
            free = _free_gb(library)
            if free is not None:
                tone = "warn" if free < LIBRARY_SIZE_GB + 2 else "muted"
                self.library_field.say(f"{free:.0f} GB free on that drive.", tone)

    def _leave_folders(self) -> bool:
        app, library, mods = (self.app_field.get(), self.library_field.get(),
                              self.mods_field.get())
        if not (app and library and mods):
            messagebox.showwarning("A folder is empty",
                                   "Fill in all three folders to continue.",
                                   parent=self)
            return False
        problems = check_folders(app, library, mods)
        if problems:
            messagebox.showwarning("Please pick a different folder",
                                   "\n\n".join(problems), parent=self)
            return False
        if os.path.normcase(str(mods)) != os.path.normcase(str(config.default_game_mods())):
            if not messagebox.askyesno(
                    "Unusual Mods folder",
                    f"Balatro normally loads mods from\n{config.default_game_mods()}\n\n"
                    "You picked a different folder. Mods placed there won't load "
                    "unless you've set Balatro up to look there. Keep it anyway?",
                    parent=self):
                return False

        settings.save({"app_folder": app, "library": library, "game_mods": mods})
        if os.path.normcase(str(app)) != os.path.normcase(str(config.ROOT)):
            old = config.ROOT
            config.set_root(app)
            modinfo.rebase()
            if not _has_data(old) and old == self._start_root and self.first_run:
                _remove_if_empty(old)
        library.mkdir(parents=True, exist_ok=True)
        mods.mkdir(parents=True, exist_ok=True)

        exe = install_exe(app)
        made = []
        if self.desktop_var.get() and make_shortcut("Desktop", exe):
            made.append("Desktop")
        if self.startmenu_var.get() and make_shortcut("Programs", exe):
            made.append("Start menu")
        self._installed = (exe, made)
        file_log(f"setup: app {app}, library {library}, mods {mods}")
        return True

    # -- Page 4: downloads ---------------------------------------------------

    def _refresh_mods_page(self) -> None:
        lovely = apply_mod.lovely_status().get("present")
        loader = pipeline.steamodded_folders()
        if lovely and loader:
            self.support_status.configure(text="Installed", foreground=theme.GREEN_BRIGHT)
        else:
            missing = [name for name, ok in (("Lovely", lovely), ("Steamodded", loader))
                       if not ok]
            self.support_status.configure(
                text="Not installed yet: " + " and ".join(missing),
                foreground=theme.MUTED)

        conn = db.connect()
        known = len(db.catalog_rows(conn))
        have = len(db.vaulted_keys(conn))
        conn.close()
        count = f"{known}" if known else "about 470"
        self.collection_text.configure(text=(
            f"Downloads every mod listed in the Balatro Mod Index and on "
            f"Thunderstore ({count} mods) into your mod library. This takes "
            f"about {LIBRARY_SIZE_GB} GB of space and roughly an hour. You can "
            f"stop at any time and pick up where you left off later."
            + (f"\n\nAlready in your library: {have}." if have else "")))
        if not self._busy():
            self.progress.configure(value=0, maximum=1)

    def install_support(self) -> None:
        self._run("support", self._support_worker)

    def download_collection(self) -> None:
        if not messagebox.askyesno(
                "Download all community mods?",
                f"This downloads roughly {LIBRARY_SIZE_GB} GB and takes about an "
                "hour. None of the mods are switched on in your game.\n\n"
                "You can keep using your computer while it runs, and press Stop "
                "at any time. Start now?", parent=self):
            return
        self._run("collection", self._collection_worker)

    def stop_download(self) -> None:
        self._stop.set()
        self.stop_button.configure(state="disabled")
        self.activity.configure(text="Stopping after the current mod…")

    def _run(self, kind: str, target) -> None:
        if self._busy():
            return
        self._stop.clear()
        self.support_button.configure(state="disabled")
        self.collection_button.configure(state="disabled")
        self.back_button.configure(state="disabled")
        self.stop_button.configure(state="normal" if kind == "collection" else "disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self._worker = threading.Thread(target=target, daemon=True)
        self._worker.start()

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _emit(self, *event) -> None:
        self._events.put(event)

    def _log(self, message: str) -> None:
        file_log(message)
        self._emit("log", message)

    def _support_worker(self) -> None:
        try:
            notes = []
            if apply_mod.lovely_status().get("present"):
                notes.append("Lovely was already installed.")
            else:
                self._emit("activity", "Downloading Lovely…")
                ok = pipeline.install_lovely(log=self._log)
                notes.append("Lovely installed." if ok else
                             "Lovely could not be installed. See the log.")
            self._emit("activity", "Downloading Steamodded…")
            folder = pipeline.ensure_steamodded(log=self._log)
            notes.append("Steamodded is in your library." if folder else
                         "Steamodded could not be downloaded.")
            self._emit("finished", "support", " ".join(notes))
        except PermissionError:
            self._emit("failed", "support",
                       "Windows wouldn't let us write to the game folder. "
                       "Close Balatro if it's running, then try again.")
        except Exception as exc:
            self._emit("failed", "support", _friendly(exc))

    def _collection_worker(self) -> None:
        try:
            self._emit("activity", "Getting the list of mods…")
            total = pipeline.sync(log=self._log)
            if total < 0:
                self._emit("failed", "collection",
                           "Couldn't get the list of mods. Check your internet "
                           "connection and try again.")
                return
            stats = pipeline.fetch(
                log=self._log, stop=self._stop,
                progress=lambda i, n, title: self._emit("progress", i, n, title))
            if stats["stopped"]:
                message = ("Stopped. Everything downloaded so far is kept; press "
                           "the button again to carry on.")
            elif stats["queued"] == 0:
                message = "Every mod in the collection is already in your library."
            else:
                message = (f"Done: {stats['ok'] + stats['no_manifest']} downloaded"
                           + (f", {stats['failed']} couldn't be reached"
                              if stats["failed"] else "") + ".")
            self._emit("finished", "collection", message)
        except Exception as exc:
            self._emit("failed", "collection", _friendly(exc))

    def _drain(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                kind = event[0]
                if kind == "activity":
                    self.activity.configure(text=event[1])
                elif kind == "progress":
                    _, index, total, title = event
                    if str(self.progress["mode"]) != "determinate":
                        self.progress.stop()
                        self.progress.configure(mode="determinate")
                    self.progress.configure(maximum=total, value=index - 1)
                    self.activity.configure(text=f"{index} of {total}: {title}")
                elif kind in ("finished", "failed"):
                    self._worker_ended(kind == "finished", event[2])
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _worker_ended(self, ok: bool, message: str) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=1, value=1 if ok else 0)
        self.activity.configure(text=message,
                                foreground=theme.TEXT if ok else theme.RED)
        self.support_button.configure(state="normal")
        self.collection_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.back_button.configure(state="normal")
        self._worker = None
        self._refresh_mods_page()
        if self._finishing:
            self._finishing = False
            self.show(self.page + 1)

    def _stop_before_leaving(self) -> bool:
        if not messagebox.askyesno(
                "Stop downloading?",
                "A download is still running. Stop it and continue? Mods "
                "already downloaded are kept, and you can finish later from "
                "the Setup button.", parent=self):
            return False
        self._stop.set()
        self._finishing = True
        self.next_button.configure(state="disabled")
        self.activity.configure(text="Stopping after the current mod…")
        self.after(200, self._wait_then_next)
        return False

    def _wait_then_next(self) -> None:
        if self._busy():
            self.after(200, self._wait_then_next)
            return
        self.next_button.configure(state="normal")

    # -- Page 5 --------------------------------------------------------------

    def _write_summary(self) -> None:
        exe, made = getattr(self, "_installed", (None, []))
        conn = db.connect()
        have = len(db.vaulted_keys(conn))
        conn.close()
        lines = [
            "You're set up.",
            "",
            f"Balatro:  {config.game_dir()}",
            f"App folder:  {config.ROOT}",
            f"Mod library:  {config.vault()}  ({have} mods)",
            f"Balatro's Mods folder:  {config.game_mods()}",
        ]
        if made:
            lines += ["", "Shortcut added to: " + ", ".join(made) + "."]
        if exe is not None and Path(sys.executable).resolve() != exe.resolve():
            lines += ["", f"The app was copied to {exe}. The file you "
                          "downloaded can be deleted."]
        self.summary.configure(text="\n".join(lines))

    def _default_profile(self) -> None:
        """Give a new user one mod list, already pointed at Steamodded.

        Otherwise the main window opens on "Create or select a profile first".
        """
        if profiles.all_profiles():
            return
        profile = profiles.Profile(name="My mods")
        loaders = pipeline.steamodded_folders()
        profile.steamodded = loaders[-1] if loaders else ""
        profile.save()


def _friendly(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    file_log(f"setup: {text}")
    if "Connection" in text or "Timeout" in text or "resolve" in text.lower():
        return ("Couldn't reach the internet. Check your connection and try "
                "again.")
    return f"Something went wrong: {exc}"


def open_window(master: tk.Misc, on_close: Callable[[], None]) -> tk.Toplevel:
    """Run the wizard in its own window, over the main one."""
    window = tk.Toplevel(master)
    window.title("Balatro Mod Loader setup")
    window.configure(background=theme.BASE)
    window.geometry("720x620")
    window.minsize(640, 560)
    window.transient(master.winfo_toplevel())

    def done(_ok: bool) -> None:
        window.destroy()
        on_close()

    page = Wizard(window, done, first_run=False)
    page.pack(fill="both", expand=True)
    window.protocol("WM_DELETE_WINDOW", page.cancel)
    window.grab_set()
    return window
