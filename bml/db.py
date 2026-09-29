"""The catalog: SQLite, three tables, no ORM.

  catalog    what the sources say a mod is (metadata only, no download)
  vault      what we actually have on disk, including its real manifest
  installed  a read-only snapshot of %APPDATA%\\Balatro\\Mods

`catalog` is what a source claims; `vault` is what the downloaded manifest
says. Keeping them apart lets `report` show where the two disagree.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog (
    key                 TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    title               TEXT,
    author              TEXT,
    repo                TEXT,
    download_url        TEXT,
    declared_version    TEXT,
    categories          TEXT,
    description         TEXT,
    requires_steamodded INTEGER DEFAULT 0,
    requires_talisman   INTEGER DEFAULT 0,
    folder_name         TEXT,
    raw                 TEXT,
    first_seen          TEXT,
    last_seen           TEXT
);

CREATE TABLE IF NOT EXISTS vault (
    key             TEXT PRIMARY KEY,
    mod_id          TEXT,
    category        TEXT,
    category_reason TEXT,
    vault_path      TEXT,
    version         TEXT,
    sha256          TEXT,
    archive_name    TEXT,
    manifest        TEXT,
    smods_objects   TEXT,
    status          TEXT,
    note            TEXT,
    fetched_at      TEXT
);

CREATE TABLE IF NOT EXISTS installed (
    folder      TEXT PRIMARY KEY,
    mod_id      TEXT,
    version     TEXT,
    fmt         TEXT,
    manifest    TEXT,
    in_catalog  INTEGER DEFAULT 0,
    seen_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_catalog_source ON catalog(source);
CREATE INDEX IF NOT EXISTS idx_vault_category ON vault(category);
CREATE INDEX IF NOT EXISTS idx_vault_mod_id ON vault(mod_id);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def upsert_catalog(conn: sqlite3.Connection, entry: dict) -> None:
    """Insert or refresh one source entry, preserving its first_seen."""
    stamp = now()
    conn.execute(
        """
        INSERT INTO catalog (key, source, title, author, repo, download_url,
                             declared_version, categories, description,
                             requires_steamodded, requires_talisman,
                             folder_name, raw, first_seen, last_seen)
        VALUES (:key, :source, :title, :author, :repo, :download_url,
                :declared_version, :categories, :description,
                :requires_steamodded, :requires_talisman,
                :folder_name, :raw, :stamp, :stamp)
        ON CONFLICT(key) DO UPDATE SET
            source=excluded.source,
            title=excluded.title,
            author=excluded.author,
            repo=excluded.repo,
            download_url=excluded.download_url,
            declared_version=excluded.declared_version,
            categories=excluded.categories,
            description=excluded.description,
            requires_steamodded=excluded.requires_steamodded,
            requires_talisman=excluded.requires_talisman,
            folder_name=excluded.folder_name,
            raw=excluded.raw,
            last_seen=excluded.last_seen
        """,
        {
            "key": entry["key"],
            "source": entry.get("source", ""),
            "title": entry.get("title", ""),
            "author": entry.get("author", ""),
            "repo": entry.get("repo", ""),
            "download_url": entry.get("download_url", ""),
            "declared_version": entry.get("declared_version", ""),
            "categories": json.dumps(entry.get("categories") or []),
            "description": entry.get("description", ""),
            "requires_steamodded": int(bool(entry.get("requires_steamodded"))),
            "requires_talisman": int(bool(entry.get("requires_talisman"))),
            "folder_name": entry.get("folder_name", ""),
            "raw": json.dumps(entry.get("raw") or {}),
            "stamp": stamp,
        },
    )


def upsert_vault(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO vault (key, mod_id, category, category_reason, vault_path,
                           version, sha256, archive_name, manifest,
                           smods_objects, status, note, fetched_at)
        VALUES (:key, :mod_id, :category, :category_reason, :vault_path,
                :version, :sha256, :archive_name, :manifest,
                :smods_objects, :status, :note, :fetched_at)
        ON CONFLICT(key) DO UPDATE SET
            mod_id=excluded.mod_id,
            category=excluded.category,
            category_reason=excluded.category_reason,
            vault_path=excluded.vault_path,
            version=excluded.version,
            sha256=excluded.sha256,
            archive_name=excluded.archive_name,
            manifest=excluded.manifest,
            smods_objects=excluded.smods_objects,
            status=excluded.status,
            note=excluded.note,
            fetched_at=excluded.fetched_at
        """,
        {**{"note": "", "fetched_at": now()}, **row},
    )


def catalog_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM catalog ORDER BY title COLLATE NOCASE"))


def vault_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM vault ORDER BY category, mod_id"))


def vaulted_keys(conn: sqlite3.Connection) -> set[str]:
    """Keys already fetched successfully -- lets `fetch` resume cheaply."""
    return {
        row["key"]
        for row in conn.execute("SELECT key FROM vault WHERE status = 'ok'")
    }


def all_catalog_keys(conn: sqlite3.Connection) -> set[str]:
    return {row["key"] for row in conn.execute("SELECT key FROM catalog")}


def prune_catalog(conn: sqlite3.Connection, fresh: set[str], log=print) -> list[str]:
    """Drop catalog entries a full sync no longer produced, and their vaults.

    The vault folder goes too, so it can't shadow a live entry.
    """
    stale = sorted(all_catalog_keys(conn) - fresh)
    if not stale:
        return []

    for key in stale:
        row = conn.execute(
            "SELECT vault_path FROM vault WHERE key = ?", (key,)
        ).fetchone()
        path = Path(row["vault_path"]) if row and row["vault_path"] else None
        if path and path.is_dir():
            log(f"  pruning {key} -> removing {path}")
            shutil.rmtree(path, ignore_errors=True)
        else:
            log(f"  pruning {key}")
    with conn:
        conn.executemany("DELETE FROM catalog WHERE key = ?", [(k,) for k in stale])
        conn.executemany("DELETE FROM vault WHERE key = ?", [(k,) for k in stale])
    return stale


def counts_by_source(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row["source"]: row["n"]
        for row in conn.execute(
            "SELECT source, COUNT(*) AS n FROM catalog GROUP BY source"
        )
    }
