"""Find Balatro.exe without asking.

Steam records where it is installed in the registry, and every extra library
folder (a second drive, usually) in `steamapps/libraryfolders.vdf`. A game
lives at `<library>/steamapps/common/<installdir>`, and Balatro's installdir is
`Balatro`. That covers nearly everyone, and the setup wizard only has to ask
when it does not.
"""

from __future__ import annotations

import os
import re
import string
from pathlib import Path

EXE_NAME = "Balatro.exe"

#: The default Steam location. Shown when nothing is found, so the Browse
#: dialog opens somewhere plausible.
USUAL_GAME_DIR = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Balatro")


def steam_roots() -> list[Path]:
    """Steam's own install folder, from the registry. Usually one."""
    roots: list[Path] = []
    try:
        import winreg
    except ImportError:  # not Windows
        return roots
    keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ]
    for hive, sub, name in keys:
        try:
            with winreg.OpenKey(hive, sub) as key:
                value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        path = Path(str(value).replace("/", "\\"))
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def library_folders(steam_root: Path) -> list[Path]:
    """Every library Steam knows about, the main install included.

    Only the `"path"` lines of `libraryfolders.vdf` matter, so a regex will do.
    """
    folders = [steam_root]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return folders
    for raw in re.findall(r'"path"\s+"([^"]+)"', text):
        path = Path(raw.replace("\\\\", "\\"))
        if path not in folders:
            folders.append(path)
    return folders


def candidates() -> list[Path]:
    """Every place Balatro.exe might be, most likely first."""
    found: list[Path] = []
    for root in steam_roots():
        for library in library_folders(root):
            found.append(library / "steamapps" / "common" / "Balatro" / EXE_NAME)
    # No Steam in the registry (a portable Steam, or a fresh Windows install
    # with the old library on another drive): try the usual folder names.
    for drive in _drives():
        for rel in (r"SteamLibrary\steamapps\common\Balatro",
                    r"Steam\steamapps\common\Balatro",
                    r"Program Files (x86)\Steam\steamapps\common\Balatro",
                    r"Program Files\Steam\steamapps\common\Balatro",
                    r"Games\Steam\steamapps\common\Balatro"):
            found.append(Path(f"{drive}:\\") / rel / EXE_NAME)
    seen: set[str] = set()
    unique = []
    for path in found:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_balatro() -> Path | None:
    for path in candidates():
        if path.is_file():
            return path
    return None


def exe_in(path: Path) -> Path | None:
    """Balatro.exe for whatever the user pointed at: the exe or its folder."""
    path = Path(path)
    if path.is_file() and path.name.lower() == EXE_NAME.lower():
        return path
    if path.is_dir():
        for child in path.iterdir():
            if child.name.lower() == EXE_NAME.lower() and child.is_file():
                return child
    return None


def _drives() -> list[str]:
    if os.name != "nt":
        return []
    return [d for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
