"""Catalog sync, downloading into the library, and installing the two loaders.

Shared by the CLI and the setup wizard. Only `install_lovely` writes to the
game directory, and it keeps the file it replaces.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable

from . import apply as apply_mod
from . import config, db, net, vault
from .sources import collect

LOVELY_RELEASES = "https://api.github.com/repos/ethangreen-dev/lovely-injector/releases/latest"
STEAMODDED_KEY = "repo:steamodded/smods"

Progress = Callable[[int, int, str], None]


def entry_from_row(row) -> dict:
    raw = json.loads(row["raw"] or "{}")
    return {
        "key": row["key"],
        "source": row["source"],
        "title": row["title"],
        "author": row["author"],
        "repo": row["repo"],
        "download_url": row["download_url"],
        "declared_version": row["declared_version"],
        "categories": json.loads(row["categories"] or "[]"),
        "description": row["description"],
        "folder_name": row["folder_name"],
        "forced_category": raw.get("bmlCategory", ""),
        "local_source": raw.get("localSource", ""),
        "alternates": raw.get("_alternates", []),
        "also_in": raw.get("_also_in", []),
        "raw": raw,
    }


def sync(log=print, only: str | None = None) -> int:
    """Refresh the catalog from every source. Returns the number of mods in it,
    or -1 if every source failed. Downloads no mods."""
    entries, failed = collect(log=log, only=only)
    if not entries:
        log("sync: no entries collected -- every source failed or was empty")
        return -1

    conn = db.connect()
    with conn:
        for entry in entries:
            db.upsert_catalog(conn, entry)

    # Prune delisted entries, but only when every source answered.
    fresh = {entry["key"] for entry in entries}
    if failed or only:
        why = f"source(s) failed: {', '.join(failed)}" if failed else "--source given"
        stale = [k for k in db.all_catalog_keys(conn) if k not in fresh]
        if stale:
            log(f"sync: {len(stale)} catalog entries not seen this run, "
                f"leaving them alone ({why})")
    else:
        removed = db.prune_catalog(conn, fresh, log=log)
        if removed:
            log(f"sync: pruned {len(removed)} catalog entries that no longer exist")

    counts = db.counts_by_source(conn)
    total = sum(counts.values())
    log(f"sync: catalog now holds {total} mods -- " +
        ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())))
    conn.close()
    return total


def fetch(log=print, *, mod: str | None = None, keys: set[str] | None = None,
          category: str | None = None, limit: int | None = None,
          force: bool = False, stop: threading.Event | None = None,
          progress: Progress | None = None) -> dict:
    """Download catalog entries into the library. Returns status counts.

    `mod` is a substring of title or key, for the CLI; `keys` is an exact set,
    for callers that know precisely what they want. `stop` is checked between
    mods, never mid-download, so stopping leaves no half-written folder.
    """
    conn = db.connect()
    rows = db.catalog_rows(conn)
    stats = {"ok": 0, "no_manifest": 0, "failed": 0, "queued": 0, "stopped": False}
    if not rows:
        log("fetch: catalog is empty -- run `sync` first")
        conn.close()
        return stats

    done = set() if force else db.vaulted_keys(conn)
    queue = []
    for row in rows:
        entry = entry_from_row(row)
        if keys is not None and entry["key"] not in keys:
            continue
        # Substring match, since nobody types a full repo-slug key.
        if mod and mod.lower() not in (
            f"{(row['title'] or '').lower()}\n{(row['key'] or '').lower()}"
        ):
            continue
        if category and category.lower() not in [
            c.lower() for c in entry["categories"]
        ]:
            continue
        if entry["key"] in done:
            continue
        queue.append(entry)

    if limit:
        queue = queue[:limit]
    stats["queued"] = len(queue)
    if not queue:
        log("fetch: nothing to do (everything selected is already in the library)")
        conn.close()
        return stats

    log(f"fetch: {len(queue)} mods to download into {config.vault()}")
    for index, entry in enumerate(queue, 1):
        if stop is not None and stop.is_set():
            stats["stopped"] = True
            log(f"fetch: stopped after {index - 1} of {len(queue)}")
            break
        log(f"[{index}/{len(queue)}] {entry['title']}")
        if progress:
            progress(index, len(queue), entry["title"] or entry["key"])
        try:
            row = vault.fetch_one(entry, log=log, force=force)
        except Exception as exc:  # never let one mod kill the run
            log(f"  ! unexpected: {type(exc).__name__}: {exc}")
            row = {
                "key": entry["key"], "mod_id": "", "category": "",
                "category_reason": "", "vault_path": "", "version": "",
                "sha256": "", "archive_name": "", "manifest": "{}",
                "smods_objects": "{}", "status": "failed",
                "note": f"{type(exc).__name__}: {exc}", "fetched_at": db.now(),
            }
        stats[row["status"]] = stats.get(row["status"], 0) + 1
        with conn:
            db.upsert_vault(conn, row)
        if row["status"] != "failed":
            log(f"  -> {row['category']}  ({row['category_reason']})")
        time.sleep(config.PAUSE_BETWEEN_DOWNLOADS)

    log(f"fetch: done -- {stats.get('ok', 0)} ok, "
        f"{stats.get('no_manifest', 0)} without a manifest, "
        f"{stats.get('failed', 0)} failed")
    conn.close()
    return stats


# -- The two loaders ---------------------------------------------------------


def steamodded_folders() -> list[str]:
    """Every Steamodded build in the library, by folder name -- the values a
    profile's `steamodded` slot takes."""
    root = config.vault() / "MODLOADERS"
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir()
        if (p / "mod").is_dir() and "steamodded" in p.name.lower().replace(" ", "")
    )


def ensure_steamodded(log=print) -> str | None:
    """Put Steamodded in the library if it is not there. Returns its folder.

    Syncs the catalog first, but downloads only Steamodded.
    """
    have = steamodded_folders()
    if have:
        return have[-1]
    conn = db.connect()
    known = STEAMODDED_KEY in db.all_catalog_keys(conn)
    conn.close()
    if not known and sync(log=log, only="index") < 0:
        return None
    fetch(log=log, keys={STEAMODDED_KEY})
    have = steamodded_folders()
    return have[-1] if have else None


def install_lovely(log=print) -> bool:
    """Download the newest Lovely release and put its version.dll in the game.

    Without Lovely the game ignores the mods folder. Any DLL it replaces is
    renamed, not deleted (see `apply.swap_lovely`).
    """
    game = config.game_dir()
    if not (game / "Balatro.exe").is_file():
        log(f"lovely: Balatro.exe is not in {game}")
        return False
    release = net.get_json(LOVELY_RELEASES)
    asset = next((a for a in release.get("assets", [])
                  if "windows" in a["name"].lower() and a["name"].endswith(".zip")),
                 None)
    if asset is None:
        log(f"lovely: release {release.get('tag_name')} has no Windows download")
        return False
    with tempfile.TemporaryDirectory(prefix="bml-lovely-") as tmp:
        archive = Path(tmp) / asset["name"]
        net.download(asset["browser_download_url"], archive)
        with zipfile.ZipFile(archive) as zf:
            member = next((n for n in zf.namelist()
                           if n.rsplit("/", 1)[-1].lower() == config.LOVELY_DLL), None)
            if member is None:
                log(f"lovely: {asset['name']} has no {config.LOVELY_DLL} in it")
                return False
            dll = Path(tmp) / config.LOVELY_DLL
            with zf.open(member) as src, dll.open("wb") as out:
                shutil.copyfileobj(src, out)
        log(f"lovely: {release.get('tag_name')} downloaded")
        return apply_mod.swap_lovely(dll, log=log)
