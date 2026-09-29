"""Desktop UI over the resolver.

    py -m bml.main gui        (or double-click bml-gui.pyw)

tkinter, because it ships with Python. The window is mostly the resolver: pick
mods, see what they pull in and what breaks, then apply. The *Setup* button
reopens the wizard (`wizard.py`); the CLI's `sync` and `fetch` do the rest.
Nothing touches the game until Apply, which shows the plan and asks first.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from . import apply as apply_mod
from . import config, db, graph, links, modinfo, pipeline, profiles, resolve, theme, wizard
from .log import log as file_log
from .sources import local_index
from .sources.merge import normalise

# Column keys for the mod table. `on` and `live` are the two states that only a
# UI can show side by side: what the profile asks for, and what the game has.
_COLUMNS = ("on", "live", "title", "version", "category")
_HEADINGS = {
    "on": "In profile",
    "live": "In game",
    "title": "Mod",
    "version": "Version",
    "category": "Category",
}
_WIDTHS = {"on": 70, "live": 66, "title": 300, "version": 150, "category": 210}


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        # Before any widget exists: clam has to be selected while the widget
        # tree is empty or already-created widgets keep the old theme's layout.
        try:
            theme.apply(master)
        except tk.TclError as exc:  # pragma: no cover -- a Tk without clam
            file_log(f"gui: could not apply the theme ({exc}); using the default")
        super().__init__(master, padding=8)
        self.master.title("Balatro Mod Loader")
        set_window_icon(self.master)
        self.master.geometry("1220x760")
        self.master.minsize(940, 560)
        self.pack(fill="both", expand=True)

        self.findings = graph.Findings()
        self.nodes: list[graph.Node] = []
        self.profile: profiles.Profile | None = None
        #: Normalised mod ids the profile asks for.
        self.members: set[str] = set()
        #: Profile entries that match nothing in the vault. Kept verbatim and
        #: written back on save so they are not lost.
        self.unmatched: list[str] = []
        self.live: dict[str, str] = {}
        self.resolution: resolve.Resolution | None = None
        self._row_of: dict[str, graph.Node] = {}
        self._events: queue.Queue = queue.Queue()
        #: The mod the Page tab is currently about.
        self._page_node: graph.Node | None = None

        self._build_widgets()
        self.reload(initial=True)
        self.after(100, self._drain_events)

    # -- Layout ------------------------------------------------------------

    def _build_widgets(self) -> None:
        self._build_profile_bar()

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=(8, 6))
        panes.add(self._build_mod_list(panes), weight=3)
        panes.add(self._build_side_panel(panes), weight=2)

        self._build_action_bar()

        self.status = ttk.Label(self, text="", anchor="w",
                                style="Status.TLabel", padding=(8, 4))
        self.status.pack(fill="x", side="bottom", pady=(6, 0))

    def _build_profile_bar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x")

        ttk.Label(bar, text="Profile").pack(side="left")
        self.profile_var = tk.StringVar()
        self.profile_box = ttk.Combobox(bar, textvariable=self.profile_var,
                                        state="readonly", width=24)
        self.profile_box.pack(side="left", padx=(6, 4))
        self.profile_box.bind("<<ComboboxSelected>>",
                              lambda _e: self.load_profile(self.profile_var.get()))

        ttk.Button(bar, text="New…", command=self.new_profile).pack(side="left", padx=2)
        ttk.Button(bar, text="Save", command=self.save_profile).pack(side="left", padx=2)
        ttk.Button(bar, text="Delete", command=self.delete_profile).pack(side="left", padx=2)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        # The Steamodded slot, up front because it decides what loads.
        ttk.Label(bar, text="Steamodded").pack(side="left")
        self.steamodded_var = tk.StringVar()
        self.steamodded_box = ttk.Combobox(bar, textvariable=self.steamodded_var,
                                           state="readonly", width=30)
        self.steamodded_box.pack(side="left", padx=(6, 0))
        self.steamodded_box.bind("<<ComboboxSelected>>", lambda _e: self.on_toggle())

        ttk.Button(bar, text="Setup…",
                   command=self.open_setup).pack(side="right")
        ttk.Button(bar, text="Reload vault",
                   command=self.reload).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="Add mod…",
                   command=self.add_mod).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="Capture installed",
                   command=self.capture_installed).pack(side="right", padx=(0, 6))

    def _build_mod_list(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent)

        filters = ttk.Frame(frame)
        filters.pack(fill="x", pady=(0, 4))

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_table())
        entry = ttk.Entry(filters, textvariable=self.search_var)
        entry.pack(side="left", fill="x", expand=True)
        entry.insert(0, "")
        ttk.Label(filters, text="  Category").pack(side="left")
        self.category_var = tk.StringVar(value="all")
        self.category_box = ttk.Combobox(filters, textvariable=self.category_var,
                                         state="readonly", width=32)
        self.category_box.pack(side="left", padx=4)
        self.category_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh_table())

        self.show_var = tk.StringVar(value="everything")
        show = ttk.Combobox(filters, textvariable=self.show_var, state="readonly",
                            width=16,
                            values=("everything", "in profile", "in game",
                                    "not in profile"))
        show.pack(side="left")
        show.bind("<<ComboboxSelected>>", lambda _e: self.refresh_table())

        holder = ttk.Frame(frame)
        holder.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(holder, columns=_COLUMNS, show="headings",
                                 selectmode="extended")
        for column in _COLUMNS:
            self.tree.heading(column, text=_HEADINGS[column])
            self.tree.column(column, width=_WIDTHS[column],
                             anchor="center" if column in ("on", "live") else "w")
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # Colour pulled-in dependencies differently from chosen mods.
        self.tree.tag_configure("dependency", foreground=theme.GREEN_BRIGHT)
        self.tree.tag_configure("problem", foreground=theme.RED)

        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_details())
        self.tree.bind("<Double-1>", lambda _e: self.on_toggle())
        self.tree.bind("<space>", lambda _e: self.on_toggle())
        self.tree.bind("<Return>", lambda _e: self.on_toggle())
        self.tree.bind("<Button-3>", self._popup_menu)
        self._build_row_menu()

        hint = ttk.Label(frame, style="Muted.TLabel",
                         text="Double-click or press Space to add/remove · "
                              "right-click for the mod's page · "
                              "green = pulled in as a dependency.")
        hint.pack(anchor="w", pady=(4, 0))
        return frame

    def _build_side_panel(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent)
        book = ttk.Notebook(frame)
        book.pack(fill="both", expand=True)

        self.details = self._text_tab(book, "Details")
        self._build_page_tab(book)
        self.problems = self._text_tab(book, "Problems")
        self.output = self._text_tab(book, "Log")
        self.book = book
        # Select tabs by widget, not index.
        self.log_tab = self.output.tab_holder
        book.bind("<<NotebookTabChanged>>", lambda _e: self._page_tab_shown())
        return frame

    def _text_tab(self, book: ttk.Notebook, label: str) -> tk.Text:
        holder = ttk.Frame(book)
        text = tk.Text(holder, wrap="word", height=10, width=44,
                       state="disabled", padx=8, pady=6,
                       **theme.text_options())
        scroll = ttk.Scrollbar(holder, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        scroll.pack(side="right", fill="y")
        book.add(holder, text=label)
        text.tag_configure("head", font=theme.FONT_HEAD, foreground=theme.TEXT)
        text.tag_configure("bad", foreground=theme.RED)
        text.tag_configure("good", foreground=theme.GREEN_BRIGHT)
        text.tag_configure("warn", foreground=theme.AMBER)
        text.tag_configure("dim", foreground=theme.MUTED)
        text.tab_holder = holder
        return text

    def _build_row_menu(self) -> None:
        """Right-click on a mod. Also the only place to fix a mod's repo URL."""
        menu = tk.Menu(self.tree, tearoff=0,
                       background=theme.PANEL, foreground=theme.TEXT,
                       activebackground=theme.GREEN_DIM,
                       activeforeground=theme.TEXT,
                       borderwidth=1, relief="solid", font=theme.FONT)
        menu.add_command(label="Add to / remove from profile",
                         command=self.on_toggle)
        menu.add_separator()
        menu.add_command(label="Show its page", command=self._menu_show_page)
        menu.add_command(label="Open on GitHub", command=self.open_repo)
        menu.add_command(label="Set the GitHub URL…", command=self.set_repo_url)
        menu.add_separator()
        menu.add_command(label="Open its vault folder",
                         command=self.open_vault_folder)
        menu.add_command(label="Copy its mod id", command=self.copy_mod_id)
        self.row_menu = menu

    def _popup_menu(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if item and item in self._row_of:
            # Right-clicking outside the selection selects that row first.
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self.tree.focus(item)
        try:
            self.row_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.row_menu.grab_release()

    def _selected_node(self) -> graph.Node | None:
        items = self.tree.selection()
        return self._row_of.get(items[0]) if items else None

    def _menu_show_page(self) -> None:
        node = self._selected_node()
        if node is None:
            return
        self.book.select(self.page_tab)
        self.show_page(node)
        self.load_page()

    def set_repo_url(self) -> None:
        """Attach a GitHub URL to a mod whose catalog entry has none, or a bad one.

        Stored in `data/repo-overrides.json`. It only changes what the Page tab
        reads, never what gets downloaded.
        """
        node = self._selected_node()
        if node is None:
            messagebox.showinfo("Set the GitHub URL", "Select a mod first.",
                                parent=self)
            return
        facts = self._facts_for(node)
        answer = simpledialog.askstring(
            "Set the GitHub URL",
            f"GitHub URL for {node.title}:\n\n"
            "Anything github.com-shaped is accepted and trimmed to the repo.\n"
            "Leave it empty to clear an override.",
            initialvalue=facts.repo, parent=self)
        if answer is None:
            return
        modinfo.set_override(node.key, answer)
        self._page_facts = None
        self.log(f"repo: {node.mod_id} -> {modinfo.normalise_repo(answer) or '(cleared)'}")
        self.show_page(node)

    def open_vault_folder(self) -> None:
        node = self._selected_node()
        if node is None or not node.vault_path:
            messagebox.showinfo("Open vault folder",
                                "This mod is not in the vault.", parent=self)
            return
        path = Path(node.vault_path)
        if not path.is_dir():
            messagebox.showerror("Open vault folder", f"{path} is gone.",
                                 parent=self)
            return
        try:
            os.startfile(str(path))  # noqa: S606 -- Explorer, on the vault only
        except (OSError, AttributeError) as exc:
            messagebox.showerror("Open vault folder", str(exc), parent=self)

    def copy_mod_id(self) -> None:
        node = self._selected_node()
        if node is None:
            return
        self.clipboard_clear()
        self.clipboard_append(node.mod_id)
        self.set_status(f"Copied {node.mod_id}")

    def _build_page_tab(self, book: ttk.Notebook) -> None:
        """The mod's own README, screenshots and all, inside the window.

        See `modinfo` for why this renders markdown instead of a browser.
        """
        holder = ttk.Frame(book)

        bar = ttk.Frame(holder)
        bar.pack(fill="x", pady=(2, 4))
        self.page_title = ttk.Label(bar, text="", style="Muted.TLabel",
                                    anchor="w")
        self.page_title.pack(side="left", fill="x", expand=True)
        ttk.Button(bar, text="Open on GitHub",
                   command=self.open_repo).pack(side="right", padx=(4, 0))
        self.page_button = ttk.Button(bar, text="Load page",
                                      command=lambda: self.load_page(refresh=True))
        self.page_button.pack(side="right")

        body = ttk.Frame(holder)
        body.pack(fill="both", expand=True)
        text = tk.Text(body, wrap="word", height=10, width=44, state="disabled",
                       padx=10, pady=8, spacing1=2, spacing3=3, cursor="arrow",
                       **theme.text_options())
        scroll = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        scroll.pack(side="right", fill="y")
        book.add(holder, text="Page")

        family = theme.FONT[0]
        text.tag_configure("h1", font=(family, 15, "bold"), foreground=theme.TEXT,
                           spacing1=10, spacing3=4)
        text.tag_configure("h2", font=(family, 12, "bold"), foreground=theme.TEXT,
                           spacing1=9, spacing3=3)
        text.tag_configure("h3", font=(family, 10, "bold"),
                           foreground=theme.GREEN_BRIGHT, spacing1=7, spacing3=2)
        text.tag_configure("head", font=theme.FONT_HEAD, foreground=theme.TEXT)
        text.tag_configure("bold", font=theme.FONT_BOLD)
        text.tag_configure("italic", font=(family, 9, "italic"))
        text.tag_configure("code", font=("Consolas", 9), foreground="#e2b374")
        text.tag_configure("block", font=("Consolas", 9), background=theme.INK,
                           foreground=theme.TEXT, lmargin1=14, lmargin2=14,
                           spacing1=4, spacing3=4)
        text.tag_configure("quote", lmargin1=12, lmargin2=12, foreground=theme.MUTED,
                           font=(family, 9, "italic"))
        text.tag_configure("item", lmargin1=14, lmargin2=26)
        text.tag_configure("item2", lmargin1=30, lmargin2=42)
        text.tag_configure("rule", foreground=theme.LINE)
        text.tag_configure("dim", foreground=theme.MUTED)
        text.tag_configure("bad", foreground=theme.RED)
        text.tag_configure("good", foreground=theme.GREEN_BRIGHT)
        text.tag_configure("link", foreground="#00a8fc", underline=True)
        self.page = text
        self.page.tab_holder = holder
        self.page_tab = holder

        #: Bumped on every selection change so a late README is discarded.
        self._page_token = 0
        self._page_facts = None
        self._page_images: list = []       # PhotoImage refs; Tk keeps none
        self._page_pending: str | None = None

    def _build_action_bar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x")

        self.adopt_var = tk.BooleanVar(value=False)
        self.force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Adopt hand-installed folders",
                        variable=self.adopt_var).pack(side="left")
        ttk.Checkbutton(bar, text="Apply despite problems",
                        variable=self.force_var).pack(side="left", padx=(10, 0))

        ttk.Button(bar, text="Launch Balatro",
                   command=self.launch).pack(side="right", padx=(6, 0))
        self.apply_button = ttk.Button(bar, text="Apply to game…",
                                       style="Accent.TButton",
                                       command=self.apply_profile)
        self.apply_button.pack(side="right")
        ttk.Button(bar, text="Preview changes",
                   command=self.preview).pack(side="right", padx=6)

    # -- Data --------------------------------------------------------------

    def reload(self, initial: bool = False) -> None:
        conn = db.connect()
        self.findings = graph.build(conn)
        conn.close()
        self.nodes = sorted(self.findings.nodes.values(),
                            key=lambda n: (n.title or n.mod_id).lower())

        categories = sorted({n.category for n in self.nodes if n.category})
        self.category_box.configure(values=("all", *categories))
        self.steamodded_box.configure(values=("(leave installed alone)",
                                              *self._steamodded_choices()))
        self.refresh_profile_list()
        self.read_live_state()

        if initial and self.profile is None and self.profile_box["values"]:
            self.profile_var.set(self.profile_box["values"][0])
            self.load_profile(self.profile_var.get())
        else:
            self.reresolve()
        self.log(f"vault: {len(self.nodes)} mods with a readable manifest")

    def _steamodded_choices(self) -> list[str]:
        """Every vaulted build of the loader, by folder name.

        Folder names, not mod ids; see `apply.find_in_vault`.
        """
        return pipeline.steamodded_folders()

    def open_setup(self) -> None:
        """Reopen the wizard: change the game or Mods folder, or download
        something skipped the first time. Reloads when it closes, since either
        can change what the window shows."""
        wizard.open_window(self, on_close=lambda: self.reload())

    def read_live_state(self) -> None:
        """What is actually in the mods folder right now."""
        self.live = {}
        mods_dir = config.game_mods()
        if not mods_dir.is_dir():
            return
        vault_root = config.vault()
        for entry in mods_dir.iterdir():
            if not entry.is_dir():
                continue
            if entry.name.lower() in config.PROTECTED_MOD_FOLDERS:
                continue
            self.live[entry.name.lower()] = links.describe(entry, vault_root)

    # -- Profiles ----------------------------------------------------------

    def refresh_profile_list(self) -> None:
        names = [p.slug for p in profiles.all_profiles()]
        self.profile_box.configure(values=names)
        if self.profile and self.profile.slug in names:
            self.profile_var.set(self.profile.slug)

    def load_profile(self, name: str) -> None:
        profile = profiles.load(name)
        if profile is None:
            return
        self.profile = profile
        self.members = set()
        self.unmatched = []
        for entry in profile.mods:
            node = resolve.find(self.findings, entry)
            if node is None:
                self.unmatched.append(entry)
            else:
                self.members.add(graph.norm_id(node.mod_id))
        self.steamodded_var.set(profile.steamodded or "(leave installed alone)")
        if self.unmatched:
            self.log(f"{profile.slug}: {len(self.unmatched)} entries match nothing "
                     f"in the vault -- kept as written, listed under Problems")
        self.reresolve()

    def new_profile(self) -> None:
        name = simpledialog.askstring("New profile", "Name:", parent=self)
        if name:
            self.new_profile_named(name)

    def new_profile_named(self, name: str) -> None:
        if profiles.load(name) is not None:
            messagebox.showerror("New profile", f"{name!r} already exists.",
                                 parent=self)
            return
        # A new profile starts empty, but keeps the Steamodded slot.
        profile = profiles.Profile(name=name)
        if self.profile is not None:
            profile.steamodded = self.profile.steamodded
        profile.save()
        self.refresh_profile_list()
        self.profile_var.set(profile.slug)
        self.load_profile(profile.slug)
        self.log(f"created profile {profile.slug}")

    def save_profile(self) -> None:
        if self.profile is None:
            return
        chosen = [n.mod_id for n in self.nodes
                  if graph.norm_id(n.mod_id) in self.members]
        # Unmatched entries go back verbatim and first.
        self.profile.mods = self.unmatched + sorted(chosen, key=str.lower)
        slot = self.steamodded_var.get()
        self.profile.steamodded = "" if slot.startswith("(") else slot
        path = self.profile.save()
        self.log(f"saved {path.name}: {len(self.profile.mods)} entries")
        self.set_status(f"Saved {self.profile.slug}")

    def delete_profile(self) -> None:
        if self.profile is None:
            return
        if not messagebox.askyesno(
            "Delete profile",
            f"Delete profile {self.profile.slug!r}?\n\n"
            "This removes the list only. No mods are deleted.",
            parent=self,
        ):
            return
        profiles.delete(self.profile.slug)
        self.log(f"deleted profile {self.profile.slug}")
        self.profile = None
        self.members, self.unmatched = set(), []
        self.refresh_profile_list()
        names = self.profile_box["values"]
        if names:
            self.profile_var.set(names[0])
            self.load_profile(names[0])
        else:
            self.profile_var.set("")
            self.reresolve()

    # -- Getting mods into the vault ---------------------------------------
    #
    # Each of these downloads exactly one thing. Bulk downloads live in Setup.

    def add_mod(self) -> None:
        """Index a mod from its GitHub URL, then fetch that one mod."""
        url = simpledialog.askstring(
            "Add a mod",
            "Paste its GitHub URL.\n\n"
            "The repo page, a release, or a tag archive — a tag in the URL is\n"
            "kept as the pin, otherwise the default branch is used.",
            parent=self)
        if not url:
            return
        try:
            fields = local_index.from_github_url(url)
        except ValueError as exc:
            messagebox.showerror("Add a mod", str(exc), parent=self)
            return

        name = simpledialog.askstring("Add a mod", "Name it:",
                                      initialvalue=fields["title"], parent=self)
        if not name:
            return
        try:
            folder = local_index.write_entry(
                name, fields["author"], repo=fields["repo"],
                download_url=fields["download_url"], version=fields["version"])
        except FileExistsError as path:
            messagebox.showerror(
                "Add a mod",
                f"{path} already exists.\nEdit it by hand, or delete it first.",
                parent=self)
            return

        self.book.select(self.log_tab)
        self.log(f"add: wrote local-index/{folder.name}")
        self._run_in_background(
            f"fetching {name}",
            lambda emit: self._sync_and_fetch(name, emit))

    def capture_installed(self) -> None:
        """Index the hand-installed mods no public source carries.

        Fixes "Not in the vault" for mods installed from somewhere like
        Discord. Each becomes a local-index entry sourced from its folder.
        """
        if not messagebox.askyesno(
            "Capture installed mods",
            f"Read {config.game_mods()} and write a local-index entry for "
            "every mod folder no source carries, sourced from the installed "
            "copy.\n\nThen fetch them into the vault.\n\n"
            "Reads the game folder. Writes only to local-index/ and the vault.",
            parent=self,
        ):
            return
        self.book.select(self.log_tab)
        self._run_in_background("capturing installed mods", self._capture_worker)

    def _capture_worker(self, emit) -> None:
        from .manifest import find_manifest

        mods_dir = config.game_mods()
        conn = db.connect()
        known = {normalise(r["title"]) for r in conn.execute("SELECT title FROM catalog")
                 if r["title"]}
        known |= {normalise(r["mod_id"])
                  for r in conn.execute("SELECT mod_id FROM vault WHERE mod_id != ''")}
        conn.close()

        written = 0
        for folder in sorted(p for p in mods_dir.iterdir() if p.is_dir()):
            if folder.name.lower() in config.PROTECTED_MOD_FOLDERS:
                continue
            manifest = find_manifest(folder)
            if manifest is None:
                continue
            names = {normalise(folder.name), normalise(manifest.mod_id),
                     normalise(manifest.name)} - {""}
            if names & known:
                continue
            author = str((manifest.authors or ["unknown"])[0]).split(",")[0].strip()
            try:
                local_index.write_entry(
                    manifest.name or manifest.mod_id or folder.name,
                    author or "unknown",
                    local_source=str(folder.resolve()),
                    version=manifest.version_raw or "",
                    description=f"Captured from the installed copy at {folder}.")
            except FileExistsError:
                continue
            written += 1
            emit(f"  + {folder.name} ({manifest.mod_id})")

        if not written:
            emit("capture: nothing new -- every installed mod is already known")
            return
        emit(f"capture: {written} entries written; syncing and fetching")
        self._sync_and_fetch(None, emit)

    def _sync_and_fetch(self, only: str | None, emit) -> None:
        """Refresh the catalog, then vault whatever is not vaulted yet.

        Syncs only local sources, so it never re-downloads the index and never
        prunes against a partial catalog.
        """
        from .pipeline import entry_from_row as _entry_from_row
        from .sources import collect
        from . import vault as vault_mod

        entries, failed = collect(log=emit, only="local")
        if failed:
            emit(f"sync: {', '.join(failed)} failed")
        conn = db.connect()
        with conn:
            for entry in entries:
                db.upsert_catalog(conn, entry)

        done = db.vaulted_keys(conn)
        queue = []
        for row in db.catalog_rows(conn):
            if row["key"] in done:
                continue
            if only and only.lower() not in (row["title"] or "").lower():
                continue
            queue.append(_entry_from_row(row))

        emit(f"fetch: {len(queue)} to download")
        for index, entry in enumerate(queue, 1):
            emit(f"[{index}/{len(queue)}] {entry['title']}")
            try:
                vault_row = vault_mod.fetch_one(entry, log=emit)
            except Exception as exc:
                emit(f"  ! {type(exc).__name__}: {exc}")
                continue
            with conn:
                db.upsert_vault(conn, vault_row)
            emit(f"  -> {vault_row['status']}  {vault_row['category']}")
        conn.close()

    def _run_in_background(self, what: str, work) -> None:
        """Run `work(emit)` off the UI thread, then reload the vault.

        Keeps network I/O off the UI thread.
        """
        self.set_status(f"{what}…")
        self.log(f"--- {what} ---")

        def runner() -> None:
            try:
                work(self._thread_log)
                self._events.put(("reload", what))
            except Exception as exc:
                self._events.put(("error", f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=runner, daemon=True).start()

    # -- Selection ---------------------------------------------------------

    def on_toggle(self) -> None:
        """Add or remove the selected mods, then re-resolve immediately.

        Turning Cryptid on shows Talisman and Amulet arrive right away.
        """
        if self.profile is None:
            messagebox.showinfo("No profile", "Create or select a profile first.",
                                parent=self)
            return
        for item in self.tree.selection():
            node = self._row_of.get(item)
            if node is None:
                continue
            key = self.norm(node.mod_id)
            if key in self.members:
                self.members.discard(key)
            else:
                self.members.add(key)
        self.reresolve()

    def reresolve(self) -> None:
        if self.profile is None:
            self.resolution = None
            self.refresh_table()
            self.render_problems()
            return
        wanted = [n.mod_id for n in self.nodes
                  if graph.norm_id(n.mod_id) in self.members]
        slot = self.steamodded_var.get()
        if slot and not slot.startswith("("):
            wanted.append(slot)
        self.resolution = resolve.resolve(self.findings, wanted)
        self.refresh_table()
        self.render_problems()
        self.update_status_line()

    def update_status_line(self) -> None:
        r = self.resolution
        if r is None:
            self.set_status("No profile selected")
            return
        trouble = (len(r.missing) + len(r.unsatisfiable) + len(r.conflicts)
                   + len(self.unmatched))
        state = "ready" if trouble == 0 else f"{trouble} problem(s)"
        self.set_status(
            f"{len(self.members)} chosen · {len(r.added)} pulled in · "
            f"{len(r.include)} total · {state}"
        )

    # -- Table -------------------------------------------------------------

    def refresh_table(self) -> None:
        selected = {self._row_of[i].mod_id for i in self.tree.selection()
                    if i in self._row_of}
        self.tree.delete(*self.tree.get_children())
        self._row_of.clear()

        term = self.search_var.get().strip().lower()
        category = self.category_var.get()
        show = self.show_var.get()
        included = set(self.resolution.include) if self.resolution else set()
        troubled = self._troubled_ids()

        shown = 0
        for node in self.nodes:
            key = graph.norm_id(node.mod_id)
            in_profile = key in self.members
            live_state = self.live.get(apply_mod.folder_for(node).lower(), "")

            if term and term not in f"{node.title}\n{node.mod_id}\n{node.key}".lower():
                continue
            if category != "all" and node.category != category:
                continue
            if show == "in profile" and not in_profile:
                continue
            if show == "not in profile" and in_profile:
                continue
            if show == "in game" and live_state not in ("ours", "real"):
                continue

            pulled = key in included and not in_profile
            tags = []
            if pulled:
                tags.append("dependency")
            if key in troubled:
                tags.append("problem")

            item = self.tree.insert("", "end", tags=tuple(tags), values=(
                "yes" if in_profile else ("dep" if pulled else ""),
                {"ours": "linked", "real": "by hand",
                 "foreign-link": "elsewhere"}.get(live_state, ""),
                node.title or node.mod_id,
                node.version_raw or "—",
                node.category,
            ))
            self._row_of[item] = node
            if node.mod_id in selected:
                self.tree.selection_add(item)
            shown += 1

        if shown == 0:
            self.tree.insert("", "end", values=("", "", "(nothing matches)", "", ""))

    def _troubled_ids(self) -> set[str]:
        """Mods involved in a problem, for the red highlight."""
        out: set[str] = set()
        r = self.resolution
        if r is None:
            return out
        for node, _ in r.missing:
            out.add(graph.norm_id(node.mod_id))
        for node, _, _ in r.unsatisfiable:
            out.add(graph.norm_id(node.mod_id))
        for a, b, _ in r.conflicts:
            out.add(graph.norm_id(a.mod_id))
            out.add(graph.norm_id(b.mod_id))
        return out

    # -- Panels ------------------------------------------------------------

    def show_details(self) -> None:
        items = self.tree.selection()
        if not items or items[0] not in self._row_of:
            return
        node = self._row_of[items[0]]
        write = self._writer(self.details)
        write(f"{node.title}\n", "head")
        write(f"{node.mod_id}  ·  {node.version_raw or 'no version'}\n", "dim")
        write(f"{node.category}\n\n", "dim")

        key = graph.norm_id(node.mod_id)
        if key in self.members:
            write("In this profile.\n", "good")
        elif self.resolution and key in self.resolution.include:
            write(f"Pulled in — {self.resolution.reasons.get(key, '')}.\n", "good")
        else:
            write("Not in this profile.\n", "dim")

        live_state = self.live.get(apply_mod.folder_for(node).lower(), "absent")
        write({
            "ours": "Currently linked into the game from the vault.\n",
            "real": "A hand-installed copy is in the game folder.\n",
            "foreign-link": "Linked into the game from outside the vault.\n",
            "absent": "Not in the game folder.\n",
        }.get(live_state, ""), "dim")

        if node.dependencies:
            write("\nRequires\n", "head")
            for dep in node.dependencies:
                met = dep.satisfied_by(self.findings.available) is not None
                names = [graph.norm_id(a.mod_id) for a in dep.alternatives]
                if all(n in graph.ENVIRONMENT for n in names):
                    write(f"  {dep}  (environment)\n", "dim")
                else:
                    write(f"  {dep}\n", "good" if met else "bad")
        if node.conflicts:
            write("\nConflicts with\n", "head")
            for conflict in node.conflicts:
                write(f"  {conflict}\n")
        if node.provides:
            write("\nProvides\n", "head")
            for provided in node.provides:
                write(f"  {provided}\n")

        facts = self._facts_for(node)
        lines = modinfo.adds_lines(facts.adds)
        if lines:
            # Counted from the mod's own Lua, not from an index description.
            write("\nAdds to the game\n", "head")
            write("  " + ", ".join(lines) + "\n", "good")
        if facts.size_bytes:
            shape = [modinfo.human_size(facts.size_bytes)]
            if facts.lua_files:
                shape.append(f"{facts.lua_files} lua")
            if facts.sprite_files:
                shape.append(f"{facts.sprite_files} sprites")
            if facts.lovely_patches:
                shape.append(f"{facts.lovely_patches} lovely patch(es)")
            write("  " + " · ".join(shape) + "\n", "dim")

        write(f"\n{node.vault_path or 'not in the vault'}\n", "dim")
        write(f"would install as: {apply_mod.folder_for(node)}\n", "dim")
        self._finish(self.details)
        self.show_page(node)

    # -- The Page tab ------------------------------------------------------

    def _facts_for(self, node: graph.Node) -> modinfo.Facts:
        if self._page_facts is not None and self._page_facts.key == node.key:
            return self._page_facts
        self._page_facts = modinfo.facts_for(node.key, node.vault_path)
        return self._page_facts

    def show_page(self, node: graph.Node) -> None:
        """Draw the header and whatever page we already have, immediately.

        No network here. An uncached README waits until the Page tab is open,
        so arrow-keying through the list doesn't burn the GitHub rate limit.
        """
        self._page_token += 1
        self._page_facts = None
        facts = self._facts_for(node)
        self._page_node = node
        self.page_title.configure(
            text=f"{facts.title or node.title}"
                 + (f"  ·  by {facts.author}" if facts.author else ""))

        if not facts.repo:
            self._draw_page_shell(node, facts)
            self._write_page_note(
                "No GitHub link for this mod.\n\n"
                "Its index entry didn't carry one — which is normal for the "
                "mods that arrived as Discord attachments. Right-click it in "
                "the list and pick “Set the GitHub URL…” to add one, and this "
                "tab will show its page from then on.", "dim")
            self.page_button.configure(text="Load page", state="disabled")
            return

        self.page_button.configure(state="normal")
        cached = modinfo.cached_page(facts.repo)
        if cached is not None and cached.markdown:
            blocks = modinfo.parse(cached.markdown, cached.base_url)
            # Cached screenshots sit beside the markdown.
            images = {}
            missing = []
            for block in blocks:
                if block.kind != "image" or block.url in images:
                    continue
                path = modinfo.cached_image_file(block.url)
                if path is not None:
                    images[block.url] = path
                elif block.url not in missing:
                    missing.append(block.url)
            self._render_page(node, facts, cached, blocks, images)
            self.page_button.configure(text="Refresh")
            if missing:
                # Retry only the missing images, not the README.
                threading.Thread(
                    target=self._image_worker,
                    args=(self._page_token, node, facts, cached, blocks,
                          images, tuple(missing)),
                    daemon=True).start()
            return

        self._draw_page_shell(node, facts)
        self._write_page_note(
            f"{facts.repo}\n\nNot downloaded yet — open this tab, or press "
            "Load page.", "dim")
        self.page_button.configure(text="Load page")
        self._page_pending = node.key
        self._page_tab_shown()

    def _page_tab_shown(self) -> None:
        """Fetch the page the moment the tab is actually looked at."""
        if getattr(self, "_page_pending", None) is None:
            return
        try:
            if self.book.select() != str(self.page_tab):
                return
        except tk.TclError:  # pragma: no cover -- during teardown
            return
        self._page_pending = None
        self.load_page()

    def load_page(self, refresh: bool = False) -> None:
        node = getattr(self, "_page_node", None)
        if node is None:
            return
        facts = self._facts_for(node)
        if not facts.repo:
            return
        self._page_pending = None
        self.page_button.configure(state="disabled")
        self._draw_page_shell(node, facts)
        self._write_page_note(f"Loading {facts.repo} …", "dim")

        token = self._page_token
        threading.Thread(target=self._page_worker,
                         args=(token, node, facts, refresh), daemon=True).start()

    def _page_worker(self, token: int, node, facts, refresh: bool) -> None:
        """Fetch and parse off the UI thread; images too, but not decoded.

        `PhotoImage` must be built on the Tk thread, so the worker only
        downloads and hands back paths.
        """
        try:
            page = modinfo.fetch_page(facts.repo, refresh=refresh)
            blocks = modinfo.parse(page.markdown, page.base_url)
            images = {}
            for block in blocks:
                if block.kind != "image" or block.url in images:
                    continue
                path = modinfo.image_file(block.url)
                if path is not None:
                    images[block.url] = path
            self._events.put(("page", (token, node, facts, page, blocks, images)))
        except Exception as exc:  # pragma: no cover -- surfaced in the pane
            self._events.put(("page-error", (token, f"{type(exc).__name__}: {exc}")))

    def _image_worker(self, token: int, node, facts, page, blocks, have,
                      missing) -> None:
        """Fill in screenshots a cached render didn't have, then redraw.

        Replaces the `[image]` placeholders once the bytes arrive.
        """
        images = dict(have)
        for url in missing:
            path = modinfo.image_file(url)
            if path is not None:
                images[url] = path
        if len(images) > len(have):
            self._events.put(("page", (token, node, facts, page, blocks, images)))

    def _draw_page_shell(self, node, facts: modinfo.Facts) -> None:
        """Title, what it adds, and where it came from. No network needed."""
        write = self._writer(self.page)
        self._page_images.clear()
        write(f"{facts.title or node.title}\n", "h1")
        subtitle = [node.mod_id, node.version_raw or "no version", node.category]
        write(" · ".join(x for x in subtitle if x) + "\n", "dim")

        lines = modinfo.adds_lines(facts.adds)
        if lines:
            write("\nAdds to the game\n", "h3")
            for line in lines:
                write(f"   {line}\n", "good")
        shape = []
        if facts.size_bytes:
            shape.append(modinfo.human_size(facts.size_bytes))
        if facts.lua_files:
            shape.append(f"{facts.lua_files} lua files")
        if facts.sprite_files:
            shape.append(f"{facts.sprite_files} images")
        if facts.lovely_patches:
            shape.append(f"{facts.lovely_patches} lovely patch(es)")
        if shape:
            write("   " + " · ".join(shape) + "\n", "dim")
        if facts.description:
            write("\n" + facts.description.strip() + "\n")
        write("\n")

    def _write_page_note(self, message: str, tag: str = "dim") -> None:
        self.page.configure(state="normal")
        self.page.insert("end", message + "\n", tag)
        self.page.configure(state="disabled")

    def _render_page(self, node, facts, page, blocks, images=None) -> None:
        """Draw the parsed README into the pane, screenshots and all."""
        self._draw_page_shell(node, facts)
        self.page.configure(state="normal")

        if page.error:
            self.page.insert("end", page.error + "\n\n", "bad")
        if not blocks:
            self.page.insert("end", "This README is empty.\n", "dim")

        self.page.insert("end", "─" * 40 + "\n", "rule")
        for block in blocks:
            self._render_block(block, images or {})
        stamp = time.strftime("%d %b %Y", time.localtime(page.fetched_at)) \
            if page.fetched_at else "unknown"
        self.page.insert("end", f"\n{page.html_url or facts.repo}\n", "dim")
        self.page.insert("end", f"page fetched {stamp}\n", "dim")
        self.page.configure(state="disabled")
        self.page.yview_moveto(0)

    def _render_block(self, block, images: dict) -> None:
        text = self.page
        if block.kind == "rule":
            text.insert("end", "─" * 40 + "\n", "rule")
            return
        if block.kind == "code":
            text.insert("end", block.text.rstrip() + "\n", "block")
            return
        if block.kind == "image":
            self._render_image(block, images)
            return

        prefix, tags = "", ()
        if block.kind == "head":
            tags = (f"h{min(block.level, 3)}",)
        elif block.kind == "quote":
            tags = ("quote",)
        elif block.kind == "table":
            tags = ("dim",)
        elif block.kind == "item":
            tags = ("item2",) if block.level else ("item",)
            prefix = f"{block.alt or '•'} "

        if prefix:
            text.insert("end", prefix, tags)
        for span in block.spans:
            self._insert_span(span, tags)
        text.insert("end", "\n", tags)

    def _insert_span(self, span, base_tags) -> None:
        text = self.page
        tags = tuple(base_tags)
        if span.style == "link" and span.href:
            # One tag per link, so each binding carries its own URL.
            name = f"url-{len(self._page_images)}-{text.index('end')}"
            text.tag_configure(name, foreground="#00a8fc", underline=True)
            text.tag_bind(name, "<Button-1>",
                          lambda _e, url=span.href: self._open_url(url))
            text.tag_bind(name, "<Enter>",
                          lambda _e: text.configure(cursor="hand2"))
            text.tag_bind(name, "<Leave>",
                          lambda _e: text.configure(cursor="arrow"))
            tags = tags + (name,)
        elif span.style:
            tags = tags + (span.style,)
        text.insert("end", span.text, tags)

    def _render_image(self, block, images: dict) -> None:
        """A screenshot, scaled to the pane. Pillow if it is there, Tk if not.

        Tk alone can only scale by whole-number steps, so Pillow gives a
        better fit when it is installed.
        """
        text = self.page
        path = images.get(block.url)
        if path is None:
            text.insert("end", f"[image] {block.alt or block.url}\n", "dim")
            return

        width = max(320, text.winfo_width() - 44)
        photo = None
        try:
            from PIL import Image, ImageTk

            with Image.open(path) as image:
                image.load()
                if image.mode not in ("RGB", "RGBA"):
                    image = image.convert("RGBA")
                if image.width > width:
                    height = round(image.height * width / image.width)
                    image = image.resize((width, height), Image.LANCZOS)
                photo = ImageTk.PhotoImage(image)
        except ImportError:
            try:
                photo = tk.PhotoImage(file=str(path))
                if photo.width() > width:
                    photo = photo.subsample(max(1, round(photo.width() / width)))
            except tk.TclError:
                photo = None
        except Exception:
            photo = None

        if photo is None:
            text.insert("end", f"[image] {block.alt or block.url}\n", "dim")
            return
        self._page_images.append(photo)   # Tk keeps no reference of its own
        text.image_create("end", image=photo, padx=2, pady=4)
        text.insert("end", "\n")
        if block.alt:
            text.insert("end", f"{block.alt}\n", "dim")

    def _open_url(self, url: str) -> None:
        if not url:
            return
        self.log(f"open: {url}")
        webbrowser.open(url)

    def open_repo(self) -> None:
        node = getattr(self, "_page_node", None)
        if node is None:
            messagebox.showinfo("Open on GitHub", "Select a mod first.",
                                parent=self)
            return
        facts = self._facts_for(node)
        if not facts.repo:
            messagebox.showinfo(
                "Open on GitHub",
                f"No GitHub URL is known for {node.title}.\n\n"
                "Right-click it in the list and pick “Set the GitHub URL…”.",
                parent=self)
            return
        self._open_url(facts.repo)

    def render_problems(self) -> None:
        write = self._writer(self.problems)
        r = self.resolution
        if r is None:
            write("No profile selected.\n", "dim")
            self._finish(self.problems)
            return

        # Lovely first: without version.dll the game loads no mods at all.
        lovely = apply_mod.lovely_status()
        if not lovely["present"]:
            write("Lovely is not installed\n", "head")
            write(f"{lovely['path']} is missing, so nothing here will load — "
                  "the game will start vanilla.\n\n", "bad")

        if self.unmatched:
            write(f"Not in the vault ({len(self.unmatched)})\n", "head")
            write("Kept in the profile exactly as written — nothing here is "
                  "lost by saving. If they are installed in the game already, "
                  "'Capture installed' vaults them from that copy; otherwise "
                  "'Add mod…' takes a GitHub URL.\n", "dim")
            for name in self.unmatched:
                write(f"  {name}\n", "bad")
            write("\n")

        for label, items, render in (
            ("Conflicts", r.conflicts,
             lambda c: f"{c[0].mod_id} vs {c[1].mod_id} — declared {c[2]}"),
            ("Wrong version", r.unsatisfiable,
             lambda u: f"{u[0].mod_id} needs {u[1]} — have {u[2]}"),
            ("Missing dependencies", r.missing,
             lambda m: f"{m[0].mod_id} needs {m[1]}"),
        ):
            if not items:
                continue
            write(f"{label} ({len(items)})\n", "head")
            for item in items:
                write(f"  {render(item)}\n", "bad")
            write("\n")

        if r.added:
            write(f"Pulled in as dependencies ({len(r.added)})\n", "head")
            for node in r.added:
                key = graph.norm_id(node.mod_id)
                write(f"  {node.title} — {r.reasons.get(key, '')}\n", "good")
            write("\n")

        if r.environment:
            # Not a problem, and labelled so; many manifests declare this.
            state = "and it is installed" if lovely["present"] else "and it is MISSING"
            write(f"Requires Lovely or the base game ({len(r.environment)})\n", "head")
            write(f"Nothing to do — these are not mods and are never vaulted. "
                  f"Lovely is the version.dll in the game folder, {state}.\n",
                  "good" if lovely["present"] else "bad")

        if r.ok and not self.unmatched and lovely["present"]:
            write("Nothing wrong with this set.\n", "good")
        self._finish(self.problems)

    def _writer(self, widget: tk.Text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")

        def write(text: str, *tags: str) -> None:
            widget.insert("end", text, tags)

        return write

    def _finish(self, widget: tk.Text) -> None:
        widget.configure(state="disabled")

    # -- Actions -----------------------------------------------------------

    def preview(self) -> None:
        plan = self._plan()
        if plan is None:
            return
        self.book.select(self.log_tab)
        self.log("--- preview ---")
        if not plan.changes and not plan.blocked:
            self.log("nothing to change; the game already matches this profile")
        for action in plan.changes:
            self.log(f"  {action}")
        for action in plan.blocked:
            self.log(f"  BLOCKED {action.folder}: {action.note}")
        self._log_still_loading(plan)

    def _log_still_loading(self, plan) -> None:
        """Name the hand-installed folders this profile will not switch off.

        `apply` never deletes a folder it did not create, so these keep
        loading whatever the profile says.
        """
        stuck = [a for a in plan.actions
                 if a.kind == "keep" and "hand-installed" in a.note]
        if not stuck:
            return
        self.log(f"  {len(stuck)} hand-installed folder(s) STILL LOAD "
                 f"— this profile cannot turn them off:")
        for action in stuck:
            self.log(f"      {action.folder}")
        self.log("  Adopt them once (apply the 'live' profile with adopt) and "
                 "they become links this manager can switch off.")

    def apply_profile(self) -> None:
        plan = self._plan()
        if plan is None:
            return
        r = self.resolution
        if r is not None and not r.ok and not self.force_var.get():
            messagebox.showerror(
                "Problems in this profile",
                "This set has missing mods, version conflicts or declared "
                "incompatibilities.\n\nSee the Problems tab. Tick "
                "'Apply despite problems' to go ahead anyway.",
                parent=self)
            return

        counts = plan.counts()
        summary = "\n".join(f"  {v} {k}" for k, v in sorted(counts.items()) if k != "keep")
        adopting = counts.get("adopt", 0)
        message = (
            f"Apply {self.profile.slug!r} to {config.game_mods()}?\n\n"
            f"{summary or '  nothing to change'}\n"
        )
        if adopting:
            message += (
                f"\n{adopting} hand-installed folder(s) will be MOVED to\n"
                f"{config.game_mods().parent / config.BACKUP_DIR_NAME}\n"
                "and replaced with links to the vault copy. Nothing is deleted."
            )
        if plan.blocked:
            # List skipped items by name, not just a count.
            message += "\n\nSKIPPED — these will NOT change:\n" + "\n".join(
                f"  {a.folder} — {a.note}" for a in plan.blocked)
        stuck = [a for a in plan.actions
                 if a.kind == "keep" and "hand-installed" in a.note]
        if stuck:
            message += (
                f"\n\n{len(stuck)} hand-installed folder(s) will STILL LOAD:\n"
                + ", ".join(a.folder for a in stuck)
                + "\n\nThis manager only switches off mods it installed."
            )
        if not messagebox.askyesno("Apply to game", message, parent=self):
            return

        self.book.select(self.log_tab)
        self.apply_button.configure(state="disabled")
        self.log("--- apply ---")
        # Off the UI thread, so the window never freezes mid-write.
        threading.Thread(target=self._apply_worker, args=(plan,), daemon=True).start()

    def _apply_worker(self, plan) -> None:
        try:
            stats = apply_mod.execute(plan, log=self._thread_log)
            self._events.put(("done", stats))
        except Exception as exc:  # pragma: no cover -- surfaced in the log pane
            self._events.put(("error", f"{type(exc).__name__}: {exc}"))

    def _thread_log(self, message: str) -> None:
        self._events.put(("log", message))

    def _drain_events(self) -> None:
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log(payload)
            elif kind == "error":
                self.log(payload)
                self.apply_button.configure(state="normal")
                messagebox.showerror("Apply failed", payload, parent=self)
            elif kind == "reload":
                # Re-read the profile: unmatched entries may match now.
                self.reload()
                if self.profile is not None:
                    self.load_profile(self.profile.slug)
                self.set_status(f"{payload}: done")
            elif kind == "page":
                token, node, facts, page, blocks, images = payload
                # Discarded if the selection moved on meanwhile.
                if token == self._page_token:
                    self._render_page(node, facts, page, blocks, images)
                    self.page_button.configure(text="Refresh", state="normal")
            elif kind == "page-error":
                token, message = payload
                if token == self._page_token:
                    self._write_page_note(message, "bad")
                    self.page_button.configure(state="normal")
            elif kind == "done":
                self.log("apply: {linked} linked, {unlinked} unlinked, "
                         "{adopted} adopted, {failed} failed".format(**payload))
                self.apply_button.configure(state="normal")
                self.read_live_state()
                self.refresh_table()
                self.update_status_line()
        self.after(150, self._drain_events)

    def _plan(self):
        if self.profile is None or self.resolution is None:
            messagebox.showinfo("No profile", "Create or select a profile first.",
                                parent=self)
            return None
        # Plan against what is on screen, not what was last saved.
        staged = profiles.Profile(name=self.profile.name)
        slot = self.steamodded_var.get()
        staged.steamodded = "" if slot.startswith("(") else slot
        return apply_mod.build_plan(self.resolution, staged,
                                    adopt=self.adopt_var.get())

    def launch(self) -> None:
        url = f"steam://rungameid/{config.STEAM_APPID}"
        self.log(f"launch: {url}")
        try:
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
        except OSError as exc:
            messagebox.showerror("Launch", str(exc), parent=self)

    # -- Misc --------------------------------------------------------------

    def norm(self, mod_id: str) -> str:
        """The one key everything here is stored under.

        One place, so every part of the window normalises ids the same way.
        """
        return graph.norm_id(mod_id)

    def vault_root(self) -> Path:
        return config.vault()

    def log(self, message: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", message + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")
        file_log(message)

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)


#: Any string, as long as it is ours and never changes. Windows keys taskbar
#: grouping and pinned shortcuts to it.
APP_ID = "BalatroModLoader.App"


def icon_path() -> Path:
    """`assets/bml.ico`, whether running from source or from the frozen exe.

    `config.BUNDLE`, which is PyInstaller's temp dir when frozen.
    """
    return config.BUNDLE / "assets" / "bml.ico"


def set_window_icon(window: tk.Misc) -> None:
    """The title bar and taskbar icon. Never fatal."""
    path = icon_path()
    if not path.is_file():
        return
    try:
        window.iconbitmap(default=str(path))
    except tk.TclError:  # pragma: no cover -- non-Windows Tk, or a bad .ico
        try:
            window.iconphoto(True, tk.PhotoImage(file=str(path.with_suffix(".png"))))
        except Exception:
            pass


def claim_taskbar_identity() -> None:
    """Tell Windows this process is its own app, not "Python".

    Without this the taskbar shows Python's icon and groups the window with
    python.exe.
    """
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:  # pragma: no cover -- not Windows, or an old shell32
        pass


def run() -> int:
    claim_taskbar_identity()   # before any window exists, or it does nothing
    root = tk.Tk()
    for option, value in theme.dialog_options().items():
        root.option_add(option, value)
    if wizard.needed() and not _first_run_setup(root):
        root.destroy()
        return 0
    App(root)
    root.mainloop()
    return 0


def _first_run_setup(root: tk.Tk) -> bool:
    """Run the wizard as the whole window. True if the user finished it.

    Uses the root window, so a first launch shows just the wizard.
    """
    try:
        theme.apply(root)
    except tk.TclError:
        pass
    root.title("Balatro Mod Loader setup")
    set_window_icon(root)
    root.geometry("720x620")
    root.minsize(640, 560)
    result = {"ok": False}

    def done(ok: bool) -> None:
        result["ok"] = ok
        page.destroy()
        root.quit()

    page = wizard.Wizard(root, done, first_run=True)
    page.pack(fill="both", expand=True)
    root.protocol("WM_DELETE_WINDOW", page.cancel)
    root.mainloop()
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    return result["ok"]


if __name__ == "__main__":
    raise SystemExit(run())
