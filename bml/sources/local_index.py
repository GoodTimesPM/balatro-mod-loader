"""Your own index: mods the public sources don't carry.

Same folder-and-meta.json shape as the community index, so an entry can be
copied straight from there and edited, or written from scratch:

    local-index/SomeAuthor@SomeMod/meta.json

        {
          "title": "Some Mod",
          "author": "SomeAuthor",
          "repo": "https://github.com/SomeAuthor/SomeMod",
          "downloadURL": "https://github.com/SomeAuthor/SomeMod/archive/refs/heads/main.zip",
          "version": "1.0.0",
          "categories": ["Content"],
          "requires-steamodded": true
        }

An entry is a note about a mod, never the mod: only `meta.json` belongs here.

`downloadURL` can be replaced by `localSource`, an absolute path to a local
folder, which `fetch` zips instead of downloading. That is the route for mods
with no public download, such as ones shared on Discord.

Entries here win every merge conflict.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from .. import config
from ..manifest import find_manifest

SOURCE = "local"

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def write_entry(
    title: str,
    author: str,
    *,
    repo: str = "",
    download_url: str = "",
    local_source: str = "",
    version: str = "",
    category: str = "",
    description: str = "",
    overwrite: bool = False,
) -> Path:
    """Create `local-index/<Author>@<Title>/meta.json` and return its folder.

    The one place entries are written. Raises FileExistsError rather than
    overwrite a hand-edited entry.
    """
    folder = config.LOCAL_INDEX / f"{_safe(author or 'unknown')}@{_safe(title)}"
    meta = folder / "meta.json"
    if meta.exists() and not overwrite:
        raise FileExistsError(str(meta))

    payload: dict = {"title": title, "author": author}
    for field, value in (
        ("repo", repo),
        ("downloadURL", download_url),
        ("localSource", local_source),
        ("version", version),
        ("bmlCategory", category),
        ("description", description),
    ):
        if value:
            payload[field] = value
    payload.setdefault("categories", ["Content"])

    folder.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return folder


def from_github_url(url: str) -> dict:
    """Turn a repo URL someone pasted into the fields an entry needs.

    Accepts a repo page, release page, or tag or branch archive URL. A tag in
    the path is kept as the pin; otherwise the default branch is used.
    """
    parsed = urlparse(url.strip() if "://" in url else f"https://{url.strip()}")
    if "github.com" not in parsed.netloc.lower():
        raise ValueError("not a github.com URL")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("URL does not name a repository (need owner/name)")

    owner, name = parts[0], re.sub(r"\.git$", "", parts[1])
    tag = ""
    match = re.search(r"/(?:releases/tag|archive/refs/tags)/([^/]+?)(?:\.zip|\.tar\.gz)?$",
                      parsed.path)
    if match:
        tag = match.group(1)

    download = (f"https://github.com/{owner}/{name}/archive/refs/tags/{tag}.zip"
                if tag else
                f"https://github.com/{owner}/{name}/archive/refs/heads/main.zip")
    return {
        "title": name.replace("-", " ").replace("_", " ").strip() or name,
        "author": owner,
        "repo": f"https://github.com/{owner}/{name}",
        "download_url": download,
        "version": tag,
    }


def _safe(text: str) -> str:
    cleaned = _UNSAFE.sub("-", str(text)).strip(" .")
    return re.sub(r"\s+", " ", cleaned)[:80] or "unnamed"


def fetch(log=print) -> list[dict]:
    root = config.LOCAL_INDEX
    entries: list[dict] = []
    if not root.is_dir():
        return entries

    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        meta_path = folder / "meta.json"
        if not meta_path.is_file():
            # Warn: a common mistake is dropping the mod itself in here.
            if find_manifest(folder) is not None:
                log(f"local: {folder.name}/ holds a mod, not an entry -- move "
                    f"those files somewhere outside local-index/ and write "
                    f"{folder.name}/meta.json with a localSource pointing at "
                    f"them (or use `add` / the window's Add mod button)")
            else:
                log(f"local: {folder.name}/ has no meta.json -- skipped")
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            log(f"local: {folder.name}/meta.json is unreadable ({exc}) -- skipped")
            continue

        title = str(meta.get("title") or "").strip()
        if not title:
            log(f"local: {folder.name}/meta.json has no title -- skipped")
            continue
        author = str(meta.get("author") or "").strip()
        if not author and "@" in folder.name:
            author = folder.name.split("@", 1)[0]

        entries.append(
            {
                "source": SOURCE,
                "title": title,
                "author": author,
                "repo": str(meta.get("repo") or "").strip(),
                "download_url": str(meta.get("downloadURL") or "").strip(),
                # A local folder instead of a URL.
                "local_source": str(meta.get("localSource") or "").strip(),
                "declared_version": str(meta.get("version") or "").strip(),
                "categories": list(meta.get("categories") or []),
                "description": str(meta.get("description") or "").strip(),
                "requires_steamodded": bool(meta.get("requires-steamodded")),
                "requires_talisman": bool(meta.get("requires-talisman")),
                "folder_name": str(meta.get("folderName") or "").strip(),
                # A category set here is final.
                "forced_category": str(meta.get("bmlCategory") or "").strip(),
                "index_folder": folder.name,
                "raw": meta,
            }
        )

    if entries:
        log(f"local: {len(entries)} personal entries")
    return entries
