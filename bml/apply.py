"""Turning a resolved profile into the live mods folder.

The only module that writes to the game. Everything becomes a `Plan` first,
so `apply --dry-run` and `apply` run the same logic. The mods folder may hold
hand-installed mods, so every entry is classified before anything happens:

    ours          a junction into the vault -- may be created and removed
    real          a directory of actual files -- never deleted, only adopted
                  (moved aside, intact) and only when explicitly asked
    foreign-link  a junction pointing somewhere else -- left alone, reported
    protected     `lovely` and friends -- never touched at all

Junctions are cheap to undo and real folders are not, so `--adopt` moves real
folders to `%APPDATA%\\Balatro\\Mods-bml-backups\\<timestamp>\\` instead.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config, links
from .graph import Node
from .resolve import Resolution

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class Action:
    kind: str  # link | relink | unlink | adopt | keep | blocked | lovely
    folder: str
    target: Path | None = None
    note: str = ""

    def __str__(self) -> str:
        arrow = f" -> {self.target}" if self.target else ""
        return f"{self.kind:<8} {self.folder}{arrow}" + (f"  ({self.note})" if self.note else "")


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    blocked: list[Action] = field(default_factory=list)

    @property
    def changes(self) -> list[Action]:
        return [a for a in self.actions if a.kind != "keep"]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for action in self.actions:
            out[action.kind] = out.get(action.kind, 0) + 1
        return out


def folder_for(node: Node) -> str:
    """The name this mod should have inside the mods folder.

    Index `folder_name` first (some mods find themselves by path), then the
    mod id, then the title.
    """
    for candidate in (node.folder_name, node.mod_id, node.title):
        cleaned = _safe(candidate)
        if cleaned:
            return cleaned
    return _safe(node.key) or "unnamed"


def _safe(text: str) -> str:
    cleaned = _UNSAFE.sub("-", str(text or "")).strip(" .")
    return re.sub(r"\s+", " ", cleaned)[:80]


def mod_dir(node: Node) -> Path | None:
    """The directory a junction should point at: `<vault>/<CAT>/<X>/mod`."""
    if not node.vault_path:
        return None
    path = Path(node.vault_path) / "mod"
    return path if path.is_dir() else None


def build_plan(resolution: Resolution, profile, adopt: bool = False) -> Plan:
    """Work out what would have to change, without changing anything."""
    plan = Plan()
    mods_dir = config.game_mods()
    vault_root = config.vault()

    existing = _installed_folder_names(mods_dir)
    # Use each folder's on-disk casing; Windows matches names case-insensitively.
    try:
        on_disk = {p.name.lower(): p.name for p in mods_dir.iterdir()}
    except OSError:
        on_disk = {}

    desired: dict[str, Path] = {}
    for key, node in sorted(resolution.include.items()):
        target = mod_dir(node)
        if target is None:
            plan.blocked.append(Action(
                "blocked", folder_for(node),
                note="no mod/ directory in the vault -- re-run fetch for it",
            ))
            continue
        name = existing.get(key) or folder_for(node)
        name = on_disk.get(name.lower(), name)
        if name in desired:
            # Two mods claiming one folder would silently enable only one.
            plan.blocked.append(Action(
                "blocked", name, target,
                note=f"folder name collides with another mod in this profile",
            ))
            continue
        desired[name] = target

    steam = _steamodded_target(resolution, profile)
    if steam is not None:
        name, target = steam
        desired[name] = target

    # -- what should exist -------------------------------------------------
    for name in sorted(desired):
        target = desired[name]
        link = mods_dir / name
        state = links.describe(link, vault_root)

        if state == "absent":
            plan.actions.append(Action("link", name, target))
        elif state == "ours":
            current = links.target_of(link)
            if current and _same_path(current, target):
                plan.actions.append(Action("keep", name, target, "already linked"))
            else:
                plan.actions.append(Action(
                    "relink", name, target, f"was pointing at {current}"))
        elif state == "real":
            if adopt:
                plan.actions.append(Action(
                    "adopt", name, target,
                    "hand-installed copy moved to the backup folder first"))
            else:
                plan.blocked.append(Action(
                    "blocked", name, target,
                    "a real folder is already here -- pass --adopt to move it "
                    "aside, or delete it yourself"))
        else:  # foreign-link or file
            plan.blocked.append(Action(
                "blocked", name, target,
                f"{state}: not ours to replace"))

    # -- what should stop existing ----------------------------------------
    if mods_dir.is_dir():
        # Compare case-insensitively, as Windows does.
        claimed = {name.lower() for name in desired}
        for entry in sorted(mods_dir.iterdir()):
            if entry.name.lower() in claimed or entry.name.lower() in config.PROTECTED_MOD_FOLDERS:
                continue
            state = links.describe(entry, vault_root)
            if state == "ours":
                if _is_loader(entry.name) and not getattr(profile, "steamodded", ""):
                    # An empty Steamodded slot means "leave the loader alone".
                    plan.actions.append(Action(
                        "keep", entry.name,
                        note="the loader; this profile names no Steamodded, "
                             "so it stays"))
                    continue
                plan.actions.append(Action(
                    "unlink", entry.name, note="not in this profile"))
            elif state == "real":
                # Left running, but reported, because the game still loads it.
                plan.actions.append(Action(
                    "keep", entry.name,
                    note="hand-installed, not managed here -- still loads"))

    return plan


def _installed_folder_names(mods_dir: Path) -> dict[str, str]:
    """Map mod id -> the folder that mod is *already* installed under, here.

    Hand-installed mods often use a different folder name than the catalog
    (`ACT` in `Actions`), and linking the catalog name would load the mod
    twice. Comes from the `installed` snapshot, and only for folders that
    still exist.
    """
    from . import db, graph

    out: dict[str, str] = {}
    try:
        conn = db.connect()
    except Exception:  # pragma: no cover -- no catalog yet
        return out
    try:
        rows = conn.execute("SELECT folder, mod_id FROM installed").fetchall()
    except Exception:  # pragma: no cover -- pre-`installed` database
        return out
    finally:
        conn.close()

    for row in rows:
        folder, mod_id = row["folder"], row["mod_id"]
        if not folder or not mod_id:
            continue
        if not (mods_dir / folder).exists():
            continue
        out[graph.norm_id(mod_id)] = folder
    return out


def _steamodded_target(resolution: Resolution, profile) -> tuple[str, Path] | None:
    """Resolve the profile's Steamodded slot to a folder and a vault path.

    An empty slot means "leave whatever is installed alone".
    """
    wanted = (getattr(profile, "steamodded", "") or "").strip()
    if not wanted:
        return None

    from . import graph

    # A slot naming a specific build must never fall back to the generic
    # `steamodded` node, which may be a different version.
    node = resolution.include.get(graph.norm_id(wanted))
    target = mod_dir(node) if node is not None else None
    if target is None:
        target = find_in_vault(wanted)
    if target is None:
        generic = resolution.include.get("steamodded")
        target = mod_dir(generic) if generic is not None else None
    return ("Steamodded", target) if target is not None else None


def find_in_vault(name: str) -> Path | None:
    """Find a vault folder by name, without going through the graph.

    The graph is keyed by mod id, so several Steamodded builds collapse into
    one node. Looking the folder up by name lets a profile pick between them.
    """
    root = config.vault()
    if not root.is_dir():
        return None
    wanted = _norm(name)
    candidates: list[Path] = []
    for category in sorted(root.iterdir()):
        if not category.is_dir():
            continue
        for folder in sorted(category.iterdir()):
            if not (folder / "mod").is_dir():
                continue
            if _norm(folder.name) == wanted:
                return folder / "mod"
            # Also match the name half of `Author@Name`.
            _, _, tail = folder.name.partition("@")
            if _norm(tail) == wanted or wanted in _norm(folder.name):
                candidates.append(folder / "mod")
    return candidates[0] if len(candidates) == 1 else None


def _norm(text: str) -> str:
    return re.sub(r"[\s_@-]+", "", (text or "").lower())


def _is_loader(folder: str) -> bool:
    return re.sub(r"[\s_-]+", "", folder.lower()) in config.LOADER_FOLDER_IDS


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a).lower() == str(b).lower()


# -- Execution -------------------------------------------------------------


def execute(plan: Plan, log=print) -> dict:
    """Carry out a plan. Blocked actions are never executed, only reported."""
    mods_dir = config.game_mods()
    vault_root = config.vault()
    mods_dir.mkdir(parents=True, exist_ok=True)
    stats = {"linked": 0, "unlinked": 0, "adopted": 0, "kept": 0, "failed": 0}
    backup_dir: Path | None = None

    for action in plan.actions:
        link = mods_dir / action.folder
        try:
            if action.kind == "keep":
                stats["kept"] += 1
            elif action.kind == "link":
                links.create(link, action.target)
                stats["linked"] += 1
                log(f"  + {action.folder}")
            elif action.kind == "relink":
                links.remove(link, vault_root)
                links.create(link, action.target)
                stats["linked"] += 1
                log(f"  ~ {action.folder} (repointed)")
            elif action.kind == "unlink":
                links.remove(link, vault_root)
                stats["unlinked"] += 1
                log(f"  - {action.folder}")
            elif action.kind == "adopt":
                if backup_dir is None:
                    backup_dir = _new_backup_dir()
                shutil.move(str(link), str(backup_dir / action.folder))
                links.create(link, action.target)
                stats["adopted"] += 1
                stats["linked"] += 1
                log(f"  * {action.folder} moved to {backup_dir.name}, then linked")
        except Exception as exc:
            # One failure must not stop the rest.
            stats["failed"] += 1
            log(f"  ! {action.folder}: {type(exc).__name__}: {exc}")

    for action in plan.blocked:
        log(f"  skipped {action.folder}: {action.note}")

    if backup_dir is not None:
        log(f"apply: hand-installed folders were moved to {backup_dir}")
    return stats


def _new_backup_dir() -> Path:
    root = config.game_mods().parent / config.BACKUP_DIR_NAME
    path = root / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path.mkdir(parents=True, exist_ok=True)
    return path


# -- Lovely ----------------------------------------------------------------


def lovely_status() -> dict:
    """What Lovely, if any, is injected into the game directory.

    Lovely has no readable version marker, so this reports size and hash.
    """
    dll = config.game_dir() / config.LOVELY_DLL
    if not dll.is_file():
        return {"present": False, "path": str(dll)}
    data = dll.read_bytes()
    return {
        "present": True,
        "path": str(dll),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest()[:16],
        "backups": sorted(
            p.name for p in dll.parent.glob(f"{config.LOVELY_DLL}{config.LOVELY_BACKUP_SUFFIX}*")
        ),
    }


def swap_lovely(source: Path, log=print, dry_run: bool = False) -> bool:
    """Replace the game's version.dll, keeping the old one beside it.

    Only one Lovely can be active at a time. The previous DLL is renamed, not
    deleted, so the swap is easy to undo.
    """
    dll = config.game_dir() / config.LOVELY_DLL
    if not source.is_file():
        log(f"lovely: {source} is not a file")
        return False
    if dll.is_file() and hashlib.sha256(dll.read_bytes()).hexdigest() == \
            hashlib.sha256(source.read_bytes()).hexdigest():
        log("lovely: already the installed build, nothing to do")
        return True
    if dry_run:
        log(f"lovely: would install {source} over {dll}")
        return True
    if dll.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = dll.with_name(f"{config.LOVELY_DLL}{config.LOVELY_BACKUP_SUFFIX}.{stamp}")
        dll.replace(backup)
        log(f"lovely: previous DLL kept as {backup.name}")
    shutil.copy2(source, dll)
    log(f"lovely: installed {source}")
    return True


def find_lovely_dll(node: Node) -> Path | None:
    """Locate a `version.dll` inside a vaulted Lovely release."""
    root = mod_dir(node)
    if root is None:
        return None
    direct = root / config.LOVELY_DLL
    if direct.is_file():
        return direct
    for found in sorted(root.rglob(config.LOVELY_DLL)):
        return found
    return None
