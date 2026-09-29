"""Paths, .env loading, and the constants that tune a run.

Every machine path is looked up in the same order: an environment variable
(from the shell or `.env`), then what the setup wizard saved (`settings.py`),
then a default that is right for an ordinary Steam install.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import settings

# Where the code is. In the exe this is a temp dir, so only read-only assets.
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def _resolve_root() -> Path:
    """Where the *data* is: vault index, catalog, profiles, .env, logs.

    Not `BUNDLE`, which is a temp dir inside the exe. In order: `BML_ROOT`,
    the wizard's app folder, a `data/catalog.db` next to the exe, the source
    tree recorded in `bml_home.txt`, then the default app folder. From source,
    the project folder unless the wizard chose another.
    """
    source = Path(__file__).resolve().parent.parent
    chosen = settings.get("app_folder")
    if not getattr(sys, "frozen", False):
        return chosen or source

    override = os.environ.get("BML_ROOT")
    if override:
        return Path(override)
    if chosen:
        return chosen

    beside_exe = Path(sys.executable).resolve().parent
    if (beside_exe / "data" / "catalog.db").is_file():
        return beside_exe

    home_file = BUNDLE / "bml_home.txt"
    if home_file.is_file():
        recorded = Path(home_file.read_text(encoding="utf-8").strip())
        if recorded.is_dir():
            return recorded

    return default_app_folder()


def default_app_folder() -> Path:
    """Where a per-user Windows app installs itself, as VS Code and Discord do."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / "Balatro Mod Loader"


def set_root(root: Path) -> None:
    """Point every data path at a new app folder and create its folders.

    Called by the setup wizard. `modinfo.rebase()` moves its own cache paths.
    """
    global ROOT, DATA, LOGS, CACHE, REPORTS, LOCAL_INDEX, PROFILES, DB_PATH
    ROOT = Path(root)
    DATA = ROOT / "data"
    LOGS = ROOT / "logs"
    CACHE = DATA / "cache"
    REPORTS = DATA / "reports"
    LOCAL_INDEX = ROOT / "local-index"
    PROFILES = ROOT / "profiles"
    DB_PATH = DATA / "catalog.db"
    for folder in (DATA, LOGS, CACHE, REPORTS, LOCAL_INDEX, PROFILES):
        folder.mkdir(parents=True, exist_ok=True)


set_root(_resolve_root())


def load_env() -> None:
    """Minimal .env reader -- avoids a python-dotenv dependency.

    Existing environment variables win. utf-8-sig skips a Notepad BOM.
    """
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# -- Machine paths ---------------------------------------------------------
#
# Environment variable, then the wizard's choice, then a default. Never cached.


def vault() -> Path:
    """The mod library: every downloaded mod, one folder each."""
    override = os.environ.get("BML_VAULT")
    if override:
        return Path(override)
    return settings.get("library") or default_library()


def default_library() -> Path:
    return ROOT / "Mod Library"


def game_mods() -> Path:
    """`%APPDATA%\\Balatro\\Mods` -- the live folder the game actually loads."""
    override = os.environ.get("BML_GAME_MODS")
    if override:
        return Path(override)
    return settings.get("game_mods") or default_game_mods()


def default_game_mods() -> Path:
    """Where Steamodded looks. Set by the loader, not by us."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Balatro" / "Mods"
    return Path.home() / "AppData" / "Roaming" / "Balatro" / "Mods"


def game_dir() -> Path:
    """The folder with Balatro.exe in it."""
    override = os.environ.get("BML_GAME_DIR")
    if override:
        return Path(override)
    chosen = settings.get("game_dir")
    if chosen:
        return chosen
    from . import steam  # a registry read, paid only when nothing is saved

    found = steam.find_balatro()
    return found.parent if found else steam.USUAL_GAME_DIR


def github_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    return token or None


# -- The vault taxonomy ----------------------------------------------------
#
# _UNSORTED holds anything the categoriser can't place with confidence.

CATEGORIES = (
    "CHALLENGES",
    "COLLECTIONS",
    "DECKS",
    "JOKERS+",
    "MODLOADERS",
    "MODPACKS",
    "SKINS",
    "SUITS-CARDS-VOUCHERS-SEALS-CONSUMABLES",
    "UTILITY+QOL",
)

UNSORTED = "_UNSORTED"

# -- Source endpoints ------------------------------------------------------

INDEX_REPO = "skyline69/balatro-mod-index"
INDEX_TARBALL = f"https://codeload.github.com/{INDEX_REPO}/tar.gz/refs/heads/main"
THUNDERSTORE_API = "https://thunderstore.io/c/balatro/api/v1/package/"

# -- Run tuning ------------------------------------------------------------

# Be polite to GitHub and Thunderstore during a long fetch.
REQUEST_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 300
PAUSE_BETWEEN_DOWNLOADS = 0.5

# Anything this big is a wrong download URL, not a mod.
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024

# The three mods that are infrastructure rather than content. They always
# categorise to MODLOADERS and the graph treats them as roots.
FRAMEWORK_IDS = ("Steamodded", "Lovely", "Talisman")

# -- Applying a profile to the game ----------------------------------------

STEAM_APPID = "2379780"

# Lovely is version.dll in the game folder, so installing it is a file swap.
LOVELY_DLL = "version.dll"
LOVELY_BACKUP_SUFFIX = ".bml-bak"

# Names in %APPDATA%\Balatro\Mods that are never a mod and must never be
# touched. `lovely` holds the injector's logs.
PROTECTED_MOD_FOLDERS = {"lovely", "lovely-dump"}

# Folder names that are Steamodded itself. Unlike FRAMEWORK_IDS, no Talisman.
LOADER_FOLDER_IDS = {"steamodded", "smods"}

# Where `apply --adopt` moves a hand-installed mod folder it is displacing.
# Outside Mods, because Steamodded would load anything left inside it.
BACKUP_DIR_NAME = "Mods-bml-backups"
