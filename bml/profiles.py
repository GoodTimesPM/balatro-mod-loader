"""Named sets of mods, stored as editable JSON.

A profile names mods by id and resolution fills in the dependencies, so it
survives the vault being re-fetched. Stored as `profiles/<slug>.json`, one
file each, so they are easy to read and edit by hand.

## The framework slots

`steamodded` and `lovely` are separate fields, not entries in `mods`, because
only one of each can be active at a time. The vault can hold several versions
and each profile picks one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config


@dataclass
class Profile:
    name: str
    description: str = ""
    #: Mods asked for by name. Dependencies are added at resolve time, not
    #: saved here, so dropping a mod also drops what only it needed.
    mods: list[str] = field(default_factory=list)
    #: Which vaulted Steamodded / Lovely to use. Empty means "leave whatever
    #: is installed alone", which is the safe default for an existing setup.
    steamodded: str = ""
    lovely: str = ""
    created: str = ""
    updated: str = ""

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def path(self) -> Path:
        return config.PROFILES / f"{self.slug}.json"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "mods": self.mods,
            "steamodded": self.steamodded,
            "lovely": self.lovely,
            "created": self.created,
            "updated": self.updated,
        }

    def save(self) -> Path:
        stamp = datetime.now().isoformat(timespec="seconds")
        self.created = self.created or stamp
        self.updated = stamp
        self.path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return self.path

    def add(self, name: str) -> bool:
        """Add a mod by whatever name the user typed. False if already there."""
        if any(_same(name, existing) for existing in self.mods):
            return False
        self.mods.append(name)
        return True

    def remove(self, name: str) -> bool:
        before = len(self.mods)
        self.mods = [m for m in self.mods if not _same(name, m)]
        return len(self.mods) != before


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-.")
    return (slug or "profile").lower()[:60]


def _same(a: str, b: str) -> bool:
    return re.sub(r"[\s_-]+", "", a.lower()) == re.sub(r"[\s_-]+", "", b.lower())


def load(name: str) -> Profile | None:
    path = config.PROFILES / f"{slugify(name)}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return Profile(
        name=data.get("name") or path.stem,
        description=data.get("description", ""),
        mods=[str(m) for m in data.get("mods") or []],
        steamodded=data.get("steamodded", ""),
        lovely=data.get("lovely", ""),
        created=data.get("created", ""),
        updated=data.get("updated", ""),
    )


def all_profiles() -> list[Profile]:
    out = []
    for path in sorted(config.PROFILES.glob("*.json")):
        profile = load(path.stem)
        if profile is not None:
            out.append(profile)
    return out


def delete(name: str) -> bool:
    path = config.PROFILES / f"{slugify(name)}.json"
    if not path.is_file():
        return False
    path.unlink()
    return True


def from_installed(conn, name: str, description: str = "") -> Profile:
    """Build a profile from the live mods folder's last snapshot.

    How existing hand-installed mods are kept. Folders are recorded by mod id
    where the manifest gave one and by folder name otherwise, so an unknown
    mod still lands in the profile and `apply` reports it.
    """
    profile = Profile(name=name, description=description)
    for row in conn.execute(
        "SELECT folder, mod_id FROM installed ORDER BY folder COLLATE NOCASE"
    ):
        folder = row["folder"]
        if folder.lower() in config.PROTECTED_MOD_FOLDERS:
            continue
        wanted = row["mod_id"] or folder
        # Steamodded goes in its slot, not the mod list.
        if wanted.strip().lower() in ("steamodded", "smods"):
            profile.steamodded = profile.steamodded or "Steamodded"
            continue
        profile.add(wanted)
    return profile
