"""Finding and parsing a mod's real manifest.

Balatro mods declare themselves in at least three ways, under arbitrary
filenames:

  * Steamodded JSON under a name of the author's choosing --
    `CardSleeves.json`, `smods.json`, `smods_metadata.json`, `manifest.json`,
    and so on.
  * A legacy `--- STEAMODDED HEADER` comment block in some `.lua` file.
  * A Thunderstore `manifest.json`, a different schema that some mods ship
    alongside their real Lua header.

So manifests are identified by their keys, never by filename:

    Steamodded   has `id` and at least one of `main_file` / `prefix`
    Thunderstore has `name` and `version_number`, no `id`
    anything else is not a manifest

Search runs to depth 2 and prefers the shallowest hit, which handles nested
folders and `<Repo>-main/` wrappers. Several distinct manifests means a modpack.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .version import Dependency, Version, parse_dependencies, parse_version

_HEADER_MARKER = "--- STEAMODDED HEADER"

# The header sits at the top of the file by contract, so there is no reason to
# read more than the first chunk of any .lua to decide.
_HEADER_PROBE_BYTES = 4096
_HEADER_PROBE_LINES = 6

# `--- KEY: value`
_HEADER_LINE_RE = re.compile(r"^---\s*([A-Z_]+)\s*:\s*(.*)$")

# Thunderstore dependency strings: `Owner-Package-1.2.3`
_TS_DEP_RE = re.compile(r"^(?P<owner>[^-]+)-(?P<package>.+)-(?P<version>\d+[\d.]*)$")

# Guard rails for pathological mods (Cryptid ships hundreds of Lua files).
_MAX_SCAN_FILES = 3000
_MAX_SCAN_BYTES = 2 * 1024 * 1024


@dataclass
class ModManifest:
    """One mod's declared identity and requirements, normalised."""

    mod_id: str
    name: str = ""
    display_name: str = ""
    authors: list[str] = field(default_factory=list)
    version_raw: str = ""
    version: Version | None = None
    prefix: str = ""
    main_file: str = ""
    priority: int = 0
    description: str = ""
    badge_colour: str = ""

    dependencies: list[Dependency] = field(default_factory=list)
    conflicts: list[Dependency] = field(default_factory=list)
    provides: list[Dependency] = field(default_factory=list)

    # Where this came from and how it was read.
    source_file: str = ""
    fmt: str = ""  # "json" | "legacy" | "thunderstore"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mod_id": self.mod_id,
            "name": self.name,
            "display_name": self.display_name,
            "authors": self.authors,
            "version": self.version_raw,
            "prefix": self.prefix,
            "main_file": self.main_file,
            "priority": self.priority,
            "description": self.description,
            "badge_colour": self.badge_colour,
            "dependencies": [d.raw for d in self.dependencies],
            "conflicts": [d.raw for d in self.conflicts],
            "provides": [d.raw for d in self.provides],
            "source_file": self.source_file,
            "format": self.fmt,
            "warnings": self.warnings,
        }


# -- Discovery -------------------------------------------------------------


def find_manifest(mod_dir: Path) -> ModManifest | None:
    """Best manifest for `mod_dir`, or None if it declares nothing.

    Steamodded JSON beats a legacy header when a mod ships both, matching
    Steamodded's own precedence. A disagreement between them becomes a warning.
    """
    found = all_manifests(mod_dir)

    # Framework detection is a fallback: some mods bundle a `src/loader.lua`.
    if not found or found[0].fmt == "thunderstore":
        framework = read_framework(mod_dir)
        if framework is not None:
            return framework
    if not found:
        return None

    primary = found[0]
    legacy = next((m for m in found if m.fmt == "legacy"), None)
    if primary.fmt == "json" and legacy is not None:
        if legacy.mod_id and legacy.mod_id != primary.mod_id:
            primary.warnings.append(
                f"legacy header declares id {legacy.mod_id!r}, JSON declares {primary.mod_id!r}"
            )
        if legacy.version_raw and legacy.version_raw != primary.version_raw:
            primary.warnings.append(
                f"legacy header version {legacy.version_raw!r} != JSON version {primary.version_raw!r}"
            )
        # A stale legacy header still loads if left in place, so it is worth
        # surfacing even when the values agree.
        primary.warnings.append(f"also ships a legacy header at {legacy.source_file}")

    # A Thunderstore manifest is never authoritative, but its dependency list
    # is real information -- fold in anything the primary didn't already say.
    if primary.fmt != "thunderstore":
        ts = next((m for m in found if m.fmt == "thunderstore"), None)
        if ts is not None:
            known = {_dep_key(d) for d in primary.dependencies}
            for dep in ts.dependencies:
                if _dep_key(dep) not in known:
                    primary.dependencies.append(dep)
            if not primary.version_raw and ts.version_raw:
                primary.version_raw = ts.version_raw
                primary.version = ts.version
    return primary


def all_manifests(mod_dir: Path) -> list[ModManifest]:
    """Every manifest under `mod_dir`, best first.

    Ordering is (Steamodded JSON, legacy header, Thunderstore) and then by
    depth, so `[0]` is the mod's own manifest and any extras are bundled
    sub-mods. A list with several distinct Steamodded ids means a modpack.
    """
    mod_dir = Path(mod_dir)
    if not mod_dir.is_dir():
        return []

    out: list[tuple[int, int, ModManifest] ] = []
    for path, depth in _candidate_files(mod_dir):
        manifest = None
        if path.suffix.lower() == ".json":
            manifest = _read_json_manifest(path)
        elif path.suffix.lower() == ".lua":
            manifest = _read_legacy_manifest(path)
        if manifest is None:
            continue
        manifest.source_file = str(path.relative_to(mod_dir)).replace("\\", "/")
        rank = {"json": 0, "legacy": 1, "thunderstore": 2}.get(manifest.fmt, 3)
        out.append((rank, depth, manifest))

    # A Thunderstore manifest is never primary, at any depth.
    out.sort(key=lambda item: (1 if item[0] == 2 else 0, item[1], item[0]))
    return [m for _, _, m in out]


def _candidate_files(mod_dir: Path):
    """Yield (path, depth) for every .json/.lua worth probing, depth <= 2."""
    seen = 0
    stack: list[tuple[Path, int]] = [(mod_dir, 0)]
    while stack:
        current, depth = stack.pop(0)
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for entry in entries:
            if seen >= _MAX_SCAN_FILES:
                return
            if entry.is_dir():
                if depth < 2 and not entry.name.startswith("."):
                    stack.append((entry, depth + 1))
                continue
            if entry.suffix.lower() in (".json", ".lua"):
                seen += 1
                yield entry, depth


def _dep_key(dep: Dependency) -> str:
    return "|".join(sorted(a.mod_id.lower() for a in dep.alternatives))


# -- JSON ------------------------------------------------------------------


def classify_json(data) -> str | None:
    """Which schema is this JSON object? Keys only -- never the filename."""
    if not isinstance(data, dict):
        return None
    if data.get("id") and (data.get("main_file") or data.get("prefix")):
        return "json"
    if data.get("name") and data.get("version_number") and not data.get("id"):
        return "thunderstore"
    return None


def _read_json_manifest(path: Path) -> ModManifest | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None

    # utf-8-sig handles a BOM; cp1252 catches older Windows-written files.
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None

    try:
        data = json.loads(text)
    except ValueError:
        data = _loads_lenient(text)
    if data is None:
        return None

    kind = classify_json(data)
    if kind == "json":
        return _from_steamodded_json(data)
    if kind == "thunderstore":
        return _from_thunderstore_json(data)
    return None


def _loads_lenient(text: str):
    """Parse a manifest strict JSON rejects but Steamodded accepts.

    Steamodded's reader is lenient, so manifests often have trailing commas
    and `//` comments. Both are stripped outside of strings.
    """
    out = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
        elif char == "/" and index + 1 < len(text) and text[index + 1] == "/":
            while index < len(text) and text[index] != "\n":
                index += 1
            continue
        elif char == ",":
            # Look ahead past whitespace: a comma before a closer is trailing.
            look = index + 1
            while look < len(text) and text[look] in " \t\r\n":
                look += 1
            if look < len(text) and text[look] in "}]":
                index += 1
                continue
            out.append(char)
        else:
            out.append(char)
        index += 1

    try:
        return json.loads("".join(out))
    except ValueError:
        return None


def _from_steamodded_json(data: dict) -> ModManifest:
    version_raw = str(data.get("version") or "")
    return ModManifest(
        mod_id=str(data.get("id") or "").strip(),
        name=str(data.get("name") or "").strip(),
        display_name=str(data.get("display_name") or "").strip(),
        authors=_as_list(data.get("author")),
        version_raw=version_raw,
        version=parse_version(version_raw),
        prefix=str(data.get("prefix") or "").strip(),
        main_file=str(data.get("main_file") or "").strip(),
        priority=_as_int(data.get("priority")),
        description=str(data.get("description") or "").strip(),
        badge_colour=str(data.get("badge_colour") or "").strip(),
        dependencies=parse_dependencies(data.get("dependencies")),
        conflicts=parse_dependencies(data.get("conflicts")),
        provides=parse_dependencies(data.get("provides")),
        fmt="json",
    )


def _from_thunderstore_json(data: dict) -> ModManifest:
    """Thunderstore's schema. Useful, but never authoritative for Steamodded."""
    version_raw = str(data.get("version_number") or "")
    deps = []
    for item in data.get("dependencies") or []:
        converted = thunderstore_dependency(str(item))
        if converted:
            deps.extend(parse_dependencies([converted]))
    return ModManifest(
        mod_id=str(data.get("name") or "").strip(),
        name=str(data.get("name") or "").strip(),
        version_raw=version_raw,
        version=parse_version(version_raw),
        description=str(data.get("description") or "").strip(),
        dependencies=deps,
        fmt="thunderstore",
    )


def thunderstore_dependency(text: str) -> str | None:
    """`Thunderstore-lovely-0.7.1` -> `lovely (>=0.7.1)`.

    Thunderstore pins an exact build, but treating that as `==` would flag
    every up-to-date install as broken, so it reads as a floor.
    """
    match = _TS_DEP_RE.match(text.strip())
    if not match:
        return None
    return f"{match.group('package')} (>={match.group('version')})"


# -- Legacy Lua header -----------------------------------------------------


def _read_legacy_manifest(path: Path) -> ModManifest | None:
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
            head = fh.read(_HEADER_PROBE_BYTES)
    except OSError:
        return None

    lines = head.splitlines()
    start = None
    for index, line in enumerate(lines[:_HEADER_PROBE_LINES]):
        if line.strip() == _HEADER_MARKER:
            start = index + 1
            break
    if start is None:
        return None

    fields: dict[str, str] = {}
    for line in lines[start:]:
        stripped = line.strip()
        # Some headers have a blank line after the marker, so skip blanks.
        if not stripped:
            continue
        if not stripped.startswith("---"):
            break  # real code -- the header block is over
        match = _HEADER_LINE_RE.match(stripped)
        if match:
            fields[match.group(1).upper()] = match.group(2).strip()

    mod_id = fields.get("MOD_ID", "").strip()
    if not mod_id:
        return None

    version_raw = fields.get("VERSION", "").strip()

    dependencies = parse_dependencies(_bracket_list(fields.get("DEPENDENCIES", "")))
    # Older mods declare their Steamodded floor as LOADER_VERSION_GEQ.
    for key, op in (("LOADER_VERSION_GEQ", ">="), ("LOADER_VERSION_LEQ", "<=")):
        bound = fields.get(key, "").strip()
        if bound:
            dependencies.extend(parse_dependencies([f"Steamodded ({op}{bound})"]))

    return ModManifest(
        mod_id=mod_id,
        name=fields.get("MOD_NAME", "").strip(),
        display_name=fields.get("DISPLAY_NAME", "").strip(),
        authors=_bracket_list(fields.get("MOD_AUTHOR", "")),
        version_raw=version_raw,
        version=parse_version(version_raw),
        prefix=fields.get("PREFIX", "").strip(),
        main_file="",
        priority=_as_int(fields.get("PRIORITY")),
        description=fields.get("MOD_DESCRIPTION", "").strip(),
        # Both spellings are in use.
        badge_colour=(
            fields.get("BADGE_COLOUR") or fields.get("BADGE_COLOR") or ""
        ).strip(),
        dependencies=dependencies,
        conflicts=parse_dependencies(_bracket_list(fields.get("CONFLICTS", ""))),
        provides=parse_dependencies(_bracket_list(fields.get("PROVIDES", ""))),
        fmt="legacy",
    )


def _bracket_list(text: str) -> list[str]:
    """`[Talisman>=2.0.9, Steamodded>=1.0.0~ALPHA-1312c]` -> two entries.

    Splitting on commas is safe here because the legacy syntax has no
    parentheses, so no constraint can itself contain a comma.
    """
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [piece.strip() for piece in text.split(",") if piece.strip()]


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _bracket_list(value) or [value.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


# Incantation declares a ~200-digit PRIORITY. Python handles the integer
# fine, SQLite does not, so it is clamped to something a column can hold.
_PRIORITY_LIMIT = 2**31 - 1


def _as_int(value) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return max(-_PRIORITY_LIMIT, min(_PRIORITY_LIMIT, number))


# -- Frameworks ------------------------------------------------------------
#
# Neither Steamodded nor Lovely declares itself like a mod, so each gets its
# own reader. Steamodded's real version is in `version.lua`.

_VERSION_LUA_RE = re.compile(r"""return\s*["']([^"']+)["']""")


def read_framework(mod_dir: Path) -> ModManifest | None:
    """Recognise Steamodded / Lovely, or None if this is an ordinary mod."""
    mod_dir = Path(mod_dir)
    name = mod_dir.name.lower()

    # "-STEAMODDED" in version.lua is the test; other mods vendor loader.lua.
    version_raw = ""
    version_file = mod_dir / "version.lua"
    if version_file.is_file():
        try:
            match = _VERSION_LUA_RE.search(
                version_file.read_text(encoding="utf-8-sig", errors="replace")
            )
            if match:
                version_raw = match.group(1).strip()
        except OSError:
            pass

    looks_like_steamodded = "steamodded" in version_raw.lower() or (
        name == "steamodded" and (mod_dir / "src" / "loader.lua").is_file()
    )
    if looks_like_steamodded:
        manifest = ModManifest(
            mod_id="Steamodded",
            name="Steamodded",
            version_raw=version_raw,
            version=parse_version(version_raw),
            description="A Balatro modding framework.",
            source_file="version.lua" if version_raw else "",
            fmt="framework",
        )
        if not version_raw:
            manifest.warnings.append("could not read version.lua")
        else:
            # The bundled Thunderstore manifest says "1.0.0", which is wrong.
            manifest.warnings.append(
                "version read from version.lua, not the bundled Thunderstore manifest.json"
            )
        return manifest

    if name == "lovely":
        version_raw = _lovely_version()
        manifest = ModManifest(
            mod_id="Lovely",
            name="Lovely",
            version_raw=version_raw,
            version=parse_version(version_raw),
            description="Lovely injector runtime scaffold.",
            fmt="framework",
        )
        if not version_raw:
            manifest.warnings.append(
                "version unknown -- Lovely leaves no version marker in the mods folder"
            )
        return manifest

    return None


def _lovely_version() -> str:
    """Lovely's version lives in the injector DLL's resource block.

    Best effort: returns "" on failure, which satisfies bare dependencies
    but not versioned ones.
    """
    try:
        import ctypes
        from ctypes import wintypes

        from . import config

        dll = config.game_dir() / "version.dll"
        if not dll.is_file():
            return ""

        path = str(dll)
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buffer):
            return ""
        block = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(
            buffer, "\\", ctypes.byref(block), ctypes.byref(length)
        ):
            return ""

        class FixedFileInfo(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
            ]

        info = ctypes.cast(block, ctypes.POINTER(FixedFileInfo)).contents
        return "{}.{}.{}".format(
            info.dwFileVersionMS >> 16,
            info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16,
        )
    except Exception:
        return ""


# -- Content fingerprinting (feeds the categoriser) ------------------------

# Which SMODS constructors a mod calls says more about what it *is* than any
# category label a human typed into an index entry.
_SMODS_OBJECTS = (
    "Joker",
    "Back",
    "Deck",
    "Challenge",
    "Consumable",
    "ConsumableType",
    "Voucher",
    "Enhancement",
    "Seal",
    "Suit",
    "Rank",
    "Edition",
    "Sticker",
    "Tag",
    "Blind",
    "Booster",
    "Atlas",
    "Sound",
    "Stake",
    "PokerHand",
)

_SMODS_CALL_RE = re.compile(
    r"\bSMODS\.(" + "|".join(_SMODS_OBJECTS) + r")\s*(?:\(|\{)"
)


def scan_smods_objects(mod_dir: Path) -> dict[str, int]:
    """Count SMODS.<Object> constructor calls across a mod's Lua source."""
    counts: dict[str, int] = {}
    mod_dir = Path(mod_dir)
    if not mod_dir.is_dir():
        return counts

    scanned = 0
    for path in mod_dir.rglob("*.lua"):
        if scanned >= _MAX_SCAN_FILES:
            break
        scanned += 1
        try:
            if path.stat().st_size > _MAX_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for match in _SMODS_CALL_RE.finditer(text):
            key = match.group(1)
            counts[key] = counts.get(key, 0) + 1
    return counts
