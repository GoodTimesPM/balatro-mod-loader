"""A mod's page, its screenshots, and what it adds.

The README is rendered from markdown into the pane's `tk.Text` rather than in
an embedded browser: tkinter has none, and the options (pywebview, cefpython)
are large and bring a second event loop.

Caches live under `data/cache/pages/` and are disposable: READMEs (7 days) and
images (by URL). `repo-overrides.json` beside them holds hand-fixed repo URLs.
Nothing here writes to the game or the vault.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

from . import config, db

PAGE_CACHE = config.CACHE / "pages"
IMAGE_CACHE = PAGE_CACHE / "img"
OVERRIDES = config.DATA / "repo-overrides.json"


def rebase() -> None:
    """Follow `config.set_root` to a new app folder. Module globals rather than
    functions because the tests swap them for temp directories."""
    global PAGE_CACHE, IMAGE_CACHE, OVERRIDES
    PAGE_CACHE = config.CACHE / "pages"
    IMAGE_CACHE = PAGE_CACHE / "img"
    OVERRIDES = config.DATA / "repo-overrides.json"

#: How long a cached README is considered current.
CACHE_TTL = 7 * 24 * 3600

#: A README image bigger than this is not a screenshot, it is a mistake.
MAX_IMAGE_BYTES = 12 * 1024 * 1024

#: Badge images (shields.io and friends) are skipped.
_BADGE_HOSTS = ("shields.io", "badgen.net", "badge.fury.io", "travis-ci",
                "circleci.com", "codecov.io", "forthebadge.com",
                "visitor-badge", "hits.seeyoufarm.com")


# -- What we know without asking anyone ------------------------------------


@dataclass
class Facts:
    """Everything about a mod that is already on disk."""

    key: str = ""
    mod_id: str = ""
    title: str = ""
    author: str = ""
    description: str = ""
    repo: str = ""
    repo_source: str = ""      # "override", "catalog", "_bml.json", or ""
    download_url: str = ""
    #: SMODS constructor counts — the honest answer to "what does it add".
    adds: dict[str, int] = field(default_factory=dict)
    size_bytes: int = 0
    lua_files: int = 0
    sprite_files: int = 0
    lovely_patches: int = 0
    local_source: str = ""


#: Plural labels for the SMODS constructor names.
_ADDS_LABELS = {
    "Joker": ("joker", "jokers"),
    "Back": ("deck", "decks"),
    "Deck": ("card deck", "card decks"),
    "Challenge": ("challenge", "challenges"),
    "Consumable": ("consumable", "consumables"),
    "ConsumableType": ("consumable type", "consumable types"),
    "Voucher": ("voucher", "vouchers"),
    "Enhancement": ("enhancement", "enhancements"),
    "Seal": ("seal", "seals"),
    "Suit": ("suit", "suits"),
    "Rank": ("rank", "ranks"),
    "Edition": ("edition", "editions"),
    "Sticker": ("sticker", "stickers"),
    "Tag": ("tag", "tags"),
    "Blind": ("blind", "blinds"),
    "Booster": ("booster pack", "booster packs"),
    "Atlas": ("sprite sheet", "sprite sheets"),
    "Sound": ("sound", "sounds"),
    "Stake": ("stake", "stakes"),
    "PokerHand": ("poker hand", "poker hands"),
}

#: Reading order, biggest gameplay impact first.
_ADDS_ORDER = ("Joker", "Consumable", "Tarot", "Planet", "Spectral", "Back",
               "Deck", "Sleeve", "Challenge", "Blind", "Voucher",
               "Enhancement", "Edition", "Seal", "Booster", "Suit", "Rank",
               "PokerHand", "Stake", "Tag", "Sticker", "ConsumableType",
               "Atlas", "Sound")


def adds_lines(adds: dict[str, int]) -> list[str]:
    """`{'Joker': 12}` -> `['12 jokers']`, in reading order."""
    out = []
    for name in _ADDS_ORDER:
        count = adds.get(name, 0)
        if not count:
            continue
        singular, plural = _ADDS_LABELS.get(name, (name, name + "s"))
        out.append(f"{count} {singular if count == 1 else plural}")
    for name, count in sorted(adds.items()):
        # Still report object types without a label.
        if name not in _ADDS_LABELS and count:
            out.append(f"{count} {name}" if count == 1 else f"{count} {name}s")
    return out


# -- Counting what a mod adds ----------------------------------------------
#
# Big mods wrap SMODS constructors in their own helpers, so constructor counts
# miss them. `localization/en-us.lua` has one key per object, so counting keys
# under `descriptions.Joker` counts the jokers.

_LUA_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'')
_LOC_KEY = re.compile(
    r'^\s*(?:\[\s*[\'"]([^\'"]+)[\'"]\s*\]|([A-Za-z_]\w*))\s*=\s*\{')

#: Localisation section -> the `_ADDS_LABELS` key that means the same thing,
#: so the two counts merge instead of double-reporting.
_LOC_SECTIONS = {
    "Joker": "Joker",
    "Back": "Back",
    "Tarot": "Tarot",
    "Planet": "Planet",
    "Spectral": "Spectral",
    "Voucher": "Voucher",
    "Enhanced": "Enhancement",
    "Edition": "Edition",
    "Blind": "Blind",
    "Booster": "Booster",
    "Tag": "Tag",
    "Stake": "Stake",
    "Sleeve": "Sleeve",
    "Challenge": "Challenge",
}

_ADDS_LABELS.update({
    "Tarot": ("tarot", "tarots"),
    "Planet": ("planet", "planets"),
    "Spectral": ("spectral", "spectrals"),
    "Sleeve": ("deck sleeve", "deck sleeves"),
})


def scan_localization(mod_dir: Path) -> dict[str, int]:
    """Count the game objects a mod defines, from its English localisation.

    Quoted strings are blanked first so `{C:attention}` tags don't count as
    braces.
    """
    mod_dir = Path(mod_dir)
    for name in ("en-us.lua", "en_us.lua", "default.lua", "en.lua"):
        for path in (mod_dir / "localization" / name,
                     mod_dir / "localisation" / name):
            if path.is_file():
                return _count_localization(path)
    return {}


def _count_localization(path: Path) -> dict[str, int]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}

    counts: dict[str, int] = {}
    stack: list[tuple[str, int]] = []   # (table name, depth it was opened at)
    depth = 0
    for line in text.split("\n"):
        clean = _LUA_STRING.sub('""', line)
        key = _LOC_KEY.match(clean)
        if key:
            name = key.group(1) or key.group(2) or ""
            path_names = [n for n, _ in stack]
            if len(path_names) == 2 and path_names[0] == "descriptions":
                section = _LOC_SECTIONS.get(path_names[1])
                if section:
                    counts[section] = counts.get(section, 0) + 1
            stack.append((name, depth))
        depth += clean.count("{") - clean.count("}")
        while stack and depth <= stack[-1][1]:
            stack.pop()
    return counts


def merge_adds(smods: dict[str, int], localised: dict[str, int]) -> dict[str, int]:
    """Take whichever count is higher per object type.

    Not a sum, since both methods can see the same objects.
    """
    out = dict(smods)
    for name, count in localised.items():
        out[name] = max(out.get(name, 0), count)
    return out


def facts_for(key: str, vault_path: str = "") -> Facts:
    """Assemble what is known locally about one vaulted mod.

    Reads the catalog and vault tables, then the mod's own `_bml.json`.
    """
    facts = Facts(key=key)
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM catalog WHERE key = ?", (key,)).fetchone()
        if row is not None:
            facts.title = row["title"] or ""
            facts.author = row["author"] or ""
            facts.description = (row["description"] or "").strip()
            facts.repo = (row["repo"] or "").strip()
            facts.repo_source = "catalog" if facts.repo else ""
            facts.download_url = row["download_url"] or ""
        vrow = conn.execute("SELECT * FROM vault WHERE key = ?", (key,)).fetchone()
        if vrow is not None:
            facts.mod_id = vrow["mod_id"] or ""
            vault_path = vault_path or (vrow["vault_path"] or "")
            try:
                facts.adds = {k: int(v) for k, v in
                              json.loads(vrow["smods_objects"] or "{}").items() if v}
            except (ValueError, AttributeError):
                facts.adds = {}
    finally:
        conn.close()

    record = _read_record(vault_path)
    if not facts.repo:
        facts.repo = str(record.get("repo") or "").strip()
        facts.repo_source = "_bml.json" if facts.repo else ""
    if not facts.adds:
        adds = record.get("smods_objects") or {}
        facts.adds = {k: int(v) for k, v in adds.items() if v}
    facts.local_source = str(record.get("localSource") or "")

    override = overrides().get(key)
    if override:
        facts.repo, facts.repo_source = override, "override"

    facts.repo = normalise_repo(facts.repo)
    _measure(vault_path, facts)
    return facts


def _read_record(vault_path: str) -> dict:
    if not vault_path:
        return {}
    path = Path(vault_path) / "_bml.json"
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _measure(vault_path: str, facts: Facts) -> None:
    """Size on disk, and file counts by type."""
    if not vault_path:
        return
    root = Path(vault_path) / "mod"
    if not root.is_dir():
        return
    facts.adds = merge_adds(facts.adds, scan_localization(root))
    total = lua = sprites = patches = 0
    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
            total += path.stat().st_size
        except OSError:
            continue
        suffix = path.suffix.lower()
        if suffix == ".lua":
            lua += 1
        elif suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            sprites += 1
        elif suffix == ".toml" and "lovely" in str(path).lower():
            patches += 1
    facts.size_bytes, facts.lua_files = total, lua
    facts.sprite_files, facts.lovely_patches = sprites, patches


def human_size(count: int) -> str:
    step = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if step < 1024 or unit == "GB":
            return f"{step:.0f} {unit}" if unit == "B" else f"{step:.1f} {unit}"
        step /= 1024
    return f"{count} B"


# -- Repo URL overrides ----------------------------------------------------


def overrides() -> dict[str, str]:
    try:
        data = json.loads(OVERRIDES.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def set_override(key: str, url: str) -> None:
    """Record a hand-entered repo URL for a mod whose index entry lacks one.

    Separate from `local-index/`, so fixing a link never changes downloads.
    """
    data = overrides()
    url = normalise_repo(url.strip())
    if url:
        data[key] = url
    else:
        data.pop(key, None)
    OVERRIDES.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def normalise_repo(url: str) -> str:
    """Reduce anything GitHub-shaped to `https://github.com/owner/repo`."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.split(":", 1)[1]
    if url.startswith("github.com/"):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.netloc.lower() not in ("github.com", "www.github.com"):
        return url
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return url
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return f"https://github.com/{owner}/{repo}"


def repo_parts(url: str) -> tuple[str, str] | None:
    parsed = urlparse(normalise_repo(url))
    if parsed.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    return (parts[0], parts[1]) if len(parts) >= 2 else None


# -- Fetching the page -----------------------------------------------------


@dataclass
class Page:
    repo: str = ""
    html_url: str = ""       # where "Open on GitHub" should go
    markdown: str = ""
    base_url: str = ""       # what relative image paths are relative to
    fetched_at: float = 0.0
    from_cache: bool = False
    error: str = ""


def _cache_path(owner: str, repo: str) -> Path:
    PAGE_CACHE.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{owner}__{repo}")
    return PAGE_CACHE / f"{safe}.json"


def cached_page(repo_url: str) -> Page | None:
    """The cached copy, however old, without touching the network.

    Used to draw a page the instant you click a mod.
    """
    parts = repo_parts(repo_url)
    if parts is None:
        return None
    page = _load_cached(_cache_path(*parts))
    if page is not None:
        page.from_cache = True
    return page


def fetch_page(repo_url: str, refresh: bool = False) -> Page:
    """The mod's README, from cache if it is fresh enough.

    One API call. Its `download_url` also gives the default branch for
    resolving relative image paths.
    """
    parts = repo_parts(repo_url)
    if parts is None:
        return Page(repo=repo_url,
                    error="Not a GitHub repository — nothing to show here.")
    owner, repo = parts
    page = Page(repo=f"https://github.com/{owner}/{repo}",
                html_url=f"https://github.com/{owner}/{repo}")

    cached = _load_cached(_cache_path(owner, repo))
    if cached and not refresh and time.time() - cached.fetched_at < CACHE_TTL:
        cached.from_cache = True
        return cached

    from . import net

    try:
        data = net.get_json(
            f"https://api.github.com/repos/{owner}/{repo}/readme")
    except Exception as exc:  # network, 404, rate limit — all the same here
        if cached:  # stale beats blank
            cached.from_cache = True
            cached.error = f"Could not refresh ({_short(exc)}); showing the cached copy."
            return cached
        page.error = _explain(exc)
        return page

    import base64

    try:
        page.markdown = base64.b64decode(data.get("content", "")).decode(
            "utf-8", errors="replace")
    except Exception:
        page.markdown = ""
    download_url = data.get("download_url") or ""
    page.base_url = download_url.rsplit("/", 1)[0] + "/" if download_url else ""
    page.html_url = data.get("html_url") or page.html_url
    page.fetched_at = time.time()
    _save_cached(_cache_path(owner, repo), page)
    return page


def _short(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:160]


def _explain(exc: Exception) -> str:
    text = str(exc)
    if "404" in text:
        return "This repository has no README (or the repo has moved)."
    if "403" in text or "rate limit" in text.lower():
        return ("GitHub is rate-limiting anonymous requests. Put a GITHUB_TOKEN "
                "in .env and the limit goes from 60/hour to 5000.")
    return f"Could not load the page — {_short(exc)}"


def _load_cached(path: Path) -> Page | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return Page(repo=data.get("repo", ""), html_url=data.get("html_url", ""),
                markdown=data.get("markdown", ""),
                base_url=data.get("base_url", ""),
                fetched_at=float(data.get("fetched_at") or 0))


def _save_cached(path: Path, page: Page) -> None:
    try:
        path.write_text(json.dumps({
            "repo": page.repo, "html_url": page.html_url,
            "markdown": page.markdown, "base_url": page.base_url,
            "fetched_at": page.fetched_at,
        }), encoding="utf-8")
    except OSError:
        pass


# -- Images ----------------------------------------------------------------


def resolve_image_url(src: str, base_url: str) -> str:
    """Turn a README image reference into something that serves bytes.

    Three shapes show up: a relative path, a `github.com/.../blob/...` link
    (which serves an HTML page, not an image), and an already-raw URL.
    """
    src = (src or "").strip()
    if not src or src.startswith("data:"):
        return ""
    if src.startswith("//"):
        src = "https:" + src
    if not urlparse(src).scheme:
        if not base_url:
            return ""
        src = urljoin(base_url, src.lstrip("./"))
    parsed = urlparse(src)
    if parsed.netloc.lower().endswith("github.com") and "/blob/" in parsed.path:
        src = ("https://raw.githubusercontent.com"
               + parsed.path.replace("/blob/", "/", 1))
    elif parsed.netloc.lower().endswith("github.com") and "/raw/" in parsed.path:
        src = ("https://raw.githubusercontent.com"
               + parsed.path.replace("/raw/", "/", 1))
    return src.split("?raw=")[0]


def is_badge(url: str) -> bool:
    lowered = (url or "").lower()
    return any(host in lowered for host in _BADGE_HOSTS)


def image_path(url: str) -> Path:
    """Where `url` would be cached. Touches neither the network nor the disk."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        suffix = ".img"
    return IMAGE_CACHE / f"{digest}{suffix}"


def cached_image_file(url: str) -> Path | None:
    """The already-downloaded file for `url`, or None. Never downloads.

    Lets a cached page show its cached screenshots.
    """
    if not url:
        return None
    dest = image_path(url)
    if dest.exists():
        return dest if dest.stat().st_size else None
    return None


def image_file(url: str) -> Path | None:
    """Download a README image once, keyed by URL. Returns the cached file.

    Refuses anything that is not an image or is too large.
    """
    if not url:
        return None
    dest = image_path(url)
    if dest.exists():
        return dest if dest.stat().st_size else None
    IMAGE_CACHE.mkdir(parents=True, exist_ok=True)

    from . import net

    try:
        with net.session().get(url, stream=True,
                               timeout=config.REQUEST_TIMEOUT) as response:
            response.raise_for_status()
            kind = response.headers.get("Content-Type", "")
            if not kind.startswith("image/"):
                dest.write_bytes(b"")  # remember the failure; don't re-ask
                return None
            size = 0
            chunks = []
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    dest.write_bytes(b"")
                    return None
                chunks.append(chunk)
        dest.write_bytes(b"".join(chunks))
    except Exception:
        return None
    return dest


# -- Markdown ---------------------------------------------------------------
#
# The subset mod READMEs use: headings, paragraphs, lists, code, images,
# links, emphasis, and tables as plain text.

_FENCE = re.compile(r"^\s*(```+|~~~+)\s*(\w+)?\s*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBER = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

_IMG_MD = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_IMG_HTML = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.I)
_LINK_MD = re.compile(r"\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_AUTOLINK = re.compile(r"<((?:https?|mailto):[^>]+)>")
_BARE_URL = re.compile(r"(?<![(\"'\w])(https?://[^\s<>()\[\]]+)")
_CODE_SPAN = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)|(?<![_\w])_([^_\n]+)_(?!_)")
_HTML_TAG = re.compile(r"</?(?!img\b)[a-zA-Z][^>]*>")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_ENTITY = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
           "&quot;": '"', "&#39;": "'", "&mdash;": "—", "&ndash;": "–"}


@dataclass
class Span:
    text: str
    style: str = ""      # "", "bold", "italic", "code", "link"
    href: str = ""


@dataclass
class Block:
    kind: str            # head para code item rule image quote table
    spans: list[Span] = field(default_factory=list)
    text: str = ""
    level: int = 0
    url: str = ""
    alt: str = ""


def parse(markdown: str, base_url: str = "", skip_badges: bool = True) -> list[Block]:
    """Markdown -> a flat list of blocks the pane can draw top to bottom."""
    blocks: list[Block] = []
    markdown = _HTML_COMMENT.sub("", markdown or "")
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    index, fence, buffer = 0, None, []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            text = " ".join(p.strip() for p in paragraph if p.strip())
            paragraph.clear()
            if text.strip():
                _emit_text(blocks, "para", text, base_url, skip_badges)

    while index < len(lines):
        line = lines[index]
        index += 1

        if fence is not None:
            if line.strip().startswith(fence):
                blocks.append(Block("code", text="\n".join(buffer)))
                fence, buffer = None, []
            else:
                buffer.append(line)
            continue

        opening = _FENCE.match(line)
        if opening:
            flush()
            fence = opening.group(1)[:3]
            continue

        if not line.strip():
            flush()
            continue

        heading = _HEADING.match(line)
        if heading:
            flush()
            _emit_text(blocks, "head", heading.group(2), base_url, skip_badges,
                       level=len(heading.group(1)))
            continue

        if _RULE.match(line):
            flush()
            blocks.append(Block("rule"))
            continue

        quote = _QUOTE.match(line)
        if quote:
            flush()
            _emit_text(blocks, "quote", quote.group(1), base_url, skip_badges)
            continue

        # A table renders as its rows, dimmed.
        if "|" in line and line.strip().startswith("|"):
            flush()
            if not _TABLE_SEP.match(line):
                # A visible separator, since `_clean` collapses whitespace.
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                _emit_text(blocks, "table", " · ".join(c for c in cells if c),
                           base_url, skip_badges)
            continue

        bullet = _BULLET.match(line)
        number = _NUMBER.match(line)
        if bullet or number:
            flush()
            indent = len((bullet or number).group(1)) // 2
            body = bullet.group(2) if bullet else number.group(3)
            marker = "•" if bullet else f"{number.group(2)}."
            _emit_text(blocks, "item", body, base_url, skip_badges,
                       level=indent, alt=marker)
            continue

        paragraph.append(line)

    flush()
    if fence is not None and buffer:
        blocks.append(Block("code", text="\n".join(buffer)))
    return _tidy(blocks)


def _emit_text(blocks: list[Block], kind: str, text: str, base_url: str,
               skip_badges: bool, level: int = 0, alt: str = "") -> None:
    """Pull the images out of a line, then emit the rest as spans.

    Images become their own full-width blocks, in order.
    """
    remaining = text
    for pattern, group_alt in ((_IMG_MD, True), (_IMG_HTML, False)):
        while True:
            match = pattern.search(remaining)
            if not match:
                break
            url = resolve_image_url(
                match.group(2) if group_alt else match.group(1), base_url)
            caption = match.group(1) if group_alt else ""
            if url and not (skip_badges and is_badge(url)):
                blocks.append(Block("image", url=url, alt=caption))
            remaining = remaining[:match.start()] + " " + remaining[match.end():]

    remaining = _clean(remaining)
    if remaining.strip():
        blocks.append(Block(kind, spans=_inline(remaining, base_url),
                            level=level, alt=alt))


def _clean(text: str) -> str:
    text = _HTML_TAG.sub(" ", text)
    for entity, char in _ENTITY.items():
        text = text.replace(entity, char)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _inline(text: str, base_url: str) -> list[Span]:
    """Split one line into styled spans, links first so nothing nests wrongly."""
    spans: list[Span] = []

    def walk(chunk: str, style: str, href: str) -> None:
        if not chunk:
            return
        if style:  # already claimed by an outer pattern; don't re-scan
            spans.append(Span(chunk, style, href))
            return
        for pattern, kind in ((_LINK_MD, "link"), (_AUTOLINK, "auto"),
                              (_CODE_SPAN, "code"), (_BOLD, "bold"),
                              (_ITALIC, "italic"), (_BARE_URL, "bare")):
            match = pattern.search(chunk)
            if not match:
                continue
            walk(chunk[:match.start()], "", "")
            if kind == "link":
                target = _absolute(match.group(2), base_url)
                walk(match.group(1) or target, "link", target)
            elif kind in ("auto", "bare"):
                spans.append(Span(match.group(1), "link", match.group(1)))
            elif kind == "code":
                spans.append(Span(match.group(1), "code", ""))
            else:
                spans.append(Span(match.group(1) or match.group(2) or "",
                                  kind, ""))
            walk(chunk[match.end():], "", "")
            return
        spans.append(Span(chunk, "", ""))

    walk(text, "", "")
    return [s for s in spans if s.text]


def _absolute(href: str, base_url: str) -> str:
    href = (href or "").strip()
    if not href or href.startswith("#"):
        return ""
    if urlparse(href).scheme:
        return href
    return urljoin(base_url, href) if base_url else ""


def _tidy(blocks: list[Block]) -> list[Block]:
    """Drop leading rules and collapse runs of them. Cosmetic, and cheap."""
    out: list[Block] = []
    for block in blocks:
        if block.kind == "rule" and (not out or out[-1].kind == "rule"):
            continue
        out.append(block)
    while out and out[-1].kind == "rule":
        out.pop()
    return out
