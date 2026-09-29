"""The four locations the setup wizard asks for, remembered between runs.

    %LOCALAPPDATA%\\Balatro Mod Loader\\settings.json

It lives at a fixed place because it records where the app folder is. Every
value is optional; a missing key falls through to the environment, then a
default. Imports nothing from the package, since `config` reads it early.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

#: What the wizard writes. `app_folder` holds the catalog, profiles and logs
#: (and the exe, when installed from one); `library` is the vault of downloaded
#: mods; `game_dir` is the folder with Balatro.exe in it; `game_mods` is the
#: folder the game loads mods from.
KEYS = ("app_folder", "library", "game_dir", "game_mods")


def path() -> Path:
    override = os.environ.get("BML_SETTINGS")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Balatro Mod Loader" / "settings.json"


#: (path, mtime) -> parsed, so hot loops don't re-read the file.
_cache: tuple[tuple[str, float], dict] | None = None


def load() -> dict:
    """The saved settings, or `{}`. A damaged file is treated as no file.

    The wizard then runs again.
    """
    global _cache
    target = path()
    try:
        stamp = (str(target), target.stat().st_mtime)
    except OSError:
        return {}
    if _cache is not None and _cache[0] == stamp:
        return dict(_cache[1])
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    data = data if isinstance(data, dict) else {}
    _cache = (stamp, data)
    return dict(data)


def save(values: dict) -> Path:
    global _cache
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    merged = {**load(), **{k: v if isinstance(v, bool) else str(v)
                           for k, v in values.items() if v is not None}}
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    tmp.replace(target)
    # Two saves inside one mtime tick would otherwise read back the first.
    _cache = None
    return target


def get(key: str) -> Path | None:
    value = load().get(key)
    return Path(value) if value else None


def setup_done() -> bool:
    return bool(load().get("setup_done"))
